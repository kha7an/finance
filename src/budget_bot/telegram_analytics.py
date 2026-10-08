from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .analytics import analytics_lines, comparison_lines, limit_status, money, period_metrics, previous_period, shift_month
from .telegram_common import button_rows
from .telegram_reports import parse_chart_period_payload, chart_period_payload
from .log_config import get_logger


logger = get_logger(__name__)


class TelegramAnalytics:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.last_notification_check = float("-inf")

    @property
    def storage(self):
        return self.bot.context.storage

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.bot.context.settings.default_timezone)).date()

    def navigation(self, tab: str = "compare", start: Optional[date] = None, end: Optional[date] = None, category: Optional[str] = None) -> Dict[str, Any]:
        settings = self.storage.analytics_settings()
        mode = "Без обязательных ✓" if settings["exclude_mandatory"] else "Все расходы ✓"
        filter_payload = f"analysis:filter:{tab}"
        if start is not None and end is not None:
            filter_payload = f"cf:{chart_period_payload(start, end, category)}"
        return {"inline_keyboard": [
            [{"text": "Расходы", "callback_data": "stats:month"},
             {"text": "Сравнение", "callback_data": "analysis:compare"}],
            [{"text": "Доходы и остаток", "callback_data": "analysis:balance"},
             {"text": "Динамика", "callback_data": "analysis:trends"}],
            [{"text": "Лимиты", "callback_data": "analysis:limits"},
             {"text": "Прогноз", "callback_data": "analysis:forecast"}],
            [{"text": mode, "callback_data": filter_payload},
             {"text": "Настройки", "callback_data": "analysis:settings"}],
            [{"text": "Диаграмма за месяц", "callback_data": "chart:month"}],
            [{"text": "Даты и категории", "callback_data": "analytics:menu"}],
        ]}

    def send_tab(self, chat_id: int, tab: str, start: Optional[date] = None, end: Optional[date] = None, category: Optional[str] = None) -> None:
        today = self.today()
        settings = self.storage.analytics_settings()
        if tab == "compare" and start is not None and end is not None:
            previous_start, _ = previous_period(start, end)
            rows = self.storage.analytics_rows(previous_start, end)
            if category:
                rows = [row for row in rows if row["category"] == category]
            lines = comparison_lines(rows, settings, start, end)
            if category:
                lines.insert(1, f"Категория: {category}")
        else:
            rows = self.storage.analytics_rows(shift_month(today.replace(day=1), -5), today)
            lines = analytics_lines(tab, rows, settings, today)
        self.bot._send_message(chat_id, "\n".join(lines), reply_markup=self.navigation(tab, start, end, category))

    def send_settings(self, chat_id: int) -> None:
        settings = self.storage.analytics_settings()
        weekly = "Недельная сводка: вкл." if settings["weekly_enabled"] else "Недельная сводка: выкл."
        rules = [f"• {category}" + (f" / {subcategory}" if subcategory else " — вся категория")
                 for category, subcategory in settings["mandatory_rules"]]
        self.bot._send_message(chat_id,
            "Настройки аналитики\nОбязательные расходы:\n" + ("\n".join(rules) if rules else "Пока не выбраны.") +
            "\nОтдельную запись можно отметить через Расходы → Записи.\n"
            "Недельная сводка: по понедельникам после 09:00 за прошлую неделю, в личный чат. "
            "При включении и после простоя приходит последняя завершённая неделя.",
            reply_markup={"inline_keyboard": [
                [{"text": "Выбрать обязательные", "callback_data": "analysis:mandatory"}],
                [{"text": weekly, "callback_data": "analysis:weekly"}],
                [{"text": "Назад", "callback_data": "analysis:compare"}],
            ]})

    def mandatory_picker(self, chat_id: int, category_index: Optional[int] = None) -> None:
        rules = self.storage.analytics_settings()["mandatory_rules"]
        items = self.bot._category_items()
        if category_index is None:
            buttons = [{"text": ("✓ " if [category, ""] in rules else "") + category,
                        "callback_data": f"analysis:mandatory:{index}"}
                       for index, (category, _) in enumerate(items)]
            rows = button_rows(buttons, columns=2)
            rows.append([{"text": "Готово", "callback_data": "analysis:settings"}])
        else:
            if not 0 <= category_index < len(items):
                self.bot._send_message(chat_id, "Категория устарела. Открой настройки заново.")
                return
            category, subcategories = items[category_index]
            buttons = [{"text": ("✓ " if [category, ""] in rules else "") + "Вся категория",
                        "callback_data": f"analysis:rule:{category_index}:-1"}]
            buttons.extend({"text": ("✓ " if [category, sub] in rules else "") + sub,
                            "callback_data": f"analysis:rule:{category_index}:{index}"}
                           for index, sub in enumerate(subcategories))
            rows = button_rows(buttons, columns=2)
            rows.append([{"text": "Назад", "callback_data": "analysis:mandatory"}])
        self.bot._send_message(chat_id, "Отметь обязательные расходы (повторное нажатие снимает отметку):",
                               reply_markup={"inline_keyboard": rows})

    def handle_callback(self, callback: Dict[str, Any], chat_id: int, payload: str) -> None:
        self.bot._answer_callback(callback["id"], "Готовлю")
        parts = payload.split(":")
        tab = parts[0]
        try:
            if tab == "comparefilter":
                period = parse_chart_period_payload(":".join(parts[1:]))
                if period is None or period[0] > period[1]:
                    raise ValueError("Invalid period")
                settings = self.storage.analytics_settings()
                self.storage.update_analytics_settings(exclude_mandatory=not settings["exclude_mandatory"])
                self.send_tab(chat_id, "compare", *period)
                return
            if tab == "filter":
                period = None
                if len(parts) in {3, 4}:
                    period = parse_chart_period_payload(":".join(parts[1:]))
                    if period is None or period[0] > period[1]:
                        raise ValueError("Invalid period")
                elif len(parts) != 1 and not (len(parts) == 2 and parts[1] in {"compare", "balance", "trends", "limits", "forecast"}):
                    raise ValueError("Invalid filter target")
                settings = self.storage.analytics_settings()
                self.storage.update_analytics_settings(exclude_mandatory=not settings["exclude_mandatory"])
                if len(parts) == 2 and parts[1] in {"compare", "balance", "trends", "limits", "forecast"}:
                    self.send_tab(chat_id, parts[1])
                elif period is not None:
                    self.bot._send_expense_report(chat_id, *period)
                else:
                    self.bot._send_expense_report(chat_id, self.today().replace(day=1), self.today())
                return
            if tab == "settings":
                self.send_settings(chat_id)
                return
            if tab == "weekly":
                if chat_id != int(callback["from"]["id"]):
                    self.bot._send_message(chat_id, "Включи сводку в личном чате с ботом.")
                    return
                enabled = not self.storage.analytics_settings()["weekly_enabled"]
                self.storage.update_analytics_settings(weekly_enabled=enabled, weekly_chat_id=chat_id)
                self.send_settings(chat_id)
                return
            if tab == "mandatory":
                self.mandatory_picker(chat_id, int(parts[1]) if len(parts) == 2 else None)
                return
            if tab == "rule":
                category_index, sub_index = int(parts[1]), int(parts[2])
                item = self.bot._category_by_index(category_index)
                if item is None or sub_index < -1 or sub_index >= len(item[1]):
                    raise ValueError("Stale category")
                self.storage.toggle_mandatory_rule(item[0], "" if sub_index == -1 else item[1][sub_index])
                self.mandatory_picker(chat_id, category_index)
                return
            if tab in {"compare", "balance", "trends", "limits", "forecast"}:
                if tab == "compare" and len(parts) in {3, 4}:
                    period = parse_chart_period_payload(":".join(parts[1:]))
                    if period is None or period[0] > period[1]:
                        raise ValueError("Invalid period")
                    self.send_tab(chat_id, tab, *period)
                else:
                    self.send_tab(chat_id, tab)
                return
        except (ValueError, IndexError):
            self.bot._send_message(chat_id, "Кнопка устарела. Открой аналитику заново.")
            return
        self.bot._send_message(chat_id, "Открой аналитику заново.")

    def handle_limit_text(self, chat_id: int, text: str, user_id: Optional[int]) -> None:
        raw = text.split(maxsplit=1)
        try:
            if len(raw) != 2:
                raise ValueError("Missing limit")
            if "|" in raw[1]:
                category, value = (part.strip() for part in raw[1].rsplit("|", 1))
                names = {name.casefold(): name for name, _ in self.bot._category_items()}
                category = names[category.casefold()]
            else:
                category, value = "", raw[1]
            cleaned = value.replace(" ", "").replace("\u00a0", "").replace(",", ".")
            amount = Decimal(cleaned)
            if not amount.is_finite() or amount < 0 or amount > Decimal("999999999999.99"):
                raise ValueError("Invalid limit")
            amount = amount.quantize(Decimal("0.01"))
            if amount == 0 and Decimal(cleaned) != 0:
                raise ValueError("Limit below one kopek")
            self.storage.set_analytics_limit(category, amount)
            if user_id is not None and chat_id == int(user_id):
                self.storage.update_analytics_settings(weekly_chat_id=chat_id)
        except (ValueError, InvalidOperation, KeyError):
            self.bot._send_message(chat_id, "Общий лимит: /limit 60000\nПо категории: /limit Еда | 15000\nСнять лимит: сумма 0. Используй существующую категорию и неотрицательную сумму.")
            return
        self.send_tab(chat_id, "limits")

    def send_due_notifications(self) -> None:
        now = time.monotonic()
        if now - self.last_notification_check < 60:
            return
        self.last_notification_check = now
        for subscriber in self.storage.analytics_subscribers():
            try:
                self._send_subscriber_notifications(subscriber)
            except Exception:
                logger.exception("analytics notification failed", extra={"owner_id": subscriber["owner_id"]})

    def _deliver(self, chat_id: int, text: str, kind: str, start: date, key: str, reply_markup=None) -> None:
        if not self.storage.claim_analytics_notification(kind, start, key):
            return
        try:
            self.bot._send_message(chat_id, text, reply_markup=reply_markup)
        except Exception:
            self.storage.release_analytics_notification(kind, start, key)
            raise
        # shortcut: Telegram has no idempotency key; a crash after sending can repeat delivery on lease expiry.
        self.storage.mark_analytics_notification(kind, start, key)

    def _send_subscriber_notifications(self, subscriber: Dict[str, Any]) -> None:
        chat_id = int(subscriber["weekly_chat_id"])
        if chat_id != int(subscriber["user_id"]) or not self.bot._is_allowed(subscriber["user_id"]):
            return
        local = datetime.now(ZoneInfo(subscriber["timezone"] or self.bot.context.settings.default_timezone))
        today = local.date()
        with self.bot.context.owner_scope(subscriber["owner_id"]):
            settings = self.storage.analytics_settings()
            if settings["weekly_enabled"] and local.hour >= 9:
                week_end = today - timedelta(days=today.weekday() + 1)
                week_start = week_end - timedelta(days=6)
                if not self.storage.analytics_notification_sent("weekly", week_start, "digest"):
                    rows = self.storage.analytics_rows(week_start - timedelta(days=7), week_end)
                    lines = ["Недельная сводка", *comparison_lines(rows, settings, week_start, week_end)]
                    monthly_rows = self.storage.analytics_rows(today.replace(day=1), today)
                    metrics = period_metrics(monthly_rows, settings, today.replace(day=1), today)
                    for item in limit_status(metrics, settings):
                        lines.append(f"Лимит {item['category'] or 'Общий'}: {item['percent']:.0f}%, осталось {money(item['remaining'])}")
                    self._deliver(chat_id, "\n".join(lines), "weekly", week_start, "digest", self.navigation())
            if settings["limits"]:
                start = today.replace(day=1)
                rows = self.storage.analytics_rows(start, today)
                metrics = period_metrics(rows, settings, start, today)
                for item in limit_status(metrics, settings):
                    threshold = item["threshold"]
                    if not threshold:
                        continue
                    key = f"{item['category']}:{threshold}:{settings['exclude_mandatory']}"
                    if self.storage.analytics_notification_sent("limit", start, key):
                        continue
                    text = (f"Лимит {item['category'] or 'Общий'}: достигнуто {threshold}%\n"
                            f"Потрачено {money(item['spent'])} из {money(item['limit'])}.\n" +
                            ("Без обязательных расходов." if settings["exclude_mandatory"] else "Все расходы."))
                    self._deliver(chat_id, text, "limit", start, key)
                    if threshold == 100:
                        self.storage.mark_analytics_notification("limit", start, f"{item['category']}:80:{settings['exclude_mandatory']}")
