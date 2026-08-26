from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from budget_bot.models import OperationType, ParsedOperation
from budget_bot.telegram_bot import TelegramBot, _written_operation_summary_lines


class FakeStatsStorage:
    def __init__(self) -> None:
        self.report_periods: List[tuple[date, date, Optional[str]]] = []

    def expense_summary(
        self,
        start_date: date,
        end_date: date,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.report_periods.append((start_date, end_date, category))
        return {
            "start_date": start_date,
            "end_date": end_date,
            "category": category,
            "total": 0.0,
            "count": 0,
            "categories": [],
            "subcategories": [],
        }


class FakeStatsContext:
    def __init__(self, storage: FakeStatsStorage) -> None:
        self.storage = storage


def _stats_bot(storage: FakeStatsStorage) -> TelegramBot:
    bot = object.__new__(TelegramBot)
    bot.context = FakeStatsContext(storage)
    bot.sent_messages = []

    def send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        bot.sent_messages.append((chat_id, text, reply_markup))

    bot._send_message = send_message
    return bot


def test_written_summary_preserves_screenshot_order() -> None:
    operations = [
        ParsedOperation(
            date=date(2026, 8, 15),
            name="Yandex Fasten",
            amount=-141,
            type=OperationType.EXPENSE,
            category="Транспорт",
            subcategory="Такси",
        ),
        ParsedOperation(
            date=date(2026, 8, 15),
            name="Waypma 24",
            amount=-800,
            type=OperationType.EXPENSE,
            category="Еда",
            subcategory="Фастфуд",
        ),
        ParsedOperation(
            date=date(2026, 8, 15),
            name="Fix Price",
            amount=-1081.5,
            type=OperationType.EXPENSE,
            category="Еда",
            subcategory="Супермаркеты",
        ),
    ]

    lines = _written_operation_summary_lines(operations)

    assert lines[2].startswith("- Yandex Fasten:")
    assert lines[3].startswith("- Waypma 24:")
    assert lines[4].startswith("- Fix Price:")


def test_stats_period_picker_offers_date_and_range_buttons() -> None:
    bot = _stats_bot(FakeStatsStorage())

    bot._send_stats_period_picker(123)

    _chat_id, text, reply_markup = bot.sent_messages[0]
    assert "01.08 или 01.08-24.08" in text
    assert reply_markup == {
        "inline_keyboard": [
            [
                {"text": "Один день", "callback_data": "statsdate:day"},
                {"text": "Период", "callback_data": "statsrange:startday"},
            ],
            [{"text": "Назад", "callback_data": "analytics:menu"}],
        ]
    }


def test_stats_date_picker_sends_single_day_report() -> None:
    storage = FakeStatsStorage()
    bot = _stats_bot(storage)

    bot._handle_stats_date_callback(123, "show:24:8", date(2026, 8, 26))

    assert storage.report_periods == [(date(2026, 8, 24), date(2026, 8, 24), None)]
    assert "Расходы 24.08 - 24.08" in bot.sent_messages[0][1]


def test_stats_range_picker_sends_sorted_period_report() -> None:
    storage = FakeStatsStorage()
    bot = _stats_bot(storage)

    bot._handle_stats_range_callback(123, "show:2026-08-24:1:8", date(2026, 8, 26))

    assert storage.report_periods == [(date(2026, 8, 1), date(2026, 8, 24), None)]
    assert "Расходы 01.08 - 24.08" in bot.sent_messages[0][1]
