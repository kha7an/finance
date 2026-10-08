from datetime import date
from decimal import Decimal

import pytest

from budget_bot.analytics import analytics_lines, comparison_lines, limit_status, period_metrics, previous_period


def settings(exclude=False):
    return {"exclude_mandatory": exclude, "mandatory_rules": [["Дом", "Аренда"]], "limits": {"": "1000", "Еда": "500"}}


def row(day, amount, category="Еда", subcategory="Супермаркеты", kind="expense", mandatory=False):
    return {"operation_date": day, "total": Decimal(str(amount)), "category": category,
            "subcategory": subcategory, "operation_type": kind, "is_mandatory": mandatory, "count": 1}


@pytest.mark.parametrize("start,end,expected", [
    (date(2026, 10, 1), date(2026, 10, 8), (date(2026, 9, 1), date(2026, 9, 8))),
    (date(2026, 3, 1), date(2026, 3, 31), (date(2026, 2, 1), date(2026, 2, 28))),
    (date(2024, 3, 1), date(2024, 3, 29), (date(2024, 2, 1), date(2024, 2, 29))),
    (date(2026, 1, 1), date(2026, 1, 8), (date(2025, 12, 1), date(2025, 12, 8))),
    (date(2026, 10, 5), date(2026, 10, 8), (date(2026, 10, 1), date(2026, 10, 4))),
])
def test_previous_period(start, end, expected):
    assert previous_period(start, end) == expected


def test_filter_excludes_rules_and_entries_but_keeps_income_and_balance():
    day = date(2026, 10, 8)
    rows = [row(day, 1000, "Дом", "Аренда"), row(day, 200, "Дом", "Вещи"),
            row(day, 100, mandatory=True), row(day, 400), row(day, 2000, kind="income")]
    result = period_metrics(rows, settings(True), day, day)
    assert result["expenses"] == 600
    assert result["excluded"] == 1100
    assert result["all_expenses"] == 1700
    assert result["income"] == 2000
    assert result["count"] == 2
    assert result["categories"] == {"Дом": 200, "Еда": 400}
    assert "Остаток за период: 300 ₽" in analytics_lines("balance", rows, settings(True), day)
    assert limit_status(result, settings(True))[1]["threshold"] == 80
    whole = settings(True)
    whole["mandatory_rules"] = [["Дом", ""]]
    assert period_metrics(rows, whole, day, day)["expenses"] == 400


def test_comparison_filters_both_periods_and_shows_categories_that_disappeared():
    rows = [row(date(2026, 9, 8), 400, "Транспорт"), row(date(2026, 9, 8), 1000, "Дом", "Аренда"),
            row(date(2026, 10, 8), 600), row(date(2026, 10, 8), 2000, "Дом", "Аренда")]
    text = "\n".join(comparison_lines(rows, settings(True), date(2026, 10, 1), date(2026, 10, 8)))
    assert "Сейчас: 600 ₽ · раньше: 400 ₽" in text
    assert "Транспорт: 400 ₽ → 0 ₽ (-400 ₽)" in text
    assert "Исключено обязательных: 2 000 ₽ · раньше: 1 000 ₽" in text


def test_forecast_trends_and_limit_edges():
    today = date(2026, 10, 8)
    rows = [row(today, 800)]
    assert "При таком темпе: ≈ 3 100 ₽" in analytics_lines("forecast", rows, settings(), today)
    assert "На оставшиеся дни: 8.70 ₽ в день" in analytics_lines("forecast", rows, settings(), today)
    assert len([line for line in analytics_lines("trends", rows, settings(), today) if line.startswith("•")]) == 6
    metrics = period_metrics(rows, settings(), today, today)
    assert [item["threshold"] for item in limit_status(metrics, settings())] == [80, 100]
    assert "(нет базы для %)" in "\n".join(comparison_lines([], settings(), date(2026, 10, 1), today))
