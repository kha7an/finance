from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from ..models import ParsedOperation


DEFAULT_OWNER_ID = "default"
_current_owner_id: ContextVar[str] = ContextVar("budget_owner_id", default=DEFAULT_OWNER_ID)


def image_hash(content: bytes) -> str:
    owner_id = _current_owner_id.get()
    if owner_id == DEFAULT_OWNER_ID:
        return hashlib.sha256(content).hexdigest()
    return hashlib.sha256(owner_id.encode("utf-8") + b"\0" + content).hexdigest()


def operation_hash(bank: str, operation: ParsedOperation) -> str:
    parts = [bank, *operation.operation_hash_parts()]
    owner_id = _current_owner_id.get()
    if owner_id != DEFAULT_OWNER_ID:
        parts.insert(0, owner_id)
    source = "|".join(parts)
    return hashlib.sha1(source.encode("utf-8")).hexdigest()


def merchant_key(name: str) -> str:
    return " ".join(name.casefold().strip().split())


def normalize_owner_id(owner_id: object) -> str:
    text = str(owner_id or "").strip()
    return text or DEFAULT_OWNER_ID


def telegram_owner_id(user_id: Optional[int]) -> str:
    return f"telegram:{user_id}" if user_id is not None else DEFAULT_OWNER_ID


def now_iso() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def operation_to_json(operation: ParsedOperation) -> Dict[str, Any]:
    data = asdict(operation)
    data["date"] = operation.date.isoformat()
    data["type"] = operation.type.value
    return data


def operation_from_json(payload: Dict[str, Any]) -> ParsedOperation:
    return ParsedOperation.from_json(payload)


def row_dict(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def float_value(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def money_row(row: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    if "total" in data:
        data["total"] = float_value(data["total"])
    return data


def entry_row(row: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    if "amount" in data:
        data["amount"] = float_value(data["amount"])
    if isinstance(data.get("operation_date"), datetime):
        data["operation_date"] = data["operation_date"].date().isoformat()
    elif isinstance(data.get("operation_date"), date):
        data["operation_date"] = data["operation_date"].isoformat()
    return data


def parse_operation_json_field(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if data is None:
        return None
    if isinstance(data.get("operation_json"), str):
        data = dict(data)
        data["operation_json"] = json.loads(data["operation_json"])
    return data
