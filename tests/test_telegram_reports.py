from __future__ import annotations

from datetime import date

from budget_bot.telegram_reports import expense_report_lines


def test_expense_report_lines_include_dynamic_analysis() -> None:
    summary = {
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 3),
        "category": None,
        "total": 1500.0,
        "count": 3,
        "period_days": 3,
        "active_days": 2,
        "previous_start_date": date(2026, 7, 29),
        "previous_end_date": date(2026, 7, 31),
        "previous_total": 1000.0,
        "previous_count": 2,
        "previous_delta": 500.0,
        "previous_delta_percent": 50.0,
        "categories": [
            {"category": "Еда", "total": 1000.0},
            {"category": "Транспорт", "total": 500.0},
        ],
        "subcategories": [],
    }

    lines = expense_report_lines(summary)

    assert "Среднее в день: 500 ₽" in lines
    assert "Средний чек: 500 ₽" in lines
    assert "Дней с расходами: 2 из 3" in lines
    assert "К прошлому периоду: на 500 ₽ больше (50%)" in lines
    assert "Лидер: Еда - 67%" in lines
