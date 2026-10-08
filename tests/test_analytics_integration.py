import os
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest

from budget_bot.models import OperationType, ParsedOperation
from budget_bot.storage import Storage

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL required")


def test_analytics_settings_filters_and_notifications_are_owner_scoped():
    storage = Storage(os.environ["DATABASE_URL"])
    owners = [f"analytics-test:{uuid4()}" for _ in range(2)]
    start, end = date(2026, 10, 1), date(2026, 10, 8)
    try:
        with storage.owner_scope(owners[0]):
            rent = storage.append_budget_entry("test", None, ParsedOperation(end, "Rent", -1000, OperationType.EXPENSE, "Дом", "Аренда"), "test")
            food = storage.append_budget_entry("test", None, ParsedOperation(end, "Food", -400, OperationType.EXPENSE, "Еда", "Супермаркеты"), "test")
            unclassified = storage.append_budget_entry("test", None, ParsedOperation(end, "Unknown", -25, OperationType.EXPENSE), "test")
            storage.append_budget_entry("test", None, ParsedOperation(date(2026, 9, 8), "Old food", -200, OperationType.EXPENSE, "Еда", "Супермаркеты"), "test")
            storage.append_budget_entry("test", None, ParsedOperation(end, "Salary", 2000, OperationType.INCOME, "Зарплата"), "test")
            storage.toggle_mandatory_rule("Дом", "Аренда")
            storage.update_analytics_settings(exclude_mandatory=True)
            storage.set_analytics_limit("Еда", Decimal("500"))
            summary = storage.expense_summary(start, end)
            assert summary["total"] == 425
            assert summary["excluded_total"] == 1000
            assert summary["previous_total"] == 200
            assert summary["previous_start_date"] == date(2026, 9, 1)
            assert storage.expense_daily_totals(start, end)[0]["total"] == 425
            assert storage.toggle_mandatory_entry(food)
            assert storage.get_budget_entry(food)["is_mandatory"]
            assert storage.expense_summary(start, end)["total"] == 25
            assert len(storage.analytics_rows(start, end)) == 4
            assert storage.analytics_settings()["limits"] == {"Еда": "500.00"}
            def claim():
                with storage.owner_scope(owners[0]):
                    return storage.claim_analytics_notification("weekly", start, "claim")
            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]
            assert not storage.analytics_notification_sent("weekly", start, "claim")
            storage.release_analytics_notification("weekly", start, "claim")
            assert storage.claim_analytics_notification("weekly", start, "claim")
            with storage._connect() as connection:
                connection.execute("""UPDATE analytics_deliveries SET sent_at = CURRENT_TIMESTAMP - INTERVAL '6 minutes'
                                      WHERE owner_id = %s AND delivery_key = 'claim'""", (owners[0],))
            assert storage.claim_analytics_notification("weekly", start, "claim")
            storage.mark_analytics_notification("weekly", start, "claim")
            assert not storage.claim_analytics_notification("weekly", start, "claim")
            storage.mark_analytics_notification("weekly", start, "digest")
            assert storage.analytics_notification_sent("weekly", start, "digest")
        with storage.owner_scope(owners[1]):
            assert not storage.toggle_mandatory_entry(food)
            assert storage.get_budget_entry(rent) is None
            assert not storage.analytics_settings()["exclude_mandatory"]
            assert not storage.analytics_notification_sent("weekly", start, "digest")
            assert not storage.analytics_rows(start, end)
            assert storage.expense_summary(start, end)["total"] == 0
            storage.toggle_mandatory_rule("Дом")
            storage.set_analytics_limit("", Decimal("60000"))
        with storage.owner_scope(owners[0]):
            assert storage.analytics_settings()["mandatory_rules"] == [["Дом", "Аренда"]]
            storage.set_analytics_limit("Еда", Decimal(0))
            assert storage.analytics_settings()["limits"] == {}
            storage.toggle_mandatory_rule("Дом", "Аренда")
            assert storage.expense_summary(start, end)["total"] == 1025
            assert storage.toggle_mandatory_entry(unclassified)
            storage.reset_all()
            assert not storage.analytics_notification_sent("weekly", start, "digest")
            assert storage.analytics_settings()["exclude_mandatory"]
    finally:
        with storage._connect() as connection:
            connection.execute("DELETE FROM users WHERE owner_id = ANY(%s)", (owners,))
        storage.close()
