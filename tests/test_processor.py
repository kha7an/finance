from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional

from budget_bot.default_categories import DEFAULT_EXPENSE_CATEGORIES, DEFAULT_INCOME_CATEGORIES
from budget_bot.models import OperationStatus, OperationType, ParsedOperation, ParsedScreenshot
from budget_bot.processor import ScreenshotProcessor
from budget_bot.storage.facade import BudgetEntryInsert, OperationRecord
import pytest


@pytest.mark.parametrize("statement_name, history_name", [
    ("YANDEX*FASTEN g. Moskva RUS", "Яндекс Фастен"),
    ("PYATEROCHKA 19880 Arsk RUS", "Пятёрочка"),
    ("TRANSKART.RU KAZAN RUS", "Транспортная карта"),
    ("MUP METROELEKTROTRANS Kazan RUS", "Метроэлектротранс"),
    ("AptekaFarmlend1001 Kazan RUS", "АптекаFarmlend25"),
    ("KRASNOE&BELOE Kazan RUS", "Красное и белое"),
    ("WAYPMA 24 Kazan RUS", "Waypma 24"),
    ("PEREKRESTOK TANDEM Kazan RUS", "Перекрёсток"),
    ("DOSTAVKA PEREKRESTKA_S MOSCOW RUS", "Перекрёсток доставка"),
])
def test_statement_matches_normalized_history(statement_name, history_name):
    from budget_bot.processor import _statement_name_key
    assert _statement_name_key(statement_name) == _statement_name_key(history_name)


def test_statement_copies_category_from_history():
    storage = FakeStorage([{
        "operation_date": "2026-08-01", "operation_type": "expense", "amount": 100,
        "name": "Яндекс Фастен", "category": "Транспорт", "subcategory": "Такси", "bank": "tbank",
    }])
    parsed = ParsedScreenshot("tbank", [ParsedOperation(
        date=date(2026, 10, 8), name="YANDEX*FASTEN g. Moskva RUS", amount=-172,
        type=OperationType.EXPENSE,
    )], {"source": "tbank_pdf"})
    result = ScreenshotProcessor(storage, storage.category_book()).process(b"statement", parsed)
    assert result.decisions[0].status == OperationStatus.AUTO_WRITTEN
    assert storage.budget_entries[0].operation.subcategory == "Такси"


def test_statement_conflicting_history_requires_review_even_with_keyword_rule():
    storage = FakeStorage([{
        "operation_date": "2026-08-01", "operation_type": "expense", "amount": 100,
        "name": "Surf Coffee", "category": "Еда", "subcategory": subcategory, "bank": "tbank",
    } for subcategory in ("Кофейни", "Рестораны")])
    parsed = ParsedScreenshot("tbank", [ParsedOperation(
        date=date(2026, 10, 8), name="Surf Coffee x Kremlin Kazan RUS", amount=-172,
        type=OperationType.EXPENSE,
    )], {"source": "tbank_pdf"})
    result = ScreenshotProcessor(storage, storage.category_book()).process(b"statement", parsed)
    assert result.decisions[0].status == OperationStatus.PENDING
    assert not storage.budget_entries


def test_statement_unknown_merchant_is_not_matched_by_similarity():
    storage = FakeStorage([{
        "operation_date": "2026-08-01", "operation_type": "expense", "amount": 100,
        "name": "IP Kostyuk A. N.", "category": "Еда", "subcategory": "Фастфуд", "bank": "tbank",
    }])
    parsed = ParsedScreenshot("tbank", [ParsedOperation(
        date=date(2026, 10, 8), name="IP KOSTYUK YE. N. KAZAN RUS", amount=-25,
        type=OperationType.EXPENSE,
    )], {"source": "tbank_pdf"})
    result = ScreenshotProcessor(storage, storage.category_book()).process(b"statement", parsed)
    assert result.decisions[0].status == OperationStatus.PENDING
    assert not storage.budget_entries


@pytest.mark.parametrize("rule_name", ["transkart.ru kazan rus", "транспортная карта"])
def test_statement_manual_rule_overrides_conflicting_history(rule_name):
    storage = FakeStorage([{
        "operation_date": "2026-08-01", "operation_type": "expense", "amount": 46,
        "name": "Транспортная карта", "category": category, "subcategory": subcategory, "bank": "tbank",
    } for category, subcategory in (("Транспорт", "Местный транспорт"), ("Авто", "Топливо"))])
    storage.expense_category_rules = lambda: {rule_name: ("Транспорт", "Местный транспорт")}
    parsed = ParsedScreenshot("tbank", [ParsedOperation(
        date=date(2026, 10, 8), name="TRANSKART.RU KAZAN RUS", amount=-46,
        type=OperationType.EXPENSE,
    )], {"source": "tbank_pdf"})
    result = ScreenshotProcessor(storage, storage.category_book()).process(b"statement", parsed)
    assert result.decisions[0].status == OperationStatus.AUTO_WRITTEN
    assert storage.budget_entries[0].operation.category == "Транспорт"


@pytest.mark.parametrize("existing_count, expected_status, remaining", [
    (1, OperationStatus.PENDING, 2),
    (2, OperationStatus.AUTO_WRITTEN, 1),
    (3, OperationStatus.IGNORED, 3),
])
def test_statement_accounts_for_existing_repeated_payments(existing_count, expected_status, remaining):
    storage = FakeStorage([{
        "operation_date": "2026-10-08", "operation_type": "expense", "amount": 46,
        "name": "Транспортная карта", "category": "Транспорт",
        "subcategory": "Местный транспорт", "bank": "tbank",
    } for _ in range(existing_count)])
    parsed = ParsedScreenshot("tbank", [ParsedOperation(
        date=date(2026, 10, 8), name="TRANSKART.RU KAZAN RUS", amount=-46,
        type=OperationType.EXPENSE,
    ) for _ in range(3)], {"source": "tbank_pdf", "warning": "Нет итогов"})

    result = ScreenshotProcessor(storage, storage.category_book()).process(b"repeated-statement", parsed)

    assert result.decisions[0].status == expected_status
    assert result.decisions[0].operation.occurrence_count == remaining
    assert result.warning == "Нет итогов"
    assert len(storage.budget_entries) == (1 if existing_count == 2 else 0)


def test_statement_normalized_duplicate_does_not_ignore_different_date_amount_or_type():
    from budget_bot.processor import _matches_existing_entry

    entry = {
        "operation_date": "2026-10-08", "operation_type": "expense", "amount": 46,
        "name": "Транспортная карта", "bank": "tbank",
    }
    operation = ParsedOperation(date(2026, 10, 8), "TRANSKART.RU KAZAN RUS", -46, OperationType.EXPENSE)
    assert _matches_existing_entry("tbank", operation, [entry])
    for field, value in [("operation_date", "2026-10-07"), ("amount", 47), ("operation_type", "income"), ("bank", "sber")]:
        assert not _matches_existing_entry("tbank", operation, [{**entry, field: value}])


class FakeStorage:

    def atomic(self):
        from contextlib import nullcontext

        return nullcontext()

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
