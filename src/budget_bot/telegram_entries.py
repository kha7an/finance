from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, Optional

from .models import OperationType, ParsedOperation
from .telegram_common import button_rows, format_money, operation_summary_text, parse_index, parse_money_amount, parse_user_operation_date


ENTRY_ACTIONS = {"entrylist", "entry", "edel", "edelok", "eamt", "edate", "ename", "ecat", "esub", "eback"}


class TelegramEntryEditor:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    def handle_callback(self, callback: Dict[str, Any], chat_id: int, action: str, payload: str) -> None:
        if action == "entrylist":
            period = _parse_entrylist_payload(payload)
            if period is None:
                self.bot._answer_callback(callback["id"], "Период устарел")
                return
            start_date, end_date, category = period
            self.bot._answer_callback(callback["id"], "Показываю записи")
            self.bot._delete_callback_message(callback)
            self.send_entry_list(chat_id, start_date, end_date, category=category)
            return

        entry_id = parse_index(payload, 0)
        if entry_id is None:
            self.bot._answer_callback(callback["id"], "Запись устарела")
            return
        entry = self.bot.context.storage.get_budget_entry(entry_id)
        if entry is None:
            self.bot._answer_callback(callback["id"], "Запись уже удалена")
            self.bot._delete_callback_message(callback)
            return

        if action == "entry":
            self.bot._answer_callback(callback["id"], "Открываю")
            self.bot._delete_callback_message(callback)
            self.send_entry_actions(chat_id, entry)
            return
        if action == "edel":
            self.bot._answer_callback(callback["id"], "Нужно подтверждение")
            self.bot._delete_callback_message(callback)
            self.send_entry_delete_confirmation(chat_id, entry)
            return
        if action == "edelok":
            self.delete_budget_entry(entry)
            self.bot._answer_callback(callback["id"], "Удалено")
            self.bot._delete_callback_message(callback)
            self.bot._send_message(chat_id, "Запись удалена.", reply_markup=self.bot._main_reply_keyboard())
            return
        if action in {"eamt", "edate", "ename"}:
            prompt = {
                "eamt": "Напиши новую сумму, например 1250 или 1 250,50.",
                "edate": "Напиши новую дату в формате 22.08.",
                "ename": "Напиши новое описание.",
            }[action]
            self.bot.context.storage.add_pending_action(
                operation_hash=entry_edit_pending_hash(chat_id),
                chat_id=chat_id,
                message_id=callback["message"].get("message_id"),
                prompt=entry_edit_state_to_prompt({"kind": "entry_edit", "entry_id": entry_id, "field": action}),
            )
            self.bot._answer_callback(callback["id"], "Жду текст")
            self.bot._delete_callback_message(callback)
            self.bot._send_message(chat_id, prompt)
            return
        if action == "ecat":
            category_index = parse_index(payload, 1)
            if category_index is None:
                self.bot._answer_callback(callback["id"], "Категория")
                self.bot._delete_callback_message(callback)
                self.send_entry_category_picker(chat_id, entry_id)
                return
            category_item = self.bot._category_by_index(category_index)
            if category_item is None:
                self.bot._answer_callback(callback["id"], "Категория устарела")
                self.bot._delete_callback_message(callback)
                self.send_entry_category_picker(chat_id, entry_id)
                return
            category, _subcategories = category_item
            self.bot._answer_callback(callback["id"], category)
            self.bot._delete_callback_message(callback)
            self.send_entry_subcategory_picker(chat_id, entry_id, category_index)
            return
        if action == "esub":
            category_index = parse_index(payload, 1)
            subcategory_index = parse_index(payload, 2)
            pair = self.bot._subcategory_by_index(category_index, subcategory_index)
            if pair is None:
                self.bot._answer_callback(callback["id"], "Подкатегория устарела")
                self.bot._delete_callback_message(callback)
                self.send_entry_category_picker(chat_id, entry_id)
                return
            category, subcategory = pair
            operation = entry_operation(entry, category=category, subcategory=subcategory)
            self.bot.context.storage.update_budget_entry(entry_id, operation)
            self.bot._answer_callback(callback["id"], f"{category} / {subcategory}")
            self.bot._delete_callback_message(callback)
            self.bot._send_message(chat_id, "Категория обновлена.", reply_markup=self.bot._main_reply_keyboard())
            return
        if action == "eback":
            self.bot._answer_callback(callback["id"], "Назад")
            self.bot._delete_callback_message(callback)
            self.send_entry_actions(chat_id, entry)
            return

    def handle_edit_text(
        self,
        chat_id: int,
        text: str,
        pending: Dict[str, Any],
        message_id: Optional[int] = None,
    ) -> None:
        if message_id is not None:
            self.bot._delete_message(chat_id, message_id)
        state = entry_edit_state_from_pending(pending)
        if state is None:
            self.bot.context.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat_id, "Старое редактирование сбросил. Открой запись заново.")
            return
        entry_id = int(state["entry_id"])
        entry = self.bot.context.storage.get_budget_entry(entry_id)
        if entry is None:
            self.bot.context.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat_id, "Запись уже удалена.")
            return
        field = str(state.get("field") or "")
        if text.casefold() in {"отмена", "cancel"}:
            self.bot.context.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat_id, "Редактирование отменено.", reply_markup=self.bot._main_reply_keyboard())
            return
        if field == "eamt":
            amount = parse_money_amount(text)
            if amount is None or amount <= 0:
                self.bot._send_message(chat_id, "Не понял сумму. Напиши число, например 1250 или 1 250,50.")
                return
            operation = entry_operation(entry, amount=amount)
        elif field == "edate":
            operation_date = parse_user_operation_date(text, date.today().year)
            if operation_date is None:
                self.bot._send_message(chat_id, "Не понял дату. Напиши так: 22.08")
                return
            operation = entry_operation(entry, operation_date=operation_date)
        elif field == "ename":
            name = text.strip()
            if not name:
                self.bot._send_message(chat_id, "Описание не должно быть пустым.")
                return
            operation = entry_operation(entry, name=name, note=name)
        else:
            self.bot.context.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat_id, "Не понял поле редактирования. Открой запись заново.")
            return
        self.bot.context.storage.update_budget_entry(entry_id, operation)
        self.bot.context.storage.delete_pending_action(pending["operation_hash"])
        self.bot._send_message(chat_id, f"Обновлено: {operation_summary_text(operation)}", reply_markup=self.bot._main_reply_keyboard())

    def send_entry_list(
        self,
        chat_id: int,
        start_date: date,
        end_date: date,
        category: Optional[str] = None,
    ) -> None:
        entries = self.bot.context.storage.budget_entries(start_date, end_date, category=category)
        if not entries:
            self.bot._send_message(chat_id, "За этот период записей не нашел.", reply_markup=self.bot._main_reply_keyboard())
            return
        lines = [f"Записи {start_date.strftime('%d.%m')} - {end_date.strftime('%d.%m')}:"]
        buttons = []
        for index, entry in enumerate(entries, start=1):
            lines.append(f"{index}. {entry_summary_text(entry)}")
            buttons.append({"text": str(index), "callback_data": f"entry:{entry['id']}"})
        rows = button_rows(buttons, columns=5)
        rows.append([{"text": "Главное меню", "callback_data": "menu:home"}])
        self.bot._send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": rows})

    def send_entry_actions(self, chat_id: int, entry: Dict[str, Any]) -> None:
        entry_id = int(entry["id"])
        self.bot._send_message(
            chat_id,
            entry_details_text(entry),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Сумма", "callback_data": f"eamt:{entry_id}"},
                        {"text": "Дата", "callback_data": f"edate:{entry_id}"},
                    ],
                    [
                        {"text": "Описание", "callback_data": f"ename:{entry_id}"},
                        {"text": "Категория", "callback_data": f"ecat:{entry_id}"},
                    ],
                    [{"text": "Удалить", "callback_data": f"edel:{entry_id}"}],
                ]
            },
        )

    def send_entry_delete_confirmation(self, chat_id: int, entry: Dict[str, Any]) -> None:
        entry_id = int(entry["id"])
        self.bot._send_message(
            chat_id,
            f"Удалить запись?\n{entry_summary_text(entry)}",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Да, удалить", "callback_data": f"edelok:{entry_id}"},
                        {"text": "Отмена", "callback_data": f"entry:{entry_id}"},
                    ]
                ]
            },
        )

    def delete_budget_entry(self, entry: Dict[str, Any]) -> None:
        entry_id = int(entry["id"])
        self.bot.context.storage.delete_budget_entry(entry_id)

    def send_entry_category_picker(self, chat_id: int, entry_id: int) -> None:
        buttons = [
            {"text": category, "callback_data": f"ecat:{entry_id}:{index}"}
            for index, (category, _subcategories) in enumerate(self.bot._category_items())
        ]
        rows = button_rows(buttons, columns=2)
        self.bot._send_message(chat_id, "Выбери новую категорию:", reply_markup={"inline_keyboard": rows})

    def send_entry_subcategory_picker(self, chat_id: int, entry_id: int, category_index: int) -> None:
        category_item = self.bot._category_by_index(category_index)
        if category_item is None:
            self.send_entry_category_picker(chat_id, entry_id)
            return
        category, subcategories = category_item
        buttons = [
            {"text": subcategory, "callback_data": f"esub:{entry_id}:{category_index}:{index}"}
            for index, subcategory in enumerate(subcategories)
        ]
        rows = button_rows(buttons, columns=2)
        rows.append([{"text": "Назад", "callback_data": f"eback:{entry_id}"}])
        self.bot._send_message(chat_id, f"Выбери подкатегорию для {category}:", reply_markup={"inline_keyboard": rows})


def entry_edit_pending_hash(chat_id: int) -> str:
    return f"entry-edit-{chat_id}"


def entry_edit_state_to_prompt(state: Dict[str, Any]) -> str:
    return "entry_edit:" + json.dumps(state, ensure_ascii=False)


def entry_edit_state_from_pending(pending: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if pending is None:
        return None
    prompt = str(pending.get("prompt") or "")
    if not prompt.startswith("entry_edit:"):
        return None
    try:
        state = json.loads(prompt.removeprefix("entry_edit:"))
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) and state.get("kind") == "entry_edit" else None


def is_entry_edit_pending(pending: Dict[str, Any]) -> bool:
    return entry_edit_state_from_pending(pending) is not None


def expense_report_keyboard(start_date: date, end_date: date, category: Optional[str] = None) -> Dict[str, Any]:
    category_payload = category or "all"
    payload = f"{start_date.isoformat()}:{end_date.isoformat()}:{category_payload}"
    return {
        "inline_keyboard": [
            [{"text": "Записи", "callback_data": f"entrylist:{payload}"}],
            [{"text": "Главное меню", "callback_data": "menu:home"}],
        ]
    }


def _parse_entrylist_payload(text: str) -> Optional[tuple[date, date, Optional[str]]]:
    parts = text.split(":", 2)
    if len(parts) < 2:
        return None
    try:
        start_date = date.fromisoformat(parts[0])
        end_date = date.fromisoformat(parts[1])
    except ValueError:
        return None
    category = parts[2].strip() if len(parts) == 3 and parts[2].strip() and parts[2] != "all" else None
    return start_date, end_date, category


def entry_summary_text(entry: Dict[str, Any]) -> str:
    operation_date = date.fromisoformat(str(entry["operation_date"]))
    return (
        f"{operation_date.strftime('%d.%m')} "
        f"{entry.get('name') or 'операция'}: "
        f"{format_money(float(entry['amount']))} - "
        f"{entry_category_summary(entry)}"
    )


def entry_details_text(entry: Dict[str, Any]) -> str:
    return (
        "Запись:\n"
        f"{entry_summary_text(entry)}\n"
        f"ID записи: {entry['id']}"
    )


def entry_category_summary(entry: Dict[str, Any]) -> str:
    category = str(entry.get("category") or "").strip()
    subcategory = str(entry.get("subcategory") or "").strip()
    if category and subcategory:
        return f"{category} / {subcategory}"
    if category:
        return category
    return str(entry.get("operation_type") or "операция")


def entry_operation(
    entry: Dict[str, Any],
    operation_date: Optional[date] = None,
    amount: Optional[float] = None,
    name: Optional[str] = None,
    note: Optional[str] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
) -> ParsedOperation:
    operation_type = OperationType(str(entry["operation_type"]))
    entry_date = operation_date or date.fromisoformat(str(entry["operation_date"]))
    entry_amount = float(entry["amount"]) if amount is None else abs(float(amount))
    signed_amount = -abs(entry_amount) if operation_type == OperationType.EXPENSE else abs(entry_amount)
    entry_name = (name if name is not None else str(entry.get("name") or "")).strip() or "операция"
    return ParsedOperation(
        date=entry_date,
        name=entry_name,
        amount=signed_amount,
        type=operation_type,
        category=category if category is not None else entry.get("category"),
        subcategory=subcategory if subcategory is not None else entry.get("subcategory"),
        confidence=1.0,
        needs_review=False,
        note=note or "",
    )
