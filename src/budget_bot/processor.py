from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Dict, List, Optional, Tuple

from .categories import CategoryBook, apply_keyword_rules
from .models import OperationStatus, OperationType, ParsedOperation, ParsedScreenshot
from .storage import Storage, image_hash as make_image_hash, operation_hash as make_operation_hash
from .storage.facade import BudgetEntryInsert, OperationRecord
from .storage.helpers import merchant_key


@dataclass(frozen=True)
class OperationDecision:
    operation: ParsedOperation
    status: OperationStatus
    reason: str
    workbook_row: Optional[int] = None


@dataclass(frozen=True)
class ProcessingResult:
    image_hash: str
    bank: str
    decisions: List[OperationDecision]


class ScreenshotProcessor:
    def __init__(
        self,
        storage: Storage,
        category_book: CategoryBook,
    ) -> None:
        self.storage = storage
        self.category_book = category_book

    def process(
        self,
        image_content: bytes,
        parsed: ParsedScreenshot,
        telegram_file_id: Optional[str] = None,
    ) -> ProcessingResult:
        image_hash = make_image_hash(image_content)
        if self.storage.image_seen(image_hash):
            return ProcessingResult(image_hash=image_hash, bank=parsed.bank, decisions=[])

        self.storage.record_image(
            image_hash=image_hash,
            telegram_file_id=telegram_file_id,
            bank=parsed.bank,
            status="processing",
            raw_response=parsed.raw,
        )

        decisions: List[OperationDecision] = []
        try:
            learned_rules = self.storage.expense_category_rules()
            grouped_operations = self._group_same_operations(parsed.operations, learned_rules)
            operation_hashes = [make_operation_hash(parsed.bank, operation) for operation in grouped_operations]
            existing_hashes = self.storage.existing_operation_hashes(operation_hashes)
            existing_entries = self._existing_entries_for_operations(grouped_operations)

            pending: List[Tuple[ParsedOperation, str, OperationStatus, str, Optional[int]]] = []
            budget_requests: List[Tuple[int, str, ParsedOperation]] = []

            for operation, operation_hash in zip(grouped_operations, operation_hashes):
                if operation_hash in existing_hashes or _matches_existing_entry(parsed.bank, operation, existing_entries):
                    decisions.append(OperationDecision(operation, OperationStatus.IGNORED, "duplicate"))
                    continue

                status, reason = self._decide(operation)
                record_index = len(pending)
                pending.append((operation, operation_hash, status, reason, None))
                if status == OperationStatus.AUTO_WRITTEN:
                    budget_requests.append((record_index, operation_hash, operation))

            if budget_requests:
                budget_ids = self.storage.append_budget_entries_batch(
                    [
                        BudgetEntryInsert(
                            source="bot",
                            operation_hash=operation_hash,
                            operation=operation,
                            bank=parsed.bank,
                        )
                        for _record_index, operation_hash, operation in budget_requests
                    ]
                )
                for (record_index, _operation_hash, _), budget_entry_id in zip(budget_requests, budget_ids):
                    operation, operation_hash, status, reason, _ = pending[record_index]
                    pending[record_index] = (operation, operation_hash, status, reason, budget_entry_id)

            if pending:
                self.storage.record_operations_batch(
                    [
                        OperationRecord(
                            operation_hash=operation_hash,
                            image_hash=image_hash,
                            bank=parsed.bank,
                            operation=operation,
                            status=status,
                            workbook_row=budget_entry_id,
                            status_note=reason,
                        )
                        for operation, operation_hash, status, reason, budget_entry_id in pending
                    ]
                )

            for operation, operation_hash, status, reason, budget_entry_id in pending:
                decisions.append(OperationDecision(operation, status, reason, budget_entry_id))
        except Exception:
            self.storage.update_image_status(image_hash, "failed")
            raise

        self.storage.update_image_status(image_hash, "processed")

        return ProcessingResult(image_hash=image_hash, bank=parsed.bank, decisions=decisions)

    def _apply_local_rules(
        self,
        operation: ParsedOperation,
        learned_rules: Dict[str, Tuple[str, str]],
    ) -> ParsedOperation:
        if operation.type != OperationType.EXPENSE:
            return operation
        key = merchant_key(operation.name)
        mapping = learned_rules.get(key) if key else None
        if mapping is None:
            mapping = apply_keyword_rules(operation.name, self.category_book)
        if mapping is None:
            mapping = apply_keyword_rules(
                " ".join(
                    item
                    for item in [operation.name, operation.category or "", operation.subcategory or ""]
                    if item
                ),
                self.category_book,
            )
        if mapping is None:
            return operation
        category, subcategory = mapping
        if not self.category_book.is_valid_expense(category, subcategory):
            return operation
        return ParsedOperation(
            date=operation.date,
            name=operation.name,
            amount=operation.amount,
            type=operation.type,
            category=category,
            subcategory=subcategory,
            confidence=operation.confidence,
            needs_review=False,
            note=operation.note,
            date_missing=operation.date_missing,
            occurrence_count=operation.occurrence_count,
            occurrence_confirmed=operation.occurrence_confirmed,
        )

    def _decide(self, operation: ParsedOperation) -> tuple[OperationStatus, str]:
        if _is_cashback_or_bonus(operation):
            return OperationStatus.IGNORED, "cashback or bonus ignored"

        if _is_internal_transfer(operation):
            return OperationStatus.IGNORED, "internal transfer ignored"

        if operation.date_missing:
            return OperationStatus.PENDING, "operation date missing"

        if operation.occurrence_count > 1 and not operation.occurrence_confirmed:
            return OperationStatus.PENDING, f"same operation appears {operation.occurrence_count} times"

        if operation.type == OperationType.INCOME:
            if operation.amount <= 0:
                return OperationStatus.PENDING, "income amount must be positive"
            return OperationStatus.PENDING, "income manual decision required"

        if operation.type in {OperationType.TRANSFER, OperationType.IGNORE}:
            return OperationStatus.PENDING, "manual decision required"

        if operation.needs_review:
            return OperationStatus.PENDING, "operation needs review"

        if operation.type == OperationType.EXPENSE:
            if operation.amount >= 0:
                return OperationStatus.PENDING, "expense amount must be negative"
            if not self.category_book.is_valid_expense(operation.category, operation.subcategory):
                return OperationStatus.PENDING, "unknown expense category or subcategory"

        return OperationStatus.AUTO_WRITTEN, "auto written"

    def _group_same_operations(
        self,
        operations: List[ParsedOperation],
        learned_rules: Dict[str, Tuple[str, str]],
    ) -> List[ParsedOperation]:
        grouped: Dict[tuple[str, str, str, str], tuple[ParsedOperation, int]] = {}
        order: List[tuple[str, str, str, str]] = []
        for operation in operations:
            operation = self._apply_local_rules(operation, learned_rules)
            key = (
                operation.date.isoformat(),
                operation.type.value,
                f"{operation.amount:.2f}",
                operation.name.casefold().strip(),
            )
            if key not in grouped:
                grouped[key] = (operation, 0)
                order.append(key)
            first, count = grouped[key]
            grouped[key] = (first, count + 1)

        result: List[ParsedOperation] = []
        for key in order:
            operation, count = grouped[key]
            if count == 1:
                result.append(operation)
                continue
            result.append(
                ParsedOperation(
                    date=operation.date,
                    name=operation.name,
                    amount=operation.amount,
                    type=operation.type,
                    category=operation.category,
                    subcategory=operation.subcategory,
                    confidence=operation.confidence,
                    needs_review=operation.needs_review,
                    note=operation.note,
                    date_missing=operation.date_missing,
                    occurrence_count=count,
                    occurrence_confirmed=False,
                )
            )
        return result

    def _existing_entries_for_operations(self, operations: List[ParsedOperation]) -> List[Dict[str, object]]:
        dates = [operation.date for operation in operations if not operation.date_missing]
        if not dates:
            return []
        return self.storage.all_budget_entries(min(dates), max(dates))


def _is_cashback_or_bonus(operation: ParsedOperation) -> bool:
    text = " ".join(
        item
        for item in [
            operation.name,
            operation.category or "",
            operation.subcategory or "",
            operation.note,
        ]
        if item
    ).casefold()
    cashback_markers = [
        "cashback",
        "cash back",
        "кэшбэк",
        "кешбэк",
        "кэшбек",
        "кешбек",
        "бонус",
    ]
    if any(marker in text for marker in cashback_markers):
        return True
    if operation.amount > 0 and operation.name.casefold().strip() in {"дебетовая карта", "black"}:
        return True
    return False


def _matches_existing_entry(bank: str, operation: ParsedOperation, entries: List[Dict[str, object]]) -> bool:
    if operation.date_missing:
        return False
    normalized_bank = str(bank or "").strip().lower()
    operation_cents = _amount_cents(operation.amount)
    for entry in entries:
        entry_bank = str(entry.get("bank") or "").strip().lower()
        if entry_bank and normalized_bank and entry_bank != normalized_bank:
            continue
        if str(entry.get("operation_date")) != operation.date.isoformat():
            continue
        if str(entry.get("operation_type")) != operation.type.value:
            continue
        if _amount_cents(entry.get("amount")) != operation_cents:
            continue
        if _names_similar(operation.name, str(entry.get("name") or "")):
            return True
    return False


def _amount_cents(value: object) -> int:
    return int(round(abs(float(value or 0)) * 100))


def _names_similar(left: str, right: str) -> bool:
    left_key = _duplicate_name_key(left)
    right_key = _duplicate_name_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted([left_key, right_key], key=len)
    if len(shorter) >= 5 and shorter in longer:
        return True
    return SequenceMatcher(None, left_key, right_key).ratio() >= 0.84


_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


def _duplicate_name_key(name: str) -> str:
    key = merchant_key(name).replace("ё", "е").translate(_CYRILLIC_TO_LATIN)
    key = key.replace("yandeks", "yandex")
    return re.sub(r"[^a-z0-9]+", "", key)


def _is_internal_transfer(operation: ParsedOperation) -> bool:
    if operation.type != OperationType.TRANSFER:
        return False
    text = " ".join(
        item
        for item in [
            operation.name,
            operation.category or "",
            operation.subcategory or "",
            operation.note,
        ]
        if item
    ).casefold()
    return any(
        marker in text
        for marker in [
            "между своими счетами",
            "между своими счётами",
            "между счетами",
            "между счётами",
            "своими счетами",
            "своими счётами",
            "перевод себе",
            "перевод на свой",
            "себе в другой банк",
            "own accounts",
            "between own accounts",
        ]
    )
