from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .categories import CategoryBook
from .excel_writer import EXPENSE_SHEET, INCOME_SHEET, WriteResult
from .models import OperationType, ParsedOperation


EXPENSE_COLUMNS = 8
INCOME_COLUMNS = 10


class GoogleSheetsBudget:
    def __init__(self, spreadsheet_url: str, credentials_path: Path) -> None:
        self.spreadsheet_url = spreadsheet_url
        self.credentials_path = credentials_path
        self._lock = threading.Lock()
        self._spreadsheet = None

    @property
    def spreadsheet(self):
        if self._spreadsheet is None:
            try:
                import gspread
            except ImportError as exc:
                raise RuntimeError("Install gspread to use Google Sheets storage") from exc
            client = gspread.service_account(filename=str(self.credentials_path))
            self._spreadsheet = client.open_by_url(self.spreadsheet_url)
        return self._spreadsheet

    def category_book(self) -> CategoryBook:
        values = self._worksheet("Справочники").get_all_values()
        if not values:
            raise ValueError("Google Sheet must contain 'Справочники' headers")
        headers = values[0]
        expense_categories: Dict[str, List[str]] = {}
        income_categories: List[str] = []
        for column_index, header in enumerate(headers):
            header = header.strip()
            if not header:
                continue
            column_values = [
                row[column_index].strip()
                for row in values[1:]
                if column_index < len(row) and row[column_index].strip()
            ]
            if header == "Доход":
                income_categories = column_values
            elif column_values:
                expense_categories[header] = column_values
        return CategoryBook(expense_categories=expense_categories, income_categories=income_categories)

    def append_operation(self, operation: ParsedOperation, bank: str) -> WriteResult:
        with self._lock:
            if operation.type == OperationType.EXPENSE:
                worksheet = self._worksheet(EXPENSE_SHEET)
                row = _next_row(worksheet)
                worksheet.append_rows(
                    [_expense_row(row, operation, bank)],
                    value_input_option="USER_ENTERED",
                )
                return WriteResult(sheet_name=EXPENSE_SHEET, row=row)
            if operation.type == OperationType.INCOME:
                worksheet = self._worksheet(INCOME_SHEET)
                row = _next_row(worksheet)
                worksheet.append_rows(
                    [_income_row(row, operation, bank)],
                    value_input_option="USER_ENTERED",
                )
                return WriteResult(sheet_name=INCOME_SHEET, row=row)
            raise ValueError(f"Cannot write operation type {operation.type.value!r} to Google Sheets")

    def reset_workbook(self) -> None:
        with self._lock:
            self._clear_data_rows(EXPENSE_SHEET, EXPENSE_COLUMNS)
            self._clear_data_rows(INCOME_SHEET, INCOME_COLUMNS)

    def update_entry(self, sheet_name: str, row: int, operation: ParsedOperation, bank: str) -> None:
        with self._lock:
            if sheet_name == EXPENSE_SHEET:
                values = [_expense_row(row, operation, bank)]
                columns = EXPENSE_COLUMNS
            elif sheet_name == INCOME_SHEET:
                values = [_income_row(row, operation, bank)]
                columns = INCOME_COLUMNS
            else:
                raise ValueError(f"Unknown budget sheet: {sheet_name!r}")
            self._worksheet(sheet_name).update(values, range_name=f"A{row}:{_column_letter(columns)}{row}", raw=False)

    def delete_entry(self, sheet_name: str, row: int):
        worksheet = self._worksheet(sheet_name)
        worksheet.delete_rows(row)
        from .excel_writer import DeleteResult

        return DeleteResult(shifted_rows=True)

    def read_budget_entries(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        entries.extend(_read_expense_entries(self._worksheet(EXPENSE_SHEET).get_all_values()))
        entries.extend(_read_income_entries(self._worksheet(INCOME_SHEET).get_all_values()))
        return entries

    def sync_to_storage(self, storage) -> int:
        return storage.replace_budget_entries(self.read_budget_entries())

    def _worksheet(self, title: str):
        spreadsheet = self.spreadsheet
        try:
            return spreadsheet.worksheet(title)
        except Exception as exc:
            raise ValueError(f"Google Sheet must contain {title!r} worksheet") from exc

    def _clear_data_rows(self, title: str, columns: int) -> None:
        worksheet = self._worksheet(title)
        row_count = len(worksheet.get_all_values())
        if row_count <= 1:
            return
        worksheet.batch_clear([f"A2:{_column_letter(columns)}{row_count}"])


def _next_row(worksheet) -> int:
    return max(len(worksheet.get_all_values()) + 1, 2)


def _expense_row(row: int, operation: ParsedOperation, bank: str) -> List[Any]:
    return [
        f'=TEXT(C{row},"mmmm")',
        f"=YEAR(C{row})",
        operation.date.isoformat(),
        operation.category or "",
        operation.subcategory or "",
        operation.note or operation.name,
        operation.excel_amount,
        _comment(bank, operation),
    ]


def _income_row(row: int, operation: ParsedOperation, bank: str) -> List[Any]:
    return [
        f'=TEXT(C{row},"mmmm")',
        f"=YEAR(C{row})",
        operation.date.isoformat(),
        operation.name,
        operation.category or "",
        operation.note,
        operation.excel_amount,
        "",
        f"=G{row}-H{row}",
        _comment(bank, operation),
    ]


def _read_expense_entries(rows: Iterable[List[str]]) -> Iterable[Dict[str, Any]]:
    for index, row in enumerate(list(rows)[1:], start=2):
        operation_date = _parse_date(_value(row, 2))
        amount = _parse_amount(_value(row, 6))
        if operation_date is None or amount is None:
            continue
        yield {
            "source": "google_sheets_sync",
            "operation_hash": None,
            "workbook_sheet": EXPENSE_SHEET,
            "workbook_row": index,
            "operation_date": operation_date.isoformat(),
            "operation_type": OperationType.EXPENSE.value,
            "amount": abs(amount),
            "category": _value(row, 3),
            "subcategory": _value(row, 4),
            "name": _value(row, 5),
            "note": _value(row, 7),
            "bank": "",
        }


def _read_income_entries(rows: Iterable[List[str]]) -> Iterable[Dict[str, Any]]:
    for index, row in enumerate(list(rows)[1:], start=2):
        operation_date = _parse_date(_value(row, 2))
        amount = _parse_amount(_value(row, 6))
        if operation_date is None or amount is None:
            continue
        yield {
            "source": "google_sheets_sync",
            "operation_hash": None,
            "workbook_sheet": INCOME_SHEET,
            "workbook_row": index,
            "operation_date": operation_date.isoformat(),
            "operation_type": OperationType.INCOME.value,
            "amount": abs(amount),
            "category": _value(row, 4),
            "subcategory": None,
            "name": _value(row, 3),
            "note": _value(row, 9),
            "bank": "",
        }


def _value(row: List[str], index: int) -> str:
    return str(row[index]).strip() if index < len(row) else ""


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            return None


def _parse_amount(value: str) -> Optional[float]:
    text = value.strip().replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _column_letter(column: int) -> str:
    result = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _comment(bank: str, operation: ParsedOperation) -> str:
    name = operation.name.strip()
    if name:
        return f"auto: {bank} / {name}"
    return f"auto: {bank}"
