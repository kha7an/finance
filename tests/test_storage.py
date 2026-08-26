from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from budget_bot.storage import Storage
from budget_bot.storage.helpers import DEFAULT_OWNER_ID


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))


def test_replace_budget_entries_uses_batch_insert() -> None:
    storage = Storage.__new__(Storage)
    connection = FakeConnection()

    @contextmanager
    def connect() -> Iterator[FakeConnection]:
        yield connection

    storage._connect = connect
    rows = [
        {
            "source": "excel_sync",
            "workbook_sheet": "Расходы",
            "workbook_row": index,
            "operation_date": "2026-08-21",
            "operation_type": "expense",
            "amount": "100.00",
            "category": "Еда",
            "subcategory": "Супермаркеты",
            "name": f"Shop {index}",
            "note": "",
            "bank": "",
        }
        for index in (2, 3)
    ]

    assert storage.replace_budget_entries(rows) == 2

    assert len(connection.calls) == 2
    delete_query, delete_params = connection.calls[0]
    insert_query, insert_params = connection.calls[1]
    assert "DELETE FROM budget_entries" in delete_query
    assert delete_params == (DEFAULT_OWNER_ID,)
    assert "INSERT INTO budget_entries" in insert_query
    assert insert_query.count("(%s, %s") == 2
    assert len(insert_params) == 30
