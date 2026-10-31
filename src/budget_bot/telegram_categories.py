from __future__ import annotations

import json
from typing import Any, Optional

from .telegram_common import button_rows


class TelegramCategories:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    def _pending_hash(self, chat_id: int) -> str:
        return f"catalog-{chat_id}"

    def _clear(self, chat_id: int) -> None:
        self.bot.context.storage.delete_pending_action(self._pending_hash(chat_id))

    def open(self, chat_id: int, kind: str = "expense") -> None:
        if kind not in {"expense", "income"}:
            raise ValueError("Неизвестный тип категории.")
        self._clear(chat_id)
        items = [row for row in self.bot.context.storage.category_catalog() if row["kind"] == kind]
        rows = [[{"text": "Расходы", "callback_data": "catalog:list:expense"}, {"text": "Доходы", "callback_data": "catalog:list:income"}]]
        rows += button_rows([{"text": row["name"], "callback_data": f"catalog:show:{row['id']}"} for row in items], columns=2)
        rows.append([{"text": "Добавить категорию", "callback_data": f"catalog:add:{kind}"}])
        rows.append([{"text": "В меню", "callback_data": "catalog:close"}])
        label = "расходов" if kind == "expense" else "доходов"
        self.bot._send_message(chat_id, f"Категории {label}", reply_markup={"inline_keyboard": rows})

    def _item(self, category_id: int, subcategory_id: Optional[int] = None):
        category = next((row for row in self.bot.context.storage.category_catalog() if row["id"] == category_id), None)
        if category is None:
            raise ValueError("Категория не найдена. Открой список заново.")
        subcategory = None
        if subcategory_id is not None:
            subcategory = next((row for row in category["subcategories"] if row["id"] == subcategory_id), None)
            if subcategory is None:
                raise ValueError("Подкатегория не найдена. Открой список заново.")
        return category, subcategory

    def show(self, chat_id: int, category_id: int, subcategory_id: Optional[int] = None) -> None:
        category, subcategory = self._item(category_id, subcategory_id)
        target = str(category_id) + (f":{subcategory_id}" if subcategory else "")
        rows = []
        if category["kind"] == "expense" and subcategory is None:
            rows = button_rows([{"text": row["name"], "callback_data": f"catalog:show:{category_id}:{row['id']}"} for row in category["subcategories"]], columns=2)
            rows.append([{"text": "Добавить подкатегорию", "callback_data": f"catalog:addsub:{category_id}"}])
        if subcategory:
            rows.append([{"text": "Перенести в другую категорию", "callback_data": f"catalog:move:{subcategory_id}"}])
        else:
            rows.append([{"text": "Изменить цвет", "callback_data": f"catalog:color:{category_id}"}])
        rows.append([{"text": "Переименовать", "callback_data": f"catalog:rename:{target}"}, {"text": "Удалить", "callback_data": f"catalog:delete:{target}"}])
        back = f"catalog:show:{category_id}" if subcategory else f"catalog:list:{category['kind']}"
        rows.append([{"text": "Назад", "callback_data": back}])
        label = category["name"] + (f" / {subcategory['name']}" if subcategory else "")
        self.bot._send_message(chat_id, label, reply_markup={"inline_keyboard": rows})

    def _prompt(self, chat_id: int, state: dict, text: str) -> None:
        self.bot.context.storage.add_pending_action(
            operation_hash=self._pending_hash(chat_id), chat_id=chat_id, message_id=None,
            prompt="catalog:" + json.dumps(state),
        )
        self.bot._send_message(chat_id, text, reply_markup={"inline_keyboard": [[{"text": "Отмена", "callback_data": "catalog:cancel"}]]})

    def handle_callback(self, callback: dict, chat_id: int, payload: str) -> None:
        parts = payload.split(":")
        action = parts[0]
        self.bot._answer_callback(callback["id"], "Категории")
        try:
            if action == "color":
                self._item(int(parts[1]))
                self._prompt(
                    chat_id, {"action": "color", "category_id": int(parts[1])}, "Новый цвет в формате #4c78a8:"
                )
                return
            if action == "move":
                source = int(parts[1])
                rows = [
                    r
                    for r in self.bot.context.storage.category_catalog()
                    if r["kind"] == "expense" and all(sub["id"] != source for sub in r["subcategories"])
                ]
                self.bot._send_message(
                    chat_id,
                    "Куда перенести подкатегорию?",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": r["name"], "callback_data": f"catalog:movepreview:{source}:{r['id']}"}]
                            for r in rows
                        ]
                    },
                )
                return
            if action in {"movepreview", "moveconfirm"}:
                source, target = map(int, parts[1:])
                count = self.bot.context.storage.move_subcategory(source, target, action == "moveconfirm")
                if action == "movepreview":
                    self.bot._send_message(
                        chat_id,
                        f"Перенести подкатегорию? Затронуто записей: {count}. Обновятся история, правила и регулярные шаблоны. Лимиты категорий останутся привязаны к своим категориям.",
                        reply_markup={
                            "inline_keyboard": [
                                [
                                    {
                                        "text": "Подтвердить перенос",
                                        "callback_data": f"catalog:moveconfirm:{source}:{target}",
                                    },
                                    {"text": "Отмена", "callback_data": "catalog:cancel"},
                                ]
                            ]
                        },
                    )
                else:
                    self.bot._send_message(chat_id, f"Перенесено. Обновлено записей: {count}.")
                return
            if action in {"close", "cancel"}:
                self._clear(chat_id)
                self.bot._delete_callback_message(callback)
                if action == "cancel":
                    self.open(chat_id)
                else:
                    self.bot._send_message(chat_id, "Главное меню:", reply_markup=self.bot._main_reply_keyboard())
                return
            if action == "list" and len(parts) == 2:
                self.open(chat_id, parts[1])
            elif action == "add" and len(parts) == 2 and parts[1] in {"expense", "income"}:
                self._prompt(chat_id, {"action": "add", "kind": parts[1]}, "Название новой категории:")
            elif action in {"show", "addsub", "rename", "delete", "confirm"} and len(parts) in {2, 3}:
                category_id = int(parts[1])
                subcategory_id = int(parts[2]) if len(parts) == 3 else None
                category, subcategory = self._item(category_id, subcategory_id)
                if action == "show":
                    self._clear(chat_id)
                    self.show(chat_id, category_id, subcategory_id)
                elif action == "addsub" and subcategory_id is None and category["kind"] == "expense":
                    self._prompt(chat_id, {"action": "addsub", "category_id": category_id}, "Название новой подкатегории:")
                elif action == "rename":
                    self._prompt(chat_id, {"action": "rename", "category_id": category_id, "subcategory_id": subcategory_id}, "Новое название:")
                elif action == "delete":
                    target = str(category_id) + (f":{subcategory_id}" if subcategory else "")
                    label = subcategory["name"] if subcategory else category["name"]
                    self.bot._send_message(chat_id, f"Удалить «{label}»?", reply_markup={"inline_keyboard": [[
                        {"text": "Удалить", "callback_data": f"catalog:confirm:{target}"},
                        {"text": "Отмена", "callback_data": f"catalog:show:{target}"},
                    ]]})
                elif action == "confirm":
                    self.bot.context.storage.delete_category(category_id, subcategory_id)
                    self.bot._send_message(chat_id, "Удалено.")
                    self.open(chat_id, category["kind"])
                else:
                    raise ValueError("Действие недоступно.")
            else:
                raise ValueError("Кнопка устарела. Открой категории заново.")
        except (ValueError, IndexError) as exc:
            self.bot._send_message(chat_id, str(exc), reply_markup={"inline_keyboard": [[{"text": "К списку", "callback_data": "catalog:list:expense"}]]})
            return
        self.bot._delete_callback_message(callback)

    def handle_text(self, chat_id: int, text: str) -> bool:
        pending = self.bot.context.storage.get_pending_action(self._pending_hash(chat_id), chat_id)
        if pending is None:
            return False
        if text.casefold() in {"отмена", "cancel", "/cancel"}:
            self.open(chat_id)
            return True
        if text.startswith("/") or text in {"Расход", "Доход", "Аналитика", "Синхронизировать", "Сброс", "Категории"}:
            self._clear(chat_id)
            return False
        try:
            state = json.loads(pending["prompt"][8:])
            action = state["action"]
        except (ValueError, KeyError, TypeError):
            self._clear(chat_id)
            self.bot._send_message(chat_id, "Ввод устарел. Открой категории заново.")
            return True
        try:
            storage = self.bot.context.storage
            if action == "color":
                storage.set_category_color(state["category_id"], text)
                self._clear(chat_id)
                self.show(chat_id, state["category_id"])
            elif action == "add":
                category_id = storage.create_category(state["kind"], text)
                self._clear(chat_id)
                self.show(chat_id, category_id)
            elif action == "addsub":
                storage.create_subcategory(state["category_id"], text)
                self._clear(chat_id)
                self.show(chat_id, state["category_id"])
            elif action == "rename":
                storage.rename_category(state["category_id"], text, state.get("subcategory_id"))
                self._clear(chat_id)
                self.show(chat_id, state["category_id"], state.get("subcategory_id"))
            else:
                raise ValueError("Открой категории заново.")
        except ValueError as exc:
            self.bot._send_message(chat_id, str(exc), reply_markup={"inline_keyboard": [[{"text": "Отмена", "callback_data": "catalog:cancel"}]]})
        return True
