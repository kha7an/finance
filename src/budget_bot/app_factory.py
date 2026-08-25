from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterator

from .config import Settings
from .llm import VisionClient, build_vision_client
from .processor import ScreenshotProcessor
from .storage import Storage


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
        print(
            f"Parse pipeline: llm start provider={self.settings.llm_provider} "
            f"bytes={len(image_content)} mime={mime_type}",
            flush=True,
        )
        parsed = self.vision_client.parse_screenshot(
            image_content=image_content,
            mime_type=mime_type,
            category_book=self.category_book,
            screenshot_date=screenshot_date,
        )
        llm_elapsed = time.monotonic() - started_at
        print(
            f"Parse pipeline: llm done elapsed={llm_elapsed:.2f}s "
            f"operations={len(parsed.operations)}",
            flush=True,
        )
        process_started_at = time.monotonic()
        result = self.processor.process(
            image_content=image_content,
            parsed=parsed,
            telegram_file_id=telegram_file_id,
        )
        print(
            f"Parse pipeline: processor done elapsed={time.monotonic() - process_started_at:.2f}s "
            f"total={time.monotonic() - started_at:.2f}s {_decision_counts_text(result.decisions)}",
            flush=True,
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
        print(
            f"Parse pipeline: llm batch start provider={self.settings.llm_provider} "
            f"images={len(images)} bytes={total_bytes}",
            flush=True,
        )
        parsed = self.vision_client.parse_screenshots(
            images=images,
            category_book=self.category_book,
            screenshot_date=screenshot_date,
        )
        llm_elapsed = time.monotonic() - started_at
        print(
            f"Parse pipeline: llm batch done elapsed={llm_elapsed:.2f}s "
            f"operations={len(parsed.operations)}",
            flush=True,
        )
        process_started_at = time.monotonic()
        result = self.processor.process(
            image_content=_combined_image_content(images),
            parsed=parsed,
            telegram_file_id=telegram_file_id,
        )
        print(
            f"Parse pipeline: processor done elapsed={time.monotonic() - process_started_at:.2f}s "
            f"total={time.monotonic() - started_at:.2f}s {_decision_counts_text(result.decisions)}",
            flush=True,
        )
        return result


def build_context() -> AppContext:
    return AppContext(Settings.from_env())


def _combined_image_content(images: list[tuple[bytes, str]]) -> bytes:
    chunks: list[bytes] = []
    for content, mime_type in images:
        chunks.append(mime_type.encode("utf-8"))
        chunks.append(str(len(content)).encode("ascii"))
        chunks.append(content)
    return b"\0".join(chunks)


def _decision_counts_text(decisions) -> str:
    counts: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    for decision in decisions:
        counts[decision.status.value] = counts.get(decision.status.value, 0) + 1
        reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
    count_text = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"
    reason_text = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())) or "none"
    return f"decisions={len(decisions)} statuses=[{count_text}] reasons=[{reason_text}]"
