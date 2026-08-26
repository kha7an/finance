from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, Optional

_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        extras = [
            f"{key}={value}"
            for key, value in sorted(record.__dict__.items())
            if key not in _RESERVED and not key.startswith("_")
        ]
        if extras:
            return f"{message} {' '.join(extras)}"
        return message


def _log_level_from_env() -> int:
    raw = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


def _log_format_from_env() -> str:
    return os.getenv("LOG_FORMAT", "text").strip().lower()


def setup_logging(level: Optional[int] = None, force: bool = False) -> None:
    if getattr(setup_logging, "_configured", False) and not force:
        return

    resolved_level = level if level is not None else _log_level_from_env()
    handler = logging.StreamHandler(sys.stderr)
    if _log_format_from_env() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)

    logging.getLogger("alembic").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    setup_logging._configured = True  # type: ignore[attr-defined]


def reconfigure_logging() -> None:
    setup_logging(force=True)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def log_extra(
    *,
    owner_id: Optional[str] = None,
    chat_id: Optional[int] = None,
    image_hash: Optional[str] = None,
    media_group_id: Optional[str] = None,
    job_id: Optional[int] = None,
    elapsed: Optional[float] = None,
    status: Optional[str] = None,
    **fields: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if owner_id is not None:
        payload["owner_id"] = owner_id
    if chat_id is not None:
        payload["chat_id"] = chat_id
    if image_hash is not None:
        payload["image_hash"] = image_hash
    if media_group_id is not None:
        payload["media_group_id"] = media_group_id
    if job_id is not None:
        payload["job_id"] = job_id
    if elapsed is not None:
        payload["elapsed"] = round(elapsed, 3)
    if status is not None:
        payload["status"] = status
    payload.update(fields)
    return payload
