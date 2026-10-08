from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt


def render_expense_chart(
    summary: Dict[str, Any],
    daily_rows: List[Dict[str, Any]],
    output_path: Path,
) -> Path:
    start_date = summary["start_date"]
    end_date = summary["end_date"]
    category = summary.get("category")
    groups = summary["subcategories"] if category else summary["categories"]
    title = f"Расходы {start_date.strftime('%d.%m')} – {end_date.strftime('%d.%m')}"
    if category:
        title = f"{title}: {category}"
    if summary.get("exclude_mandatory"):
        title += f"\nБез обязательных · исключено: {_format_money_text(summary['excluded_total'])}"

    pie_labels, pie_values = _pie_slices(groups, float(summary["total"]))
    daily_dates, daily_values = _daily_series(start_date, end_date, daily_rows)
    show_daily = len(daily_dates) > 1

    figure_height = 9.5 if show_daily else 6.5
    rows = 3 if show_daily else 2
    height_ratios = [1.0, 3.8, 2.4] if show_daily else [1.0, 3.8]
    figure, axes = plt.subplots(rows, 1, figsize=(10, figure_height), gridspec_kw={"height_ratios": height_ratios})
    if not show_daily:
        metric_axis, pie_axis = axes
        bar_axis = None
    else:
        metric_axis, pie_axis, bar_axis = axes

    _render_metrics(metric_axis, summary)

    if pie_values:
        pie_axis.pie(
            pie_values,
            labels=pie_labels,
            autopct=lambda value: f"{value:.0f}%" if value >= 5 else "",
            startangle=90,
            textprops={"fontsize": 9},
        )
        pie_axis.set_title(title, fontsize=12, pad=12)
    else:
        pie_axis.text(0.5, 0.5, "Нет данных", ha="center", va="center")
        pie_axis.set_title(title, fontsize=12, pad=12)
        pie_axis.axis("off")

    if bar_axis is not None:
        average = sum(daily_values) / len(daily_values) if daily_values else 0.0
        bar_axis.bar(daily_dates, daily_values, color="#4C78A8", width=0.8)
        if average > 0:
            bar_axis.axhline(average, color="#F58518", linestyle="--", linewidth=1.2, label="Среднее")
            bar_axis.legend(loc="upper left", frameon=False)
        bar_axis.set_title("По дням", fontsize=11)
        bar_axis.set_ylabel("₽")
        bar_axis.yaxis.set_major_formatter(plt.FuncFormatter(_format_axis_money))
        bar_axis.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(daily_dates) // 10)))
        bar_axis.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        figure.autofmt_xdate(rotation=45, ha="right")

    figure.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(figure)
    return output_path


def _render_metrics(axis, summary: Dict[str, Any]) -> None:
    total = float(summary["total"])
    count = int(summary["count"] or 0)
    period_days = int(summary.get("period_days") or 1)
    previous_total = float(summary.get("previous_total") or 0)
    previous_delta = float(summary.get("previous_delta") or (total - previous_total))
    previous_delta_percent = summary.get("previous_delta_percent")

    lines = [
        f"Всего: {_format_money_text(total)}",
        f"Операций: {count}",
    ]
    if period_days > 1:
        lines.append(f"Среднее в день: {_format_money_text(total / period_days)}")
    if count > 0:
        lines.append(f"Средний чек: {_format_money_text(total / count)}")
    if previous_total > 0:
        direction = "+" if previous_delta >= 0 else "-"
        if previous_delta_percent is None:
            lines.append(f"К прошлому периоду: {direction}{_format_money_text(abs(previous_delta))}")
        else:
            lines.append(
                f"К прошлому периоду: {direction}{_format_money_text(abs(previous_delta))} "
                f"({direction}{abs(float(previous_delta_percent)):.0f}%)"
            )
    elif total > 0 and summary.get("previous_start_date"):
        lines.append("К прошлому периоду: раньше расходов не было")

    axis.axis("off")
    axis.text(0.0, 0.72, "   ".join(lines[:3]), fontsize=11, weight="bold", transform=axis.transAxes)
    if len(lines) > 3:
        axis.text(
            0.0,
            0.34,
            "   ".join(lines[3:]),
            fontsize=10,
            color="#4A4A4A",
            transform=axis.transAxes,
        )
    if previous_total > 0 or total > 0:
        max_value = max(total, previous_total, 1)
        axis.barh([0.05], [previous_total], color="#BAB0AC", height=0.06)
        axis.barh([0.16], [total], color="#4C78A8", height=0.06)
        axis.text(max_value * 1.01, 0.05, "прошлый", va="center", fontsize=8, color="#666666")
        axis.text(max_value * 1.01, 0.16, "текущий", va="center", fontsize=8, color="#333333")
        axis.set_xlim(0, max_value * 1.35)
        axis.set_ylim(0, 0.65)


def _pie_slices(groups: List[Dict[str, Any]], total: float, limit: int = 8) -> tuple[List[str], List[float]]:
    if total <= 0 or not groups:
        return [], []
    labels: List[str] = []
    values: List[float] = []
    shown_total = 0.0
    for row in groups[:limit]:
        amount = float(row["total"])
        if amount <= 0:
            continue
        name = row.get("subcategory") or row.get("category") or "Без категории"
        labels.append(_truncate_label(str(name)))
        values.append(amount)
        shown_total += amount
    remainder = total - shown_total
    if remainder > 0.01:
        labels.append("Прочее")
        values.append(remainder)
    return labels, values


def _daily_series(
    start_date: date,
    end_date: date,
    daily_rows: List[Dict[str, Any]],
) -> tuple[List[date], List[float]]:
    totals = {
        row["operation_date"] if isinstance(row["operation_date"], date) else date.fromisoformat(str(row["operation_date"])): float(row["total"])
        for row in daily_rows
    }
    dates: List[date] = []
    values: List[float] = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        values.append(totals.get(current, 0.0))
        current += timedelta(days=1)
    return dates, values


def _truncate_label(text: str, limit: int = 22) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _format_axis_money(value: float, _position: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:.0f}"


def _format_money_text(value: float) -> str:
    formatted = f"{value:,.0f}".replace(",", " ")
    return f"{formatted} ₽"
