from __future__ import annotations

from budget_bot.models import ParsedScreenshot


def test_missing_date_above_first_header_is_not_filled_from_later_header() -> None:
    parsed = ParsedScreenshot.from_json(
        {
            "bank": "tbank",
            "period": {"month": 8, "year": 2026, "screenshot_date": "2026-08-24"},
            "operations": [
                {
                    "date": None,
                    "date_status": "missing",
                    "name": "Yandex Fasten",
                    "amount": -155,
                    "type": "expense",
                    "category": "Транспорт",
                    "subcategory": "Такси",
                    "needs_review": False,
                },
                {
                    "date": "2026-08-24",
                    "date_status": "relative",
                    "name": "IP Hakimov F.D",
                    "amount": -621.77,
                    "type": "expense",
                    "category": "Еда",
                    "subcategory": "Фастфуд",
                    "needs_review": False,
                },
            ],
        }
    )

    assert parsed.operations[0].date_missing is True
    assert parsed.operations[0].date.isoformat() == "2026-08-24"
    assert parsed.operations[1].date_missing is False


def test_missing_date_below_visible_header_is_filled_forward() -> None:
    parsed = ParsedScreenshot.from_json(
        {
            "bank": "tbank",
            "period": {"month": 8, "year": 2026, "screenshot_date": "2026-08-24"},
            "operations": [
                {
                    "date": "2026-08-24",
                    "date_status": "relative",
                    "name": "IP Hakimov F.D",
                    "amount": -621.77,
                    "type": "expense",
                    "category": "Еда",
                    "subcategory": "Фастфуд",
                    "needs_review": False,
                },
                {
                    "date": None,
                    "date_status": "missing",
                    "name": "Second row under same header",
                    "amount": -100,
                    "type": "expense",
                    "category": "Еда",
                    "subcategory": "Фастфуд",
                    "needs_review": False,
                },
            ],
        }
    )

    assert parsed.operations[1].date_missing is False
    assert parsed.operations[1].date.isoformat() == "2026-08-24"
