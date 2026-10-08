from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from budget_bot.telegram_analytics import TelegramAnalytics
from budget_bot.telegram_entries import expense_report_keyboard


class FakeStorage:
    def __init__(self):
        self.owner = "a"
        self.preferences = {owner: {"exclude_mandatory": False, "mandatory_rules": [], "limits": {},
                                   "weekly_enabled": True, "weekly_chat_id": chat}
                            for owner, chat in [("a", 111), ("b", 222)]}
        self.deliveries = set()
        self.claims = set()

    def analytics_settings(self):
        return self.preferences[self.owner]

    def update_analytics_settings(self, **changes):
        self.preferences[self.owner].update(changes)

    def analytics_rows(self, start, end):
        return [{"operation_date": end, "operation_type": "expense", "category": "Еда", "subcategory": "Супермаркеты",
                 "is_mandatory": False, "total": Decimal("900"), "count": 1}]

    def set_analytics_limit(self, category, amount):
        self.preferences[self.owner]["limits"][category] = str(amount)

    def analytics_subscribers(self):
        return [{**pref, "owner_id": owner, "user_id": pref["weekly_chat_id"], "timezone": "Europe/Moscow"}
                for owner, pref in self.preferences.items()]

    def analytics_notification_sent(self, kind, start, key):
        return (self.owner, kind, start, key) in self.deliveries

    def claim_analytics_notification(self, kind, start, key):
        item = (self.owner, kind, start, key)
        if item in self.deliveries or item in self.claims:
            return False
        self.claims.add(item)
        return True

    def release_analytics_notification(self, kind, start, key):
        self.claims.discard((self.owner, kind, start, key))

    def mark_analytics_notification(self, kind, start, key):
        self.claims.discard((self.owner, kind, start, key))
        self.deliveries.add((self.owner, kind, start, key))


class FakeBot:
    def __init__(self):
        storage = FakeStorage()
        @contextmanager
        def scope(owner):
            previous = storage.owner
            storage.owner = owner
            try:
                yield
            finally:
                storage.owner = previous
        self.context = SimpleNamespace(storage=storage, settings=SimpleNamespace(default_timezone="Europe/Moscow"), owner_scope=scope)
        self.messages = []
        self.reports = []
        self._send_expense_report = lambda *args, **kwargs: self.reports.append(args)
        self._answer_callback = lambda *args: None
        self._is_allowed = lambda user: True
        self._category_items = lambda: [("Еда", ["Супермаркеты"]), ("Дом", ["Аренда"])]

    def _send_message(self, chat, text, reply_markup=None):
        self.messages.append((self.context.storage.owner, chat, text, reply_markup))
        return 1


def test_tabs_and_filter_keep_period_and_all_category_sentinel():
    bot = FakeBot()
    analytics = TelegramAnalytics(bot)
    callback = {"id": "cb", "from": {"id": 111}}
    analytics.handle_callback(callback, 111, "filter:2026-10-01:2026-10-08:all")
    assert bot.context.storage.analytics_settings()["exclude_mandatory"]
    assert bot.reports == [(111, date(2026, 10, 1), date(2026, 10, 8), None)]
    analytics.handle_callback(callback, 111, "compare:2026-09-01:2026-09-08")
    assert "01.09.2026–08.09.2026" in bot.messages[-1][2]
    buttons = [button for row in bot.messages[-1][3]["inline_keyboard"] for button in row]
    assert any(button["callback_data"] == "cf:2026-09-01:2026-09-08:all" for button in buttons)
    for category in [None, "Связь и сервисы", "Путешествия"]:
        keyboard = expense_report_keyboard(date(2026, 10, 1), date(2026, 10, 8), category)
        assert all(len(button["callback_data"].encode()) <= 64 for row in keyboard["inline_keyboard"] for button in row)


@pytest.mark.parametrize("text", ["/limit NaN", "/limit Infinity", "/limit -1", "/limit 0.001", "/limit Несуществующая | 500", "/limit 1e100"])
def test_invalid_limits_do_not_change_settings(text):
    bot = FakeBot()
    TelegramAnalytics(bot).handle_limit_text(111, text, 111)
    assert not bot.context.storage.analytics_settings()["limits"]
    assert "неотрицательную" in bot.messages[-1][2]


def test_limit_input_and_weekly_only_in_private_chat():
    bot = FakeBot()
    analytics = TelegramAnalytics(bot)
    analytics.handle_limit_text(111, "/limit еда | 1 200,50", 111)
    assert bot.context.storage.analytics_settings()["limits"] == {"Еда": "1200.50"}
    analytics.handle_callback({"id": "cb", "from": {"id": 111}}, -999, "weekly")
    assert bot.context.storage.analytics_settings()["weekly_chat_id"] == 111
    assert "личном чате" in bot.messages[-1][2]


def test_scheduler_uses_local_time_owner_scope_and_deduplicates(monkeypatch):
    bot = FakeBot()
    bot.context.storage.preferences["a"]["limits"] = {"": "1000"}
    analytics = TelegramAnalytics(bot)
    class Clock:
        @staticmethod
        def now(zone):
            # Monday 09:00 in Moscow, UTC still 06:00.
            return datetime(2026, 10, 5, 6, tzinfo=timezone.utc).astimezone(zone)
    monkeypatch.setattr("budget_bot.telegram_analytics.datetime", Clock)
    analytics.send_due_notifications()
    assert {(owner, chat) for owner, chat, _, _ in bot.messages} == {("a", 111), ("b", 222)}
    assert ("a", "weekly", date(2026, 9, 28), "digest") in bot.context.storage.deliveries
    assert ("b", "weekly", date(2026, 9, 28), "digest") in bot.context.storage.deliveries
    assert ("a", "limit", date(2026, 10, 1), ":80:False") in bot.context.storage.deliveries
    assert bot.context.storage.owner == "a"
    sent = len(bot.messages)
    analytics.last_notification_check = float("-inf")
    analytics.send_due_notifications()
    assert len(bot.messages) == sent


def test_failed_notification_is_not_marked_delivered(monkeypatch):
    bot = FakeBot()
    def fail(*args, **kwargs):
        raise RuntimeError("Telegram unavailable")
    bot._send_message = fail
    analytics = TelegramAnalytics(bot)
    class Clock:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 5, 9, tzinfo=zone)
    monkeypatch.setattr("budget_bot.telegram_analytics.datetime", Clock)
    analytics.send_due_notifications()
    assert not bot.context.storage.deliveries
    assert not bot.context.storage.claims
    assert bot.context.storage.owner == "a"


def test_one_failed_subscriber_does_not_block_other_owners(monkeypatch):
    bot = FakeBot()
    original = bot._send_message
    def send(chat, *args, **kwargs):
        if chat == 111:
            raise RuntimeError("Blocked by user")
        return original(chat, *args, **kwargs)
    bot._send_message = send
    class Clock:
        @staticmethod
        def now(zone):
            return datetime(2026, 10, 5, 9, tzinfo=zone)
    monkeypatch.setattr("budget_bot.telegram_analytics.datetime", Clock)
    TelegramAnalytics(bot).send_due_notifications()
    assert {owner for owner, _, _, _ in bot.messages} == {"b"}
    assert {owner for owner, _, _, _ in bot.context.storage.deliveries} == {"b"}


def test_category_comparison_and_filter_preserve_category():
    bot = FakeBot()
    analytics = TelegramAnalytics(bot)
    callback = {"id": "cb", "from": {"id": 111}}
    analytics.handle_callback(callback, 111, "compare:2026-10-01:2026-10-08:Дом")
    assert "Категория: Дом" in bot.messages[-1][2]
    assert "Сейчас: 0 ₽" in bot.messages[-1][2]
    analytics.handle_callback(callback, 111, "comparefilter:2026-10-01:2026-10-08:Дом")
    assert "Категория: Дом" in bot.messages[-1][2]
    assert bot.context.storage.analytics_settings()["exclude_mandatory"]


def test_bad_filter_callback_does_not_mutate_settings():
    bot = FakeBot()
    TelegramAnalytics(bot).handle_callback({"id": "cb"}, 111, "filter:bad:date")
    assert not bot.context.storage.analytics_settings()["exclude_mandatory"]
