from __future__ import annotations

import os
from datetime import date

import pytest

from budget_bot.models import OperationStatus, OperationType, ParsedOperation, ParsedScreenshot
from budget_bot.processor import ScreenshotProcessor
from budget_bot.storage import Storage, operation_hash, telegram_owner_id


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL is required for integration tests",
)


@pytest.fixture()
def storage() -> Storage:
    return Storage(os.environ["DATABASE_URL"])


@pytest.fixture()
def processor(storage: Storage) -> ScreenshotProcessor:
    return ScreenshotProcessor(storage, storage.category_book())


def test_owner_scope_isolates_operation_hashes(storage: Storage) -> None:
    operation = ParsedOperation(
        date=date(2026, 8, 21),
        name="Test Shop",
        amount=-100.0,
        type=OperationType.EXPENSE,
        category="Еда",
        subcategory="Супермаркеты",
    )
    with storage.owner_scope("telegram:111"):
        hash_a = operation_hash("mock", operation)
    with storage.owner_scope("telegram:222"):
        hash_b = operation_hash("mock", operation)
    assert hash_a != hash_b


def test_parse_job_queue_roundtrip(storage: Storage) -> None:
    owner_id = telegram_owner_id(999001)
    with storage.owner_scope(owner_id):
        storage.reset_all()
        job_id = storage.enqueue_parse_job(
            chat_id=123456,
            job_kind="single",
            payload={"file_ids": ["file-1"], "screenshot_date": date.today().isoformat()},
        )
        claimed = storage.claim_next_parse_job()
        assert claimed is not None
        assert int(claimed["id"]) == job_id
        assert claimed["status"] == "running"
        storage.finish_parse_job(job_id, "done")
        finished = storage.get_parse_job(job_id)
        assert finished is not None
        assert finished["status"] == "done"


def test_processor_duplicate_is_ignored(processor: ScreenshotProcessor, storage: Storage) -> None:
    owner_id = telegram_owner_id(999002)
    with storage.owner_scope(owner_id):
        storage.reset_all()
        parsed = ParsedScreenshot.from_json(
            {
                "bank": "mock",
                "period": {"month": 8, "year": 2026, "screenshot_date": "2026-08-21"},
                "operations": [
                    {
                        "date": "2026-08-21",
                        "name": "Duplicate Shop",
                        "amount": -50.0,
                        "type": "expense",
                        "category": "Еда",
                        "subcategory": "Супермаркеты",
                    }
                ],
            }
        )
        content = b"duplicate-image-bytes"
        first = processor.process(content, parsed, telegram_file_id="f1")
        second = processor.process(content, parsed, telegram_file_id="f2")
        assert len(first.decisions) == 1
        assert first.decisions[0].status == OperationStatus.AUTO_WRITTEN
        assert second.decisions == []
