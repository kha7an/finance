from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

import requests

from .log_config import get_logger, log_extra


logger = get_logger(__name__)


class TelegramApiError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


TELEGRAM_POLLING_ERROR_SLEEP_SECONDS = 5.0
TELEGRAM_POLLING_CONFLICT_SLEEP_SECONDS = 60.0


class TelegramApiClient:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        settings = bot.context.settings
        self.token = settings.telegram_bot_token
        self.base_url = f"{settings.telegram_api_base_url}/bot{self.token}"
        self.file_base_url = f"{settings.telegram_file_base_url}/bot{self.token}"
        self.session = requests.Session()
        self.timeout = settings.telegram_timeout_seconds
        self.session.trust_env = settings.use_env_proxy and not settings.telegram_proxy_url
        if settings.telegram_proxy_url:
            self.session.proxies.update(
                {
                    "http": settings.telegram_proxy_url,
                    "https": settings.telegram_proxy_url,
                }
            )

    def api(self, method: str, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        started_at = time.monotonic()
        request_timeout = timeout or self.timeout
        log = logger.debug if method == "getUpdates" else logger.info
        log(
            "telegram api request",
            extra=log_extra(
                method=method,
                timeout=request_timeout,
                payload=self._log_payload(payload),
                status="request",
            ),
        )

        response: Optional[requests.Response] = None
        try:
            response = self.session.post(
                f"{self.base_url}/{method}",
                json=payload,
                timeout=request_timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            status_code = response.status_code if response is not None else None
            logger.warning(
                "telegram api failed",
                extra=log_extra(
                    method=method,
                    elapsed=time.monotonic() - started_at,
                    status="error",
                    http_status=status_code,
                    error=self._sanitize_error(str(exc)),
                ),
            )
            raise TelegramApiError(
                self._sanitize_error(f"Telegram API {method} failed: {exc}"),
                status_code=status_code,
            ) from None
        except ValueError as exc:
            status_code = response.status_code if response is not None else None
            logger.warning(
                "telegram api invalid json",
                extra=log_extra(
                    method=method,
                    elapsed=time.monotonic() - started_at,
                    status="error",
                    http_status=status_code,
                    error=str(exc),
                ),
            )
            raise TelegramApiError(
                self._sanitize_error(f"Telegram API {method} returned invalid JSON: {exc}"),
                status_code=status_code,
            ) from None
        if not data.get("ok"):
            description = data.get("description") or "Telegram API error"
            logger.warning(
                "telegram api rejected",
                extra=log_extra(
                    method=method,
                    elapsed=time.monotonic() - started_at,
                    status="error",
                    error=self._sanitize_error(description),
                ),
            )
            raise TelegramApiError(self._sanitize_error(description), status_code=data.get("error_code"))

        result = data.get("result")
        updates_count = len(result) if isinstance(result, list) else None
        log(
            "telegram api response",
            extra=log_extra(
                method=method,
                elapsed=time.monotonic() - started_at,
                status="ok",
                updates=updates_count,
            ),
        )
        return data

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        return self.api("sendMessage", payload)["result"]

    def send_document(
        self,
        chat_id: int,
        document_path: Union[str, Path],
        caption: Optional[str] = None,
    ) -> bool:
        path = Path(document_path)
        started_at = time.monotonic()
        logger.info(
            "telegram api request",
            extra=log_extra(method="sendDocument", chat_id=chat_id, file=path.name, status="request"),
        )
        try:
            with path.open("rb") as handle:
                files = {"document": (path.name, handle, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
                data: Dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                response = self.session.post(
                    f"{self.base_url}/sendDocument",
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
            response.raise_for_status()
            payload = response.json()
            logger.info(
                "telegram api response",
                extra=log_extra(
                    method="sendDocument",
                    chat_id=chat_id,
                    elapsed=time.monotonic() - started_at,
                    status="ok" if payload.get("ok") else "error",
                ),
            )
            return bool(payload.get("ok"))
        except Exception as exc:
            logger.warning(
                "telegram send document ignored",
                extra=log_extra(method="sendDocument", chat_id=chat_id, error=str(exc), status="error"),
            )
            return False

    def send_photo(
        self,
        chat_id: int,
        photo_path: Union[str, Path],
        caption: Optional[str] = None,
    ) -> bool:
        path = Path(photo_path)
        started_at = time.monotonic()
        logger.info(
            "telegram api request",
            extra=log_extra(method="sendPhoto", chat_id=chat_id, file=path.name, status="request"),
        )
        try:
            with path.open("rb") as handle:
                files = {"photo": (path.name, handle, "image/png")}
                data: Dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                response = self.session.post(
                    f"{self.base_url}/sendPhoto",
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
            response.raise_for_status()
            payload = response.json()
            logger.info(
                "telegram api response",
                extra=log_extra(
                    method="sendPhoto",
                    chat_id=chat_id,
                    elapsed=time.monotonic() - started_at,
                    status="ok" if payload.get("ok") else "error",
                ),
            )
            return bool(payload.get("ok"))
        except Exception as exc:
            logger.warning(
                "telegram send photo ignored",
                extra=log_extra(method="sendPhoto", chat_id=chat_id, error=str(exc), status="error"),
            )
            return False

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except Exception as exc:
            logger.warning("telegram delete message ignored: %s", exc)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: Dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self.api("answerCallbackQuery", payload)

    def download_file(self, file_id: str) -> tuple[bytes, str]:
        started_at = time.monotonic()
        file_info = self.api("getFile", {"file_id": file_id})["result"]
        file_path = file_info["file_path"]
        mime_type = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        response = self.session.get(f"{self.file_base_url}/{file_path}", timeout=self.timeout)
        response.raise_for_status()
        logger.info(
            "telegram file download",
            extra=log_extra(
                file_id=file_id,
                bytes=len(response.content),
                elapsed=time.monotonic() - started_at,
                status="ok",
            ),
        )
        return response.content, mime_type

    def polling_error_sleep_seconds(self, exc: Exception) -> float:
        if isinstance(exc, TelegramApiError) and exc.status_code == 409:
            return TELEGRAM_POLLING_CONFLICT_SLEEP_SECONDS
        return TELEGRAM_POLLING_ERROR_SLEEP_SECONDS

    def get_updates_request_timeout(self, poll_timeout: int) -> int:
        return poll_timeout + max(self.timeout, 30)

    def is_getupdates_timeout(self, exc: Exception) -> bool:
        if not isinstance(exc, TelegramApiError):
            return False
        message = str(exc).casefold()
        return "getupdates" in message and "timed out" in message

    def log_update(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            callback = update["callback_query"]
            logger.info(
                "telegram callback update",
                extra=log_extra(
                    chat_id=callback.get("message", {}).get("chat", {}).get("id"),
                    update_id=update.get("update_id"),
                    status="callback",
                ),
            )
            return
        message = update.get("message")
        if message is None:
            logger.info(
                "telegram update without message",
                extra=log_extra(update_id=update.get("update_id"), status="ignored"),
            )
            return
        chat_id = message.get("chat", {}).get("id")
        if "photo" in message:
            logger.info(
                "telegram photo update",
                extra=log_extra(
                    chat_id=chat_id,
                    update_id=update.get("update_id"),
                    media_group_id=message.get("media_group_id"),
                    status="photo",
                ),
            )
            return
        text = message.get("text", "")
        logger.info(
            "telegram text update",
            extra=log_extra(chat_id=chat_id, update_id=update.get("update_id"), status="text", text_preview=text[:40]),
        )

    def _log_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        safe: Dict[str, Any] = {}
        for key, value in payload.items():
            if key == "reply_markup":
                safe[key] = "<keyboard>"
                continue
            if key == "text" and isinstance(value, str):
                safe[key] = value[:120]
                continue
            safe[key] = value
        return safe

    def _sanitize_error(self, text: str) -> str:
        return text.replace(self.token, "<telegram-token>")
