from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterator

from .config import Settings, validate_llm_settings
from .llm import VisionClient, build_vision_client
from .log_config import get_logger, log_extra
from .processor import ScreenshotProcessor
from .storage import Storage


logger = get_logger(__name__)


@dataclass
class BudgetRuntime:
    processor: ScreenshotProcessor


class AppContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = Storage(settings.database_url)
        self._runtime_cache: Dict[str, BudgetRuntime] = {}
        self.vision_client = build_vision_client(
            provider=settings.llm_provider,
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model,
            openai_proxy_url=settings.openai_proxy_url,
            openai_timeout_seconds=settings.openai_timeout_seconds,
            gemini_api_key=settings.gemini_api_key,
            gemini_model=settings.gemini_model,
        )

    @contextmanager
    def owner_scope(self, owner_id: str) -> Iterator[None]:
        with self.storage.owner_scope(owner_id):
            yield

    @property
    def owner_id(self) -> str:
        return self.storage.owner_id

    @property
    def category_book(self):
        return self.storage.category_book()

    @property
    def processor(self) -> ScreenshotProcessor:
        return self._runtime().processor

    def budget_account_summary(self) -> str:
        return f"Postgres: {self.settings.database_url.split('@')[-1]}"

    def _runtime(self) -> BudgetRuntime:
        owner_id = self.owner_id
        cached = self._runtime_cache.get(owner_id)
        if cached is not None:
            return cached
        runtime = BudgetRuntime(
            processor=ScreenshotProcessor(
                storage=self.storage,
                category_book=self.storage.category_book(),
            ),
        )
        self._runtime_cache[owner_id] = runtime
        return runtime

    def parse_and_process(
        self,
        image_content: bytes,
        mime_type: str,
        screenshot_date: date,
        telegram_file_id: str | None = None,
    ):
        started_at = time.monotonic()
        logger.info(
            "parse pipeline llm start",
            extra=log_extra(
                owner_id=self.owner_id,
                status="llm_start",
                bytes=len(image_content),
                mime_type=mime_type,
            ),
        )
        parsed = self.vision_client.parse_screenshot(
            image_content=image_content,
            mime_type=mime_type,
            category_book=self.category_book,
            screenshot_date=screenshot_date,
        )
        llm_elapsed = time.monotonic() - started_at
        logger.info(
            "parse pipeline llm done",
            extra=log_extra(
                owner_id=self.owner_id,
                status="llm_done",
                elapsed=llm_elapsed,
                operations=len(parsed.operations),
            ),
        )
        process_started_at = time.monotonic()
        result = self.processor.process(
            image_content=image_content,
            parsed=parsed,
            telegram_file_id=telegram_file_id,
        )
        logger.info(
            "parse pipeline processor done",
            extra=log_extra(
                owner_id=self.owner_id,
                status="processor_done",
                elapsed=time.monotonic() - process_started_at,
                total=time.monotonic() - started_at,
                decisions=len(result.decisions),
            ),
        )
        return result

    def parse_and_process_many(
        self,
        images: list[tuple[bytes, str]],
        screenshot_date: date,
        telegram_file_id: str | None = None,
    ):
        started_at = time.monotonic()
        total_bytes = sum(len(content) for content, _mime_type in images)
        logger.info(
            "parse pipeline llm batch start",
            extra=log_extra(
                owner_id=self.owner_id,
                status="llm_batch_start",
                images=len(images),
                bytes=total_bytes,
            ),
        )
        parsed = self.vision_client.parse_screenshots(
            images=images,
            category_book=self.category_book,
            screenshot_date=screenshot_date,
        )
        llm_elapsed = time.monotonic() - started_at
        logger.info(
            "parse pipeline llm batch done",
            extra=log_extra(
                owner_id=self.owner_id,
                status="llm_batch_done",
                elapsed=llm_elapsed,
                operations=len(parsed.operations),
            ),
        )
        process_started_at = time.monotonic()
        result = self.processor.process(
            image_content=_combined_image_content(images),
            parsed=parsed,
            telegram_file_id=telegram_file_id,
        )
        logger.info(
            "parse pipeline processor done",
            extra=log_extra(
                owner_id=self.owner_id,
                status="processor_done",
                elapsed=time.monotonic() - process_started_at,
                total=time.monotonic() - started_at,
                decisions=len(result.decisions),
            ),
        )
        return result


def build_context() -> AppContext:
    settings = Settings.from_env()
    validate_llm_settings(settings)
    return AppContext(settings)


def _combined_image_content(images: list[tuple[bytes, str]]) -> bytes:
    chunks: list[bytes] = []
    for content, mime_type in images:
        chunks.append(mime_type.encode("utf-8"))
        chunks.append(str(len(content)).encode("ascii"))
        chunks.append(content)
    return b"\0".join(chunks)
