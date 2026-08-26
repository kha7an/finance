from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
from threading import Lock
from typing import Deque, Dict, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile

from .app_factory import AppContext, build_context
from .log_config import get_logger, log_extra


logger = get_logger(__name__)

RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 20


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._events: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._events[key]
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
            return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.context = build_context()
    app.state.rate_limiter = RateLimiter(RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)
    logger.info("fastapi app started")
    yield
    app.state.context.storage.close()


app = FastAPI(title="Budget Bot", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    started_at = time.monotonic()
    logger.info(
        "http request",
        extra=log_extra(
            method=request.method,
            path=request.url.path,
            client=request.client.host if request.client else "unknown",
            status="request",
        ),
    )
    response = await call_next(request)
    logger.info(
        "http response",
        extra=log_extra(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            elapsed=time.monotonic() - started_at,
            status="ok" if response.status_code < 400 else "error",
        ),
    )
    return response


def get_context(request: Request) -> AppContext:
    return request.app.state.context


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def _authorize_upload(context: AppContext, authorization: Optional[str]) -> str:
    token = context.settings.api_token
    if not token:
        raise HTTPException(status_code=403, detail="Set BUDGET_API_TOKEN to enable uploads")
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid upload token")
    return token


@app.get("/health")
def health(context: AppContext = Depends(get_context)) -> dict:
    return {
        "ok": True,
        "llm_provider": context.settings.llm_provider,
        "database": context.settings.database_url.split("@")[-1],
    }


@app.post("/parse-image")
async def parse_image(
    request: Request,
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(default=None),
    context: AppContext = Depends(get_context),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
) -> dict:
    token = _authorize_upload(context, authorization)
    client_key = request.client.host if request.client else "unknown"
    if not rate_limiter.allow(f"{token}:{client_key}"):
        raise HTTPException(status_code=429, detail="Too many upload requests")

    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Upload must be an image")

    content = await file.read()
    if len(content) > context.settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Upload is too large")

    started_at = time.monotonic()
    result = context.parse_and_process(
        image_content=content,
        mime_type=file.content_type or "image/jpeg",
        screenshot_date=date.today(),
        telegram_file_id=None,
    )
    logger.info(
        "http parse-image done",
        extra=log_extra(
            elapsed=time.monotonic() - started_at,
            status="done",
            decisions=len(result.decisions),
        ),
    )
    return {
        "image_hash": result.image_hash,
        "bank": result.bank,
        "decisions": [
            {
                "date": item.operation.date.isoformat(),
                "name": item.operation.name,
                "amount": item.operation.amount,
                "type": item.operation.type.value,
                "category": item.operation.category,
                "subcategory": item.operation.subcategory,
                "status": item.status.value,
                "reason": item.reason,
                "workbook_row": item.workbook_row,
            }
            for item in result.decisions
        ],
    }
