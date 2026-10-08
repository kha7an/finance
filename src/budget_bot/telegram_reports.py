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
    total = float(summary["total"])
    count = int(summary["count"] or 0)
    period_days = int(summary.get("period_days") or (end_date - start_date).days + 1)
    active_days = int(summary.get("active_days") or 0)
    if period_days > 1:
        lines.append(f"Среднее в день: {format_money(total / period_days)}")
    if count > 0:
        lines.append(f"Средний чек: {format_money(total / count)}")
    if active_days > 0 and active_days != period_days:
        lines.append(f"Дней с расходами: {active_days} из {period_days}")
    previous_total = float(summary.get("previous_total") or 0)
    previous_delta = float(summary.get("previous_delta") or (total - previous_total))
    previous_delta_percent = summary.get("previous_delta_percent")
    if previous_total > 0:
        direction = "больше" if previous_delta > 0 else "меньше"
        if abs(previous_delta) < 0.01:
            lines.append("К прошлому периоду: без изменений")
        elif previous_delta_percent is None:
            lines.append(f"К прошлому периоду: на {format_money(abs(previous_delta))} {direction}")
        else:
            lines.append(
                f"К прошлому периоду: на {format_money(abs(previous_delta))} {direction} ({abs(float(previous_delta_percent)):.0f}%)"
            )
    elif total > 0 and summary.get("previous_start_date"):
        lines.append("К прошлому периоду: раньше расходов не было")
    groups = summary["subcategories"] if category else summary["categories"]
    if groups and total > 0:
        leader = groups[0]
        name = leader.get("subcategory") if category else leader.get("category")
        leader_total = float(leader["total"])
        lines.append(f"Лидер: {name or 'Без категории'} - {leader_total / total:.0%}")
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
