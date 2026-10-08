from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from budget_bot.telegram_charts import render_expense_chart


@pytest.mark.parametrize("filtered", [False, True])
def test_render_expense_chart_writes_png(tmp_path: Path, filtered: bool) -> None:
    summary = {
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 8, 3),
        "category": None,
        "exclude_mandatory": filtered,
        "excluded_total": 37000,
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
    daily_rows = [
        {"operation_date": date(2026, 8, 1), "total": 500.0},
        {"operation_date": date(2026, 8, 3), "total": 1000.0},
    ]
    output_path = tmp_path / "chart.png"
    render_expense_chart(summary, daily_rows, output_path)
    assert output_path.exists()
    assert output_path.stat().st_size > 0
