"""Calculations shared by text analytics and scheduled summaries."""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List

from .telegram_common import format_money


def shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    year, month = divmod(month_index, 12)
    return date(year, month + 1, min(value.day, monthrange(year, month + 1)[1]))


def previous_period(start: date, end: date) -> tuple[date, date]:
    if start.day == 1 and start.year == end.year and start.month == end.month:
        previous_start = shift_month(start, -1)
        previous_days = monthrange(previous_start.year, previous_start.month)[1]
        day = previous_days if end.day == monthrange(end.year, end.month)[1] else min(end.day, previous_days)
        return previous_start, previous_start.replace(day=day)
    previous_end = start - timedelta(days=1)
    return previous_end - (end - start), previous_end


def is_mandatory(row: Dict[str, Any], settings: Dict[str, Any]) -> bool:
    return bool(row.get("is_mandatory")) or any(
        row["category"] == category and (not subcategory or row["subcategory"] == subcategory)
        for category, subcategory in settings["mandatory_rules"]
    )


def period_metrics(rows: List[Dict[str, Any]], settings: Dict[str, Any], start: date, end: date) -> Dict[str, Any]:
    totals = {key: Decimal(0) for key in ("expenses", "all_expenses", "excluded", "income")}
    categories: Dict[str, Decimal] = defaultdict(Decimal)
    count = 0
    for row in rows:
        if not start <= row["operation_date"] <= end:
            continue
        amount = Decimal(str(row["total"]))
        if row["operation_type"] == "income":
            totals["income"] += amount
        elif row["operation_type"] == "expense":
            totals["all_expenses"] += amount
            if settings["exclude_mandatory"] and is_mandatory(row, settings):
                totals["excluded"] += amount
                continue
            totals["expenses"] += amount
            categories[row["category"] or "Без категории"] += amount
            count += int(row["count"])
    return {**totals, "categories": dict(categories), "count": count}


def money(value: Decimal) -> str:
    return format_money(float(value))


def comparison_lines(rows: List[Dict[str, Any]], settings: Dict[str, Any], start: date, end: date) -> List[str]:
    old_start, old_end = previous_period(start, end)
    current = period_metrics(rows, settings, start, end)
    previous = period_metrics(rows, settings, old_start, old_end)
    delta = current["expenses"] - previous["expenses"]
    percent = f" ({delta / previous['expenses']:+.0%})" if previous["expenses"] else " (нет базы для %)"
    lines = [
        f"Сравнение: {start:%d.%m.%Y}–{end:%d.%m.%Y}",
        f"С {old_start:%d.%m.%Y}–{old_end:%d.%m.%Y}",
        f"Сейчас: {money(current['expenses'])} · раньше: {money(previous['expenses'])}",
        f"Изменение: {'+' if delta > 0 else ''}{money(delta)}{percent}",
    ]
    if (end - start).days != (old_end - old_start).days:
        lines.append("В периодах разное число дней; учитывай среднее в день.")
    current_daily = current["expenses"] / Decimal((end - start).days + 1)
    previous_daily = previous["expenses"] / Decimal((old_end - old_start).days + 1)
    lines.append(f"В день: {money(current_daily)} · раньше: {money(previous_daily)}")
    names = current["categories"].keys() | previous["categories"].keys()
    differences = [(name, current["categories"].get(name, Decimal(0)) - previous["categories"].get(name, Decimal(0))) for name in names]
    lines.append("Что изменилось по категориям:")
    for name, change in sorted(differences, key=lambda item: (-abs(item[1]), item[0]))[:10]:
        before = previous["categories"].get(name, Decimal(0))
        after = current["categories"].get(name, Decimal(0))
        lines.append(f"• {name}: {money(before)} → {money(after)} ({'+' if change > 0 else ''}{money(change)})")
    if not names:
        lines.append("Нет записанных расходов.")
    if settings["exclude_mandatory"]:
        lines.append(f"Исключено обязательных: {money(current['excluded'])} · раньше: {money(previous['excluded'])}")
    return lines


def limit_status(metrics: Dict[str, Any], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for category, raw_limit in settings["limits"].items():
        limit = Decimal(str(raw_limit))
        spent = metrics["expenses"] if category == "" else metrics["categories"].get(category, Decimal(0))
        ratio = spent / limit
        result.append({"category": category, "limit": limit, "spent": spent,
                       "remaining": limit - spent, "threshold": 100 if ratio >= 1 else 80 if ratio >= Decimal("0.8") else 0,
                       "percent": ratio * 100})
    return sorted(result, key=lambda item: item["category"])


def analytics_lines(tab: str, rows: List[Dict[str, Any]], settings: Dict[str, Any], today: date) -> List[str]:
    start = today.replace(day=1)
    metrics = period_metrics(rows, settings, start, today)
    if tab == "compare":
        return comparison_lines(rows, settings, start, today)
    if tab == "balance":
        balance = metrics["income"] - metrics["all_expenses"]
        lines = [f"Доходы и остаток · {start:%d.%m}–{today:%d.%m.%Y}",
                 f"Доходы: {money(metrics['income'])}", f"Все расходы: {money(metrics['all_expenses'])}",
                 f"Остаток за период: {money(balance)}"]
        if metrics["income"]:
            lines.append(f"Доля оставшихся доходов: {balance / metrics['income']:.0%}")
        lines.append("По записанным операциям, не баланс банковского счёта. Обязательные расходы здесь учитываются.")
        return lines
    if tab == "limits":
        lines = [f"Лимиты · {start:%m.%Y}"]
        for item in limit_status(metrics, settings):
            label = item["category"] or "Общий"
            left = f"Осталось {money(item['remaining'])}" if item["remaining"] >= 0 else f"Превышение {money(-item['remaining'])}"
            lines.append(f"• {label}: {money(item['spent'])} / {money(item['limit'])} ({item['percent']:.0f}%). {left}")
        if not settings["limits"]:
            lines.append("Лимиты пока не заданы.")
        lines.extend(["Задать: /limit 60000", "По категории: /limit Еда | 15000", "Снять: та же команда с суммой 0.",
                      "Уведомления при 80% и 100% — один раз за месяц для каждого порога и режима фильтра."])
    elif tab == "trends":
        lines = ["Динамика за 6 месяцев · расходы / доходы"]
        for offset in range(-5, 1):
            month = shift_month(start, offset)
            end = today if offset == 0 else month.replace(day=monthrange(month.year, month.month)[1])
            item = period_metrics(rows, settings, month, end)
            suffix = f" (по {today:%d.%m})" if offset == 0 else ""
            lines.append(f"• {month:%m.%Y}: {money(item['expenses'])} / {money(item['income'])}{suffix}")
        lines.append("Месяц без операций может означать, что данные не загружены.")
    elif tab == "forecast":
        # shortcut: linear pace cannot predict future fixed payments; upgrade when planned payments exist.
        days = monthrange(today.year, today.month)[1]
        forecast = metrics["expenses"] / Decimal(today.day) * days
        lines = [f"Оценка расходов на конец {today:%m.%Y}",
                 f"Записано: {money(metrics['expenses'])}",
                 f"Текущий темп: {money(metrics['expenses'] / Decimal(today.day))} в день",
                 f"При таком темпе: ≈ {money(forecast)}",
                 "Приблизительная оценка: крупные покупки и будущие фиксированные платежи могут изменить результат."]
        if "" in settings["limits"]:
            limit = Decimal(settings["limits"][""])
            remaining = max(Decimal(0), limit - metrics["expenses"])
            days_left = days - today.day
            lines.append(f"Осталось в бюджете: {money(remaining)}")
            if days_left:
                lines.append(f"На оставшиеся дни: {money(remaining / days_left)} в день")
    else:
        raise ValueError("Unknown analytics tab")
    if settings["exclude_mandatory"]:
        lines.append(f"Без обязательных расходов. Исключено за текущий месяц: {money(metrics['excluded'])}")
    else:
        lines.append("Учитываются все расходы.")
    return lines
