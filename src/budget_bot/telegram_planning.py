"""Telegram planning and shared-budget controls, backed by persistent state."""

import json
from decimal import Decimal
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .analytics import money
from .planning import amount_decimal

STATUS_LABELS={'pending':'ожидает решения','recorded':'записан','skipped':'пропущен'}


class TelegramPlanning:
    def __init__(self, bot):
        self.bot, self.storage = bot, bot.context.storage

    def prompt(self, chat, state, text):
        state["actor"] = self.storage.actor_id
        self.storage.add_pending_action(
            f"planning-{chat}-{self.storage.actor_id}", chat, None, "planning:" + json.dumps(state, ensure_ascii=False)
        )
        self.bot._send_message(chat, text + "\nОтмена: /cancel")

    def show(self, chat):
        info = self.storage.planning_summary(self.bot._analytics().today())
        lines = [
            f"Планирование · {info['start']:%d.%m.%Y}–{info['end']:%d.%m.%Y}",
            f"Начальная сумма: {money(info['opening'])}",
            f"Доходы: {money(info['income'])}",
            f"Исходные оплаты: {money(info['paid'])}",
            f"Возмещения получено: {money(info['reimbursements'])}",
            f"Личные расходы: {money(info['all_expenses'])}",
            f"Резерв обязательных платежей: {money(info['reserve'])}",
            f"Доступно: {money(info['available'])}",
            f"На день: {money(info['daily'])} · осталось дней: {info['days_left']}",
        ]
        if info["available"] < 0:
            lines.append(f"Дефицит: {money(-info['available'])}")
        upcoming = info["upcoming"]
        lines.append(
            f"Предстоящие и просроченные расходы: {money(sum((r['amount'] for r in upcoming if r['operation_type']=='expense'),Decimal(0)))}"
        )
        lines.append(
            "По записанным операциям. Начальная сумма не является доходом. Прогноз по темпу расходов — отдельная оценка."
        )
        self.bot._send_message(
            chat,
            "\n".join(lines),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Платежи", "callback_data": "plan:list:0"},
                        {"text": "Шаблоны", "callback_data": "plan:templates"},
                    ],
                    [{"text": "История платежей", "callback_data": "plan:history:0"}],
                    [
                        {"text": "Новый шаблон", "callback_data": "plan:new"},
                        {"text": "Период и начальная сумма", "callback_data": "plan:period"},
                    ],
                ]
            },
        )

    def payments(self, chat, page=0, history=False):
        today = self.bot._analytics().today()
        rows = self.storage.recurring_instances(today + timedelta(days=62), history)
        if history:
            rows = [r for r in rows if r["status"] != "pending"]
        lines = [
            (
                "История платежей:"
                if history
                else "Платежи: просроченные и ближайшие 62 дня. Проведение только после подтверждения."
            )
        ]
        buttons = []
        for row in rows[page * 5 : page * 5 + 5]:
            lines.append(
                f"#{row['id']} · {row['due_date']:%d.%m.%Y} · {row['name']} · {money(row['amount'])}"
                + (" · " + STATUS_LABELS[row["status"]] if history else " · просрочен" if row["due_date"] < today else "")
            )
            buttons.append([{"text": row["name"], "callback_data": f"plan:payment:{row['id']}"}])
        if not rows:
            lines.append("Предстоящих записанных платежей нет.")
        if page:
            buttons.append([{"text": "Назад", "callback_data": f"plan:{'history' if history else 'list'}:{page-1}"}])
        if len(rows) > (page + 1) * 5:
            buttons.append([{"text": "Далее", "callback_data": f"plan:{'history' if history else 'list'}:{page+1}"}])
        self.bot._send_message(chat, "\n".join(lines), reply_markup={"inline_keyboard": buttons})

    def callback(self, callback, chat, payload):
        self.bot._answer_callback(callback["id"], "Планирование")
        parts = payload.split(":")
        action = parts[0]
        if action == "home":
            self.show(chat)
        elif action == "period":
            self.prompt(
                chat,
                {"stage": "period"},
                "День начала периода (1–31) и начальная доступная сумма для текущего периода через пробел.\nНапример: 15 25000\nДля календарного месяца: 1 25000",
            )
        elif action == "new":
            self.prompt(chat, {"stage": "template", "step": 0, "values": []}, "Название регулярного платежа:")
        elif action in {"list", "history"}:
            self.payments(chat, int(parts[1]), action == "history")
        elif action == "templates":
            rows = self.storage.recurring_templates()
            self.bot._send_message(
                chat,
                "Регулярные шаблоны:" if rows else "Шаблонов пока нет.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": f"#{r['id']} {r['name']} · {money(r['amount'])}"
                                + (" · пауза" if r["paused"] else ""),
                                "callback_data": f"plan:template:{r['id']}",
                            }
                        ]
                        for r in rows
                    ]
                },
            )
        elif action == "template":
            row = next((r for r in self.storage.recurring_templates() if r["id"] == int(parts[1])), None)
            if not row:
                raise ValueError("Шаблон не найден.")
            self.bot._send_message(
                chat,
                f"{row['name']} · {money(row['amount'])}\n{row['starts_on']} · {'еженедельно' if row['frequency']=='week' else 'ежемесячно'} · {row['reminder_time']} ({row['timezone']})\nИстория проведённых платежей сохраняется.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Возобновить" if row["paused"] else "Пауза",
                                "callback_data": f"plan:{'resume' if row['paused'] else 'pause'}:{row['id']}",
                            }
                        ],
                        [{"text": "Изменить будущую сумму", "callback_data": f"plan:tamount:{row['id']}"}],
                        [{"text": "Изменить расписание", "callback_data": f"plan:edit:{row['id']}"}],
                        [{"text": "Удалить шаблон", "callback_data": f"plan:deleteask:{row['id']}"}],
                    ]
                },
            )
        elif action in {"pause", "resume", "delete"}:
            self.storage.edit_recurring(int(parts[1]), action)
            self.bot._send_message(chat, "Шаблон обновлён.")
        elif action == "deleteask":
            self.bot._send_message(
                chat,
                "Удалить шаблон? Проведённая история останется.",
                reply_markup={
                    "inline_keyboard": [[{"text": "Да, удалить", "callback_data": f"plan:delete:{parts[1]}"}]]
                },
            )
        elif action == "edit":
            self.prompt(
                chat,
                {"stage": "schedule", "id": int(parts[1])},
                "Новые дата начала, периодичность, время и дата окончания (или -):\n2026-11-15 месяц 09:00 -\nИзменятся только будущие экземпляры.",
            )
        elif action in {"tamount", "amount", "link"}:
            self.prompt(
                chat,
                {"stage": action, "id": int(parts[1])},
                {
                    "tamount": "Новая сумма будущих платежей:",
                    "amount": "Сумма для этого платежа:",
                    "link": "ID уже записанной банковской операции. Будет связана существующая запись; второй операции не будет.",
                }[action],
            )
        elif action == "payment":
            row = next((r for r in self.storage.recurring_instances(date.max, True) if r["id"] == int(parts[1])), None)
            if not row:
                raise ValueError("Платёж не найден.")
            self.bot._send_message(
                chat,
                f"{row['name']} · {row['due_date']} · {money(row['amount'])}\n{row['category']} / {row['subcategory'] or ''} · {STATUS_LABELS[row['status']]}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "Записать", "callback_data": f"plan:record:{row['id']}"},
                            {"text": "Изменить сумму", "callback_data": f"plan:amount:{row['id']}"},
                        ],
                        [{"text": "Связать с записанной операцией", "callback_data": f"plan:link:{row['id']}"}],
                        [{"text": "Пропустить этот платёж", "callback_data": f"plan:skip:{row['id']}"}],
                    ]
                },
            )
        elif action in {"record", "skip"}:
            result = self.storage.confirm_recurring(int(parts[1]), action)
            self.bot._send_message(chat, "Платёж записан." if result else "Платёж пропущен.")
        elif action == "linkok":
            pending = self.storage.get_pending_action(f"planning-{chat}-{self.storage.actor_id}", chat)
            state = json.loads(pending["prompt"][9:]) if pending else {}
            if (
                state.get("stage") != "linkconfirm"
                or state.get("actor") != self.storage.actor_id
                or state.get("id") != int(parts[1])
                or state.get("entry_id") != int(parts[2])
            ):
                raise ValueError("Подтверждение сопоставления устарело.")
            self.storage.confirm_recurring(state["id"], entry_id=state["entry_id"], confirmed_match=True)
            self.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat, "Платёж связан с существующей операцией. Второй записи нет.")

    def text(self, chat, text, pending):
        if not str(pending.get("prompt", "")).startswith("planning:"):
            return False
        state = json.loads(pending["prompt"][9:])
        if state.get("actor") != self.storage.actor_id:
            return False
        if text.casefold() in {"отмена", "/cancel"}:
            self.storage.delete_pending_action(pending["operation_hash"])
            self.bot._send_message(chat, "Планирование отменено.")
            return True
        stage = state["stage"]
        if stage == "period":
            day, opening = text.split(maxsplit=1)
            self.storage.configure_period(int(day), opening, self.bot._analytics().today())
        elif stage == "link":
            entry = self.storage.get_budget_entry(int(text))
            instance = next(
                (r for r in self.storage.recurring_instances(date.max, True) if r["id"] == state["id"]), None
            )
            if not entry or not instance:
                raise ValueError("Запись или платёж не найдены.")
            state.update(stage="linkconfirm", entry_id=entry["id"])
            self.prompt(
                chat,
                state,
                f"Сопоставить платёж {instance['name']} · {instance['due_date']} · {money(instance['amount'])}\nс записью #{entry['id']} {entry['name']} · {entry['operation_date']} · {entry['amount']:.2f} ₽?\nФактическая дата и сумма записи сохранятся.",
            )
            self.bot._send_message(
                chat,
                "Подтверди сопоставление:",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Подтвердить связь", "callback_data": f"plan:linkok:{instance['id']}:{entry['id']}"}]
                    ]
                },
            )
            return True
        elif stage in {"tamount", "amount"}:
            if stage == "tamount":
                self.storage.edit_recurring(state["id"], "amount", text)
            else:
                self.storage.confirm_recurring(
                    state["id"],
                    amount=amount_decimal(text) if stage == "amount" else None,
                    entry_id=int(text) if stage == "link" else None,
                )
        elif stage == "schedule":
            start, frequency, clock, end = text.split()
            self.storage.edit_recurring_schedule(
                state["id"],
                date.fromisoformat(start),
                {"месяц": "month", "неделя": "week"}.get(frequency, frequency),
                clock,
                None if end == "-" else date.fromisoformat(end),
                self.bot._analytics().today(),
            )
        elif stage == "template":
            step = state["step"]
            values = state["values"]
            prompts = [
                "Тип: расход или доход",
                "Сумма:",
                "Категория / Подкатегория (для дохода только категория):",
                "Дата начала: ГГГГ-ММ-ДД",
                "Периодичность: неделя или месяц",
                "Время напоминания: ЧЧ:ММ",
                "Дата окончания: ГГГГ-ММ-ДД или -",
            ]
            values.append(text)
            if step < len(prompts):
                state.update(step=step + 1, values=values)
                self.prompt(chat, state, prompts[step])
                return True
            name, kind, amount, pair, start, frequency, clock, end = values
            kind = {"расход": "expense", "доход": "income"}.get(kind.casefold(), kind)
            category, _, sub = pair.partition("/")
            self.storage.create_recurring(
                name,
                amount,
                kind,
                category.strip(),
                sub.strip() or None,
                date.fromisoformat(start),
                {"неделя": "week", "месяц": "month"}.get(frequency.casefold(), frequency),
                clock,
                self.bot.context.settings.default_timezone,
                chat,
                None if end == "-" else date.fromisoformat(end),
            )
        else:
            return False
        self.storage.delete_pending_action(pending["operation_hash"])
        self.show(chat)
        return True

    def budgets(self, chat):
        rows = self.storage.list_budgets(self.storage.actor_id)
        self.bot._send_message(
            chat,
            "Бюджеты. Текущий: "
            + self.storage.owner_id
            + "\n/newbudget Название — создать отдельный совместный\n/invite — приглашение\n/join КОД — принять приглашение\n/members — участники\n/leave — выйти",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": r["name"] + (" ✓" if r["id"] == self.storage.owner_id else ""),
                            "callback_data": f"budget:{r['id']}",
                        }
                    ]
                    for r in rows
                ]
            },
        )

    def command(self, chat, text):
        command, _, arg = text.partition(" ")
        if command == "/receipts" or text == "Поступления":
            from .telegram_receipts import TelegramReceipts

            TelegramReceipts(self.bot).listing(chat)
        elif command == "/web":
            if chat != int(self.storage.actor_id.split(":")[1]):
                raise ValueError("Открой /web в личном чате с ботом.")
            url = self.bot.context.settings.web_base_url
            if not url:
                raise ValueError("Для панели укажи BUDGET_WEB_URL — адрес /dashboard на сервере API.")
            token = self.storage.issue_web_token(self.storage.actor_id)
            self.bot._send_message(
                chat,
                "Вход в аналитику (ссылка действует 5 минут и используется один раз):\n"
                + url.rstrip("/")
                + "#login="
                + token,
            )
        elif command == "/plan" or text == "Планирование":
            self.show(chat)
        elif command == "/budgets" or text == "Бюджеты":
            self.budgets(chat)
        elif command == "/newbudget":
            budget = self.storage.create_shared_budget(arg)
            self.storage.select_budget(budget)
            self.bot._send_message(
                chat, "Совместный бюджет создан и выбран. Личная история сохранена отдельно. /budgets"
            )
        elif command == "/invite":
            self.bot._send_message(
                chat,
                "Передай участнику команду. Он должен принять приглашение сам:\n/join " + self.storage.invite_member(),
            )
        elif command == "/join":
            budget = self.storage.accept_invite(arg.strip())
            self.bot._send_message(chat, "Приглашение принято. Выбери бюджет: /budgets")
        elif command == "/members":
            rows = self.storage.budget_members()
            self.bot._send_message(
                chat,
                "Участники:\n"
                + "\n".join(f"{r['user_id']} · {'владелец' if r['role']=='owner' else 'участник'}" for r in rows)
                + "\nВладелец: /remove telegram:ID",
            )
        elif command in {"/remove", "/leave"}:
            self.storage.remove_member(self.storage.actor_id if command == "/leave" else arg.strip())
            self.bot._send_message(chat, "Доступ прекращён. История операций сохранена.")
        else:
            return False
        return True

    def due(self):
        # One list after downtime, rather than a separate reminder for every missed date.
        for subscriber in self.storage.recurring_subscribers():
            if not self.bot._is_allowed(subscriber["user_id"]):
                continue
            local = datetime.now(ZoneInfo(subscriber["timezone"]))
            with self.bot.context.owner_scope(subscriber["owner_id"]):
                rows = self.storage.recurring_instances(local.date())
                due = [
                    r
                    for r in rows
                    if not r["notified"]
                    and r["chat_id"] == subscriber["chat_id"]
                    and (r["due_date"] < local.date() or local.time().replace(tzinfo=None) >= r["reminder_time"])
                ]
                if not due:
                    continue
                claimed = self.storage.claim_recurring_notifications([r["id"] for r in due])
                if not claimed:
                    continue
                current = next((row for row in due if row["id"] in claimed), None)
                if len(claimed) == 1 and current["due_date"] == local.date():
                    notification = f"{current['name']} · {current['due_date']} · {money(current['amount'])}\nЗаписать этот платёж?"
                    keyboard = [
                        [
                            {"text": "Записать", "callback_data": f"pay:{current['id']}:record"},
                            {"text": "Изменить сумму", "callback_data": f"pay:{current['id']}:amount"},
                        ],
                        [{"text": "Пропустить этот платёж", "callback_data": f"pay:{current['id']}:skip"}],
                    ]
                else:
                    notification = f"Ожидают решения платежи: {len(claimed)}. Автоматически ничего не проведено."
                    keyboard = [[{"text": "Открыть платежи", "callback_data": f"planbudget:{subscriber['owner_id']}"}]]
                try:
                    self.bot._send_message(
                        subscriber["chat_id"],
                        notification,
                        reply_markup={"inline_keyboard": keyboard},
                    )
                    # Telegram has no delivery idempotency key; a crash after send can repeat this list once.
                    self.storage.finish_recurring_notifications(claimed)
                except Exception:
                    self.storage.release_recurring_notifications(claimed)
                    raise
