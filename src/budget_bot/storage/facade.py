from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from psycopg.types.json import Jsonb

from ..categories import CategoryBook
from ..default_categories import DEFAULT_EXPENSE_CATEGORIES, DEFAULT_INCOME_CATEGORIES
from ..migrations.runner import upgrade_head
from ..models import OperationStatus, OperationType, ParsedOperation
from .connection import DbConnection
from .helpers import (
    DEFAULT_OWNER_ID,
    entry_row,
    float_value,
    image_hash,
    merchant_key,
    money_row,
    normalize_owner_id,
    now_iso,
    operation_from_json,
    operation_hash,
    operation_to_json,
    parse_operation_json_field,
    row_dict,
    telegram_owner_id,
)


@dataclass(frozen=True)
class OperationRecord:
    operation_hash: str
    image_hash: str
    bank: str
    operation: ParsedOperation
    status: OperationStatus
    workbook_row: Optional[int] = None
    status_note: str = ""


@dataclass(frozen=True)
class BudgetEntryInsert:
    source: str
    operation_hash: Optional[str]
    operation: ParsedOperation
    bank: str


class Storage(DbConnection):
    def __init__(self, database_url: str) -> None:
        super().__init__(database_url)
        self._init_db()

    @contextmanager
    def owner_scope(self, owner_id: str) -> Iterator[None]:
        with super().owner_scope(owner_id):
            self.ensure_owner()
            yield

    @contextmanager
    def _connect(self):
        with self.connect() as connection:
            yield connection

    def _init_db(self) -> None:
        upgrade_head(self.database_url)
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
        if storage_kind not in {"postgres"}:
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
            return row_dict(row)

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
        return operation_hash in self.existing_operation_hashes([operation_hash])

    def existing_operation_hashes(self, operation_hashes: Iterable[str]) -> Set[str]:
        hashes = list(operation_hashes)
        if not hashes:
            return set()
        self.ensure_owner()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT operation_hash FROM operations
                WHERE owner_id = %s AND operation_hash = ANY(%s)
                """,
                (self.owner_id, hashes),
            ).fetchall()
            return {row["operation_hash"] for row in rows}

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

    def record_operations_batch(self, records: Iterable[OperationRecord]) -> None:
        items = list(records)
        if not items:
            return
        self.ensure_owner()
        timestamp = now_iso()
        params: List[Any] = []
        value_rows: List[str] = []
        for record in items:
            value_rows.append("(%s, %s, %s, %s, %s, %s, %s, %s, %s)")
            params.extend(
                [
                    self.owner_id,
                    record.operation_hash,
                    record.image_hash,
                    record.bank,
                    Jsonb(operation_to_json(record.operation)),
                    record.status.value,
                    record.workbook_row,
                    record.status_note,
                    timestamp,
                ]
            )
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO operations
                    (owner_id, operation_hash, image_hash, bank, operation_json, status, workbook_row, status_note, created_at)
                VALUES {", ".join(value_rows)}
                ON CONFLICT(owner_id, operation_hash) DO NOTHING
                """,
                params,
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
            return row_dict(row)

    def get_operation(self, operation_hash: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE owner_id = %s AND operation_hash = %s",
                (self.owner_id, operation_hash),
            ).fetchone()
            data = row_dict(row)
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
            return [row_dict(row) for row in rows]

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
            return row_dict(row)

    def reset_all(self) -> None:
        with self._connect() as connection:
            for table in [
                "parse_jobs",
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

    def append_budget_entries_batch(self, entries: Iterable[BudgetEntryInsert]) -> List[int]:
        items = list(entries)
        if not items:
            return []
        timestamp = now_iso()
        params: List[Any] = []
        value_rows: List[str] = []
        for entry in items:
            resolved_hash = entry.operation_hash or f"manual:{self.owner_id}:{timestamp}:{entry.operation.name}"
            value_rows.append("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
            params.extend(
                [
                    self.owner_id,
                    entry.source,
                    resolved_hash,
                    entry.operation.date,
                    entry.operation.type.value,
                    Decimal(f"{entry.operation.excel_amount:.2f}"),
                    entry.operation.category,
                    entry.operation.subcategory,
                    entry.operation.name,
                    entry.operation.note,
                    entry.bank,
                    timestamp,
                    timestamp,
                ]
            )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                INSERT INTO budget_entries
                    (
                        owner_id, source, operation_hash, operation_date, operation_type,
                        amount, category, subcategory, name, note, bank, created_at, updated_at
                    )
                VALUES {", ".join(value_rows)}
                RETURNING id
                """,
                params,
            ).fetchall()
            return [int(row["id"]) for row in rows]

    def replace_budget_entries(self, entries: Iterable[Dict[str, Any]]) -> int:
        rows = list(entries)
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute("DELETE FROM budget_entries WHERE owner_id = %s", (self.owner_id,))
            if rows:
                params: List[Any] = []
                value_rows: List[str] = []
                for entry in rows:
                    value_rows.append("(" + ", ".join(["%s"] * 15) + ")")
                    params.extend(
                        [
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
                        ]
                    )
                connection.execute(
                    f"""
                    INSERT INTO budget_entries
                        (
                            owner_id, source, operation_hash, export_sheet, export_row,
                            operation_date, operation_type, amount, category, subcategory,
                            name, note, bank, created_at, updated_at
                        )
                    VALUES {", ".join(value_rows)}
                    """,
                    params,
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
            "total": float_value(total_row["total"]),
            "count": int(total_row["count"] or 0),
            "categories": [money_row(row) for row in categories],
            "subcategories": [money_row(row) for row in subcategories],
        }

    def expense_daily_totals(
        self,
        start_date: date,
        end_date: date,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
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
            rows = connection.execute(
                f"""
                SELECT operation_date, COALESCE(SUM(amount), 0) AS total
                FROM budget_entries
                WHERE {where}
                GROUP BY operation_date
                ORDER BY operation_date
                """,
                params,
            ).fetchall()
            return [money_row(row) for row in rows]

    def budget_entries(
        self,
        start_date: date,
        end_date: date,
        operation_type: OperationType = OperationType.EXPENSE,
        category: Optional[str] = None,
        limit: int = 30,
        offset: int = 0,
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
        params.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, owner_id, source, operation_hash,
                       export_sheet AS workbook_sheet, export_row AS workbook_row,
                       operation_date, operation_type, amount, category, subcategory,
                       name, note, bank, created_at, updated_at
                FROM budget_entries
                WHERE {" AND ".join(clauses)}
                ORDER BY operation_date DESC, id ASC
                LIMIT %s OFFSET %s
                """,
                params,
            ).fetchall()
            return [entry_row(row) for row in rows]

    def count_budget_entries(
        self,
        start_date: date,
        end_date: date,
        operation_type: OperationType = OperationType.EXPENSE,
        category: Optional[str] = None,
    ) -> int:
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
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total FROM budget_entries
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchone()
            return int(row["total"]) if row is not None else 0

    def find_budget_entries(
        self,
        start_date: date,
        end_date: date,
        query: str,
        operation_type: OperationType = OperationType.EXPENSE,
        category: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []
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
        if normalized.isdigit():
            clauses.append("id = %s")
            params.append(int(normalized))
        else:
            pattern = f"%{normalized}%"
            clauses.append("(name ILIKE %s OR note ILIKE %s)")
            params.extend([pattern, pattern])
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
                ORDER BY operation_date DESC, id ASC
                LIMIT %s
                """,
                params,
            ).fetchall()
            return [entry_row(row) for row in rows]

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
            return [entry_row(row) for row in rows]

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
            return entry_row(row) if row is not None else None

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
            return [row_dict(row) for row in rows]

    def reminder_sent(self, chat_id: int, reminder_date: date) -> bool:
        return chat_id in self.reminder_sent_chat_ids([chat_id], reminder_date)

    def reminder_sent_chat_ids(self, chat_ids: Iterable[int], reminder_date: date) -> Set[int]:
        ids = list(chat_ids)
        if not ids:
            return set()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chat_id FROM reminder_deliveries
                WHERE reminder_date = %s AND chat_id = ANY(%s)
                """,
                (reminder_date, ids),
            ).fetchall()
            return {int(row["chat_id"]) for row in rows}

    def mark_reminder_sent(self, chat_id: int, reminder_date: date) -> None:
        self.mark_reminders_sent([(chat_id, reminder_date)])

    def mark_reminders_sent(self, deliveries: Iterable[tuple[int, date]]) -> None:
        items = list(deliveries)
        if not items:
            return
        timestamp = now_iso()
        params: List[Any] = []
        value_rows: List[str] = []
        for chat_id, reminder_date in items:
            value_rows.append("(%s, %s, %s)")
            params.extend([chat_id, reminder_date, timestamp])
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO reminder_deliveries (chat_id, reminder_date, sent_at)
                VALUES {", ".join(value_rows)}
                ON CONFLICT(chat_id, reminder_date) DO NOTHING
                """,
                params,
            )

    def expense_category_rules(self) -> Dict[str, tuple[str, str]]:
        self.ensure_owner()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT merchant_key, category, subcategory FROM learned_expense_categories
                WHERE owner_id = %s
                """,
                (self.owner_id,),
            ).fetchall()
            return {row["merchant_key"]: (row["category"], row["subcategory"]) for row in rows}

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

    def enqueue_parse_job(
        self,
        chat_id: int,
        job_kind: str,
        payload: Dict[str, Any],
        max_attempts: int = 3,
    ) -> int:
        self.ensure_owner()
        timestamp = now_iso()
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO parse_jobs
                    (owner_id, chat_id, status, job_kind, payload, attempts, max_attempts,
                     created_at, updated_at)
                VALUES (%s, %s, 'queued', %s, %s, 0, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.owner_id,
                    chat_id,
                    job_kind,
                    Jsonb(payload),
                    max_attempts,
                    timestamp,
                    timestamp,
                ),
            ).fetchone()
            return int(row["id"])

    def claim_next_parse_job(self) -> Optional[Dict[str, Any]]:
        timestamp = now_iso()
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE parse_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    started_at = %s,
                    updated_at = %s
                WHERE id = (
                    SELECT id FROM parse_jobs
                    WHERE status = 'queued' AND attempts < max_attempts
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING *
                """,
                (timestamp, timestamp),
            ).fetchone()
            return row_dict(row)

    def finish_parse_job(self, job_id: int, status: str, error_message: Optional[str] = None) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE parse_jobs
                SET status = %s,
                    error_message = %s,
                    finished_at = %s,
                    updated_at = %s
                WHERE id = %s AND status = 'running'
                """,
                (status, error_message, timestamp, timestamp, job_id),
            )

    def requeue_failed_parse_job(self, job_id: int, error_message: str) -> None:
        timestamp = now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts, max_attempts FROM parse_jobs WHERE id = %s AND status = 'running'",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            status = "queued" if attempts < max_attempts else "failed"
            connection.execute(
                """
                UPDATE parse_jobs
                SET status = %s,
                    error_message = %s,
                    updated_at = %s,
                    finished_at = CASE WHEN %s = 'failed' THEN %s ELSE finished_at END
                WHERE id = %s AND status = 'running'
                """,
                (status, error_message, timestamp, status, timestamp, job_id),
            )

    def get_parse_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM parse_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
            return row_dict(row)
