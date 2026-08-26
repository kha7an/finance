from __future__ import annotations

from datetime import date, time as local_time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import OperationType, ParsedOperation
from .telegram_common import format_money, parse_user_operation_date


def parse_stats_period(text: str, year: int) -> Optional[Tuple[date, date]]:
    cleaned = text.strip().replace(" ", "")
    if "-" not in cleaned:
        single_date = parse_user_operation_date(cleaned, year)
        if single_date is None:
            return None
        return single_date, single_date
    start_text, end_text = cleaned.split("-", 1)
    start_date = parse_user_operation_date(start_text, year)
    end_date = parse_user_operation_date(end_text, year)
    if start_date is None or end_date is None:
        return None
    if end_date < start_date:
        return end_date, start_date
    return start_date, end_date


def expense_report_lines(summary: Dict[str, Any]) -> List[str]:
    start_date = summary["start_date"]
    end_date = summary["end_date"]
    category = summary.get("category")
    title = f"Расходы {start_date.strftime('%d.%m')} - {end_date.strftime('%d.%m')}"
    if category:
        title = f"{title}: {category}"
    lines = [
        title,
        f"Всего: {format_money(summary['total'])}",
        f"Операций: {summary['count']}",
    ]
    groups = summary["subcategories"] if category else summary["categories"]
    if groups:
        lines.append("Топ:")
        for row in groups[:5]:
            name = row.get("subcategory") if category else row.get("category")
            lines.append(f"- {name or 'Без категории'}: {format_money(float(row['total']))}")
    return lines


def chart_period_payload(start_date: date, end_date: date, category: Optional[str] = None) -> str:
    category_payload = category or "all"
    return f"{start_date.isoformat()}:{end_date.isoformat()}:{category_payload}"


def parse_chart_period_payload(text: str) -> Optional[Tuple[date, date, Optional[str]]]:
    parts = text.split(":", 2)
    if len(parts) < 2:
        return None
    try:
        start_date = date.fromisoformat(parts[0])
        end_date = date.fromisoformat(parts[1])
    except ValueError:
        return None
    category = None
    if len(parts) == 3:
        category_part = parts[2].strip()
        if category_part and category_part != "all":
            category = category_part
    return start_date, end_date, category


def expense_totals_by_date(operations: Sequence[ParsedOperation]) -> Dict[date, float]:
    totals: Dict[date, float] = {}
    for operation in operations:
        if operation.type != OperationType.EXPENSE:
            continue
        totals[operation.date] = totals.get(operation.date, 0.0) + operation.excel_amount * operation.occurrence_count
    return totals


def parse_reminder_time(text: str) -> Optional[local_time]:
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        return local_time(hour, minute)
    except ValueError:
        return None
