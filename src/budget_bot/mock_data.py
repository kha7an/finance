from __future__ import annotations

from datetime import date
from typing import Any, Dict


def sample_screenshot_payload(run_date: date, tag: str = "demo") -> Dict[str, Any]:
    suffix = f" [{tag}]" if tag else ""
    return {
        "bank": "tbank",
        "period": {
            "month": run_date.month,
            "year": run_date.year,
            "screenshot_date": run_date.isoformat(),
        },
        "operations": [
            {
                "date": run_date.isoformat(),
                "name": f"Бургер Кинг{suffix}",
                "amount": -709.96,
                "type": "expense",
                "category": "Еда",
                "subcategory": "Фастфуд",
                "confidence": 0.97,
                "note": "mock expense",
            },
            {
                "date": run_date.isoformat(),
                "name": f"Озон возврат{suffix}",
                "amount": 1250.0,
                "type": "income",
                "category": "Возврат",
                "subcategory": None,
                "confidence": 0.96,
                "note": "mock income",
            },
            {
                "date": run_date.isoformat(),
                "name": f"Альберт X.{suffix}",
                "amount": -2000.0,
                "type": "transfer",
                "category": None,
                "subcategory": None,
                "confidence": 0.99,
                "note": "mock transfer",
            },
        ],
    }


def sample_image_content(tag: str = "demo") -> bytes:
    return f"budget-bot-mock-image:{tag}".encode("utf-8")
