from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import Any, Dict, Optional

from .models import OperationStatus, OperationType, ParsedOperation
from .storage import operation_hash as make_operation_hash
from .telegram_common import button_rows, parse_index, parse_money_amount, parse_user_operation_date


MANUAL_ACTIONS = {"mtoday", "mcat", "msub", "mincat", "mcancel"}


class TelegramManualEntry:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    def start(
        self,
        chat_id: int,
        operation_type: OperationType,
        message_id: Optional[int] = None,
    ) -> None:
        if message_id is not None:
            self.bot._delete_message(chat_id, message_id)
        state = {
            "kind": "manual_entry",
            "type": operation_type.value,
            "stage": "date",
            "cleanup_message_ids": [],
        }
        label = "расход" if operation_type == OperationType.EXPENSE else "доход"
        self.send_prompt(
            chat_id,
            state,
            f"Добавляем {label}. Напиши дату в формате 22.08 или нажми «Сегодня».",
            reply_markup=manual_date_keyboard(),
        )

    def handle_text(
        self,
        chat_id: int,
        text: str,
        pending: Dict[str, Any],
        message_id: Optional[int] = None,
    ) -> None:
        if message_id is not None:
            self.bot._delete_message(chat_id, message_id)
        if text.casefold() in {"отмена", "cancel"}:
            state = manual_state_from_pending(pending)
            if state is not None:
                self.cleanup_messages(chat_id, state)
            self.bot.context.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat_id, "Ручное добавление отменено.", reply_markup=self.bot._main_reply_keyboard())
            return

        state = manual_state_from_pending(pending)
        if state is None:
            self.bot.context.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat_id, "Старый ручной ввод сбросил. Нажми «Расход» или «Доход» заново.")
            return

        stage = state.get("stage")
        if stage == "date":
            operation_date = date.today() if text == "Сегодня" else parse_user_operation_date(text, date.today().year)
            if operation_date is None:
                self.send_prompt(chat_id, state, "Не понял дату. Напиши так: 22.08", reply_markup=manual_date_keyboard())
                return
            state["date"] = operation_date.isoformat()
            state["stage"] = "amount"
            self.send_prompt(chat_id, state, "Теперь сумма. Можно писать 36857 или 36 857,50.")
            return

        if stage == "amount":
            amount = parse_money_amount(text)
            if amount is None or amount <= 0:
                self.send_prompt(chat_id, state, "Не понял сумму. Напиши число, например 36857 или 51700,70.")
                return
            state["amount"] = amount
            if state.get("type") == OperationType.EXPENSE.value:
                state["stage"] = "category"
                self.send_prompt(chat_id, state, "Выбери категорию расхода:", reply_markup=self.category_keyboard(chat_id))
                return
            state["stage"] = "income_category"
            self.send_prompt(chat_id, state, "Выбери категорию дохода:", reply_markup=self.income_category_keyboard(chat_id))
            return

        if stage == "description":
            operation = manual_operation_from_state(state, text.strip() or None)
            if operation is None:
                self.cleanup_messages(chat_id, state)
                self.bot.context.storage.delete_pending_action(pending["operation_hash"])
                self.bot._send_message(chat_id, "Не смог собрать операцию. Начни заново через «Расход» или «Доход».")
                return
            self.cleanup_messages(chat_id, state)
            self.bot.context.storage.delete_pending_action(pending["operation_hash"])
            self.write_operation(chat_id, operation)
            return

        self.send_prompt(chat_id, state, "Выбери вариант кнопкой ниже или напиши «Отмена».")

    def handle_callback(self, callback: Dict[str, Any], chat_id: int, action: str, payload: str) -> None:
        pending_hash = manual_pending_hash(chat_id)
        pending = self.bot.context.storage.get_pending_action(pending_hash, chat_id)
        state = manual_state_from_pending(pending) if pending is not None else None
        if state is None:
            self.bot._answer_callback(callback["id"], "Ручной ввод не найден")
            self.bot._delete_callback_message(callback)
            return
        if action == "mcancel":
            self.cleanup_messages(chat_id, state)
            self.bot.context.storage.delete_pending_action(pending_hash)
            self.bot._answer_callback(callback["id"], "Отменено")
            self.bot._delete_callback_message(callback)
            self.bot._send_message(chat_id, "Ручное добавление отменено.", reply_markup=self.bot._main_reply_keyboard())
            return
        if action == "mtoday":
            state["date"] = date.today().isoformat()
            state["stage"] = "amount"
            self.bot._answer_callback(callback["id"], "Сегодня")
            self.bot._delete_callback_message(callback)
            self.send_prompt(chat_id, state, "Теперь сумма. Можно писать 36857 или 36 857,50.")
            return
        if action == "mcat":
            category_index = parse_index(payload, 1)
            category_item = self.bot._category_by_index(category_index)
            if category_item is None:
                self.bot._answer_callback(callback["id"], "Категория устарела")
                self.bot._delete_callback_message(callback)
                self.bot._send_message(chat_id, "Выбери категорию расхода:", reply_markup=self.category_keyboard(chat_id))
                return
            category, _subcategories = category_item
            state["category"] = category
            state["stage"] = "subcategory"
            self.bot._answer_callback(callback["id"], category)
            self.bot._delete_callback_message(callback)
            self.send_prompt(
                chat_id,
                state,
                f"Выбери подкатегорию для {category}:",
                reply_markup=self.subcategory_keyboard(chat_id, category_index),
            )
            return
        if action == "msub":
            category_index = parse_index(payload, 1)
            subcategory_index = parse_index(payload, 2)
            pair = self.bot._subcategory_by_index(category_index, subcategory_index)
            if pair is None:
                self.bot._answer_callback(callback["id"], "Подкатегория устарела")
                self.bot._delete_callback_message(callback)
                self.bot._send_message(chat_id, "Выбери категорию расхода:", reply_markup=self.category_keyboard(chat_id))
                return
            category, subcategory = pair
            state["category"] = category
            state["subcategory"] = subcategory
            operation = manual_operation_from_state(state)
            if operation is None:
                self.cleanup_messages(chat_id, state)
                self.bot.context.storage.delete_pending_action(pending_hash)
                self.bot._answer_callback(callback["id"], "Не смог собрать операцию")
                self.bot._delete_callback_message(callback)
                return
            self.cleanup_messages(chat_id, state)
            self.bot.context.storage.delete_pending_action(pending_hash)
            self.bot._answer_callback(callback["id"], f"{category} / {subcategory}")
            self.bot._delete_callback_message(callback)
            self.write_operation(chat_id, operation)
            return
        if action == "mincat":
            category_index = parse_index(payload, 1)
            category = self.bot._income_category_by_index(category_index)
            if category is None:
                self.bot._answer_callback(callback["id"], "Категория устарела")
                self.bot._delete_callback_message(callback)
                self.bot._send_message(chat_id, "Выбери категорию дохода:", reply_markup=self.income_category_keyboard(chat_id))
                return
            state["category"] = category
            operation = manual_operation_from_state(state)
            if operation is None:
                self.cleanup_messages(chat_id, state)
                self.bot.context.storage.delete_pending_action(pending_hash)
                self.bot._answer_callback(callback["id"], "Не смог собрать операцию")
                self.bot._delete_callback_message(callback)
                return
            self.cleanup_messages(chat_id, state)
            self.bot.context.storage.delete_pending_action(pending_hash)
            self.bot._answer_callback(callback["id"], category)
            self.bot._delete_callback_message(callback)
            self.write_operation(chat_id, operation)
            return

    def send_prompt(
        self,
        chat_id: int,
        state: Dict[str, Any],
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        message_id = self.bot._send_message(chat_id, text, reply_markup=reply_markup)
        if message_id is not None:
            cleanup_message_ids = list(state.get("cleanup_message_ids") or [])
            cleanup_message_ids.append(message_id)
            state["cleanup_message_ids"] = cleanup_message_ids
        self.save_state(chat_id, state)

    def cleanup_messages(self, chat_id: int, state: Dict[str, Any]) -> None:
        for message_id in state.get("cleanup_message_ids") or []:
            self.bot._delete_message(chat_id, int(message_id))

    def save_state(self, chat_id: int, state: Dict[str, Any]) -> None:
        self.bot.context.storage.add_pending_action(
            operation_hash=manual_pending_hash(chat_id),
            chat_id=chat_id,
            message_id=None,
            prompt=manual_state_to_prompt(state),
        )

    def write_operation(self, chat_id: int, operation: ParsedOperation) -> None:
        bank = f"manual:{chat_id}:{time.time_ns()}"
        op_hash = make_operation_hash(bank, operation)
        first_row, _last_row = self.bot._write_operation_entries(op_hash, operation, bank, source="manual")
        self.bot.context.storage.record_operation(
            operation_hash=op_hash,
            image_hash=f"manual:{op_hash}",
            bank=bank,
            operation=operation,
            status=OperationStatus.AUTO_WRITTEN,
            workbook_row=first_row,
            status_note="manual entry",
        )
        summary_operations = [operation]
        if operation.type == OperationType.EXPENSE:
            summary_operations = _expense_operations_for_day(self.bot.context.storage, operation.date) or [operation]
        self.bot._send_message(
            chat_id,
            "\n".join(self.bot._written_operation_summary_lines(summary_operations)),
            reply_markup=self.bot._main_reply_keyboard(),
        )

    def category_keyboard(self, chat_id: int) -> Dict[str, Any]:
        pending_hash = manual_pending_hash(chat_id)
        buttons = [
            {"text": category, "callback_data": f"mcat:{pending_hash}:{index}"}
            for index, (category, _subcategories) in enumerate(self.bot._category_items())
        ]
        rows = button_rows(buttons, columns=2)
        rows.append([{"text": "Отмена", "callback_data": f"mcancel:{pending_hash}"}])
        return {"inline_keyboard": rows}

    def subcategory_keyboard(self, chat_id: int, category_index: int) -> Dict[str, Any]:
        category_item = self.bot._category_by_index(category_index)
        if category_item is None:
            return self.category_keyboard(chat_id)
        pending_hash = manual_pending_hash(chat_id)
        _category, subcategories = category_item
        buttons = [
            {"text": subcategory, "callback_data": f"msub:{pending_hash}:{category_index}:{index}"}
            for index, subcategory in enumerate(subcategories)
        ]
        rows = button_rows(buttons, columns=2)
        rows.append([{"text": "Отмена", "callback_data": f"mcancel:{pending_hash}"}])
        return {"inline_keyboard": rows}

    def income_category_keyboard(self, chat_id: int) -> Dict[str, Any]:
        pending_hash = manual_pending_hash(chat_id)
        buttons = [
            {"text": category, "callback_data": f"mincat:{pending_hash}:{index}"}
            for index, category in enumerate(self.bot.context.category_book.income_categories)
        ]
        rows = button_rows(buttons, columns=2)
        rows.append([{"text": "Отмена", "callback_data": f"mcancel:{pending_hash}"}])
        return {"inline_keyboard": rows}


def manual_pending_hash(chat_id: int) -> str:
    return f"manual-{chat_id}"


def manual_state_to_prompt(state: Dict[str, Any]) -> str:
    return "manual:" + json.dumps(state, ensure_ascii=False)


def manual_state_from_pending(pending: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if pending is None:
        return None
    prompt = str(pending.get("prompt") or "")
    if not prompt.startswith("manual:"):
        return None
    try:
        state = json.loads(prompt.removeprefix("manual:"))
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) and state.get("kind") == "manual_entry" else None


def is_manual_pending(pending: Dict[str, Any]) -> bool:
    return manual_state_from_pending(pending) is not None


def manual_date_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Сегодня", "callback_data": "mtoday:manual"},
                {"text": "Отмена", "callback_data": "mcancel:unused"},
            ]
        ]
    }


def manual_operation_from_state(state: Dict[str, Any], name: Optional[str] = None) -> Optional[ParsedOperation]:
    try:
        operation_type = OperationType(str(state["type"]))
        operation_date = date.fromisoformat(str(state["date"]))
        amount = float(state["amount"])
    except (KeyError, ValueError, TypeError):
        return None
    category = str(state.get("category") or "").strip() or None
    subcategory = str(state.get("subcategory") or "").strip() or None
    signed_amount = -abs(amount) if operation_type == OperationType.EXPENSE else abs(amount)
    operation_name = (name or subcategory or category or "Ручная операция").strip()
    return ParsedOperation(
        date=operation_date,
        name=operation_name,
        amount=signed_amount,
        type=operation_type,
        category=category,
        subcategory=subcategory,
        confidence=1.0,
        needs_review=False,
        note="",
    )


def parse_manual_operation_text(text: str, year: int, category_book) -> Optional[ParsedOperation]:
    match = re.match(r"^\s*([+-])\s*([\d\s]+(?:[,.]\d{1,2})?)\s+(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)\s+(.+)$", text)
    if match is None:
        return None
    sign, amount_text, date_text, rest = match.groups()
    amount = parse_money_amount(amount_text)
    operation_date = parse_user_operation_date(date_text, year)
    if amount is None or operation_date is None:
        return None
    if sign == "-":
        resolved = _parse_manual_expense_tail(rest, category_book)
        if resolved is None:
            return None
        category, subcategory, name = resolved
        return ParsedOperation(
            date=operation_date,
            name=name,
            amount=-amount,
            type=OperationType.EXPENSE,
            category=category,
            subcategory=subcategory,
            confidence=1.0,
            note=name,
        )
    resolved_income = _parse_manual_income_tail(rest, category_book)
    if resolved_income is None:
        return None
    category, name = resolved_income
    return ParsedOperation(
        date=operation_date,
        name=name,
        amount=amount,
        type=OperationType.INCOME,
        category=category,
        confidence=1.0,
        note=name,
    )


def _parse_manual_expense_tail(text: str, category_book) -> Optional[tuple[str, str, str]]:
    normalized = text.strip()
    for category, subcategories in category_book.expense_categories.items():
        prefix = f"{category} / "
        if not normalized.casefold().startswith(prefix.casefold()):
            continue
        tail = normalized[len(prefix) :].strip()
        for subcategory in sorted(subcategories, key=len, reverse=True):
            if tail.casefold() == subcategory.casefold():
                return category, subcategory, subcategory
            sub_prefix = f"{subcategory} "
            if tail.casefold().startswith(sub_prefix.casefold()):
                name = tail[len(sub_prefix) :].strip()
                return category, subcategory, name or subcategory
    return None


def _parse_manual_income_tail(text: str, category_book) -> Optional[tuple[str, str]]:
    normalized = text.strip()
    for category in sorted(category_book.income_categories, key=len, reverse=True):
        if normalized.casefold() == category.casefold():
            return category, category
        prefix = f"{category} "
        if normalized.casefold().startswith(prefix.casefold()):
            name = normalized[len(prefix) :].strip()
            return category, name or category
    return None


def _expense_operations_for_day(storage, operation_date: date) -> list[ParsedOperation]:
    entries = storage.budget_entries(
        operation_date,
        operation_date,
        operation_type=OperationType.EXPENSE,
        limit=100,
    )
    return [
        ParsedOperation(
            date=operation_date,
            name=str(entry.get("name") or "операция"),
            amount=-abs(float(entry["amount"])),
            type=OperationType.EXPENSE,
            category=entry.get("category"),
            subcategory=entry.get("subcategory"),
            confidence=1.0,
            needs_review=False,
            note=str(entry.get("note") or ""),
        )
        for entry in entries
    ]
