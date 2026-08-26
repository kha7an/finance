from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional

from budget_bot.default_categories import DEFAULT_EXPENSE_CATEGORIES, DEFAULT_INCOME_CATEGORIES
from budget_bot.models import OperationStatus, ParsedOperation, ParsedScreenshot
from budget_bot.processor import ScreenshotProcessor
from budget_bot.storage.facade import BudgetEntryInsert, OperationRecord


class FakeStorage:
    def __init__(self, existing_entries: Optional[List[Dict[str, object]]] = None) -> None:
        self.existing_entries = existing_entries or []
        self.budget_entries: List[BudgetEntryInsert] = []
        self.operation_records: List[OperationRecord] = []
        self.entry_range: Optional[tuple[date, date]] = None

    def image_seen(self, image_hash: str) -> bool:
        return False

    def record_image(
        self,
        image_hash: str,
        telegram_file_id: Optional[str],
        bank: str,
        status: str,
        raw_response: Dict[str, object],
    ) -> None:
        pass

    def update_image_status(self, image_hash: str, status: str) -> None:
        pass

    def expense_category_rules(self) -> Dict[str, tuple[str, str]]:
        return {}

    def existing_operation_hashes(self, operation_hashes: Iterable[str]) -> set[str]:
        return set()

    def all_budget_entries(self, start_date: date, end_date: date) -> List[Dict[str, object]]:
        self.entry_range = (start_date, end_date)
        return self.existing_entries

    def append_budget_entries_batch(self, entries: Iterable[BudgetEntryInsert]) -> List[int]:
        self.budget_entries.extend(entries)
        return list(range(1, len(self.budget_entries) + 1))

    def record_operations_batch(self, records: Iterable[OperationRecord]) -> None:
        self.operation_records.extend(records)

    def category_book(self):
        from budget_bot.categories import CategoryBook

        return CategoryBook(
            expense_categories=DEFAULT_EXPENSE_CATEGORIES,
            income_categories=DEFAULT_INCOME_CATEGORIES,
        )


def test_processor_ignores_existing_entry_with_transliterated_name() -> None:
    storage = FakeStorage(
        existing_entries=[
            {
                "operation_date": "2026-08-15",
                "operation_type": "expense",
                "amount": 141.0,
                "name": "Яндекс Фастен",
                "bank": "tbank",
            }
        ]
    )
    processor = ScreenshotProcessor(storage, storage.category_book())
    parsed = ParsedScreenshot.from_json(
        {
            "bank": "tbank",
            "period": {"month": 8, "year": 2026, "screenshot_date": "2026-08-16"},
            "operations": [
                {
                    "date": "2026-08-15",
                    "date_status": "relative",
                    "name": "Yandex Fasten",
                    "amount": -141,
                    "type": "expense",
                    "category": "Транспорт",
                    "subcategory": "Такси",
                    "needs_review": False,
                },
                {
                    "date": "2026-08-15",
                    "date_status": "relative",
                    "name": "Yandex Fasten",
                    "amount": -269,
                    "type": "expense",
                    "category": "Транспорт",
                    "subcategory": "Такси",
                    "needs_review": False,
                },
            ],
        }
    )

    result = processor.process(b"image-16-aug", parsed)

    assert [decision.status for decision in result.decisions] == [
        OperationStatus.IGNORED,
        OperationStatus.AUTO_WRITTEN,
    ]
    assert result.decisions[0].reason == "duplicate"
    assert [entry.operation.name for entry in storage.budget_entries] == ["Yandex Fasten"]
    assert storage.entry_range == (date(2026, 8, 15), date(2026, 8, 15))


def test_processor_allows_same_entry_from_different_bank() -> None:
    storage = FakeStorage(
        existing_entries=[
            {
                "operation_date": "2026-08-15",
                "operation_type": "expense",
                "amount": 141.0,
                "name": "Яндекс Фастен",
                "bank": "tbank",
            }
        ]
    )
    processor = ScreenshotProcessor(storage, storage.category_book())
    parsed = ParsedScreenshot.from_json(
        {
            "bank": "yapay",
            "period": {"month": 8, "year": 2026, "screenshot_date": "2026-08-16"},
            "operations": [
                {
                    "date": "2026-08-15",
                    "date_status": "relative",
                    "name": "Yandex Fasten",
                    "amount": -141,
                    "type": "expense",
                    "category": "Транспорт",
                    "subcategory": "Такси",
                    "needs_review": False,
                },
            ],
        }
    )

    result = processor.process(b"image-yapay-15-aug", parsed)

    assert [decision.status for decision in result.decisions] == [OperationStatus.AUTO_WRITTEN]
    assert [entry.operation.name for entry in storage.budget_entries] == ["Yandex Fasten"]


def test_processor_keeps_operation_above_first_date_header_pending() -> None:
    storage = FakeStorage()
    processor = ScreenshotProcessor(storage, storage.category_book())
    parsed = ParsedScreenshot.from_json(
        {
            "bank": "tbank",
            "period": {"month": 8, "year": 2026, "screenshot_date": "2026-08-24"},
            "operations": [
                {
                    "date": None,
                    "date_status": "missing",
                    "name": "Yandex Fasten",
                    "amount": -155,
                    "type": "expense",
                    "category": "Транспорт",
                    "subcategory": "Такси",
                    "needs_review": False,
                },
                {
                    "date": "2026-08-24",
                    "date_status": "relative",
                    "name": "IP Hakimov F.D",
                    "amount": -621.77,
                    "type": "expense",
                    "category": "Еда",
                    "subcategory": "Фастфуд",
                    "needs_review": False,
                },
            ],
        }
    )

    result = processor.process(b"image-24-aug", parsed)

    assert [decision.status for decision in result.decisions] == [
        OperationStatus.PENDING,
        OperationStatus.AUTO_WRITTEN,
    ]
    assert result.decisions[0].reason == "operation date missing"
    assert [entry.operation.name for entry in storage.budget_entries] == ["IP Hakimov F.D"]
