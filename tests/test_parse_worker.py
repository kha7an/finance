from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from budget_bot.parse_worker import ParseJobWorker


class FakeResult:
    decisions = [1]


class FakeContext:
    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self.owner_id = "default"

    @contextmanager
    def owner_scope(self, owner_id: str):
        previous = self.owner_id
        self.owner_id = owner_id
        try:
            yield
        finally:
            self.owner_id = previous

    def parse_and_process(self, **_kwargs):
        return FakeResult()


class FakeStorage:
    def __init__(self) -> None:
        self.job = {
            "id": 7,
            "owner_id": "telegram:42",
            "chat_id": 100,
            "job_kind": "single",
            "payload": {"file_ids": ["file-1"]},
        }
        self.finished: List[int] = []
        self.requeued: List[int] = []

    def claim_next_parse_job(self) -> Optional[Dict[str, Any]]:
        job = self.job
        self.job = None
        return job

    def finish_parse_job(self, job_id: int, status: str) -> None:
        assert status == "done"
        self.finished.append(job_id)

    def requeue_failed_parse_job(self, job_id: int, error_text: str) -> None:
        self.requeued.append(job_id)


class FakeBot:
    def __init__(self) -> None:
        self.storage = FakeStorage()
        self.context = FakeContext(self.storage)
        self.send_owner_ids: List[str] = []

    def _download_file(self, file_id: str):
        return b"image", "image/jpeg"

    def _save_image_for_replay(self, content: bytes, mime_type: str) -> None:
        return None

    def _send_processing_result(self, chat_id: int, result: Any) -> None:
        self.send_owner_ids.append(self.context.owner_id)


def test_parse_worker_sends_result_inside_job_owner_scope() -> None:
    bot = FakeBot()

    assert ParseJobWorker(bot).process_next_job() is True

    assert bot.storage.finished == [7]
    assert bot.storage.requeued == []
    assert bot.send_owner_ids == ["telegram:42"]
    assert bot.context.owner_id == "default"


def test_parse_worker_does_not_requeue_done_job_when_response_send_fails() -> None:
    class FailingSendBot(FakeBot):
        def _send_processing_result(self, chat_id: int, result: Any) -> None:
            raise RuntimeError("telegram is down")

    bot = FailingSendBot()

    assert ParseJobWorker(bot).process_next_job() is True

    assert bot.storage.finished == [7]
    assert bot.storage.requeued == []
