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

    pie_labels, pie_values = _pie_slices(groups, float(summary["total"]))
    daily_dates, daily_values = _daily_series(start_date, end_date, daily_rows)
    show_daily = len(daily_dates) > 1

    figure_height = 8.5 if show_daily else 5.5
    figure, axes = plt.subplots(2 if show_daily else 1, 1, figsize=(10, figure_height))
    if not show_daily:
        pie_axis = axes
        bar_axis = None
    else:
        pie_axis, bar_axis = axes

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
        bar_axis.bar(daily_dates, daily_values, color="#4C78A8", width=0.8)
        bar_axis.set_title("По дням", fontsize=11)
        bar_axis.set_ylabel("₽")
        bar_axis.yaxis.set_major_formatter(plt.FuncFormatter(_format_axis_money))
        bar_axis.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        figure.autofmt_xdate(rotation=45, ha="right")

    figure.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(figure)
    return output_path


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
