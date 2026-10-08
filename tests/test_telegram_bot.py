from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from budget_bot.models import OperationStatus, OperationType, ParsedOperation
from budget_bot.processor import OperationDecision, ProcessingResult
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
    bot._answer_callback = lambda _callback_id, _text: None
    bot._delete_callback_message = lambda _callback: None
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


def test_document_handler_enqueues_pdf_and_rejects_large_or_other_files() -> None:
    bot = _stats_bot(FakeStatsStorage())
    bot.context.settings = SimpleNamespace(max_upload_bytes=1000)
    queued = []
    bot.parse_jobs = SimpleNamespace(enqueue_document=lambda *args: queued.append(args))

    for document in [
        {"file_id": "pdf", "file_name": "statement.PDF", "file_size": 500},
        {"file_id": "large", "mime_type": "application/pdf", "file_size": 1001},
        {"file_id": "other", "file_name": "statement.xlsx"},
    ]:
        bot._handle_document({"chat": {"id": 123}, "document": document})

    assert queued == [(123, "pdf")]
    assert "Обрабатываю PDF" in bot.sent_messages[0][1]
    assert "размер" in bot.sent_messages[1][1]
    assert "формате PDF" in bot.sent_messages[2][1]


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


def test_month_report_has_month_navigation() -> None:
    bot = _stats_bot(FakeStatsStorage())

    bot._send_expense_report(123, date(2026, 8, 1), date(2026, 8, 31))

    _chat_id, _text, reply_markup = bot.sent_messages[0]
    assert reply_markup["inline_keyboard"][0] == [
        {"text": "<", "callback_data": "statsmonth:2026-07"},
        {"text": "08.2026", "callback_data": "stats:month"},
        {"text": ">", "callback_data": "statsmonth:2026-09"},
    ]


def test_stats_month_callback_sends_selected_month_report() -> None:
    storage = FakeStatsStorage()
    bot = _stats_bot(storage)

    bot._handle_stats_callback({"id": "callback-id"}, 123, "statsmonth", "2025-02")

    assert storage.report_periods == [(date(2025, 2, 1), date(2025, 2, 28), None)]
    assert "Расходы 01.02 - 28.02" in bot.sent_messages[0][1]


def test_processing_result_has_edit_entries_button_for_written_period() -> None:
    bot = _stats_bot(FakeStatsStorage())
    result = ProcessingResult(
        image_hash="image-hash",
        bank="tbank",
        decisions=[
            OperationDecision(
                ParsedOperation(
                    date=date(2026, 8, 12),
                    name="Пятёрочка",
                    amount=-526.87,
                    type=OperationType.EXPENSE,
                    category="Еда",
                    subcategory="Супермаркеты",
                ),
                OperationStatus.AUTO_WRITTEN,
                "auto written",
            ),
            OperationDecision(
                ParsedOperation(
                    date=date(2026, 8, 5),
                    name="Такси",
                    amount=-132,
                    type=OperationType.EXPENSE,
                    category="Транспорт",
                    subcategory="Такси",
                ),
                OperationStatus.AUTO_WRITTEN,
                "auto written",
            ),
        ],
    )

    bot._send_processing_result(123, result)

    _chat_id, text, reply_markup = bot.sent_messages[0]
    assert "Засчитано:" in text
    assert reply_markup == {
        "inline_keyboard": [
            [
                {
                    "text": "Редактировать записи",
                    "callback_data": "entrylist:2026-08-05:2026-08-12:all",
                }
            ]
        ]
    }
