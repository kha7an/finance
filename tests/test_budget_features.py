"""Acceptance checks use only an explicitly selected synthetic database."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from budget_bot.analytics import period_metrics
from budget_bot.excel_exporter import ExcelExporter
from budget_bot.models import OperationType, OperationStatus, ParsedOperation, ParsedScreenshot
from budget_bot.planning import amount_decimal, financial_period, recurring_dates
from budget_bot.processor import ScreenshotProcessor
from budget_bot.storage import Storage
from budget_bot.web_auth import validate_init_data


@pytest.mark.parametrize(
    "today,day,start,end",
    [
        ("2026-02-28", 31, "2026-02-28", "2026-03-30"),
        ("2026-03-30", 31, "2026-02-28", "2026-03-30"),
        ("2026-03-31", 31, "2026-03-31", "2026-04-29"),
        ("2026-01-01", 15, "2025-12-15", "2026-01-14"),
        ("2028-02-29", 31, "2028-02-29", "2028-03-30"),
    ],
)
def test_financial_period_boundaries(today, day, start, end):
    assert financial_period(date.fromisoformat(today), day) == (date.fromisoformat(start), date.fromisoformat(end))
    next_start, _ = financial_period(date.fromisoformat(end) + timedelta(days=1), day)
    assert next_start == date.fromisoformat(end) + timedelta(days=1)


def test_recurring_anchor_returns_to_31_and_money_precision():
    assert list(recurring_dates(date(2026, 1, 31), date(2026, 4, 30), "month")) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]
    for raw in ["NaN", "Infinity", "0", "-1", "0.001", "1000000000000"]:
        with pytest.raises(ValueError):
            amount_decimal(raw)
    assert amount_decimal("1 250,50") == Decimal("1250.50")


def signed_data(token="synthetic", timestamp=1000):
    data = {"auth_date": str(timestamp), "user": json.dumps({"id": 123})}
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(
        secret, "\n".join(f"{k}={v}" for k, v in sorted(data.items())).encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(data)


def test_telegram_auth_rejects_tampering_expiry_and_duplicate_fields():
    raw = signed_data()
    assert validate_init_data(raw, "synthetic", 1001) == 123
    for value, now in [(raw.replace("1000", "1001"), 1001), (raw, 2000), (raw + "&auth_date=1000", 1001)]:
        with pytest.raises(ValueError):
            validate_init_data(value, "synthetic", now)


@pytest.fixture
def db():
    url = os.getenv("FEATURE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FEATURE_TEST_DATABASE_URL required (isolated synthetic database)")
    storage = Storage(url)
    owner = f"feature-test:{uuid4()}"
    with storage.owner_scope(owner):
        yield storage, owner
    with storage._connect() as connection:
        connection.execute("DELETE FROM users WHERE owner_id=%s", (owner,))
    storage.close()


def purchase(storage, amount=1000, day=date(2026, 10, 30), name="Кафе"):
    return storage.append_budget_entry(
        "test", str(uuid4()), ParsedOperation(day, name, -amount, OperationType.EXPENSE, "Еда", "Кофейни"), "test"
    )


def receipt(storage, amount=500, day=date(2026, 10, 31), key=None, purchase_id=None):
    return storage.confirm_receipt(
        ParsedOperation(day, "Иван", amount, OperationType.INCOME), key or str(uuid4()), "reimbursement", purchase_id
    )


def metrics(storage, start, end):
    return period_metrics(storage.analytics_rows(start, end), storage.analytics_settings(), start, end)


def test_refund_partial_full_overpayment_replay_edit_and_delete(db):
    s, _ = db
    p = purchase(s)
    key = str(uuid4())
    r = receipt(s, key=key, purchase_id=p)
    assert receipt(s, key=key, purchase_id=p) == r
    assert s.get_budget_entry(p)["personal_amount"] == 500
    assert metrics(s, date(2026, 10, 1), date(2026, 10, 31))["income"] == 0
    receipt(s, 200, purchase_id=p)
    receipt(s, 300, purchase_id=p)
    assert s.get_budget_entry(p)["personal_amount"] == 0
    assert metrics(s, date(2026, 10, 1), date(2026, 10, 31))["count"] == 1
    with pytest.raises(ValueError, match="ещё"):
        receipt(s, 1, purchase_id=p)
    with pytest.raises(ValueError, match="меньше"):
        s.update_budget_entry(
            p, ParsedOperation(date(2026, 10, 30), "Кафе", -999, OperationType.EXPENSE, "Еда", "Кофейни")
        )
    with pytest.raises(ValueError, match="возвраты"):
        s.delete_budget_entry(p)
    s.delete_budget_entry(r)
    assert s.get_budget_entry(p)["personal_amount"] == 500


def test_cross_period_reassignment_and_income_never_double_counts(db, tmp_path):
    s, _ = db
    p = purchase(s)
    q = purchase(s, 1200, date(2026, 9, 30), "Магазин")
    r = receipt(s, 500, date(2026, 11, 2), purchase_id=p)
    october = metrics(s, date(2026, 10, 1), date(2026, 10, 31))
    november = metrics(s, date(2026, 11, 1), date(2026, 11, 30))
    assert october["expenses"] == 500 and october["cash_flow"] == -1000
    assert november["expenses"] == 0 and november["cash_flow"] == 500 and november["income"] == 0
    s.assign_receipt(r, "income", category="Зарплата")
    assert s.get_budget_entry(p)["personal_amount"] == 1000
    assert metrics(s, date(2026, 11, 1), date(2026, 11, 30))["income"] == 500
    s.assign_receipt(r, "reimbursement", q)
    assert s.get_budget_entry(q)["personal_amount"] == 700
    assert metrics(s, date(2026, 11, 1), date(2026, 11, 30))["cash_flow"] == 500
    path = ExcelExporter(s, tmp_path).export(s.owner_id, date.min, date.max)
    book = load_workbook(path, data_only=False)
    assert {"Учет расходов", "Учет доходов", "Справочники", "Возмещения"} <= set(book.sheetnames)
    assert sum(row[6].value or 0 for row in list(book["Учет расходов"].rows)[1:]) == 1700
    assert book["Возмещения"].cell(2, 2).value == q


def test_refunds_inherit_mandatory_and_restore_limit(db):
    s, _ = db
    p = purchase(s)
    s.toggle_mandatory_entry(p)
    s.update_analytics_settings(exclude_mandatory=True)
    receipt(s, purchase_id=p)
    m = metrics(s, date(2026, 10, 1), date(2026, 10, 31))
    assert m["expenses"] == 0 and m["excluded"] == 500 and m["cash_flow"] == -500
    assert s.expense_summary(date(2026, 10, 1), date(2026, 10, 31))["excluded_total"] == 500


def test_two_concurrent_refunds_and_owner_isolation(db):
    s, owner = db
    p = purchase(s)

    def confirm(_):
        with s.owner_scope(owner):
            try:
                receipt(s, 600, purchase_id=p)
                return True
            except ValueError:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(confirm, range(2))) == [False, True]
    assert s.get_budget_entry(p)["personal_amount"] == 400
    other = f"feature-test:{uuid4()}"
    try:
        with s.owner_scope(other):
            assert s.get_budget_entry(p) is None
            with pytest.raises(ValueError):
                receipt(s, purchase_id=p)
    finally:
        with s._connect() as connection:
            connection.execute("DELETE FROM users WHERE owner_id=%s", (other,))


def test_date_rules_updates_and_database_constraint(db):
    s, _ = db
    p = purchase(s)
    with pytest.raises(ValueError, match="Дата"):
        receipt(s, day=date(2026, 10, 29), purchase_id=p)
    r = receipt(s, purchase_id=p)
    with pytest.raises(ValueError):
        s.update_budget_entry(r, ParsedOperation(date(2026, 10, 31), "Иван", 1001, OperationType.REIMBURSEMENT))
    import psycopg

    with pytest.raises(psycopg.errors.RaiseException):
        with s._connect() as connection:
            connection.execute("UPDATE budget_entries SET amount=1001 WHERE id=%s", (r,))
    assert s.get_budget_entry(p)["personal_amount"] == 500


def test_reimport_and_reuse_manual_receipt(db):
    s, _ = db
    p = purchase(s)
    r = receipt(s, purchase_id=p)
    processor = ScreenshotProcessor(s, s.category_book())
    operation = ParsedOperation(date(2026, 10, 31), "Иван", 500, OperationType.INCOME, "Зарплата", confidence=1)
    result = processor.process(b"unique-synthetic-receipt", ParsedScreenshot("test", [operation], {}))
    assert result.decisions[0].status == OperationStatus.IGNORED
    assert len(s.reimbursements(p)) == 1
    op = replace(operation, name="Банковский перевод")
    s.record_operation("source", "image", "bank", op, OperationStatus.PENDING)
    s.reuse_receipt_source("source", r)
    assert s.get_operation("source")["workbook_row"] == r
    assert len(s.reimbursements(p)) == 1


def test_recurring_idempotency_existing_entry_and_period_opening(db):
    s, owner = db
    template = s.create_recurring(
        "Подписка", "100", "expense", "Еда", "Кофейни", date(2026, 1, 31), "month", "09:00", "Europe/Moscow", 123
    )
    instances = s.recurring_instances(date(2026, 3, 31))
    assert [r["due_date"] for r in instances] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    feb = instances[1]

    def confirm(_):
        with s.owner_scope(owner):
            return s.confirm_recurring(feb["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(confirm, range(2)))
    assert ids[0] == ids[1]
    entry = purchase(s, 100, date(2026, 3, 31), "Подписка")
    assert s.confirm_recurring(instances[2]["id"], entry_id=entry) == entry
    s.configure_period(31, "500", date(2026, 3, 31))
    info = s.planning_summary(date(2026, 3, 31))
    assert info["opening"] == 500 and info["days_left"] == 30 and info["reserve"] == 0
    assert s.planning_summary(date(2026, 4, 30))["opening"] == 0
    s.edit_recurring(template, "delete")
    assert s.get_budget_entry(ids[0]) is not None


def test_colors_frequent_and_atomic_subcategory_move(db):
    s, _ = db
    catalog = s.category_catalog()
    source = next(r for r in catalog if r["name"] == "Еда" and r["kind"] == "expense")
    sub = next(r for r in source["subcategories"] if r["name"] == "Кофейни")
    target = s.create_category("expense", "Обучение")
    p = purchase(s)
    s.save_expense_category_rule("Кафе", "Еда", "Кофейни")
    color = s.category_colors()["Еда"]
    assert s.frequent_categories(date(2026, 10, 31))[0]["subcategory"] == "Кофейни"
    assert s.move_subcategory(sub["id"], target) == 1
    assert s.get_budget_entry(p)["category"] == "Еда"
    s.move_subcategory(sub["id"], target, True)
    assert s.get_budget_entry(p)["category"] == "Обучение"
    assert s.get_expense_category_rule("Кафе") == ("Обучение", "Кофейни")
    s.rename_category(source["id"], "Питание")
    assert s.category_colors()["Питание"] == color


def test_shared_budget_permissions_revoke_and_author(db):
    s, _ = db
    actors = [f"telegram:{uuid4().int%10**14}" for _ in range(2)]
    shared = None
    try:
        with s.user_scope(actors[0]):
            shared = s.create_shared_budget("Семейный")
        with s.user_scope(actors[0], shared):
            invite = s.invite_member()
            p = purchase(s)
        with s.user_scope(actors[1]):
            assert s.accept_invite(invite) == shared
        with s.user_scope(actors[1], shared):
            assert s.get_budget_entry(p)["author_id"] == actors[0]
            with pytest.raises(ValueError):
                s.delete_budget_entry(p)
            with pytest.raises(ValueError):
                receipt(s, purchase_id=p)
            with pytest.raises(ValueError):
                s.create_category("expense", "Чужая")
            own = purchase(s, 50)
            s.delete_budget_entry(own)
        with s.user_scope(actors[0], shared):
            s.remove_member(actors[1])
        with pytest.raises(ValueError):
            with s.user_scope(actors[1], shared):
                pass
        with s.user_scope(actors[1]):
            with pytest.raises(ValueError,match='уже использовано'):
                s.accept_invite(invite)
    finally:
        with s._connect() as connection:
            connection.execute("DELETE FROM users WHERE owner_id=ANY(%s)", (actors + [shared],))


def test_web_tokens_single_use_and_member_access(db):
    s, _ = db
    actor = "telegram:123"
    token = s.issue_web_token(actor)
    assert s.consume_login_token(token) == actor
    with pytest.raises(ValueError):
        s.consume_login_token(token)
    session = s.issue_web_token(actor, "session")
    assert s.web_identity(session) == actor
    s.revoke_web_token(session)
    assert s.web_identity(session) is None


def test_migrate_existing_schema_preserves_old_income_and_expense():
    url = os.getenv("FEATURE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FEATURE_TEST_DATABASE_URL required")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from alembic import command
    from budget_bot.migrations.runner import alembic_config

    name = "migration_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        test_url = make_conninfo(**{**conninfo_to_dict(url), "dbname": name})
        # SQLAlchemy accepts the URI rather than libpq keyword notation.
        from sqlalchemy.engine import URL

        parts = conninfo_to_dict(test_url)
        migration_url = URL.create(
            "postgresql+psycopg",
            username=parts["user"],
            password=parts["password"],
            host=parts["host"],
            port=int(parts["port"]),
            database=name,
        ).render_as_string(hide_password=False)
        try:
            config = alembic_config(migration_url)
            command.upgrade(config, "004_analytics_claims")
            with psycopg.connect(test_url) as connection:
                connection.execute("INSERT INTO users VALUES ('legacy',now(),now())")
                connection.execute(
                    """INSERT INTO budget_entries(owner_id,source,operation_date,operation_type,amount,category,name,created_at,updated_at)
                    VALUES ('legacy','test','2026-10-01','expense',1000,'Еда','Кафе',now(),now()),
                           ('legacy','test','2026-10-02','income',500,'Зарплата','Поступление',now(),now())"""
                )
            command.upgrade(config, "head")
            with psycopg.connect(test_url) as connection:
                assert connection.execute(
                    "SELECT operation_type,amount,author_id FROM budget_entries ORDER BY id"
                ).fetchall() == [("expense", Decimal("1000"), "legacy"), ("income", Decimal("500"), "legacy")]
                assert connection.execute(
                    "SELECT role FROM budget_members WHERE budget_id='legacy' AND user_id='legacy'"
                ).fetchone() == ("owner",)
                assert connection.execute("SELECT personal_amount FROM effective_entries ORDER BY id").fetchall() == [
                    (Decimal("1000"),),
                    (Decimal("500"),),
                ]
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_telegram_short_input_receipt_restart_cancel_and_confirm(db):
    from budget_bot.app_factory import AppContext
    from budget_bot.telegram_bot import TelegramBot
    from budget_bot.telegram_receipts import TelegramReceipts

    s, owner = db
    context = AppContext.__new__(AppContext)
    context.storage = s
    context.settings = SimpleNamespace(default_timezone="Europe/Moscow")
    bot = TelegramBot.__new__(TelegramBot)
    bot.context = context
    messages = []
    bot._send_message = lambda chat, text, reply_markup=None: messages.append((text, reply_markup)) or len(messages)
    bot._delete_message = lambda *args: None
    bot._answer_callback = lambda *args: None
    bot._delete_callback_message = lambda *args: None
    chat = 123
    today = bot._analytics().today()
    p = purchase(s, 1000, today)
    s.set_analytics_limit("Еда", Decimal("2000"))
    assert bot._manual_entry().short(chat, "500", 99)
    keyboard = messages[-1][1]["inline_keyboard"]
    quick = keyboard[0][0]["callback_data"].split(":", 1)[1]
    bot._manual_entry().handle_callback({"id": "test"}, chat, "mquick", quick)
    assert "Остаток лимита" in messages[-1][0]
    assert bot._manual_entry().short(chat, "+500 от Ивана", 100)
    state = TelegramReceipts(bot).state(chat)
    nonce = state["nonce"]
    # A recreated controller reads the existing persisted dialog.
    restarted = TelegramReceipts(bot)
    restarted.callback({"id": "test"}, chat, f"{nonce}:pick:{p}")
    assert s.get_budget_entry(p)["personal_amount"] == 1000
    restarted.callback({"id": "test"}, chat, f"{nonce}:cancel")
    assert s.get_budget_entry(p)["personal_amount"] == 1000
    bot._manual_entry().short(chat, "+500 от Ивана", 101)
    state = TelegramReceipts(bot).state(chat)
    nonce = state["nonce"]
    TelegramReceipts(bot).callback({"id": "test"}, chat, f"{nonce}:pick:{p}")
    TelegramReceipts(bot).callback({"id": "test"}, chat, f"{nonce}:confirm")
    TelegramReceipts(bot).callback({"id": "test"}, chat, f"{nonce}:confirm")
    assert s.get_budget_entry(p)["personal_amount"] == 500
    assert len(s.reimbursements(p)) == 1


def test_recurring_history_and_explicit_mismatch_confirmation(db):
    s, _ = db
    template = s.create_recurring(
        "Подписка", "100", "expense", "Еда", "Кофейни", date(2026, 10, 1), "month", "09:00", "Europe/Moscow", 123
    )
    instance = s.recurring_instances(date(2026, 10, 1))[0]
    existing = purchase(s, 150, date(2026, 10, 2))
    with pytest.raises(ValueError, match="подтверждение"):
        s.confirm_recurring(instance["id"], entry_id=existing)
    s.confirm_recurring(instance["id"], entry_id=existing, confirmed_match=True)
    s.edit_recurring(template, "delete")
    history = s.recurring_instances(date.max, True)
    assert history[0]["entry_id"] == existing and history[0]["status"] == "recorded"


def test_pending_payment_card_is_openable_and_link_preview_is_persisted(db):
    from budget_bot.app_factory import AppContext
    from budget_bot.telegram_bot import TelegramBot
    from budget_bot.telegram_planning import TelegramPlanning

    s, _ = db
    template = s.create_recurring(
        "Платёж", "100", "expense", "Еда", "Кофейни", date(2026, 10, 1), "month", "09:00", "Europe/Moscow", 123
    )
    instance = s.recurring_instances(date(2026, 10, 1))[0]
    context = AppContext.__new__(AppContext)
    context.storage = s
    context.settings = SimpleNamespace(default_timezone="Europe/Moscow")
    bot = TelegramBot.__new__(TelegramBot)
    bot.context = context
    messages = []
    bot._send_message = lambda chat, text, reply_markup=None: messages.append((text, reply_markup))
    bot._answer_callback = lambda *args: None
    controller = TelegramPlanning(bot)
    controller.callback({"id": "test"}, 123, f"payment:{instance['id']}")
    assert "Платёж" in messages[-1][0]
    assert any(
        button["callback_data"] == f"plan:record:{instance['id']}"
        for row in messages[-1][1]["inline_keyboard"]
        for button in row
    )
    entry = purchase(s, 100, date(2026, 10, 1))
    controller.callback({"id": "test"}, 123, f"link:{instance['id']}")
    pending = s.latest_pending_for_chat(123)
    controller.text(123, str(entry), pending)
    pending = s.latest_pending_for_chat(123)
    assert "linkconfirm" in pending["prompt"]
    controller.callback({"id": "test"}, 123, f"linkok:{instance['id']}:{entry}")
    assert s.recurring_instances(date.max, True)[0]["entry_id"] == entry


def test_shared_album_preserves_actor_and_rechecks_revoke(db):
    from budget_bot.app_factory import AppContext
    from budget_bot.telegram_bot import TelegramBot
    from budget_bot.parse_worker import ParseJobWorker

    s, _ = db
    actors = [f"telegram:{uuid4().int%10**14}" for _ in range(2)]
    shared = None
    context = AppContext.__new__(AppContext)
    context.storage = s
    context.settings = SimpleNamespace(default_timezone="Europe/Moscow")
    bot = TelegramBot.__new__(TelegramBot)
    bot.context = context
    bot.media_groups = {}
    bot.parse_jobs = ParseJobWorker(bot)
    messages = []
    bot._send_message = lambda chat, text, reply_markup=None: messages.append(text)
    try:
        with s.user_scope(actors[0]):
            shared = s.create_shared_budget("Альбомы")
        with s.user_scope(actors[0], shared):
            invite = s.invite_member()
        with s.user_scope(actors[1]):
            s.accept_invite(invite)
        message = {
            "chat": {"id": int(actors[1].split(":")[1])},
            "media_group_id": "one",
            "photo": [{"file_id": "synthetic-photo"}],
        }
        with s.user_scope(actors[1], shared):
            bot._queue_media_group_photo(message)
        for group in bot.media_groups.values():
            group["updated_at"] = -100
        bot._flush_ready_media_groups()
        with s._connect() as connection:
            jobs = connection.execute("SELECT payload FROM parse_jobs WHERE owner_id=%s", (shared,)).fetchall()
        assert len(jobs) == 1 and jobs[0]["payload"]["actor_id"] == actors[1]
        with s.user_scope(actors[1], shared):
            bot._queue_media_group_photo({**message, "media_group_id": "two"})
        with s.user_scope(actors[0], shared):
            s.remove_member(actors[1])
        for group in bot.media_groups.values():
            group["updated_at"] = -100
        bot._flush_ready_media_groups()
        assert "Альбом не обработан" in messages[-1]
        with s._connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) AS n FROM parse_jobs WHERE owner_id=%s", (shared,)).fetchone()["n"]
                == 1
            )
    finally:
        with s._connect() as connection:
            connection.execute("DELETE FROM users WHERE owner_id=ANY(%s)", (actors + [shared],))


def test_recurring_notification_claim_recovers_after_restart(db):
    s, _ = db
    s.create_recurring(
        "Напоминание", "100", "expense", "Еда", "Кофейни", date(2026, 10, 1), "month", "09:00", "Europe/Moscow", 123
    )
    instance = s.recurring_instances(date(2026, 10, 1))[0]["id"]
    assert s.claim_recurring_notifications([instance]) == [instance]
    assert s.claim_recurring_notifications([instance]) == []
    with s._connect() as connection:
        connection.execute(
            "UPDATE recurring_instances SET notification_claimed_at=now()-INTERVAL '6 minutes' WHERE id=%s",
            (instance,),
        )
    assert s.claim_recurring_notifications([instance]) == [instance]
    s.finish_recurring_notifications([instance])
    assert s.claim_recurring_notifications([instance]) == []


def test_excel_cross_period_flags_and_cash_totals(db, tmp_path):
    s, _ = db
    p = purchase(s)
    receipt(s, 500, date(2026, 11, 2), purchase_id=p)
    exporter = ExcelExporter(s, tmp_path)
    october = exporter.export(s.owner_id, date(2026, 10, 1), date(2026, 10, 31))
    book = load_workbook(october)
    totals = {r[0].value: r[1].value for r in book["Итоги"].rows}
    assert totals["Личные расходы"] == 500 and totals["Исходные оплаты"] == 1000
    assert totals["Полученные за период возмещения"] == 0 and totals["Движение денег"] == -1000
    assert [book["Возмещения"].cell(2, col).value for col in (8, 9, 10)] == ["Да", "Нет", "Да"]
    november = exporter.export(s.owner_id, date(2026, 11, 1), date(2026, 11, 30))
    assert october != november and october.exists()
    book = load_workbook(november)
    totals = {r[0].value: r[1].value for r in book["Итоги"].rows}
    assert totals["Личные расходы"] == 0 and totals["Исходные оплаты"] == 0
    assert totals["Полученные за период возмещения"] == 500 and totals["Движение денег"] == 500
    assert [book["Возмещения"].cell(2, col).value for col in (8, 9, 10)] == ["Нет", "Да", "Нет"]


def test_payment_callback_budget_lookup_requires_membership(db):
    s, _ = db
    actor = f"telegram:{uuid4().int%10**14}"
    template = s.create_recurring(
        "Тест", "100", "expense", "Еда", "Кофейни", date(2026, 10, 1), "month", "09:00", "Europe/Moscow", 123
    )
    instance = s.recurring_instances(date(2026, 10, 1))[0]["id"]
    try:
        with s.user_scope(actor):
            with pytest.raises(ValueError, match="Нет доступа"):
                s.recurring_access_budget(instance)
    finally:
        with s._connect() as connection:
            connection.execute("DELETE FROM users WHERE owner_id=%s", (actor,))


def test_edit_template_amount_preserves_past_pending_instances(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    s,_=db
    today=datetime.now(ZoneInfo('Europe/Moscow')).date()
    template=s.create_recurring('Будущая сумма','100','expense','Еда','Кофейни',today-timedelta(days=1),'week','09:00','Europe/Moscow',123)
    s.recurring_instances(today+timedelta(days=6))
    s.edit_recurring(template,'amount','120')
    rows=s.recurring_instances(today+timedelta(days=6))
    assert rows[0]['due_date']<today and rows[0]['amount']==100
    assert rows[1]['due_date']>today and rows[1]['amount']==120
