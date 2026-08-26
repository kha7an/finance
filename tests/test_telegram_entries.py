from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from budget_bot.telegram_entries import TelegramEntryEditor


class FakeEntryStorage:
    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self.entries = entries
        self.deleted_entry_ids: List[int] = []
        self.last_count_period: Optional[tuple[date, date]] = None
        self.last_list_period: Optional[tuple[date, date]] = None

    def get_budget_entry(self, entry_id: int) -> Optional[Dict[str, Any]]:
        for entry in self.entries:
            if int(entry["id"]) == entry_id:
                return entry
        return None

    def delete_budget_entry(self, entry_id: int) -> None:
        self.deleted_entry_ids.append(entry_id)
        self.entries = [entry for entry in self.entries if int(entry["id"]) != entry_id]

    def count_budget_entries(self, start_date: date, end_date: date, category: Optional[str] = None) -> int:
        self.last_count_period = (start_date, end_date)
        return len(self._period_entries(start_date, end_date, category))

    def budget_entries(
        self,
        start_date: date,
        end_date: date,
        category: Optional[str] = None,
        limit: int = 15,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        self.last_list_period = (start_date, end_date)
        return self._period_entries(start_date, end_date, category)[offset : offset + limit]

    def _period_entries(
        self,
        start_date: date,
        end_date: date,
        category: Optional[str],
    ) -> List[Dict[str, Any]]:
        result = []
        for entry in self.entries:
            operation_date = date.fromisoformat(str(entry["operation_date"]))
            if not start_date <= operation_date <= end_date:
                continue
            if category is not None and entry.get("category") != category:
                continue
            result.append(entry)
        return result


class FakeEntryBot:
    def __init__(self, storage: FakeEntryStorage) -> None:
        self.context = type("Context", (), {"storage": storage})()
        self.answers: List[str] = []
        self.deleted_callback_messages: List[Dict[str, Any]] = []
        self.sent_messages: List[tuple[int, str, Optional[Dict[str, Any]]]] = []

    def _answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append(text)

    def _delete_callback_message(self, callback: Dict[str, Any]) -> None:
        self.deleted_callback_messages.append(callback)

    def _send_message(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        self.sent_messages.append((chat_id, text, reply_markup))

    def _main_reply_keyboard(self) -> Dict[str, Any]:
        return {"keyboard": []}


def test_delete_entry_returns_to_same_day_entry_list() -> None:
    storage = FakeEntryStorage(
        [
            {
                "id": 126,
                "operation_date": "2026-08-24",
                "operation_type": "expense",
                "name": "IP Hakimov F.D",
                "amount": 621.77,
                "category": "Еда",
                "subcategory": "Фастфуд",
            },
            {
                "id": 127,
                "operation_date": "2026-08-24",
                "operation_type": "expense",
                "name": "Other Cafe",
                "amount": 100,
                "category": "Еда",
                "subcategory": "Кафе",
            },
        ]
    )
    bot = FakeEntryBot(storage)
    editor = TelegramEntryEditor(bot)

    editor.handle_callback(
        {"id": "callback-1", "message": {"message_id": 10}},
        chat_id=123,
        action="edelok",
        payload="126",
    )

    assert storage.deleted_entry_ids == [126]
    assert storage.last_count_period == (date(2026, 8, 24), date(2026, 8, 24))
    assert storage.last_list_period == (date(2026, 8, 24), date(2026, 8, 24))
    assert bot.answers == ["Удалено"]
    assert len(bot.sent_messages) == 1
    assert "Записи 24.08 - 24.08" in bot.sent_messages[0][1]
    assert "Other Cafe" in bot.sent_messages[0][1]
    assert "IP Hakimov F.D" not in bot.sent_messages[0][1]


def test_day_entry_list_has_previous_and_next_day_buttons() -> None:
    storage = FakeEntryStorage(
        [
            {
                "id": 1,
                "operation_date": "2026-08-01",
                "operation_type": "expense",
                "name": "Августина",
                "amount": 309.96,
                "category": "Еда",
                "subcategory": "Супермаркеты",
            }
        ]
    )
    bot = FakeEntryBot(storage)
    editor = TelegramEntryEditor(bot)

    editor.send_entry_list(123, date(2026, 8, 1), date(2026, 8, 1))

    reply_markup = bot.sent_messages[0][2]
    assert reply_markup is not None
    rows = reply_markup["inline_keyboard"]
    assert [
        {"text": "← 31.07", "callback_data": "entrylist:2026-07-31:2026-07-31:all"},
        {"text": "02.08 →", "callback_data": "entrylist:2026-08-02:2026-08-02:all"},
    ] in rows


def test_empty_day_entry_list_keeps_previous_and_next_day_buttons() -> None:
    storage = FakeEntryStorage([])
    bot = FakeEntryBot(storage)
    editor = TelegramEntryEditor(bot)

    editor.send_entry_list(123, date(2026, 8, 1), date(2026, 8, 1))

    assert "За 01.08 записей не нашел." in bot.sent_messages[0][1]
    reply_markup = bot.sent_messages[0][2]
    assert reply_markup is not None
    rows = reply_markup["inline_keyboard"]
    assert [
        {"text": "← 31.07", "callback_data": "entrylist:2026-07-31:2026-07-31:all"},
        {"text": "02.08 →", "callback_data": "entrylist:2026-08-02:2026-08-02:all"},
    ] in rows
