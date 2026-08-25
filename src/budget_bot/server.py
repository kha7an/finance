from __future__ import annotations

from datetime import date

from fastapi import FastAPI, File, Header, HTTPException, UploadFile

from .app_factory import build_context


context = build_context()
app = FastAPI(title="Budget Bot", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "llm_provider": context.settings.llm_provider,
        "database": context.settings.database_url.split("@")[-1],
    }


@app.post("/parse-image")
async def parse_image(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    _authorize_upload(authorization)
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Upload must be an image")

    content = await file.read()
    if len(content) > context.settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Upload is too large")

    result = context.parse_and_process(
        image_content=content,
        mime_type=file.content_type or "image/jpeg",
        screenshot_date=date.today(),
        telegram_file_id=None,
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


def _authorize_upload(authorization: str | None) -> None:
    token = context.settings.api_token
    if not token:
        raise HTTPException(status_code=403, detail="Set BUDGET_API_TOKEN to enable uploads")
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid upload token")
