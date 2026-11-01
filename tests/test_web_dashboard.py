import os
from decimal import Decimal
from threading import Thread
import time
from types import SimpleNamespace
from uuid import uuid4
from datetime import date

import pytest
import requests
import uvicorn
from fastapi import FastAPI

from budget_bot.models import ParsedOperation, OperationType
from budget_bot.server import RateLimiter
from budget_bot.storage import Storage
from budget_bot.web_dashboard import router


@pytest.fixture
def web_server(request):
    url = os.getenv("FEATURE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FEATURE_TEST_DATABASE_URL required (isolated database)")
    storage = Storage(url)
    actor = f"telegram:{uuid4().int%10**14}"
    other = f"telegram:{uuid4().int%10**14}"
    with storage.user_scope(actor):
        entry = storage.append_budget_entry(
            "test",
            "web-purchase",
            ParsedOperation(date(2026, 10, 30), "Кафе", -1000, OperationType.EXPENSE, "Еда", "Кофейни"),
            "test",
        )
        storage.confirm_receipt(
            ParsedOperation(date(2026, 11, 2), "Иван", 500, OperationType.INCOME),
            "web-receipt",
            "reimbursement",
            entry,
        )
    with storage.user_scope(other):
        foreign = storage.append_budget_entry(
            "test",
            "foreign",
            ParsedOperation(date(2026, 10, 30), "Чужая", -500, OperationType.EXPENSE, "Еда", "Кофейни"),
            "test",
        )
    app = FastAPI()
    app.include_router(router)
    app.state.context = SimpleNamespace(
        storage=storage,
        settings=SimpleNamespace(
            default_timezone="Europe/Moscow",
            telegram_bot_token="synthetic",
            telegram_allow_all=False,
            telegram_allowed_user_ids={int(actor.split(":")[1])},
            web_base_url=getattr(request, "param", ""),
        ),
    )
    app.state.rate_limiter = RateLimiter(100, 60)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="error"))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    try:
        yield storage, actor, other, entry, foreign, base
    finally:
        server.should_exit = True
        thread.join(5)
        with storage._connect() as connection:
            connection.execute("DELETE FROM web_tokens WHERE user_id=ANY(%s)", ([actor, other],))
            connection.execute("DELETE FROM users WHERE owner_id=ANY(%s)", ([actor, other],))
        storage.close()


def test_real_http_login_financial_totals_and_idor(web_server):
    storage, actor, other, entry, foreign, base = web_server
    client = requests.Session()
    assert client.get(base + "/api/dashboard").status_code == 401
    assert client.post(base + "/api/login", json={"init_data": "user=123&hash=forged"}).status_code == 401
    token = storage.issue_web_token(actor)
    assert client.post(base + "/api/login", json={"token": token}).status_code == 200
    assert client.cookies["budget_session"]
    assert client.post(base + "/api/login", json={"token": token}).status_code == 401
    assert client.get(base + "/api/dashboard", params={"budget": other}).status_code == 403
    assert client.get(base + f"/api/purchases/{foreign}").status_code == 404
    result = client.get(base + "/api/dashboard", params={"start": "2026-10-01", "end": "2026-10-31"})
    assert result.status_code == 200, result.text
    payload = result.json()
    assert Decimal(payload["metrics"]["expenses"]) == 500
    assert Decimal(payload["metrics"]["paid"]) == 1000
    assert Decimal(payload["metrics"]["cash_flow"]) == -1000
    assert payload["expenses"][0]["id"] == entry
    assert payload["series"][0]["amount"] == "500.00"
    assert client.get(base + "/api/dashboard", params={"sort": "amount;drop table users"}).status_code == 422
    for path in [
        "/dashboard/../../.env",
        "/dashboard/data/images",
        "/data/exports",
        "/dashboard/../TECHNICAL_SPEC.md",
    ]:
        assert client.get(base + path).status_code == 404
    assert client.post(base + "/api/logout").status_code == 200
    assert client.get(base + "/api/dashboard").status_code == 401


def test_mini_app_works_without_cookies_and_revokes_bearer(web_server):
    import hashlib
    import hmac
    import json
    from urllib.parse import urlencode

    storage, actor, other, entry, foreign, base = web_server
    user = int(actor.split(":")[1])
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user})}
    key = hmac.new(b"WebAppData", b"synthetic", hashlib.sha256).digest()
    data["hash"] = hmac.new(
        key, "\n".join(f"{name}={value}" for name, value in sorted(data.items())).encode(), hashlib.sha256
    ).hexdigest()
    response = requests.post(base + "/api/login", json={"init_data": urlencode(data)})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    token = response.json()["session_token"]
    headers = {"Authorization": "Bearer " + token}
    # No Session/cookies: this is how an embedded Mini App can authenticate.
    assert requests.get(base + "/api/budgets", headers=headers).status_code == 200
    assert requests.get(base + "/api/dashboard", params={"budget": other}, headers=headers).status_code == 403
    assert requests.get(base + f"/api/purchases/{foreign}", headers=headers).status_code == 404
    assert requests.post(base + "/api/logout", headers=headers).status_code == 200
    assert requests.get(base + "/api/budgets", headers=headers).status_code == 401


def test_invalid_authorization_does_not_fall_back_to_valid_cookie(web_server):
    storage, actor, _, _, _, base = web_server
    client = requests.Session()
    response = client.post(base + "/api/login", json={"token": storage.issue_web_token(actor)})
    assert "session_token" not in response.json()
    assert client.get(base + "/api/budgets").status_code == 200
    for authorization in ["Bearer invalid", "Basic ignored", "Bearer "]:
        assert client.get(base + "/api/budgets", headers={"Authorization": authorization}).status_code == 401
    assert client.get(base + "/api/budgets").status_code == 200


@pytest.mark.parametrize("web_server", ["https://finance.example/dashboard"], indirect=True)
def test_https_deployment_sets_secure_cookie_behind_http_proxy(web_server):
    storage, actor, _, _, _, base = web_server
    response = requests.post(base + "/api/login", json={"token": storage.issue_web_token(actor)})
    assert response.status_code == 200
    assert next(iter(response.cookies)).secure
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=strict" in response.headers["Set-Cookie"]
