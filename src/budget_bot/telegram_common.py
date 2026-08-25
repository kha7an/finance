from __future__ import annotations

import re
from datetime import date
from typing import Dict, Optional, Sequence

from .models import OperationType, ParsedOperation


def button_rows(buttons: Sequence[Dict[str, str]], columns: int) -> list[list[Dict[str, str]]]:
    return [list(buttons[index : index + columns]) for index in range(0, len(buttons), columns)]


def parse_money_amount(text: str) -> Optional[float]:
    cleaned = text.strip().replace(" ", "").replace("₽", "").replace(",", ".")
    cleaned = cleaned.lstrip("+")
    if not re.fullmatch(r"-?\d+(?:\.\d{1,2})?", cleaned):
        return None
    return abs(float(cleaned))


def parse_user_operation_date(text: str, year: int) -> Optional[date]:
    parts = text.strip().split(".")
    if len(parts) not in {2, 3}:
        return None
    try:
        day = int(parts[0])
        month = int(parts[1])
        parsed_year = int(parts[2]) if len(parts) == 3 and parts[2] else year
        if parsed_year < 100:
            parsed_year += 2000
        return date(parsed_year, month, day)
    except ValueError:
        return None


def parse_index(payload: str, position: int) -> Optional[int]:
    parts = payload.split(":")
    if position >= len(parts):
        return None
    try:
        return int(parts[position])
    except ValueError:
        return None


def operation_summary_text(operation: ParsedOperation) -> str:
    name = operation.name or "операция"
    amount = format_money(operation.amount)
    category = category_summary(operation)
    count = f" x{operation.occurrence_count}" if operation.occurrence_count > 1 else ""
    return f"{name}: {amount}{count} - {category}"


def category_summary(operation: ParsedOperation) -> str:
    if operation.type == OperationType.INCOME:
        return operation.category or "доход"
    if operation.category and operation.subcategory:
        return f"{operation.category} / {operation.subcategory}"
    if operation.category:
        return operation.category
    return operation.type.value


def format_money(amount: float) -> str:
    formatted = f"{amount:,.2f}".replace(",", " ")
    if formatted.endswith(".00"):
        formatted = formatted[:-3]
    return f"{formatted} ₽"
