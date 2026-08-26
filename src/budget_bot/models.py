from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional


class OperationType(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    IGNORE = "ignore"


class OperationStatus(str, Enum):
    AUTO_WRITTEN = "auto_written"
    PENDING = "pending"
    IGNORED = "ignored"
    FAILED = "failed"


@dataclass(frozen=True)
class ParsedOperation:
    date: date
    name: str
    amount: float
    type: OperationType
    category: Optional[str] = None
    subcategory: Optional[str] = None
    confidence: float = 0.0
    needs_review: bool = False
    note: str = ""
    date_missing: bool = False
    occurrence_count: int = 1
    occurrence_confirmed: bool = True

    @property
    def excel_amount(self) -> float:
        return abs(float(self.amount))

    @classmethod
    def from_json(cls, payload: Dict[str, Any], fallback_date: Optional[date] = None) -> "ParsedOperation":
        raw_type = str(payload.get("type", "")).strip().lower()
        if raw_type not in {item.value for item in OperationType}:
            raise ValueError(f"Unsupported operation type: {raw_type!r}")

        raw_value = payload.get("date")
        raw_date = str(raw_value).strip() if raw_value is not None else ""
        if raw_date.casefold() in {"null", "none", "nil"}:
            raw_date = ""
        date_status = str(payload.get("date_status", "")).strip().lower()
        date_missing = date_status == "missing"
        if not raw_date:
            if fallback_date is None:
                raise ValueError("Operation date is required")
            raw_date = fallback_date.isoformat()
            date_missing = True

        return cls(
            date=date.fromisoformat(raw_date),
            name=str(payload.get("name", "")).strip(),
            amount=float(payload.get("amount")),
            type=OperationType(raw_type),
            category=_clean_optional(payload.get("category")),
            subcategory=_clean_optional(payload.get("subcategory")),
            confidence=float(payload.get("confidence", 1.0)),
            needs_review=_clean_bool(payload.get("needs_review")),
            note=str(payload.get("note", "") or "").strip(),
            date_missing=date_missing,
            occurrence_count=max(1, int(payload.get("occurrence_count", 1) or 1)),
            occurrence_confirmed=bool(payload.get("occurrence_confirmed", True)),
        )

    def operation_hash_parts(self) -> Iterable[str]:
        return (
            self.date.isoformat(),
            self.type.value,
            f"{self.amount:.2f}",
            self.name.casefold().strip(),
        )


@dataclass(frozen=True)
class ParsedScreenshot:
    bank: str
    operations: List[ParsedOperation]
    raw: Dict[str, Any]

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "ParsedScreenshot":
        operations_payload = payload.get("operations")
        if not isinstance(operations_payload, list):
            raise ValueError("LLM response must contain operations list")
        fallback_date = _period_screenshot_date(payload.get("period"))
        operations = [ParsedOperation.from_json(item, fallback_date=fallback_date) for item in operations_payload]

        return cls(
            bank=str(payload.get("bank", "unknown")).strip().lower() or "unknown",
            operations=_fill_missing_dates_from_visible_operations(operations),
            raw=payload,
        )


def _clean_optional(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes", "да"}
    return bool(value)


def _period_screenshot_date(period: Any) -> Optional[date]:
    if not isinstance(period, dict):
        return None
    raw_date = str(period.get("screenshot_date", "")).strip()
    if not raw_date:
        return None
    return date.fromisoformat(raw_date)


def _fill_missing_dates_from_visible_operations(operations: List[ParsedOperation]) -> List[ParsedOperation]:
    filled_operations: List[ParsedOperation] = []
    current_visible_date: Optional[date] = None
    for operation in operations:
        if not operation.date_missing:
            current_visible_date = operation.date
            filled_operations.append(operation)
            continue
        if current_visible_date is None:
            filled_operations.append(operation)
            continue
        filled_operations.append(_with_date(operation, current_visible_date))

    return filled_operations


def _with_date(operation: ParsedOperation, operation_date: date) -> ParsedOperation:
    return ParsedOperation(
        date=operation_date,
        name=operation.name,
        amount=operation.amount,
        type=operation.type,
        category=operation.category,
        subcategory=operation.subcategory,
        confidence=operation.confidence,
        needs_review=operation.needs_review,
        note=operation.note,
        date_missing=False,
        occurrence_count=operation.occurrence_count,
        occurrence_confirmed=operation.occurrence_confirmed,
    )
