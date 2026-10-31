"""Persisted receipt-purpose and reimbursement selection dialog."""

import json
import secrets
from datetime import date, timedelta

from .analytics import money
from .models import OperationType, ParsedOperation
from .planning import amount_decimal
from .storage import operation_from_json


class TelegramReceipts:
    def __init__(self, bot):
        self.bot, self.storage = bot, bot.context.storage

    def key(self, chat):
        return f"receipt-{chat}-{self.storage.actor_id}"

    def income_choices(self):
        return [
            (row["name"], f"category:{row['id']}")
            for row in self.storage.category_catalog()
            if row["kind"] == "income"
        ]

    def listing(self, chat, page=0):
        rows = self.storage.receipt_candidates(page)
        buttons = [
            [("#{id} · {operation_date} · {name} · {amount} ₽".format(**row), f"entry:{row['id']}")]
            for row in rows[:5]
        ]
        markup = [[{"text": label, "callback_data": action} for label, action in line] for line in buttons]
        if page:
            markup.append([{"text": "Назад", "callback_data": f"receipts:{page-1}"}])
        if len(rows) > 5:
            markup.append([{"text": "Далее", "callback_data": f"receipts:{page+1}"}])
        self.bot._send_message(
            chat,
            "Записанные поступления:" if rows else "Записанных поступлений пока нет.",
            reply_markup={"inline_keyboard": markup},
        )

    def save(self, chat, state):
        self.storage.add_pending_action(self.key(chat), chat, None, "receipt:" + json.dumps(state, ensure_ascii=False))

    def state(self, chat):
        row = self.storage.get_pending_action(self.key(chat), chat)
        return json.loads(row["prompt"][8:]) if row else None

    def send(self, chat, state, text, buttons):
        self.save(chat, state)
        rows = [[{"text": label, "callback_data": f"receipt:{state['nonce']}:{action}"}] for label, action in buttons]
        self.bot._send_message(chat, text, reply_markup={"inline_keyboard": rows})

    def start(self, chat, operation=None, source_hash=None, entry_id=None, purchase_id=None, initial_purpose=None):
        state = {"nonce": secrets.token_hex(4), "key": "receipt:" + secrets.token_hex(16), "stage": "purpose"}
        if source_hash:
            row = self.storage.get_operation(source_hash)
            if not row or row["status"] != "pending":
                raise ValueError("Поступление уже обработано.")
            operation = operation_from_json(row["operation_json"])
            with self.storage._connect() as connection:
                self.storage._require_edit(connection, row)
            if operation.amount <= 0 or operation.date_missing or operation.occurrence_count != 1:
                raise ValueError("Уточни дату и количество поступлений перед выбором назначения.")
            state.update(source_hash=source_hash, bank=row["bank"], key=source_hash)
        if entry_id:
            row = self.storage.get_budget_entry(entry_id)
            if not row or row["operation_type"] not in {"income", "reimbursement", "transfer"}:
                raise ValueError("Поступление не найдено.")
            operation = ParsedOperation(
                date.fromisoformat(row["operation_date"]), row["name"], row["amount"], OperationType.INCOME
            )
            state["entry_id"] = entry_id
        if operation:
            state.update(
                date=operation.date.isoformat(), amount=str(amount_decimal(abs(operation.amount))), name=operation.name
            )
        if source_hash:
            duplicates = self.storage.receipt_duplicates(operation)
            if duplicates:
                self.send(
                    chat,
                    state,
                    "Есть записанные поступления с этой датой и суммой. Это тот же перевод? Выбери запись или продолжи как новое поступление.",
                    [(f"#{r['id']} · {r['name']} · {r['amount']} ₽", f"reuse:{r['id']}") for r in duplicates]
                    + [("Это новое поступление", "purpose"), ("Отмена", "cancel")],
                )
                return
        if purchase_id:
            purchase = self.storage.get_budget_entry(purchase_id)
            if not purchase or purchase["operation_type"] != "expense":
                raise ValueError("Покупка не найдена.")
            state.update(purchase_id=purchase_id, stage="new_or_existing")
            self.send(
                chat,
                state,
                "Добавить возврат к покупке: " + purchase["name"],
                [
                    ("Ввести новое поступление", "new"),
                    ("Выбрать записанное поступление", "existing:0"),
                    ("Отмена", "cancel"),
                ],
            )
        elif initial_purpose == "reimbursement":
            self.purchases(chat, state)
        elif initial_purpose == "income":
            self.send(
                chat,
                state,
                "Категория дохода:",
                self.income_choices() + [("Отмена", "cancel")],
            )
        elif initial_purpose == "transfer":
            state.update(purpose="transfer", stage="confirm")
            self.send(
                chat,
                state,
                f"Подтвердить перевод между своими: {state['amount']} ₽ · {state['date']}",
                [("Подтвердить", "confirm"), ("Назначение", "purpose"), ("Отмена", "cancel")],
            )
        else:
            self.purpose(chat, state)

    def purpose(self, chat, state):
        state["stage"] = "purpose"
        self.send(
            chat,
            state,
            f"Поступление: {state.get('amount', '')} ₽ · {state.get('date', '')} · {state.get('name', '')}\nВыбери назначение:",
            [
                ("Считать доходом", "income"),
                ("Отнять от траты", "purchases:0"),
                ("Перевод между своими", "transfer"),
                (
                    "Пропустить" if state.get("source_hash") else "Отмена",
                    "skip" if state.get("source_hash") else "cancel",
                ),
            ],
        )

    def purchases(self, chat, state, page=0):
        today = self.bot._analytics().today()
        start = date.fromisoformat(state.get("start", (today - timedelta(days=89)).isoformat()))
        end = date.fromisoformat(state.get("end", today.isoformat()))
        rows = self.storage.refund_candidates(start, end, state.get("query", ""), page)
        buttons = []
        lines = [f"Выбери трату · {start:%d.%m.%Y}–{end:%d.%m.%Y}"]
        for row in rows[:5]:
            lines.append(
                f"{row['operation_date']:%d.%m} · {row['name']} · {row['category']} / {row['subcategory']} · оплачено {money(row['amount'])} · осталось {money(row['personal_amount'])}"
            )
            buttons.append((f"#{row['id']} {row['name'][:24]}", f"pick:{row['id']}"))
        if not rows:
            lines.append("За выбранный период подходящих трат нет. Можно изменить период или открыть всю историю.")
        if page:
            buttons.append(("Назад", f"purchases:{page-1}"))
        if len(rows) > 5:
            buttons.append(("Далее", f"purchases:{page+1}"))
        buttons += [
            ("Поиск по названию", "search"),
            ("Выбрать период", "period"),
            ("Вся история", "all"),
            ("Изменить дату поступления", "date"),
            ("Назначение", "purpose"),
            ("Отмена", "cancel"),
        ]
        state["stage"] = "purchases"
        self.send(chat, state, "\n".join(lines), buttons)

    def preview(self, chat, state, purchase_id):
        row = self.storage.get_budget_entry(purchase_id)
        if not row or row["operation_type"] != "expense":
            raise ValueError("Покупка не найдена.")
        value = amount_decimal(state["amount"])
        paid = amount_decimal(row["amount"])
        old = self.storage.get_budget_entry(state["entry_id"]) if state.get("entry_id") else None
        returned = row["returned_amount"] - (
            amount_decimal(old["amount"]) if old and old.get("expense_entry_id") == purchase_id else 0
        )
        if value + returned > paid:
            raise ValueError(f"Можно возместить ещё {money(paid-returned)}. Выбери другую трату.")
        if date.fromisoformat(state["date"]) < date.fromisoformat(row["operation_date"]):
            raise ValueError("Дата поступления раньше покупки. Исправь дату поступления.")
        state.update(purchase_id=purchase_id, stage="confirm", purpose="reimbursement")
        text = (
            f"Отнять поступление от траты?\nПоступление: {state['amount']} ₽ · {state['date']} · {state['name']}\n"
            f"Трата: {row['name']} · {row['operation_date']} · {row['category']} / {row['subcategory']}\n"
            f"Оплачено: {money(paid)}\nУже вернули: {money(returned)}\nЭтот возврат: {money(value)}\nВаш расход после возврата: {money(paid-returned-value)}"
        )
        self.send(
            chat,
            state,
            text,
            [("Подтвердить", "confirm"), ("Выбрать другую трату", "purchases:0"), ("Отмена", "cancel")],
        )

    def callback(self, callback, chat, payload):
        parts = payload.split(":")
        state = self.state(chat)
        self.bot._answer_callback(callback["id"], "Поступление")
        if not state or parts[0] != state["nonce"]:
            self.bot._send_message(chat, "Этот диалог уже закрыт. Открой поступление заново.")
            return
        action = parts[1]
        if action == "reuse":
            self.storage.reuse_receipt_source(state["source_hash"], int(parts[2]))
            self.storage.delete_pending_action(self.key(chat))
            self.bot._send_message(chat, "Источник связан с существующим поступлением. Второй записи нет.")
        elif action == "skip":
            from .models import OperationStatus

            self.storage.update_operation_status(
                state["source_hash"], OperationStatus.IGNORED, status_note="skipped receipt"
            )
            self.storage.delete_pending_action(state["source_hash"])
            self.storage.delete_pending_action(self.key(chat))
            self.bot._send_message(chat, "Поступление пропущено.")
        elif action == "cancel":
            self.storage.delete_pending_action(self.key(chat))
            self.bot._send_message(chat, "Выбор отменён. Учёт не изменён.")
        elif action == "purpose":
            self.purpose(chat, state)
        elif action == "new":
            state["stage"] = "amount"
            state["date"] = self.bot._analytics().today().isoformat()
            self.send(chat, state, "Сумма нового возврата (дата — сегодня):", [("Отмена", "cancel")])
        elif action == "existing":
            page = int(parts[2])
            rows = self.storage.receipt_candidates(page)
            self.send(
                chat,
                state,
                "Выбери поступление:" if rows else "Записанных поступлений нет.",
                [
                    (f"#{row['id']} {row['operation_date']} · {row['name']} · {row['amount']} ₽", f"use:{row['id']}")
                    for row in rows[:5]
                ]
                + ([("Далее", f"existing:{page+1}")] if len(rows) > 5 else [])
                + [("Новое поступление", "new"), ("Отмена", "cancel")],
            )
        elif action == "use":
            row = self.storage.get_budget_entry(int(parts[2]))
            if not row or row["operation_type"] not in {"income", "reimbursement", "transfer"}:
                raise ValueError("Поступление не найдено.")
            state.update(entry_id=row["id"], date=row["operation_date"], amount=str(row["amount"]), name=row["name"])
            self.preview(chat, state, state["purchase_id"])
        elif action == "purchases":
            self.purchases(chat, state, int(parts[2]))
        elif action == "all":
            state.update(start=date.min.isoformat(), end=date.max.isoformat(), query="")
            self.purchases(chat, state)
        elif action in {"search", "period", "date"}:
            state["stage"] = action
            self.send(
                chat,
                state,
                {
                    "search": "Название покупки:",
                    "period": "Период: ГГГГ-ММ-ДД ГГГГ-ММ-ДД",
                    "date": "Дата поступления: ГГГГ-ММ-ДД",
                }[action],
                [("Отмена", "cancel")],
            )
        elif action == "pick":
            self.preview(chat, state, int(parts[2]))
        elif action == "income":
            self.send(
                chat,
                state,
                "Категория дохода:",
                self.income_choices() + [("Отмена", "cancel")],
            )
        elif action in {"category", "transfer"}:
            state.update(purpose="income" if action == "category" else "transfer", stage="confirm")
            if action == "category":
                row = next(
                    (r for r in self.storage.category_catalog() if r["id"] == int(parts[2]) and r["kind"] == "income"),
                    None,
                )
                if row is None:
                    raise ValueError("Категория дохода изменилась. Открой назначение заново.")
                state["category"] = row["name"]
            self.send(
                chat,
                state,
                f"Подтвердить назначение: {'доход · '+state['category'] if action=='category' else 'перевод между своими'}\n{state['amount']} ₽ · {state['date']}",
                [("Подтвердить", "confirm"), ("Назад", "purpose"), ("Отмена", "cancel")],
            )
        elif action == "confirm" and state["stage"] == "confirm":
            if state.get("entry_id"):
                self.storage.assign_receipt(
                    state["entry_id"], state["purpose"], state.get("purchase_id"), state.get("category")
                )
                entry_id = state["entry_id"]
            else:
                operation = ParsedOperation(
                    date.fromisoformat(state["date"]),
                    state["name"],
                    amount_decimal(state["amount"]),
                    OperationType.INCOME,
                )
                entry_id = self.storage.confirm_receipt(
                    operation,
                    state["key"],
                    state["purpose"],
                    state.get("purchase_id"),
                    state.get("category"),
                    state.get("bank", "manual"),
                    state.get("source_hash"),
                )
            self.storage.delete_pending_action(self.key(chat))
            self.bot._send_message(
                chat,
                "Назначение сохранено: "
                + {"income": "доход", "transfer": "перевод между своими", "reimbursement": "возмещение"}[
                    state["purpose"]
                ]
                + f" · {state['amount']} ₽",
            )
            self.bot._entry_editor().send_entry_actions(
                chat,
                self.storage.get_budget_entry(
                    state.get("purchase_id") if state["purpose"] == "reimbursement" else entry_id
                ),
            )

    def text(self, chat, text, pending):
        if not str(pending.get("prompt", "")).startswith("receipt:") or pending["operation_hash"] != self.key(chat):
            return False
        state = self.state(chat)
        if text.casefold() in {"отмена", "/cancel"}:
            self.storage.delete_pending_action(self.key(chat))
            self.bot._send_message(chat, "Выбор отменён. Учёт не изменён.")
            return True
        stage = state["stage"]
        if stage == "amount":
            state.update(amount=str(amount_decimal(text)), name="Ручной возврат")
            self.preview(chat, state, state["purchase_id"])
        elif stage == "search":
            state["query"] = text.strip()
            self.purchases(chat, state)
        elif stage == "period":
            start, end = map(date.fromisoformat, text.split())
            if start > end:
                raise ValueError("Конец периода раньше начала.")
            state.update(start=start.isoformat(), end=end.isoformat())
            self.purchases(chat, state)
        elif stage == "date":
            if state.get("entry_id"):
                raise ValueError(
                    "Для записанного поступления измени дату в карточке записи, затем открой назначение заново."
                )
            state["date"] = date.fromisoformat(text).isoformat()
            self.purchases(chat, state)
        else:
            self.bot._send_message(chat, "Выбери действие кнопкой или напиши «Отмена».")
        return True
