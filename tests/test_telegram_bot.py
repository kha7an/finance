from __future__ import annotations

from datetime import date

from budget_bot.models import OperationType, ParsedOperation
from budget_bot.telegram_bot import _written_operation_summary_lines


def test_written_summary_preserves_screenshot_order() -> None:
    operations = [
        ParsedOperation(
            date=date(2026, 8, 15),
            name="Yandex Fasten",
            amount=-141,
            type=OperationType.EXPENSE,
            category="Транспорт",
            subcategory="Такси",
        ),
        ParsedOperation(
            date=date(2026, 8, 15),
            name="Waypma 24",
            amount=-800,
            type=OperationType.EXPENSE,
            category="Еда",
            subcategory="Фастфуд",
        ),
        ParsedOperation(
            date=date(2026, 8, 15),
            name="Fix Price",
            amount=-1081.5,
            type=OperationType.EXPENSE,
            category="Еда",
            subcategory="Супермаркеты",
        ),
    ]

    lines = _written_operation_summary_lines(operations)

    assert lines[2].startswith("- Yandex Fasten:")
    assert lines[3].startswith("- Waypma 24:")
    assert lines[4].startswith("- Fix Price:")
