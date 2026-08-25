from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Iterator, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .categories import CategoryBook
from .default_categories import DEFAULT_EXPENSE_CATEGORIES, DEFAULT_INCOME_CATEGORIES
from .models import OperationStatus, OperationType, ParsedOperation


DEFAULT_OWNER_ID = "default"
_current_owner_id: ContextVar[str] = ContextVar("budget_owner_id", default=DEFAULT_OWNER_ID)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    owner_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id BIGSERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')),
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(owner_id, kind, name)
);

CREATE TABLE IF NOT EXISTS subcategories (
    id BIGSERIAL PRIMARY KEY,
    category_id BIGINT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(category_id, name)
);

CREATE TABLE IF NOT EXISTS budget_accounts (
    owner_id TEXT PRIMARY KEY REFERENCES users(owner_id) ON DELETE CASCADE,
    storage_kind TEXT NOT NULL,
    workbook_path TEXT,
    spreadsheet_url TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS source_images (
    id BIGSERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
    image_hash TEXT NOT NULL,
    telegram_file_id TEXT,
    bank TEXT,
    status TEXT NOT NULL,
    raw_response JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(owner_id, image_hash)
);

CREATE TABLE IF NOT EXISTS operations (
    id BIGSERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
    operation_hash TEXT NOT NULL,
    image_hash TEXT NOT NULL,
    bank TEXT NOT NULL,
    operation_json JSONB NOT NULL,
    status TEXT NOT NULL,
    workbook_row INTEGER,
    status_note TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(owner_id, operation_hash)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id BIGSERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
    operation_hash TEXT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT,
    prompt TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(owner_id, operation_hash)
);

CREATE TABLE IF NOT EXISTS learned_expense_categories (
    owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
    merchant_key TEXT NOT NULL,
    merchant_name TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(owner_id, merchant_key)
);

CREATE TABLE IF NOT EXISTS budget_entries (
    id BIGSERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    operation_hash TEXT,
    export_sheet TEXT,
    export_row INTEGER,
    operation_date DATE NOT NULL,
    operation_type TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL,
    category TEXT,
    subcategory TEXT,
    name TEXT NOT NULL,
    note TEXT,
    bank TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(owner_id, operation_hash, export_row)
);

CREATE TABLE IF NOT EXISTS telegram_chats (
    chat_id BIGINT PRIMARY KEY,
    user_id BIGINT,
    owner_id TEXT REFERENCES users(owner_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_settings (
    chat_id BIGINT PRIMARY KEY REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL,
    time_local TEXT NOT NULL,
    timezone TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_deliveries (
    chat_id BIGINT NOT NULL REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
    reminder_date DATE NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chat_id, reminder_date)
);

CREATE INDEX IF NOT EXISTS categories_owner_kind_idx
    ON categories(owner_id, kind, sort_order);
CREATE INDEX IF NOT EXISTS source_images_owner_hash_idx
    ON source_images(owner_id, image_hash);
CREATE INDEX IF NOT EXISTS operations_owner_hash_idx
    ON operations(owner_id, operation_hash);
CREATE INDEX IF NOT EXISTS pending_actions_owner_chat_idx
    ON pending_actions(owner_id, chat_id);
CREATE INDEX IF NOT EXISTS budget_entries_owner_date_type_idx
    ON budget_entries(owner_id, operation_date, operation_type);
CREATE INDEX IF NOT EXISTS budget_entries_owner_category_idx
    ON budget_entries(owner_id, category, subcategory);
"""


class Storage:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        self.database_url = database_url
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._init_db()

    @property
    def owner_id(self) -> str:
        return _current_owner_id.get()

    @contextmanager
    def owner_scope(self, owner_id: str) -> Iterator[None]:
        token = _current_owner_id.set(normalize_owner_id(owner_id))
        try:
            self.ensure_owner()
            yield
        finally:
            _current_owner_id.reset(token)

    @contextmanager
    def _connect(self):
        with self._pool.connection() as connection:
            yield connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA_SQL)
        self.ensure_owner(DEFAULT_OWNER_ID)

    def ensure_owner(self, owner_id: Optional[str] = None) -> None:
        owner_id = normalize_owner_id(owner_id or self.owner_id)
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (owner_id, created_at, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, timestamp, timestamp),
            )
            exists = connection.execute(
                "SELECT 1 FROM categories WHERE owner_id = %s LIMIT 1",
                (owner_id,),
            ).fetchone()
            if exists is None:
                self._insert_default_categories(connection, owner_id)

    def _insert_default_categories(self, connection, owner_id: str) -> None:
        timestamp = now_iso()
        for category_order, (category, subcategories) in enumerate(DEFAULT_EXPENSE_CATEGORIES.items()):
            row = connection.execute(
                """
                INSERT INTO categories (owner_id, kind, name, sort_order, created_at, updated_at)
                VALUES (%s, 'expense', %s, %s, %s, %s)
                ON CONFLICT(owner_id, kind, name) DO UPDATE SET updated_at = excluded.updated_at
                RETURNING id
                """,
                (owner_id, category, category_order, timestamp, timestamp),
            ).fetchone()
            category_id = row["id"]
            for subcategory_order, subcategory in enumerate(subcategories):
                connection.execute(
                    """
                    INSERT INTO subcategories (category_id, name, sort_order, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT(category_id, name) DO NOTHING
                    """,
                    (category_id, subcategory, subcategory_order, timestamp, timestamp),
                )
        for category_order, category in enumerate(DEFAULT_INCOME_CATEGORIES):
            connection.execute(
                """
                INSERT INTO categories (owner_id, kind, name, sort_order, created_at, updated_at)
                VALUES (%s, 'income', %s, %s, %s, %s)
                ON CONFLICT(owner_id, kind, name) DO NOTHING
                """,
                (owner_id, category, category_order, timestamp, timestamp),
            )

    def category_book(self) -> CategoryBook:
        self.ensure_owner()
        with self._connect() as connection:
            category_rows = connection.execute(
                """
                SELECT id, kind, name FROM categories
                WHERE owner_id = %s
                ORDER BY sort_order, name
                """,
                (self.owner_id,),
            ).fetchall()
            subcategory_rows = connection.execute(
                """
                SELECT c.name AS category, s.name AS subcategory
                FROM categories c
                JOIN subcategories s ON s.category_id = c.id
                WHERE c.owner_id = %s AND c.kind = 'expense'
                ORDER BY c.sort_order, s.sort_order, s.name
                """,
                (self.owner_id,),
            ).fetchall()

        expense_categories: Dict[str, List[str]] = {
            row["name"]: []
            for row in category_rows
            if row["kind"] == "expense"
        }
        for row in subcategory_rows:
            expense_categories.setdefault(row["category"], []).append(row["subcategory"])
        income_categories = [row["name"] for row in category_rows if row["kind"] == "income"]
        return CategoryBook(expense_categories=expense_categories, income_categories=income_categories)

    def set_budget_account(
        self,
        owner_id: str,
        storage_kind: str,
        workbook_path: Optional[str] = None,
        spreadsheet_url: Optional[str] = None,
    ) -> None:
        if storage_kind not in {"excel", "google_sheets", "postgres"}:
            raise ValueError(f"Unsupported storage kind: {storage_kind!r}")
        owner_id = normalize_owner_id(owner_id)
        self.ensure_owner(owner_id)
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO budget_accounts
                    (owner_id, storage_kind, workbook_path, spreadsheet_url, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id) DO UPDATE SET
                    storage_kind = excluded.storage_kind,
                    workbook_path = excluded.workbook_path,
                    spreadsheet_url = excluded.spreadsheet_url,
                    updated_at = excluded.updated_at
                """,
                (owner_id, storage_kind, workbook_path, spreadsheet_url, timestamp, timestamp),
            )

    def budget_account(self, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        self.ensure_owner(owner_id or self.owner_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM budget_accounts WHERE owner_id = %s",
                (normalize_owner_id(owner_id or self.owner_id),),
            ).fetchone()
            return _row_dict(row)

    def record_image(
        self,
        image_hash: str,
        telegram_file_id: Optional[str],
        bank: str,
        status: str,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.ensure_owner()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_images
                    (owner_id, image_hash, telegram_file_id, bank, status, raw_response, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id, image_hash) DO UPDATE SET
                    telegram_file_id = excluded.telegram_file_id,
                    bank = excluded.bank,
                    status = excluded.status,
                    raw_response = excluded.raw_response
                """,
                (
                    self.owner_id,
                    image_hash,
                    telegram_file_id,
                    bank,
                    status,
                    Jsonb(raw_response) if raw_response is not None else None,
                    now_iso(),
                ),
            )

    def image_seen(self, image_hash: str) -> bool:
        self.ensure_owner()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM source_images
                WHERE owner_id = %s AND image_hash = %s AND status IN ('parsed', 'processed')
                """,
                (self.owner_id, image_hash),
            ).fetchone()
            return row is not None

    def update_image_status(self, image_hash: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE source_images SET status = %s WHERE owner_id = %s AND image_hash = %s",
                (status, self.owner_id, image_hash),
            )

    def operation_seen(self, operation_hash: str) -> bool:
        self.ensure_owner()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM operations WHERE owner_id = %s AND operation_hash = %s",
                (self.owner_id, operation_hash),
            ).fetchone()
            return row is not None

    def record_operation(
        self,
        operation_hash: str,
        image_hash: str,
        bank: str,
        operation: ParsedOperation,
        status: OperationStatus,
        workbook_row: Optional[int] = None,
        status_note: str = "",
    ) -> None:
        self.ensure_owner()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operations
                    (owner_id, operation_hash, image_hash, bank, operation_json, status, workbook_row, status_note, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id, operation_hash) DO NOTHING
                """,
                (
                    self.owner_id,
                    operation_hash,
                    image_hash,
                    bank,
                    Jsonb(operation_to_json(operation)),
                    status.value,
                    workbook_row,
                    status_note,
                    now_iso(),
                ),
            )

    def add_pending_action(
        self,
        operation_hash: str,
        chat_id: int,
        message_id: Optional[int],
        prompt: str,
    ) -> None:
        self.ensure_owner()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_actions
                    (owner_id, operation_hash, chat_id, message_id, prompt, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id, operation_hash) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    message_id = excluded.message_id,
                    prompt = excluded.prompt,
                    created_at = excluded.created_at
                """,
                (self.owner_id, operation_hash, chat_id, message_id, prompt, now_iso()),
            )

    def get_pending_action(self, operation_hash: str, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pending_actions
                WHERE owner_id = %s AND operation_hash = %s AND chat_id = %s
                """,
                (self.owner_id, operation_hash, chat_id),
            ).fetchone()
            return _row_dict(row)

    def get_operation(self, operation_hash: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE owner_id = %s AND operation_hash = %s",
                (self.owner_id, operation_hash),
            ).fetchone()
            data = _row_dict(row)
            if data is not None and isinstance(data["operation_json"], str):
                data["operation_json"] = json.loads(data["operation_json"])
            return data

    def operations_for_image(self, image_hash: str) -> list[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operations
                WHERE owner_id = %s AND image_hash = %s
                ORDER BY id
                """,
                (self.owner_id, image_hash),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def update_operation_status(
        self,
        operation_hash: str,
        status: OperationStatus,
        workbook_row: Optional[int] = None,
        status_note: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE operations
                SET status = %s, workbook_row = COALESCE(%s, workbook_row), status_note = %s
                WHERE owner_id = %s AND operation_hash = %s
                """,
                (status.value, workbook_row, status_note, self.owner_id, operation_hash),
            )

    def update_operation_json(self, operation_hash: str, operation: ParsedOperation) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE operations SET operation_json = %s WHERE owner_id = %s AND operation_hash = %s",
                (Jsonb(operation_to_json(operation)), self.owner_id, operation_hash),
            )

    def delete_pending_action(self, operation_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM pending_actions WHERE owner_id = %s AND operation_hash = %s",
                (self.owner_id, operation_hash),
            )

    def latest_pending_for_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pending_actions
                WHERE owner_id = %s AND chat_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (self.owner_id, chat_id),
            ).fetchone()
            return _row_dict(row)

    def reset_all(self) -> None:
        with self._connect() as connection:
            for table in [
                "pending_actions",
                "operations",
                "source_images",
                "learned_expense_categories",
                "budget_entries",
            ]:
                connection.execute(f"DELETE FROM {table} WHERE owner_id = %s", (self.owner_id,))
            connection.execute(
                """
                DELETE FROM reminder_deliveries
                WHERE chat_id IN (SELECT chat_id FROM telegram_chats WHERE owner_id = %s)
                """,
                (self.owner_id,),
            )

    def upsert_budget_entry(
        self,
        source: str,
        operation_hash: Optional[str],
        workbook_sheet: Optional[str],
        workbook_row: Optional[int],
        operation: ParsedOperation,
        bank: str,
        amount: Optional[float] = None,
    ) -> None:
        timestamp = now_iso()
        entry_amount = operation.excel_amount if amount is None else abs(float(amount))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO budget_entries
                    (
                        owner_id, source, operation_hash, export_sheet, export_row,
                        operation_date, operation_type, amount, category, subcategory,
                        name, note, bank, created_at, updated_at
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id, operation_hash, export_row) DO UPDATE SET
                    source = excluded.source,
                    export_sheet = excluded.export_sheet,
                    operation_date = excluded.operation_date,
                    amount = excluded.amount,
                    category = excluded.category,
                    subcategory = excluded.subcategory,
                    name = excluded.name,
                    note = excluded.note,
                    bank = excluded.bank,
                    updated_at = excluded.updated_at
                """,
                (
                    self.owner_id,
                    source,
                    operation_hash or f"manual:{self.owner_id}:{timestamp}:{operation.name}",
                    workbook_sheet,
                    workbook_row,
                    operation.date,
                    operation.type.value,
                    Decimal(f"{entry_amount:.2f}"),
                    operation.category,
                    operation.subcategory,
                    operation.name,
                    operation.note,
                    bank,
                    timestamp,
                    timestamp,
                ),
            )

    def append_budget_entry(
        self,
        source: str,
        operation_hash: Optional[str],
        operation: ParsedOperation,
        bank: str,
    ) -> int:
        timestamp = now_iso()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO budget_entries
                    (
                        owner_id, source, operation_hash, operation_date, operation_type,
                        amount, category, subcategory, name, note, bank, created_at, updated_at
                    )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.owner_id,
                    source,
                    operation_hash or f"manual:{self.owner_id}:{timestamp}:{operation.name}",
                    operation.date,
                    operation.type.value,
                    Decimal(f"{operation.excel_amount:.2f}"),
                    operation.category,
                    operation.subcategory,
                    operation.name,
                    operation.note,
                    bank,
                    timestamp,
                    timestamp,
                ),
            ).fetchone()
            return int(row["id"])

    def replace_budget_entries(self, entries: Iterable[Dict[str, Any]]) -> int:
        rows = list(entries)
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("DELETE FROM budget_entries WHERE owner_id = %s", (self.owner_id,))
            for entry in rows:
                connection.execute(
                    """
                    INSERT INTO budget_entries
                        (
                            owner_id, source, operation_hash, export_sheet, export_row,
                            operation_date, operation_type, amount, category, subcategory,
                            name, note, bank, created_at, updated_at
                        )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        self.owner_id,
                        entry.get("source", "import"),
                        entry.get("operation_hash"),
                        entry.get("workbook_sheet") or entry.get("export_sheet"),
                        entry.get("workbook_row") or entry.get("export_row"),
                        date.fromisoformat(str(entry["operation_date"])),
                        entry["operation_type"],
                        Decimal(f"{float(entry['amount']):.2f}"),
                        entry.get("category"),
                        entry.get("subcategory"),
                        entry.get("name") or "",
                        entry.get("note") or "",
                        entry.get("bank") or "",
                        timestamp,
                        timestamp,
                    ),
                )
        return len(rows)

    def expense_summary(
        self,
        start_date: date,
        end_date: date,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        clauses = [
            "owner_id = %s",
            "operation_type = %s",
            "operation_date >= %s",
            "operation_date <= %s",
        ]
        params: List[Any] = [self.owner_id, OperationType.EXPENSE.value, start_date, end_date]
        if category:
            clauses.append("category = %s")
            params.append(category)
        where = " AND ".join(clauses)
        with self._connect() as connection:
            total_row = connection.execute(
                f"""
                SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
                FROM budget_entries
                WHERE {where}
                """,
                params,
            ).fetchone()
            categories = connection.execute(
                f"""
                SELECT category, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
                FROM budget_entries
                WHERE {where}
                GROUP BY category
                ORDER BY total DESC, category
                LIMIT 8
                """,
                params,
            ).fetchall()
            subcategories = connection.execute(
                f"""
                SELECT subcategory, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
                FROM budget_entries
                WHERE {where}
                GROUP BY subcategory
                ORDER BY total DESC, subcategory
                LIMIT 8
                """,
                params,
            ).fetchall()
        return {
            "start_date": start_date,
            "end_date": end_date,
            "category": category,
            "total": _float(total_row["total"]),
            "count": int(total_row["count"] or 0),
            "categories": [_money_row(row) for row in categories],
            "subcategories": [_money_row(row) for row in subcategories],
        }

    def budget_entries(
        self,
        start_date: date,
        end_date: date,
        operation_type: OperationType = OperationType.EXPENSE,
        category: Optional[str] = None,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        clauses = [
            "owner_id = %s",
            "operation_type = %s",
            "operation_date >= %s",
            "operation_date <= %s",
        ]
        params: List[Any] = [self.owner_id, operation_type.value, start_date, end_date]
        if category:
            clauses.append("category = %s")
            params.append(category)
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, owner_id, source, operation_hash,
                       export_sheet AS workbook_sheet, export_row AS workbook_row,
                       operation_date, operation_type, amount, category, subcategory,
                       name, note, bank, created_at, updated_at
                FROM budget_entries
                WHERE {" AND ".join(clauses)}
                ORDER BY operation_date DESC, id DESC
                LIMIT %s
                """,
                params,
            ).fetchall()
            return [_entry_row(row) for row in rows]

    def all_budget_entries(
        self,
        start_date: date,
        end_date: date,
    ) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, owner_id, source, operation_hash,
                       export_sheet AS workbook_sheet, export_row AS workbook_row,
                       operation_date, operation_type, amount, category, subcategory,
                       name, note, bank, created_at, updated_at
                FROM budget_entries
                WHERE owner_id = %s AND operation_date >= %s AND operation_date <= %s
                ORDER BY operation_date, id
                """,
                (self.owner_id, start_date, end_date),
            ).fetchall()
            return [_entry_row(row) for row in rows]

    def get_budget_entry(self, entry_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, owner_id, source, operation_hash,
                       export_sheet AS workbook_sheet, export_row AS workbook_row,
                       operation_date, operation_type, amount, category, subcategory,
                       name, note, bank, created_at, updated_at
                FROM budget_entries
                WHERE owner_id = %s AND id = %s
                """,
                (self.owner_id, entry_id),
            ).fetchone()
            return _entry_row(row) if row is not None else None

    def update_budget_entry(self, entry_id: int, operation: ParsedOperation) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE budget_entries
                SET operation_date = %s,
                    amount = %s,
                    category = %s,
                    subcategory = %s,
                    name = %s,
                    note = %s,
                    updated_at = %s
                WHERE owner_id = %s AND id = %s
                """,
                (
                    operation.date,
                    Decimal(f"{operation.excel_amount:.2f}"),
                    operation.category,
                    operation.subcategory,
                    operation.name,
                    operation.note,
                    now_iso(),
                    self.owner_id,
                    entry_id,
                ),
            )

    def delete_budget_entry(self, entry_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM budget_entries WHERE owner_id = %s AND id = %s",
                (self.owner_id, entry_id),
            )

    def shift_budget_entry_rows_after_delete(self, workbook_sheet: str, deleted_row: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE budget_entries
                SET export_row = export_row - 1,
                    updated_at = %s
                WHERE owner_id = %s AND export_sheet = %s AND export_row > %s
                """,
                (now_iso(), self.owner_id, workbook_sheet, deleted_row),
            )

    def register_telegram_chat(
        self,
        chat_id: int,
        user_id: Optional[int],
        reminder_enabled: bool,
        reminder_time: str,
        timezone_name: str,
    ) -> None:
        self.ensure_owner()
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_chats (chat_id, user_id, owner_id, created_at, last_seen_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    owner_id = excluded.owner_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (chat_id, user_id, self.owner_id, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO reminder_settings
                    (chat_id, enabled, time_local, timezone, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(chat_id) DO NOTHING
                """,
                (chat_id, 1 if reminder_enabled else 0, reminder_time, timezone_name, timestamp),
            )

    def update_reminder_enabled(self, chat_id: int, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE reminder_settings
                SET enabled = %s, updated_at = %s
                WHERE chat_id = %s
                """,
                (1 if enabled else 0, now_iso(), chat_id),
            )

    def reminder_settings(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rs.chat_id, rs.enabled, rs.time_local, rs.timezone
                FROM reminder_settings rs
                JOIN telegram_chats tc ON tc.chat_id = rs.chat_id
                WHERE tc.owner_id = %s
                ORDER BY rs.chat_id
                """,
                (self.owner_id,),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def reminder_sent(self, chat_id: int, reminder_date: date) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM reminder_deliveries
                WHERE chat_id = %s AND reminder_date = %s
                """,
                (chat_id, reminder_date),
            ).fetchone()
            return row is not None

    def mark_reminder_sent(self, chat_id: int, reminder_date: date) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reminder_deliveries (chat_id, reminder_date, sent_at)
                VALUES (%s, %s, %s)
                ON CONFLICT(chat_id, reminder_date) DO NOTHING
                """,
                (chat_id, reminder_date, now_iso()),
            )

    def save_expense_category_rule(self, merchant_name: str, category: str, subcategory: str) -> None:
        key = merchant_key(merchant_name)
        if not key:
            return
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO learned_expense_categories
                    (owner_id, merchant_key, merchant_name, category, subcategory, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_id, merchant_key) DO UPDATE SET
                    merchant_name = excluded.merchant_name,
                    category = excluded.category,
                    subcategory = excluded.subcategory,
                    updated_at = excluded.updated_at
                """,
                (self.owner_id, key, merchant_name.strip(), category, subcategory, timestamp, timestamp),
            )

    def get_expense_category_rule(self, merchant_name: str) -> Optional[tuple[str, str]]:
        key = merchant_key(merchant_name)
        if not key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT category, subcategory FROM learned_expense_categories
                WHERE owner_id = %s AND merchant_key = %s
                """,
                (self.owner_id, key),
            ).fetchone()
            if row is None:
                return None
            return row["category"], row["subcategory"]


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


def _row_dict(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def _money_row(row: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    if "total" in data:
        data["total"] = _float(data["total"])
    return data


def _entry_row(row: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    if "amount" in data:
        data["amount"] = _float(data["amount"])
    if isinstance(data.get("operation_date"), datetime):
        data["operation_date"] = data["operation_date"].date().isoformat()
    elif isinstance(data.get("operation_date"), date):
        data["operation_date"] = data["operation_date"].isoformat()
    return data
