from __future__ import annotations

import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .log_config import get_logger, log_extra
from .processor import ProcessingResult


logger = get_logger(__name__)


class ParseJobWorker:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    def process_next_job(self) -> bool:
        job = self.bot.context.storage.claim_next_parse_job()
        if job is None:
            return False

        job_id = int(job["id"])
        owner_id = str(job["owner_id"])
        chat_id = int(job["chat_id"])
        payload = job["payload"] or {}
        started_at = time.monotonic()

        logger.info(
            "parse job started",
            extra=log_extra(
                owner_id=owner_id,
                chat_id=chat_id,
                job_id=job_id,
                status="running",
                job_kind=job.get("job_kind"),
            ),
        )

        try:
            with self.bot.context.owner_scope(owner_id):
                result = self._run_job(job, payload)
        except Exception as exc:
            error_text = str(exc)
            with self.bot.context.owner_scope(owner_id):
                self.bot.context.storage.requeue_failed_parse_job(job_id, error_text)
                job_row = self.bot.context.storage.get_parse_job(job_id)
            if job_row and job_row.get("status") == "failed":
                try:
                    self.bot._send_message(chat_id, f"Не смог обработать скрин: {error_text}")
                except Exception:
                    logger.exception(
                        "parse job failure notification failed",
                        extra=log_extra(owner_id=owner_id, chat_id=chat_id, job_id=job_id, status="failed"),
                    )
            logger.exception(
                "parse job failed",
                extra=log_extra(
                    owner_id=owner_id,
                    chat_id=chat_id,
                    job_id=job_id,
                    status="failed",
                    elapsed=time.monotonic() - started_at,
                ),
            )
            return True

        with self.bot.context.owner_scope(owner_id):
            self.bot.context.storage.finish_parse_job(job_id, "done")
            try:
                self.bot._send_processing_result(chat_id, result)
            except Exception:
                logger.exception(
                    "parse job response send failed",
                    extra=log_extra(owner_id=owner_id, chat_id=chat_id, job_id=job_id, status="done"),
                )
        logger.info(
            "parse job done",
            extra=log_extra(
                owner_id=owner_id,
                chat_id=chat_id,
                job_id=job_id,
                status="done",
                elapsed=time.monotonic() - started_at,
                decisions=len(result.decisions),
            ),
        )
        return True

    def _run_job(self, job: Dict[str, Any], payload: Dict[str, Any]) -> ProcessingResult:
        job_kind = str(job["job_kind"])
        screenshot_date = date.fromisoformat(str(payload.get("screenshot_date") or date.today().isoformat()))
        file_ids: List[str] = list(payload.get("file_ids") or [])
        media_group_id = payload.get("media_group_id")

        images: List[Tuple[bytes, str]] = []
        for file_id in file_ids:
            content, mime_type = self.bot._download_file(file_id)
            self.bot._save_image_for_replay(content, mime_type)
            images.append((content, mime_type))

        if job_kind == "album":
            return self.bot.context.parse_and_process_many(
                images=images,
                screenshot_date=screenshot_date,
                telegram_file_id=f"media_group:{media_group_id}" if media_group_id else None,
            )

        if not images:
            raise ValueError("Parse job has no images")
        content, mime_type = images[0]
        return self.bot.context.parse_and_process(
            image_content=content,
            mime_type=mime_type,
            screenshot_date=screenshot_date,
            telegram_file_id=file_ids[0] if file_ids else None,
        )

    def enqueue_single(self, chat_id: int, file_id: str, screenshot_date: Optional[date] = None) -> int:
        payload = {
            "file_ids": [file_id],
            "screenshot_date": (screenshot_date or date.today()).isoformat(),
        }
        return self.bot.context.storage.enqueue_parse_job(chat_id, "single", payload)

    def enqueue_album(
        self,
        chat_id: int,
        file_ids: List[str],
        media_group_id: str,
        screenshot_date: Optional[date] = None,
    ) -> int:
        payload = {
            "file_ids": file_ids,
            "media_group_id": media_group_id,
            "screenshot_date": (screenshot_date or date.today()).isoformat(),
        }
        return self.bot.context.storage.enqueue_parse_job(chat_id, "album", payload)
