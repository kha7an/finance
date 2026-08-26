from __future__ import annotations

import mimetypes
import threading
import time
from collections import defaultdict
from datetime import date, datetime, time as local_time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .app_factory import AppContext
from .excel_exporter import ExcelExporter
from .excel_writer import EXPENSE_SHEET, INCOME_SHEET
from .log_config import get_logger, log_extra
from .models import OperationStatus, OperationType, ParsedOperation
from .parse_worker import ParseJobWorker
from .storage import image_hash, operation_from_json, telegram_owner_id
from .storage.facade import BudgetEntryInsert
from .telegram_api import (
    TELEGRAM_POLLING_CONFLICT_SLEEP_SECONDS,
    TELEGRAM_POLLING_ERROR_SLEEP_SECONDS,
    TelegramApiClient,
    TelegramApiError,
)
from .telegram_common import (
    button_rows as _button_rows,
    format_money as _format_money,
    operation_summary_text as _operation_summary_text,
    parse_index as _parse_index,
    parse_user_operation_date as _parse_user_operation_date,
)
from .telegram_entries import ENTRY_ACTIONS, TelegramEntryEditor, expense_report_keyboard, is_entry_edit_pending, is_entry_search_pending
from .telegram_manual import (
    MANUAL_ACTIONS,
    TelegramManualEntry,
    is_manual_pending,
    parse_manual_operation_text as _parse_manual_operation_text,
)
from .telegram_reports import expense_report_lines as _expense_report_lines
from .telegram_reports import parse_stats_period as _parse_stats_period
from .telegram_reports import chart_period_payload as _chart_period_payload
from .telegram_reports import parse_chart_period_payload as _parse_chart_period_payload
from .telegram_charts import render_expense_chart


logger = get_logger(__name__)

MEDIA_GROUP_SETTLE_SECONDS = 2.0
REMINDER_CHECK_SECONDS = 60.0 * 60


class TelegramBot:
    def __init__(self, context: AppContext) -> None:
        if not context.settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not context.settings.telegram_allowed_user_ids and not context.settings.telegram_allow_all:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS is required, or set TELEGRAM_ALLOW_ALL=true")
        self.context = context
        self.token = context.settings.telegram_bot_token
        self._last_reminder_check = 0.0
        self.api_client = TelegramApiClient(self)
        self.media_groups: Dict[str, Dict[str, Any]] = {}
        self.parse_jobs = ParseJobWorker(self)
        self._parse_worker_stop = threading.Event()
        self._parse_worker_thread: Optional[threading.Thread] = None
        self._preview_summary_messages: Dict[str, tuple[int, int]] = {}

    @property
    def timeout(self) -> int:
        return self.api_client.timeout

    def run_polling(self) -> None:
        while True:
            try:
                me = self.check_connection()
                username = me.get("username") or me.get("first_name") or me.get("id")
                logger.info("telegram polling started for @%s", username)
                print(f"Telegram polling started for @{username}. Press Ctrl+C to stop.", flush=True)
                self._api("deleteWebhook", {"drop_pending_updates": False})
                self._start_parse_worker()
                break
            except Exception as exc:
                logger.exception("telegram polling startup error")
                time.sleep(self._polling_error_sleep_seconds(exc))

        offset: Optional[int] = None
        while True:
            try:
                poll_timeout = 1 if self.media_groups else 30
                updates = self._api(
                    "getUpdates",
                    {"timeout": poll_timeout, "offset": offset},
                    timeout=max(self.api_client.timeout, poll_timeout + 5),
                )
                for update in updates.get("result", []):
                    offset = update["update_id"] + 1
                    self._log_update(update)
                    self.handle_update(update)
                self._flush_ready_media_groups()
                self._send_due_reminders()
                time.sleep(0.5)
            except Exception as exc:
                logger.exception("telegram polling error")
                time.sleep(self._polling_error_sleep_seconds(exc))

    def _start_parse_worker(self) -> None:
        if self._parse_worker_thread and self._parse_worker_thread.is_alive():
            return

        def run() -> None:
            while not self._parse_worker_stop.is_set():
                try:
                    processed = self.parse_jobs.process_next_job()
                    if not processed:
                        self._parse_worker_stop.wait(1.0)
                except Exception:
                    logger.exception("parse worker loop error")
                    self._parse_worker_stop.wait(5.0)

        self._parse_worker_thread = threading.Thread(target=run, name="parse-job-worker", daemon=True)
        self._parse_worker_thread.start()

    def check_connection(self) -> Dict[str, Any]:
        return self._api("getMe", {})["result"]

    def handle_update(self, update: Dict[str, Any]) -> None:
        owner_id = telegram_owner_id(_telegram_user_id_from_update(update))
        with self.context.owner_scope(owner_id):
            self._handle_update_scoped(update)

    def _handle_update_scoped(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        if not self._is_allowed(user_id):
            logger.warning(
                "telegram user rejected",
                extra=log_extra(chat_id=chat_id, user_id=user_id, status="rejected"),
            )
            self._send_message(chat_id, "Этот бот принимает сообщения только от владельца.")
            return
        self.context.storage.register_telegram_chat(
            chat_id=chat_id,
            user_id=user_id,
            reminder_enabled=self.context.settings.reminder_enabled,
            reminder_time=self.context.settings.reminder_default_time,
            timezone_name=self.context.settings.default_timezone,
        )
        if "photo" in message:
            self._handle_photo(message)
            return
        if "text" in message:
            self._handle_text(message)
            return
        self._send_message(chat_id, "Пришли скриншот истории операций.", reply_markup=self._main_reply_keyboard())

    def _handle_photo(self, message: Dict[str, Any]) -> None:
        if message.get("media_group_id"):
            self._queue_media_group_photo(message)
            return

        chat_id = message["chat"]["id"]
        photo = message["photo"][-1]
        file_id = photo["file_id"]
        try:
            job_id = self.parse_jobs.enqueue_single(chat_id, file_id, date.today())
            logger.info(
                "parse job queued",
                extra=log_extra(
                    owner_id=self.context.owner_id,
                    chat_id=chat_id,
                    job_id=job_id,
                    status="queued",
                ),
            )
            self._send_message(chat_id, "Обрабатываю скрин...")
        except Exception as exc:
            logger.exception("telegram photo enqueue error", extra=log_extra(chat_id=chat_id))
            self._send_message(chat_id, f"Не смог поставить скрин в очередь: {exc}")

    def _queue_media_group_photo(self, message: Dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        media_group_id = str(message["media_group_id"])
        group_key = f"{chat_id}:{media_group_id}"
        group = self.media_groups.setdefault(
            group_key,
            {
                "chat_id": chat_id,
                "media_group_id": media_group_id,
                "owner_id": self.context.owner_id,
                "messages": [],
                "updated_at": time.monotonic(),
            },
        )
        group["messages"].append(message)
        group["updated_at"] = time.monotonic()
        logger.info(
            "telegram album queued",
            extra=log_extra(
                chat_id=chat_id,
                media_group_id=media_group_id,
                status="queued",
            ),
        )

    def _flush_ready_media_groups(self) -> None:
        now = time.monotonic()
        ready_keys = [
            key
            for key, group in self.media_groups.items()
            if now - float(group["updated_at"]) >= MEDIA_GROUP_SETTLE_SECONDS
        ]
        for key in ready_keys:
            group = self.media_groups.pop(key)
            owner_id = str(group.get("owner_id") or "")
            if owner_id:
                with self.context.owner_scope(owner_id):
                    self._handle_photo_group(group)
            else:
                self._handle_photo_group(group)

    def _handle_photo_group(self, group: Dict[str, Any]) -> None:
        chat_id = int(group["chat_id"])
        media_group_id = str(group["media_group_id"])
        messages = list(group["messages"])
        file_ids = [message["photo"][-1]["file_id"] for message in messages]
        try:
            job_id = self.parse_jobs.enqueue_album(chat_id, file_ids, media_group_id, date.today())
            logger.info(
                "parse album job queued",
                extra=log_extra(
                    owner_id=self.context.owner_id,
                    chat_id=chat_id,
                    media_group_id=media_group_id,
                    job_id=job_id,
                    status="queued",
                ),
            )
            self._send_message(chat_id, f"Обрабатываю альбом из {len(file_ids)} скринов...")
        except Exception as exc:
            logger.exception(
                "telegram album enqueue error",
                extra=log_extra(chat_id=chat_id, media_group_id=media_group_id),
            )
            self._send_message(chat_id, f"Не смог поставить альбом в очередь: {exc}")

    def _handle_callback(self, callback: Dict[str, Any]) -> None:
        chat_id = callback["message"]["chat"]["id"]
        user_id = callback.get("from", {}).get("id")
        if not self._is_allowed(user_id):
            self._answer_callback(callback["id"], "Нет доступа")
            return

        try:
            action, payload = callback.get("data", "").split(":", 1)
        except ValueError:
            self._answer_callback(callback["id"], "Не понял действие")
            return

        if action in {"reset", "reset_confirm", "reset_cancel"}:
            self._handle_reset_callback(callback, chat_id, action)
            return
        if action in ENTRY_ACTIONS:
            self._entry_editor().handle_callback(callback, chat_id, action, payload)
            return
        if action in {"menu", "stats", "statscat", "sync", "analytics", "chart"}:
            self._handle_stats_callback(callback, chat_id, action, payload)
            return
        if action in MANUAL_ACTIONS:
            self._handle_manual_callback(callback, chat_id, action, payload)
            return

        operation_hash = _operation_hash_from_callback(action, payload)
        if operation_hash is None:
            self._answer_callback(callback["id"], "Не понял действие")
            return

        row = self.context.storage.get_operation(operation_hash)
        if not _can_decide_operation(row):
            self.context.storage.delete_pending_action(operation_hash)
            self._answer_callback(callback["id"], "Операция уже закрыта")
            self._delete_callback_message(callback)
            return
        operation = operation_from_json(row["operation_json"])
        if operation.date_missing and action != "skip":
            self.context.storage.add_pending_action(
                operation_hash=operation_hash,
                chat_id=chat_id,
                message_id=callback["message"].get("message_id"),
                prompt="Напиши дату операции в формате 22.08",
            )
            self._answer_callback(callback["id"], "Сначала нужна дата")
            self._delete_callback_message(callback)
            self._send_message(chat_id, "На скрине не вижу дату операции. Напиши дату в формате 22.08")
            return
        if _needs_quantity(operation) and action != "qty":
            self.context.storage.add_pending_action(
                operation_hash=operation_hash,
                chat_id=chat_id,
                message_id=callback["message"].get("message_id"),
                prompt="Выбери количество одинаковых операций",
            )
            self._answer_callback(callback["id"], "Нужно количество")
            self._delete_callback_message(callback)
            self._send_quantity_picker(chat_id, operation_hash, operation)
            return

        if action == "skip":
            image_hash_value = row["image_hash"]
            self.context.storage.update_operation_status(
                operation_hash,
                OperationStatus.IGNORED,
                status_note="ignored by user",
            )
            self.context.storage.delete_pending_action(operation_hash)
            self._answer_callback(callback["id"], "Пропущено")
            self._delete_callback_message(callback)
            self._send_final_summary_if_ready(chat_id, image_hash_value)
            return

        if action == "count":
            self.context.storage.add_pending_action(
                operation_hash=operation_hash,
                chat_id=chat_id,
                message_id=callback["message"].get("message_id"),
                prompt="Выбери категорию",
            )
            self._answer_callback(callback["id"], "Выбери категорию")
            self._delete_callback_message(callback)
            self._send_category_picker(chat_id, operation_hash)
            return

        if action == "inc":
            self._answer_callback(callback["id"], "Считаю доход")
            self._delete_callback_message(callback)
            self._write_income_operation(chat_id, row, operation_hash)
            return

        if action == "incat":
            category_index = _parse_index(payload, 1)
            category = self._income_category_by_index(category_index)
            if category is None:
                self._answer_callback(callback["id"], "Категория устарела")
                self._delete_callback_message(callback)
                self._send_income_category_picker(chat_id, operation_hash)
                return
            self._answer_callback(callback["id"], category)
            self._delete_callback_message(callback)
            self._write_income_operation(chat_id, row, operation_hash, category=category)
            return

        if action == "back":
            self.context.storage.add_pending_action(
                operation_hash=operation_hash,
                chat_id=chat_id,
                message_id=callback["message"].get("message_id"),
                prompt="Выбери категорию",
            )
            self._answer_callback(callback["id"], "Выбери категорию")
            self._delete_callback_message(callback)
            self._send_category_picker(chat_id, operation_hash)
            return

        if action == "cat":
            category_index = _parse_index(payload, 1)
            category_item = self._category_by_index(category_index)
            if category_item is None:
                self._answer_callback(callback["id"], "Категория устарела")
                self._delete_callback_message(callback)
                self._send_category_picker(chat_id, operation_hash)
                return

            category, _subcategories = category_item
            self.context.storage.add_pending_action(
                operation_hash=operation_hash,
                chat_id=chat_id,
                message_id=callback["message"].get("message_id"),
                prompt=f"Выбери подкатегорию: {category}",
            )
            self._answer_callback(callback["id"], category)
            self._delete_callback_message(callback)
            self._send_subcategory_picker(chat_id, operation_hash, category_index)
            return

        if action == "sub":
            category_index = _parse_index(payload, 1)
            subcategory_index = _parse_index(payload, 2)
            pair = self._subcategory_by_index(category_index, subcategory_index)
            if pair is None:
                self._answer_callback(callback["id"], "Подкатегория устарела")
                self._delete_callback_message(callback)
                self._send_category_picker(chat_id, operation_hash)
                return

            category, subcategory = pair
            self._answer_callback(callback["id"], f"{category} / {subcategory}")
            self._delete_callback_message(callback)
            self._write_counted_operation(chat_id, row, operation_hash, category, subcategory)
            return

        if action == "qty":
            count = _parse_index(payload, 1)
            self._answer_callback(callback["id"], "Количество выбрано")
            self._delete_callback_message(callback)
            self._handle_quantity_choice(chat_id, row, operation_hash, count)
            return

        self._answer_callback(callback["id"], "Не понял действие")

    def _handle_text(self, message: Dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = str(message.get("text", "")).strip()
        if text.startswith("/start") or text.startswith("/help"):
            self._send_message(
                chat_id,
                "Готов. Пришли скриншот истории операций, я распознаю его и запишу в бюджет.",
                reply_markup=self._main_reply_keyboard(),
            )
            return
        if text.startswith("/ping"):
            self._send_message(chat_id, "pong")
            return
        if text.startswith("/reset"):
            self._send_reset_confirmation(chat_id)
            return
        if text.startswith("/sync"):
            self._send_message(chat_id, "Синхронизации с Excel больше нет. Используй /export для выгрузки Excel.", reply_markup=self._main_reply_keyboard())
            return
        if text.startswith("/export"):
            self._handle_export_text(chat_id, text)
            return
        if text.startswith("/sheet"):
            self._handle_sheet_command(chat_id, text)
            return
        if text.startswith("/budget"):
            self._send_message(chat_id, self.context.budget_account_summary(), reply_markup=self._main_reply_keyboard())
            return
        if text.startswith("/reminders"):
            self._send_reminder_settings(chat_id)
            return
        if text.startswith("/stats"):
            self._handle_stats_text(chat_id, text)
            return
        if text.startswith("/menu"):
            self._send_message(chat_id, "Главное меню:", reply_markup=self._main_reply_keyboard())
            return

        today = date.today()
        message_id = message.get("message_id")
        if text in {"Расход", "+ Расход", "Добавить расход"}:
            self._start_manual_entry(chat_id, OperationType.EXPENSE, message_id=message_id)
            return
        if text in {"Доход", "+ Доход", "Добавить доход"}:
            self._start_manual_entry(chat_id, OperationType.INCOME, message_id=message_id)
            return
        pending = self.context.storage.latest_pending_for_chat(chat_id)
        if pending is not None and is_manual_pending(pending):
            self._handle_manual_text(chat_id, text, pending, message_id=message_id)
            return
        if pending is not None and is_entry_edit_pending(pending):
            self._entry_editor().handle_edit_text(chat_id, text, pending, message_id=message_id)
            return
        if pending is not None and is_entry_search_pending(pending):
            self._entry_editor().handle_search_text(chat_id, text, pending, message_id=message_id)
            return
        if text == "Аналитика":
            self._send_analytics_menu(chat_id)
            return
        if text == "Синхронизировать":
            self._handle_export_text(chat_id, "/export")
            return
        if text in {"Сбросить все", "Сброс"}:
            self._send_reset_confirmation(chat_id)
            return

        if pending is None:
            manual_operation = _parse_manual_operation_text(text, today.year, self.context.category_book)
            if manual_operation is not None:
                self._write_manual_operation(chat_id, manual_operation)
                return
            period = _parse_stats_period(text, today.year)
            if period is not None:
                start_date, end_date = period
                self._send_expense_report(chat_id, start_date, end_date)
                return
            self._send_message(chat_id, "Пришли скриншот истории операций.", reply_markup=self._main_reply_keyboard())
            return

        operation_hash = pending["operation_hash"]
        row = self.context.storage.get_operation(operation_hash)
        if row is not None:
            pending_operation = operation_from_json(row["operation_json"])
            if pending_operation.date_missing:
                self._handle_missing_date_text(chat_id, text, row, operation_hash)
                return
            if _needs_quantity(pending_operation):
                self._send_quantity_picker(chat_id, operation_hash, pending_operation)
                return

        category, subcategory = _parse_category_pair(text)
        if not category or not subcategory:
            self._send_message(
                chat_id,
                "Можно выбрать кнопками ниже или написать так: Категория / Подкатегория",
                reply_markup=self._category_keyboard(operation_hash),
            )
            return
        resolved = _resolve_expense_category_pair(self.context.category_book, category, subcategory)
        if resolved is None:
            self._send_message(
                chat_id,
                "Такой пары нет в справочнике Excel. Выбери из кнопок ниже.",
                reply_markup=self._category_keyboard(operation_hash),
            )
            return
        category, subcategory = resolved

        if not _can_decide_operation(row):
            self.context.storage.delete_pending_action(operation_hash)
            self._send_message(chat_id, "Эта операция уже обработана, старый запрос закрыл.")
            return

        self._write_counted_operation(chat_id, row, operation_hash, category, subcategory)

    def _handle_missing_date_text(
        self,
        chat_id: int,
        text: str,
        row: Dict[str, Any],
        operation_hash: str,
    ) -> None:
        operation = operation_from_json(row["operation_json"])
        operation_date = _parse_user_operation_date(text, operation.date.year)
        if operation_date is None:
            self._send_message(chat_id, "Напиши дату в формате 22.08")
            return

        updated = ParsedOperation(
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
        self.context.storage.update_operation_json(operation_hash, updated)

        if _needs_quantity(updated):
            self.context.storage.add_pending_action(
                operation_hash=operation_hash,
                chat_id=chat_id,
                message_id=None,
                prompt="Выбери количество одинаковых операций",
            )
            self._send_quantity_picker(chat_id, operation_hash, updated)
            return

        if self._can_write_operation(updated):
            first_row, _last_row = self._write_operation_entries(operation_hash, updated, row["bank"])
            self.context.storage.update_operation_status(
                operation_hash,
                OperationStatus.AUTO_WRITTEN,
                workbook_row=first_row,
                status_note="written after user date",
            )
            self.context.storage.delete_pending_action(operation_hash)
            self._send_final_summary_if_ready(chat_id, row["image_hash"])
            return

        self.context.storage.add_pending_action(
            operation_hash=operation_hash,
            chat_id=chat_id,
            message_id=None,
            prompt="Выбери категорию",
        )
        self._send_message(
            chat_id,
            f"Дату поставил: {operation_date.strftime('%d.%m')}. Теперь выбери, считать операцию или пропустить.",
            reply_markup=self._manual_decision_keyboard(operation_hash, updated),
        )

    def _handle_quantity_choice(
        self,
        chat_id: int,
        row: Dict[str, Any],
        operation_hash: str,
        count: Optional[int],
    ) -> None:
        operation = operation_from_json(row["operation_json"])
        if count is None or count < 0 or count > operation.occurrence_count:
            self._send_quantity_picker(chat_id, operation_hash, operation)
            return
        if count == 0:
            image_hash_value = row["image_hash"]
            self.context.storage.update_operation_status(
                operation_hash,
                OperationStatus.IGNORED,
                status_note="ignored by user quantity",
            )
            self.context.storage.delete_pending_action(operation_hash)
            self._send_final_summary_if_ready(chat_id, image_hash_value)
            return

        updated = ParsedOperation(
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
            occurrence_confirmed=True,
        )
        self.context.storage.update_operation_json(operation_hash, updated)

        if self._can_write_operation(updated):
            first_row, _last_row = self._write_operation_entries(operation_hash, updated, row["bank"])
            self.context.storage.update_operation_status(
                operation_hash,
                OperationStatus.AUTO_WRITTEN,
                workbook_row=first_row,
                status_note=f"written with quantity {count}",
            )
            self.context.storage.delete_pending_action(operation_hash)
            self._send_final_summary_if_ready(chat_id, row["image_hash"])
            return

        self.context.storage.add_pending_action(
            operation_hash=operation_hash,
            chat_id=chat_id,
            message_id=None,
            prompt="Выбери категорию",
        )
        self._send_message(
            chat_id,
            f"Количество поставил: {count}. Теперь выбери, считать операцию или пропустить.",
            reply_markup=self._manual_decision_keyboard(operation_hash, updated),
        )

    def _can_write_operation(self, operation: ParsedOperation) -> bool:
        if operation.date_missing or _needs_quantity(operation):
            return False
        if operation.type == OperationType.EXPENSE:
            return (
                operation.amount < 0
                and self.context.category_book.is_valid_expense(operation.category, operation.subcategory)
            )
        if operation.type == OperationType.INCOME:
            return False
        return False

    def _start_manual_entry(
        self,
        chat_id: int,
        operation_type: OperationType,
        message_id: Optional[int] = None,
    ) -> None:
        self._manual_entry().start(chat_id, operation_type, message_id=message_id)

    def _handle_manual_text(
        self,
        chat_id: int,
        text: str,
        pending: Dict[str, Any],
        message_id: Optional[int] = None,
    ) -> None:
        self._manual_entry().handle_text(chat_id, text, pending, message_id=message_id)

    def _handle_manual_callback(self, callback: Dict[str, Any], chat_id: int, action: str, payload: str) -> None:
        self._manual_entry().handle_callback(callback, chat_id, action, payload)

    def _send_manual_prompt(
        self,
        chat_id: int,
        state: Dict[str, Any],
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._manual_entry().send_prompt(chat_id, state, text, reply_markup=reply_markup)

    def _cleanup_manual_messages(self, chat_id: int, state: Dict[str, Any]) -> None:
        self._manual_entry().cleanup_messages(chat_id, state)

    def _save_manual_state(self, chat_id: int, state: Dict[str, Any]) -> None:
        self._manual_entry().save_state(chat_id, state)

    def _write_manual_operation(self, chat_id: int, operation: ParsedOperation) -> None:
        self._manual_entry().write_operation(chat_id, operation)

    def _write_operation_entries(
        self,
        operation_hash: str,
        operation: ParsedOperation,
        bank: str,
        source: str = "bot",
    ) -> tuple[int, int]:
        if operation.occurrence_count <= 0:
            raise RuntimeError("Cannot write zero operations")
        entries = [
            BudgetEntryInsert(
                source=source,
                operation_hash=operation_hash if index == 0 else f"{operation_hash}:{index + 1}",
                operation=operation,
                bank=bank,
            )
            for index in range(operation.occurrence_count)
        ]
        entry_ids = self.context.storage.append_budget_entries_batch(entries)
        return entry_ids[0], entry_ids[-1]

    def _append_operation_copies(self, operation: ParsedOperation, bank: str) -> tuple[int, int]:
        from .storage import operation_hash

        return self._write_operation_entries(operation_hash(bank, operation), operation, bank)

    def _write_counted_operation(
        self,
        chat_id: int,
        row: Dict[str, Any],
        operation_hash: str,
        category: str,
        subcategory: str,
    ) -> None:
        operation = operation_from_json(row["operation_json"])
        counted = ParsedOperation(
            date=operation.date,
            name=operation.name,
            amount=-abs(operation.amount),
            type=OperationType.EXPENSE,
            category=category,
            subcategory=subcategory,
            confidence=1.0,
            needs_review=False,
            note=operation.note,
            date_missing=False,
            occurrence_count=operation.occurrence_count,
            occurrence_confirmed=True,
        )
        first_row, _last_row = self._write_operation_entries(operation_hash, counted, row["bank"])
        self.context.storage.save_expense_category_rule(operation.name, category, subcategory)
        self.context.storage.update_operation_json(operation_hash, counted)
        self.context.storage.update_operation_status(
            operation_hash,
            OperationStatus.AUTO_WRITTEN,
            workbook_row=first_row,
            status_note="written by user decision",
        )
        self.context.storage.delete_pending_action(operation_hash)
        self._send_final_summary_if_ready(chat_id, row["image_hash"])

    def _write_income_operation(
        self,
        chat_id: int,
        row: Dict[str, Any],
        operation_hash: str,
        category: Optional[str] = None,
    ) -> None:
        operation = operation_from_json(row["operation_json"])
        income_category = category or _resolve_income_category(operation, self.context.category_book) or operation.category
        if not self.context.category_book.is_valid_income(income_category):
            self.context.storage.add_pending_action(
                operation_hash=operation_hash,
                chat_id=chat_id,
                message_id=None,
                prompt="Выбери категорию дохода",
            )
            self._send_income_category_picker(chat_id, operation_hash)
            return
        counted = ParsedOperation(
            date=operation.date,
            name=operation.name,
            amount=abs(operation.amount),
            type=OperationType.INCOME,
            category=income_category,
            subcategory=None,
            confidence=1.0,
            needs_review=False,
            note=operation.note,
            date_missing=False,
            occurrence_count=operation.occurrence_count,
            occurrence_confirmed=True,
        )
        first_row, _last_row = self._write_operation_entries(operation_hash, counted, row["bank"])
        self.context.storage.update_operation_json(operation_hash, counted)
        self.context.storage.update_operation_status(
            operation_hash,
            OperationStatus.AUTO_WRITTEN,
            workbook_row=first_row,
            status_note="income written by user decision",
        )
        self.context.storage.delete_pending_action(operation_hash)
        self._send_final_summary_if_ready(chat_id, row["image_hash"])

    def _upsert_budget_entries_for_rows(
        self,
        operation_hash: str,
        operation: ParsedOperation,
        bank: str,
        first_row: int,
        last_row: int,
        source: str = "bot",
    ) -> None:
        return None

    def _handle_reset_callback(self, callback: Dict[str, Any], chat_id: int, action: str) -> None:
        if action == "reset":
            self._answer_callback(callback["id"], "Нужно подтверждение")
            self._delete_callback_message(callback)
            self._send_reset_confirmation(chat_id)
            return
        if action == "reset_cancel":
            self._answer_callback(callback["id"], "Отменено")
            self._delete_callback_message(callback)
            self._send_message(chat_id, "Сброс отменен.")
            return
        if action == "reset_confirm":
            self.context.storage.reset_all()
            self._clear_saved_images()
            self._answer_callback(callback["id"], "Сброшено")
            self._delete_callback_message(callback)
            self._send_message(chat_id, "Готово: расходы, доходы, история и сохраненные копии скринов очищены.")
            return

    def _handle_stats_callback(self, callback: Dict[str, Any], chat_id: int, action: str, payload: str) -> None:
        self._answer_callback(callback["id"], "Готовлю")
        self._delete_callback_message(callback)
        if action == "menu":
            self._send_message(chat_id, "Главное меню:", reply_markup=self._main_reply_keyboard())
            return
        if action == "sync":
            self._sync_budget_entries(chat_id)
            return
        if action == "analytics":
            self._send_analytics_menu(chat_id)
            return
        today = date.today()
        if action == "stats":
            if payload == "today":
                self._send_expense_report(chat_id, today, today)
                return
            if payload == "week":
                self._send_expense_report(chat_id, today - timedelta(days=today.weekday()), today)
                return
            if payload == "cats":
                self._send_category_report_picker(chat_id)
                return
            if payload == "period":
                self._send_message(chat_id, "Напиши дату или период так: 01.08 или 01.08-24.08", reply_markup=self._main_reply_keyboard())
                return
            self._send_expense_report(chat_id, today.replace(day=1), today)
            return
        if action == "statscat":
            index = _parse_index(payload, 0)
            category_item = self._category_by_index(index)
            if category_item is None:
                self._send_message(chat_id, "Категория устарела. Открой список заново.", reply_markup=self._main_reply_keyboard())
                return
            category, _subcategories = category_item
            self._send_expense_report(chat_id, today.replace(day=1), today, category=category)
            return
        if action == "chart":
            if payload == "today":
                self._send_expense_chart(chat_id, today, today)
                return
            if payload == "week":
                self._send_expense_chart(chat_id, today - timedelta(days=today.weekday()), today)
                return
            if payload in {"month", ""}:
                self._send_expense_chart(chat_id, today.replace(day=1), today)
                return
            if payload == "period":
                self._send_message(
                    chat_id,
                    "Напиши период для диаграммы: 01.08 или 01.08-24.08",
                    reply_markup=self._main_reply_keyboard(),
                )
                return
            period = _parse_chart_period_payload(payload)
            if period is None:
                self._send_message(chat_id, "Период устарел. Открой аналитику заново.", reply_markup=self._main_reply_keyboard())
                return
            start_date, end_date, category = period
            self._send_expense_chart(chat_id, start_date, end_date, category=category)
            return

    def _handle_stats_text(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        today = date.today()
        if len(parts) == 1:
            self._send_expense_report(chat_id, today.replace(day=1), today)
            return
        period = _parse_stats_period(parts[1], today.year)
        if period is None:
            self._send_message(chat_id, "Не понял дату или период. Пример: /stats 01.08 или /stats 01.08-24.08")
            return
        start_date, end_date = period
        self._send_expense_report(chat_id, start_date, end_date)

    def _sync_budget_entries(self, chat_id: int) -> None:
        self._handle_export_text(chat_id, "/export")

    def _handle_export_text(self, chat_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        today = date.today()
        if len(parts) > 1:
            period = _parse_stats_period(parts[1], today.year)
            if period is None:
                self._send_message(chat_id, "Не понял период. Пример: /export 01.08-24.08")
                return
            start_date, end_date = period
        else:
            start_date, end_date = today.replace(day=1), today
        path = ExcelExporter(self.context.storage, self.context.settings.export_dir).export(
            owner_id=self.context.owner_id,
            start_date=start_date,
            end_date=end_date,
        )
        if self._send_document(chat_id, path):
            return
        self._send_message(chat_id, f"Excel export готов: {path}", reply_markup=self._main_reply_keyboard())

    def _handle_sheet_command(self, chat_id: int, text: str) -> None:
        self._send_message(
            chat_id,
            "Google Sheets больше не используется как хранилище. Данные пишутся в Postgres, Excel можно получить через /export.",
            reply_markup=self._main_reply_keyboard(),
        )

    def _send_reminder_settings(self, chat_id: int) -> None:
        self._send_message(
            chat_id,
            f"Ежедневное напоминание: {self.context.settings.reminder_default_time}.",
            reply_markup=self._main_reply_keyboard(),
        )

    def _send_expense_report(
        self,
        chat_id: int,
        start_date: date,
        end_date: date,
        category: Optional[str] = None,
    ) -> None:
        summary = self.context.storage.expense_summary(start_date, end_date, category=category)
        self._send_message(chat_id, "\n".join(_expense_report_lines(summary)), reply_markup=expense_report_keyboard(start_date, end_date, category))

    def _send_expense_chart(
        self,
        chat_id: int,
        start_date: date,
        end_date: date,
        category: Optional[str] = None,
    ) -> None:
        summary = self.context.storage.expense_summary(start_date, end_date, category=category)
        if summary["count"] == 0:
            self._send_message(
                chat_id,
                "За этот период расходов нет — нечего рисовать.",
                reply_markup=expense_report_keyboard(start_date, end_date, category),
            )
            return
        daily_rows = self.context.storage.expense_daily_totals(start_date, end_date, category=category)
        chart_dir = self.context.settings.export_dir / "charts"
        chart_name = _chart_period_payload(start_date, end_date, category).replace(":", "-")
        chart_path = chart_dir / f"{self.context.owner_id}-{chart_name}.png"
        try:
            render_expense_chart(summary, daily_rows, chart_path)
        except Exception as exc:
            logger.exception("expense chart render failed", extra=log_extra(chat_id=chat_id))
            self._send_message(chat_id, f"Не смог построить диаграмму: {exc}", reply_markup=self._main_reply_keyboard())
            return
        caption = f"Расходы {start_date.strftime('%d.%m')} – {end_date.strftime('%d.%m')}"
        if category:
            caption = f"{caption}: {category}"
        if not self._send_photo(chat_id, chart_path, caption=caption):
            self._send_message(chat_id, "Не смог отправить диаграмму.", reply_markup=self._main_reply_keyboard())

    def _entry_editor(self) -> TelegramEntryEditor:
        editor = getattr(self, "_telegram_entry_editor", None)
        if editor is None:
            editor = TelegramEntryEditor(self)
            self._telegram_entry_editor = editor
        return editor

    def _manual_entry(self) -> TelegramManualEntry:
        entry = getattr(self, "_telegram_manual_entry", None)
        if entry is None:
            entry = TelegramManualEntry(self)
            self._telegram_manual_entry = entry
        return entry

    def _written_operation_summary_lines(self, operations: Sequence[ParsedOperation]) -> List[str]:
        return _written_operation_summary_lines(operations)

    def _send_category_report_picker(self, chat_id: int) -> None:
        buttons = [
            {"text": category, "callback_data": f"statscat:{index}"}
            for index, (category, _subcategories) in enumerate(self._category_items())
        ]
        rows = _button_rows(buttons, columns=2)
        rows.append([{"text": "Назад", "callback_data": "menu:home"}])
        self._send_message(chat_id, "Выбери категорию:", reply_markup={"inline_keyboard": rows})

    def _send_analytics_menu(self, chat_id: int) -> None:
        self._send_message(
            chat_id,
            "Аналитика:",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Месяц", "callback_data": "stats:month"},
                        {"text": "Сегодня", "callback_data": "stats:today"},
                        {"text": "Неделя", "callback_data": "stats:week"},
                    ],
                    [
                        {"text": "Категории", "callback_data": "stats:cats"},
                        {"text": "Даты", "callback_data": "stats:period"},
                    ],
                    [
                        {"text": "Диаграмма", "callback_data": "chart:month"},
                    ],
                ]
            },
        )

    def _send_due_reminders(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_reminder_check < REMINDER_CHECK_SECONDS:
            return
        self._last_reminder_check = now_monotonic
        due_by_date: Dict[date, List[int]] = defaultdict(list)
        for settings in self.context.storage.reminder_settings():
            if not settings["enabled"]:
                continue
            timezone_name = str(settings["timezone"] or self.context.settings.default_timezone)
            try:
                now_local = datetime.now(ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError:
                now_local = datetime.now()
            reminder_time = _parse_reminder_time(str(settings["time_local"]))
            if reminder_time is None or now_local.time() < reminder_time:
                continue
            due_by_date[now_local.date()].append(int(settings["chat_id"]))

        delivered: List[tuple[int, date]] = []
        for reminder_date, chat_ids in due_by_date.items():
            already_sent = self.context.storage.reminder_sent_chat_ids(chat_ids, reminder_date)
            for chat_id in chat_ids:
                if chat_id in already_sent:
                    continue
                self._send_message(
                    chat_id,
                    "Не забудь отправить скриншоты расходов за сегодня.",
                    reply_markup=self._main_reply_keyboard(),
                )
                delivered.append((chat_id, reminder_date))

        if delivered:
            self.context.storage.mark_reminders_sent(delivered)

    def _send_reset_confirmation(self, chat_id: int) -> None:
        self._send_message(
            chat_id,
            "Сбросить все записи расходов/доходов и историю обработанных скринов?",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Да, сбросить все", "callback_data": "reset_confirm:all"},
                        {"text": "Отмена", "callback_data": "reset_cancel:all"},
                    ]
                ]
            },
        )

    def _main_reply_keyboard(self) -> Dict[str, Any]:
        return {
            "keyboard": [
                [{"text": "Расход"}, {"text": "Доход"}],
                [{"text": "Аналитика"}, {"text": "Синхронизировать"}, {"text": "Сброс"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def _clear_saved_images(self) -> None:
        image_dir = self.context.settings.export_dir.parent / "images"
        if not image_dir.exists():
            return
        for path in image_dir.iterdir():
            if path.is_file():
                path.unlink()

    def _delete_callback_message(self, callback: Dict[str, Any]) -> None:
        message = callback.get("message") or {}
        message_id = message.get("message_id")
        chat_id = message.get("chat", {}).get("id")
        if message_id is None or chat_id is None:
            return
        self._delete_message(chat_id, message_id)

    def _delete_pending_action_message(self, chat_id: int, operation_hash: str) -> None:
        pending = self.context.storage.get_pending_action(operation_hash, chat_id)
        if pending is None:
            return
        message_id = pending.get("message_id")
        if message_id is None:
            return
        self._delete_message(chat_id, message_id)

    def _delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramApiError as exc:
            if exc.status_code == 400:
                return
            logger.warning("telegram delete message ignored: %s", exc, extra=log_extra(chat_id=chat_id))
            self._clear_callback_buttons(chat_id, message_id)

    def _clear_callback_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            self._api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id})
        except TelegramApiError as exc:
            if exc.status_code == 400:
                return
            logger.warning("telegram clear buttons ignored: %s", exc, extra=log_extra(chat_id=chat_id))

    def _send_category_picker(self, chat_id: int, operation_hash: str) -> None:
        self._send_message(
            chat_id,
            "Выбери категорию расхода:",
            reply_markup=self._category_keyboard(operation_hash),
        )

    def _send_subcategory_picker(self, chat_id: int, operation_hash: str, category_index: int) -> None:
        category_item = self._category_by_index(category_index)
        if category_item is None:
            self._send_category_picker(chat_id, operation_hash)
            return
        category, _subcategories = category_item
        self._send_message(
            chat_id,
            f"Выбери подкатегорию для {category}:",
            reply_markup=self._subcategory_keyboard(operation_hash, category_index),
        )

    def _send_quantity_picker(
        self,
        chat_id: int,
        operation_hash: str,
        operation: ParsedOperation,
        prefix: Optional[str] = None,
    ) -> Optional[int]:
        text = (
            f"Нашел одинаковые операции: {operation.name or 'операция'} / "
            f"{operation.amount:.2f} ₽ - {operation.occurrence_count} шт. "
            "Сколько записать?"
        )
        if prefix:
            text = f"{prefix}\n\n{text}"
        return self._send_message(
            chat_id,
            text,
            reply_markup=self._quantity_keyboard(operation_hash, operation.occurrence_count),
        )

    def _send_income_category_picker(self, chat_id: int, operation_hash: str) -> None:
        self._send_message(
            chat_id,
            "Выбери категорию дохода:",
            reply_markup=self._income_category_keyboard(operation_hash),
        )

    def _category_keyboard(self, operation_hash: str) -> Dict[str, Any]:
        buttons = [
            {"text": category, "callback_data": f"cat:{operation_hash}:{index}"}
            for index, (category, _subcategories) in enumerate(self._category_items())
        ]
        rows = _button_rows(buttons, columns=2)
        rows.append([{"text": "Пропустить", "callback_data": f"skip:{operation_hash}"}])
        return {"inline_keyboard": rows}

    def _quantity_keyboard(self, operation_hash: str, count: int) -> Dict[str, Any]:
        buttons = [
            {"text": str(index), "callback_data": f"qty:{operation_hash}:{index}"}
            for index in range(1, count + 1)
        ]
        rows = _button_rows(buttons, columns=3)
        rows.append([{"text": "Пропустить", "callback_data": f"qty:{operation_hash}:0"}])
        return {"inline_keyboard": rows}

    def _income_category_keyboard(self, operation_hash: str) -> Dict[str, Any]:
        buttons = [
            {"text": category, "callback_data": f"incat:{operation_hash}:{index}"}
            for index, category in enumerate(self.context.category_book.income_categories)
        ]
        rows = _button_rows(buttons, columns=2)
        rows.append([{"text": "Пропустить", "callback_data": f"skip:{operation_hash}"}])
        return {"inline_keyboard": rows}

    def _manual_category_keyboard(self, chat_id: int) -> Dict[str, Any]:
        return self._manual_entry().category_keyboard(chat_id)

    def _manual_subcategory_keyboard(self, chat_id: int, category_index: int) -> Dict[str, Any]:
        return self._manual_entry().subcategory_keyboard(chat_id, category_index)

    def _manual_income_category_keyboard(self, chat_id: int) -> Dict[str, Any]:
        return self._manual_entry().income_category_keyboard(chat_id)

    def _manual_decision_keyboard(
        self,
        operation_hash: str,
        operation: Optional[ParsedOperation] = None,
    ) -> Dict[str, Any]:
        if operation is not None and operation.type == OperationType.INCOME:
            return {
                "inline_keyboard": [
                    [
                        {"text": "Считать доход", "callback_data": f"inc:{operation_hash}"},
                        {"text": "Пропустить", "callback_data": f"skip:{operation_hash}"},
                    ]
                ]
            }
        return {
            "inline_keyboard": [
                [
                    {"text": "Считать", "callback_data": f"count:{operation_hash}"},
                    {"text": "Пропустить", "callback_data": f"skip:{operation_hash}"},
                ]
            ]
        }

    def _subcategory_keyboard(self, operation_hash: str, category_index: int) -> Dict[str, Any]:
        category_item = self._category_by_index(category_index)
        if category_item is None:
            return self._category_keyboard(operation_hash)
        _category, subcategories = category_item
        buttons = [
            {"text": subcategory, "callback_data": f"sub:{operation_hash}:{category_index}:{index}"}
            for index, subcategory in enumerate(subcategories)
        ]
        rows = _button_rows(buttons, columns=2)
        rows.append(
            [
                {"text": "Назад", "callback_data": f"back:{operation_hash}"},
                {"text": "Пропустить", "callback_data": f"skip:{operation_hash}"},
            ]
        )
        return {"inline_keyboard": rows}

    def _category_items(self) -> List[tuple[str, List[str]]]:
        return list(self.context.category_book.expense_categories.items())

    def _category_by_index(self, index: Optional[int]) -> Optional[tuple[str, List[str]]]:
        if index is None:
            return None
        items = self._category_items()
        if index < 0 or index >= len(items):
            return None
        return items[index]

    def _income_category_by_index(self, index: Optional[int]) -> Optional[str]:
        if index is None:
            return None
        items = self.context.category_book.income_categories
        if index < 0 or index >= len(items):
            return None
        return items[index]

    def _subcategory_by_index(
        self,
        category_index: Optional[int],
        subcategory_index: Optional[int],
    ) -> Optional[tuple[str, str]]:
        category_item = self._category_by_index(category_index)
        if category_item is None or subcategory_index is None:
            return None
        category, subcategories = category_item
        if subcategory_index < 0 or subcategory_index >= len(subcategories):
            return None
        return category, subcategories[subcategory_index]

    def _send_processing_result(self, chat_id: int, result) -> None:
        if not result.decisions:
            self._send_message(chat_id, "Этот скрин уже обработан, дубли не добавляю.")
            return

        written = [item for item in result.decisions if item.status == OperationStatus.AUTO_WRITTEN]
        pending = [item for item in result.decisions if item.status == OperationStatus.PENDING]
        ignored = [item for item in result.decisions if item.status == OperationStatus.IGNORED]

        if written:
            message_id = self._send_message(chat_id, "\n".join(_written_summary_lines(written)))
            if pending and message_id is not None:
                self._preview_summary_messages[result.image_hash] = (chat_id, message_id)
        elif ignored and not pending:
            self._send_message(chat_id, "Ничего нового не записал.")
        elif pending:
            self._send_message(
                chat_id,
                f"Пока ничего не записал автоматически: нужно решить {len(pending)} операций.",
                reply_markup=self._main_reply_keyboard(),
            )

        for decision in pending:
            op_hash = self._find_operation_hash(result.bank, decision.operation)
            text = (
                f"Решить: {decision.operation.name or 'операция'}\n"
                f"{decision.operation.date.isoformat()} / {decision.operation.amount:.2f} ₽\n"
                f"Причина: {_user_reason_text(decision.reason)}"
            )
            if decision.operation.date_missing:
                message_id = self._send_message(
                    chat_id,
                    f"{text}\nНа скрине не вижу дату операции. Напиши дату в формате 22.08",
                )
                self.context.storage.add_pending_action(
                    operation_hash=op_hash,
                    chat_id=chat_id,
                    message_id=message_id,
                    prompt="Напиши дату операции в формате 22.08",
                )
                continue
            if _needs_quantity(decision.operation):
                message_id = self._send_quantity_picker(chat_id, op_hash, decision.operation, prefix=text)
                self.context.storage.add_pending_action(
                    operation_hash=op_hash,
                    chat_id=chat_id,
                    message_id=message_id,
                    prompt="Выбери количество одинаковых операций",
                )
                continue
            self._send_message(
                chat_id,
                text,
                reply_markup=self._manual_decision_keyboard(op_hash, decision.operation),
            )

    def _send_final_summary_if_ready(self, chat_id: int, image_hash_value: str) -> None:
        rows = self.context.storage.operations_for_image(image_hash_value)
        has_pending = any(row["status"] == OperationStatus.PENDING.value for row in rows)
        if has_pending:
            return

        preview = self._preview_summary_messages.pop(image_hash_value, None)
        if preview is not None:
            preview_chat_id, preview_message_id = preview
            self._delete_message(preview_chat_id, preview_message_id)

        written_operations = [
            operation_from_json(row["operation_json"])
            for row in rows
            if row["status"] == OperationStatus.AUTO_WRITTEN.value
        ]
        if written_operations:
            self._send_message(chat_id, "\n".join(_written_operation_summary_lines(written_operations)))
        else:
            self._send_message(chat_id, "Ничего не засчитал.")

    def _find_operation_hash(self, bank: str, operation: ParsedOperation) -> str:
        from .storage import operation_hash

        return operation_hash(bank, operation)

    def _download_file(self, file_id: str) -> tuple[bytes, str]:
        return self.api_client.download_file(file_id)

    def _save_image_for_replay(self, content: bytes, mime_type: str) -> Path:
        suffix = mimetypes.guess_extension(mime_type) or ".jpg"
        image_dir = self.context.settings.export_dir.parent / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        path = image_dir / f"{image_hash(content)}{suffix}"
        if not path.exists():
            path.write_bytes(content)
        return path

    def _api(self, method: str, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        return self.api_client.api(method, payload, timeout=timeout)

    def _polling_error_sleep_seconds(self, exc: Exception) -> float:
        return self.api_client.polling_error_sleep_seconds(exc)

    def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = self._api("sendMessage", payload)
        result = response.get("result") or {}
        message_id = result.get("message_id")
        return int(message_id) if message_id is not None else None

    def _send_document(self, chat_id: int, path: Path) -> bool:
        return self.api_client.send_document(chat_id, path)

    def _send_photo(self, chat_id: int, path: Path, caption: Optional[str] = None) -> bool:
        return self.api_client.send_photo(chat_id, path, caption=caption)

    def _answer_callback(self, callback_id: str, text: str) -> None:
        self._api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def _is_allowed(self, user_id: Optional[int]) -> bool:
        if self.context.settings.telegram_allow_all:
            return True
        allowed = self.context.settings.telegram_allowed_user_ids
        return user_id in allowed

    def _log_update(self, update: Dict[str, Any]) -> None:
        message = update.get("message") or update.get("callback_query", {}).get("message")
        user = update.get("message", {}).get("from") or update.get("callback_query", {}).get("from") or {}
        if not message:
            logger.info("telegram update without message", extra=log_extra(status="ignored"))
            return
        kinds = [kind for kind in ("text", "photo", "document") if kind in message]
        logger.info(
            "telegram update",
            extra=log_extra(
                chat_id=message.get("chat", {}).get("id"),
                user_id=user.get("id"),
                status="update",
                update_id=update.get("update_id"),
                kinds=",".join(kinds) or "other",
            ),
        )

    def _sanitize_error(self, text: str) -> str:
        return text.replace(self.token, "<telegram-token>")


def _parse_category_pair(text: str) -> tuple[Optional[str], Optional[str]]:
    if "/" not in text:
        return None, None
    category, subcategory = text.split("/", 1)
    return category.strip(), subcategory.strip()


def _parse_reminder_time(text: str) -> Optional[local_time]:
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        return local_time(hour, minute)
    except ValueError:
        return None


def _resolve_expense_category_pair(category_book, category: str, subcategory: str) -> Optional[tuple[str, str]]:
    normalized_category = category.casefold()
    normalized_subcategory = subcategory.casefold()
    for known_category, known_subcategories in category_book.expense_categories.items():
        if known_category.casefold() != normalized_category:
            continue
        for known_subcategory in known_subcategories:
            if known_subcategory.casefold() == normalized_subcategory:
                return known_category, known_subcategory
    return None


def _resolve_income_category(operation: ParsedOperation, category_book) -> Optional[str]:
    text = " ".join(
        item
        for item in [operation.name, operation.category or "", operation.note]
        if item
    ).casefold()
    aliases = [
        ("Зарплата", ["зарплата", "заработная плата", "salary", "гдоу"]),
        ("Аванс", ["аванс"]),
        ("Возврат", ["возврат", "refund", "озон", "яндекс маркет", "wildberries", "wb", "алиэкспресс"]),
        ("Подработка", ["подработка"]),
        ("Аренда", ["аренда"]),
        ("Разное", ["разное"]),
    ]
    for category, markers in aliases:
        if category_book.is_valid_income(category) and any(marker in text for marker in markers):
            return category
    return None


def _operation_hash_from_callback(action: str, payload: str) -> Optional[str]:
    if action in {"skip", "count", "back", "inc"}:
        return payload
    if action in {"cat", "sub", "qty", "incat"}:
        operation_hash = payload.split(":", 1)[0]
        return operation_hash or None
    return None


def _telegram_user_id_from_update(update: Dict[str, Any]) -> Optional[int]:
    if "callback_query" in update:
        return update["callback_query"].get("from", {}).get("id")
    message = update.get("message") or {}
    return message.get("from", {}).get("id")


def _needs_quantity(operation: ParsedOperation) -> bool:
    return operation.occurrence_count > 1 and not operation.occurrence_confirmed


def _written_summary_lines(decisions: Sequence[Any], limit: int = 20) -> List[str]:
    return _written_operation_summary_lines([decision.operation for decision in decisions], limit=limit)


def _written_operation_summary_lines(operations: Sequence[ParsedOperation], limit: int = 20) -> List[str]:
    lines = ["Засчитано:"]
    current_date: Optional[date] = None
    shown = 0
    sorted_operations = sorted(operations, key=lambda item: (item.date, item.name.casefold()))
    expense_totals_by_date = _expense_totals_by_date(sorted_operations)
    for operation in sorted_operations:
        if shown >= limit:
            remaining = len(operations) - shown
            if remaining > 0:
                lines.append(f"...и еще {remaining}")
            break

        if operation.date != current_date:
            current_date = operation.date
            expense_total = expense_totals_by_date.get(operation.date, 0.0)
            if expense_total:
                lines.append(f"{operation.date.strftime('%d.%m')} - траты: {_format_money(expense_total)}")
            else:
                lines.append(operation.date.strftime("%d.%m"))

        lines.append(f"- {_operation_summary_text(operation)}")
        shown += 1
    return lines


def _expense_totals_by_date(operations: Sequence[ParsedOperation]) -> Dict[date, float]:
    totals: Dict[date, float] = {}
    for operation in operations:
        if operation.type != OperationType.EXPENSE:
            continue
        totals[operation.date] = totals.get(operation.date, 0.0) + operation.excel_amount * operation.occurrence_count
    return totals


def _user_reason_text(reason: str) -> str:
    if reason.startswith("same operation appears "):
        count = reason.removeprefix("same operation appears ").removesuffix(" times")
        return f"одинаковая операция найдена {count} раза"
    return {
        "operation date missing": "не вижу дату операции",
        "operation needs review": "нужна ручная проверка",
        "manual decision required": "нужно ручное решение",
        "expense amount must be negative": "расход распознан с неправильным знаком суммы",
        "unknown expense category or subcategory": "не понял категорию расхода",
        "income amount must be positive": "доход распознан с неправильным знаком суммы",
        "income manual decision required": "доход нужно подтвердить кнопкой",
        "unknown income category": "не понял категорию дохода",
    }.get(reason, reason)


def _budget_sheet_name(operation: ParsedOperation) -> str:
    if operation.type == OperationType.INCOME:
        return INCOME_SHEET
    return EXPENSE_SHEET


def _can_decide_operation(row: Optional[Dict[str, Any]]) -> bool:
    if row is None:
        return False
    return row.get("status") == OperationStatus.PENDING.value and row.get("workbook_row") is None
