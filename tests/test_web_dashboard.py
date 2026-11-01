import os
from decimal import Decimal
from threading import Thread
import time
from types import SimpleNamespace
from uuid import uuid4
from datetime import date
from concurrent.futures import ThreadPoolExecutor

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
            ParsedOperation(date(2026, 10, 30), "Кафе", -1000, OperationType.EXPENSE, "Еда", "Кофейни", note="Исходная заметка"),
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
            telegram_allowed_user_ids={int(user.split(":")[1]) for user in (actor, other)},
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


def edit_body(detail, **changes):
    row = detail["purchase"]
    return {
        "operation_date": row["operation_date"],
        "amount": str(row["amount"]),
        "name": row["name"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "version": detail["version"],
        **changes,
    }


def test_http_edit_saves_recalculates_and_preserves_receipts(web_server):
    storage, actor, _, entry, foreign, base = web_server
    client = requests.Session()
    assert client.post(base + "/api/login", json={"token": storage.issue_web_token(actor)}).status_code == 200
    url = base + f"/api/purchases/{entry}"
    detail = client.get(url).json()
    assert detail["can_edit"] is True
    target = next(row for row in detail["categories"] if row["name"] != "Еда" and row["subcategories"])
    body = edit_body(detail, name=" Новое кафе ", amount="1250.75", operation_date="2026-10-29",
                     category=target["name"], subcategory=target["subcategories"][0]["name"])
    assert requests.put(url, json=body, headers={"X-Budget-Edit": "1"}).status_code == 401
    assert client.put(url, json=body).status_code == 403
    assert client.put(url, json=body, headers={"X-Budget-Edit": "1", "Authorization": "Bearer forged"}).status_code == 401
    assert client.put(base + f"/api/purchases/{foreign}", json=body, headers={"X-Budget-Edit": "1"}).status_code == 404
    response = client.put(url, json=body, headers={"X-Budget-Edit": "1"})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    updated = response.json()
    assert updated["purchase"]["name"] == "Новое кафе"
    assert updated["purchase"]["operation_date"] == "2026-10-29"
    assert Decimal(str(updated["purchase"]["amount"])) == Decimal("1250.75")
    assert Decimal(updated["purchase"]["personal_amount"]) == Decimal("750.75")
    assert updated["purchase"]["category"] == target["name"]
    assert updated["purchase"]["note"] == "Исходная заметка"
    assert updated["purchase"]["author_id"] == actor
    assert updated["purchase"]["operation_type"] == "expense"
    assert len(updated["reimbursements"]) == len(detail["reimbursements"])
    for before, after in zip(detail["reimbursements"], updated["reimbursements"]):
        for key in ("id", "expense_entry_id", "operation_date", "amount", "name"):
            assert after[key] == before[key]
        assert after["purchase_date"] == "2026-10-29"
        assert after["category"] == target["name"]
    assert updated["version"] != detail["version"]
    totals = client.get(base + "/api/dashboard", params={"start": "2026-10-01", "end": "2026-10-31"}).json()
    assert Decimal(totals["metrics"]["expenses"]) == Decimal("750.75")
    assert Decimal(totals["metrics"]["paid"]) == Decimal("1250.75")
    with storage._connect() as connection:
        audit = connection.execute(
            "SELECT actor_id FROM entry_changes WHERE owner_id=%s AND entry_id=%s ORDER BY id DESC LIMIT 1",
            (actor, entry),
        ).fetchone()
    assert audit["actor_id"] == actor


def test_http_edit_rejects_invalid_fields_and_receipt_conflicts(web_server):
    storage, actor, _, entry, _, base = web_server
    client = requests.Session()
    client.post(base + "/api/login", json={"token": storage.issue_web_token(actor)})
    url = base + f"/api/purchases/{entry}"
    detail = client.get(url).json()
    invalid = [
        {"amount": value} for value in ("0", "-1", "NaN", "Infinity", "0.001", "1000000000000", "499.99")
    ] + [
        {"operation_date": "2026-11-03"}, {"operation_date": "2026-02-30"}, {"name": "   "},
        {"category": "Чужая категория"}, {"subcategory": "Чужая подкатегория"},
        {"category": "Дом", "subcategory": "Кофейни"}, {"operation_type": "income"},
        {"author_id": "telegram:another"}, {"note": "Подмена заметки"},
    ]
    for changes in invalid:
        response = client.put(url, json=edit_body(detail, **changes), headers={"X-Budget-Edit": "1"})
        assert response.status_code == 422, (changes, response.text)
        assert client.get(url).json()["version"] == detail["version"]


def test_http_edit_detects_parallel_changes_and_new_reimbursements(web_server):
    storage, actor, _, entry, _, base = web_server
    client = requests.Session()
    client.post(base + "/api/login", json={"token": storage.issue_web_token(actor)})
    url = base + f"/api/purchases/{entry}"
    detail = client.get(url).json()
    cookies = client.cookies.get_dict()
    def save(name):
        return requests.put(url, json=edit_body(detail, name=name), cookies=cookies, headers={"X-Budget-Edit": "1"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["Первая правка", "Вторая правка"]))
    assert sorted(response.status_code for response in results) == [200, 409]
    fresh = client.get(url).json()
    with storage.user_scope(actor):
        storage.confirm_receipt(
            ParsedOperation(date(2026, 11, 3), "Дополнительный возврат", 50, OperationType.INCOME),
            "extra-web-receipt", "reimbursement", entry,
        )
    # The purchase's timestamp does not change when a new reimbursement is linked.
    assert client.get(url).json()["purchase"]["updated_at"] == fresh["purchase"]["updated_at"]
    assert client.put(url, json=edit_body(fresh, name="Старая форма"), headers={"X-Budget-Edit": "1"}).status_code == 409


def test_http_edit_enforces_shared_budget_authorship_and_revocation(web_server):
    storage, owner, member, _, _, base = web_server
    with storage.user_scope(owner):
        shared = storage.create_shared_budget("Тест редактирования")
    try:
        with storage.user_scope(owner, shared):
            invite = storage.invite_member()
            owner_entry = storage.append_budget_entry(
                "test", "owner-entry", ParsedOperation(date(2026, 10, 30), "Расход владельца", -1000,
                                                        OperationType.EXPENSE, "Еда", "Кофейни"), "test")
        with storage.user_scope(member):
            storage.accept_invite(invite)
        with storage.user_scope(member, shared):
            own_entry = storage.append_budget_entry(
                "test", "member-entry", ParsedOperation(date(2026, 10, 30), "Свой расход", -300,
                                                         OperationType.EXPENSE, "Еда", "Кофейни"), "test")
        owner_client, member_client = requests.Session(), requests.Session()
        for client, actor in [(owner_client, owner), (member_client, member)]:
            client.post(base + "/api/login", json={"token": storage.issue_web_token(actor)})
        params, headers = {"budget": shared}, {"X-Budget-Edit": "1"}
        url = base + f"/api/purchases/{owner_entry}"
        foreign = member_client.get(url, params=params).json()
        assert foreign["can_edit"] is False
        assert member_client.put(url, params=params, json=edit_body(foreign), headers=headers).status_code == 403
        own_url = base + f"/api/purchases/{own_entry}"
        own = member_client.get(own_url, params=params).json()
        assert own["can_edit"] is True
        assert member_client.put(own_url, params=params, json=edit_body(own, amount="350"), headers=headers).status_code == 200
        owner_view = owner_client.get(own_url, params=params).json()
        assert owner_view["can_edit"] is True
        assert owner_client.put(own_url, params=params, json=edit_body(owner_view, name="Правка владельца"), headers=headers).status_code == 200
        with storage.user_scope(owner, shared):
            storage.remove_member(member)
        assert member_client.put(own_url, params=params, json=edit_body(own), headers=headers).status_code == 403
    finally:
        with storage._connect() as connection:
            connection.execute("DELETE FROM users WHERE owner_id=%s", (shared,))
