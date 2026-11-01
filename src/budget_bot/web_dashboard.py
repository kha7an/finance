"""Dashboard and purchase editing API. Money calculations stay on the server."""

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .analytics import period_metrics, is_mandatory
from .models import OperationType, ParsedOperation
from .planning import amount_decimal, financial_period
from .web_auth import validate_init_data

router = APIRouter()
ASSETS = Path(__file__).parent / "web"


class Login(BaseModel):
    token: str = ""
    init_data: str = ""


class PurchaseEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation_date: date
    amount: Decimal
    name: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=64)
    subcategory: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value):
        return amount_decimal(value)


@router.get("/dashboard")
async def dashboard():
    return FileResponse(
        ASSETS / "index.html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
        },
    )


@router.get("/dashboard/{asset}")
async def asset(asset: str):
    if asset not in {"app.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(ASSETS / asset)


def allowed(context, user):
    return context.settings.telegram_allow_all or user in context.settings.telegram_allowed_user_ids


@router.post("/api/login")
async def login(body: Login, request: Request):
    context = request.app.state.context
    limiter = request.app.state.rate_limiter
    if not limiter.allow("login:" + (request.client.host if request.client else "unknown")):
        raise HTTPException(429, detail="Слишком много попыток входа.")
    try:
        if body.init_data:
            user = validate_init_data(body.init_data, context.settings.telegram_bot_token)
            actor = f"telegram:{user}"
        else:
            actor = context.storage.consume_login_token(body.token)
            user = int(actor.split(":")[1])
        if not allowed(context, user):
            raise ValueError("Нет доступа.")
        token = context.storage.issue_web_token(actor, "session")
    except (ValueError, IndexError):
        raise HTTPException(401, detail="Открой новую ссылку из команды /web в личном чате с ботом.") from None
    payload = {"ok": True}
    if body.init_data:
        # Mini Apps in Telegram Web may run with third-party cookies blocked.
        payload["session_token"] = token
    response = JSONResponse(payload)
    response.set_cookie(
        "budget_session",
        token,
        max_age=86400,
        httponly=True,
        secure=request.url.scheme == "https"
        or urlsplit(getattr(context.settings, "web_base_url", "")).scheme == "https",
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/logout")
async def logout(request: Request):
    request.app.state.context.storage.revoke_web_token(session_credential(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie("budget_session")
    response.headers["Cache-Control"] = "no-store"
    return response


def session_credential(request: Request) -> str:
    authorization = request.headers.get("authorization")
    if authorization is None:
        return request.cookies.get("budget_session", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token:
        raise HTTPException(401, detail="Недействительная сессия.")
    return token


async def authorized(request: Request, budget: str = ""):
    context = request.app.state.context
    actor = context.storage.web_identity(session_credential(request))
    if not actor or not allowed(context, int(actor.split(":")[1])):
        raise HTTPException(401, detail="Войди через Telegram: команда /web.")
    try:
        with context.storage.user_scope(actor, budget or actor):
            yield context
    except ValueError as exc:
        raise HTTPException(403, detail=str(exc)) from None


@router.get("/api/budgets")
async def budgets(context=Depends(authorized)):
    return context.storage.list_budgets(context.storage.actor_id)


@router.get("/api/dashboard")
async def data(
    request: Request,
    start: date = None,
    end: date = None,
    category: str = "",
    search: str = "",
    author: str = "",
    page: int = Query(0, ge=0),
    sort: str = "date",
    bucket: str = "day",
    exclude_mandatory: bool = False,
    context=Depends(authorized),
):
    today = datetime.now(ZoneInfo(context.settings.default_timezone)).date()
    settings = context.storage.analytics_settings()
    default_start, default_end = financial_period(today, settings.get("period_day", 1))
    start, end = start or default_start, end or today
    if (
        start > end
        or (end - start).days > 3660
        or sort not in {"date", "date_asc", "amount", "amount_asc"}
        or bucket not in {"day", "month"}
    ):
        raise HTTPException(422, detail="Проверь период (до 10 лет), сортировку и шаг графика.")
    storage = context.storage
    planning = storage.planning_summary(today)
    with storage.snapshot():
        rows = storage.dashboard_rows(start, end, category, search, author)
        settings = {**settings, "exclude_mandatory": exclude_mandatory}
        metrics = period_metrics(rows, settings, start, end)
        expenses = [
            row
            for row in rows
            if row["operation_type"] == "expense" and not (exclude_mandatory and is_mandatory(row, settings))
        ]
        field = "operation_date" if sort.startswith("date") else "personal_amount"
        expenses.sort(key=lambda row: (row[field], row["id"]), reverse=not sort.endswith("_asc"))
        points = {}
        for row in expenses:
            day = row["operation_date"].isoformat() if bucket == "day" else row["operation_date"].strftime("%Y-%m")
            key = (day, row["category"] or "Без категории")
            points[key] = points.get(key, Decimal(0)) + row["personal_amount"]
        payload = {
            "start": start,
            "end": end,
            "metrics": metrics,
            "expenses": expenses[page * 25 : page * 25 + 25],
            "count": len(expenses),
            "page": page,
            "series": [
                {"date": day, "category": name, "amount": value} for (day, name), value in sorted(points.items())
            ],
            "colors": storage.category_colors(),
            "categories": storage.category_catalog(),
            "members": storage.budget_authors(),
            "planning": planning,
            "limits": settings["limits"],
        }
    return JSONResponse(
        jsonable_encoder(payload, custom_encoder={Decimal: str}), headers={"Cache-Control": "no-store"}
    )


@router.get("/api/purchases/{entry_id}")
async def purchase(entry_id: int, context=Depends(authorized)):
    with context.storage.snapshot():
        row = context.storage.get_budget_entry(entry_id)
        if not row or row["operation_type"] != "expense":
            raise HTTPException(404, detail="Покупка не найдена.")
        payload = purchase_payload(context.storage, row)
    return JSONResponse(
        jsonable_encoder(payload, custom_encoder={Decimal: str}), headers={"Cache-Control": "no-store"}
    )


def purchase_version(row):
    # Compare the whole record: timestamps elsewhere have only second precision.
    encoded = jsonable_encoder(row, custom_encoder={Decimal: str})
    return sha256(json.dumps(encoded, sort_keys=True).encode()).hexdigest()


def can_edit_purchase(storage, row):
    return storage.actor_id in {storage.owner_id, row["author_id"]} or any(
        member["user_id"] == storage.actor_id and member["role"] == "owner"
        for member in storage.budget_members()
    )


def purchase_payload(storage, row):
    return {
        "purchase": row,
        "reimbursements": storage.reimbursements(row["id"]),
        "can_edit": can_edit_purchase(storage, row),
        "version": purchase_version(row),
        "categories": [item for item in storage.category_catalog() if item["kind"] == "expense"],
    }


@router.put("/api/purchases/{entry_id}")
async def edit_purchase(
    entry_id: int,
    body: PurchaseEdit,
    x_budget_edit: str = Header(""),
    context=Depends(authorized),
):
    # A cross-origin page cannot send this header without an allowed CORS preflight.
    if x_budget_edit != "1":
        raise HTTPException(403, detail="Сохраните запись через приложение.")
    storage = context.storage
    with storage.atomic():
        row = storage.get_budget_entry(entry_id)
        if not row or row["operation_type"] != "expense":
            raise HTTPException(404, detail="Покупка не найдена.")
        if not can_edit_purchase(storage, row):
            raise HTTPException(403, detail="Можно изменять свои записи. Владелец бюджета может изменять любые.")
        if body.version != purchase_version(row):
            raise HTTPException(409, detail="Запись уже изменилась. Загрузите актуальную версию перед редактированием.")
        if not storage.category_book().is_valid_expense(body.category, body.subcategory):
            raise HTTPException(422, detail="Выберите категорию и подкатегорию из справочника бюджета.")
        try:
            storage.update_budget_entry(
                entry_id,
                ParsedOperation(
                    date=body.operation_date,
                    name=body.name,
                    amount=-body.amount,
                    type=OperationType.EXPENSE,
                    category=body.category,
                    subcategory=body.subcategory,
                    note=row.get("note") or "",
                ),
            )
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        payload = purchase_payload(storage, storage.get_budget_entry(entry_id))
    return JSONResponse(
        jsonable_encoder(payload, custom_encoder={Decimal: str}), headers={"Cache-Control": "no-store"}
    )
