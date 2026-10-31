"""Budget membership, reimbursements and confirmed payment schedules."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import secrets
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from ..models import ParsedOperation
from ..planning import amount_decimal, financial_period, recurring_dates
from .helpers import now_iso

_actor = ContextVar("budget_actor", default=None)
_snapshot = ContextVar("budget_snapshot", default=None)


class BudgetFeatures:
    @contextmanager
    def atomic(self):
        if _snapshot.get() is not None:
            yield
            return
        with self._connect() as connection:
            self._lock_budget(connection)
            token = _snapshot.set(connection)
            try:
                yield
            finally:
                _snapshot.reset(token)

    def source_image_hashes(self):
        with self._connect() as connection:
            return [
                r["image_hash"]
                for r in connection.execute(
                    "SELECT image_hash FROM source_images WHERE owner_id=%s", (self.owner_id,)
                ).fetchall()
            ]

    def issue_web_token(self, user_id, kind="login"):
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("DELETE FROM web_tokens WHERE expires_at<now()")
            connection.execute(
                "INSERT INTO web_tokens VALUES (%s,%s,%s,%s)",
                (
                    sha256(token.encode()).hexdigest(),
                    user_id,
                    kind,
                    now_iso() + timedelta(minutes=5) if kind == "login" else now_iso() + timedelta(days=1),
                ),
            )
        return token

    def consume_login_token(self, token):
        with self._connect() as connection:
            row = connection.execute(
                "DELETE FROM web_tokens WHERE token_hash=%s AND kind='login' AND expires_at>now() RETURNING user_id",
                (sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row:
                raise ValueError("Ссылка недействительна или уже использована.")
            return row["user_id"]

    def web_identity(self, token):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM web_tokens WHERE token_hash=%s AND kind='session' AND expires_at>now()",
                (sha256(token.encode()).hexdigest(),),
            ).fetchone()
            return row["user_id"] if row else None

    def revoke_web_token(self, token):
        with self._connect() as connection:
            connection.execute("DELETE FROM web_tokens WHERE token_hash=%s", (sha256(token.encode()).hexdigest(),))

    def dashboard_rows(self, start, end, category="", search="", author=""):
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT e.*,e.personal_amount AS total,1 AS count,e.paid_amount AS paid,e.returned_amount AS returned
                FROM effective_entries e LEFT JOIN budget_entries p ON p.id=e.expense_entry_id AND p.owner_id=e.owner_id
                WHERE e.owner_id=%s AND e.operation_date BETWEEN %s AND %s
                AND (%s='' OR COALESCE(p.category,e.category)=%s)
                AND (%s='' OR e.name ILIKE %s)
                AND (%s='' OR e.author_id=%s)
                ORDER BY e.operation_date,e.id""",
                (self.owner_id, start, end, category, category, search, f"%{search}%", author, author),
            ).fetchall()
        return [dict(row) for row in rows]

    @contextmanager
    def telegram_update(self, update_id):
        if update_id is None:
            yield True
            return
        with self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.actor_id,))
            exists = connection.execute(
                "SELECT 1 FROM telegram_updates WHERE user_id=%s AND update_id=%s", (self.actor_id, update_id)
            ).fetchone()
            if exists:
                yield False
                return
            token = _snapshot.set(connection)
            connection.execute("SELECT set_config('budget.actor',%s,TRUE)", (self.actor_id,))
            try:
                yield True
                connection.execute(
                    "INSERT INTO telegram_updates(user_id,update_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (self.actor_id, update_id),
                )
            finally:
                _snapshot.reset(token)

    @contextmanager
    def snapshot(self):
        if _snapshot.get() is not None:
            yield
            return
        with self.connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            token = _snapshot.set(connection)
            try:
                yield
            finally:
                _snapshot.reset(token)

    @property
    def actor_id(self):
        return _actor.get() or self.owner_id

    @contextmanager
    def user_scope(self, user_id: str, budget_id=None):
        with self._connect() as connection:
            if budget_id is None:
                selection = connection.execute(
                    "SELECT budget_id FROM budget_selection WHERE user_id = %s", (user_id,)
                ).fetchone()
                budget_id = selection["budget_id"] if selection else user_id
            if (
                budget_id != user_id
                and not connection.execute(
                    "SELECT 1 FROM budget_members WHERE budget_id = %s AND user_id = %s",
                    (budget_id, user_id),
                ).fetchone()
            ):
                raise ValueError("Нет доступа к бюджету.")
        token = _actor.set(user_id)
        try:
            with self.owner_scope(budget_id):
                yield
        finally:
            _actor.reset(token)

    def _require_owner(self, connection):
        if _actor.get() is None or self.actor_id == self.owner_id:
            return
        role = connection.execute(
            "SELECT role FROM budget_members WHERE budget_id = %s AND user_id = %s", (self.owner_id, self.actor_id)
        ).fetchone()
        if not role or role["role"] != "owner":
            raise ValueError("Это действие доступно владельцу бюджета.")

    def _require_edit(self, connection, entry):
        if entry is None:
            raise ValueError("Запись не найдена в этом бюджете.")
        if entry.get("author_id") != self.actor_id:
            self._require_owner(connection)

    def _lock_budget(self, connection):
        # One ordering for all money mutations, including two different purchases.
        connection.execute("SELECT owner_id FROM users WHERE owner_id = %s FOR UPDATE", (self.owner_id,)).fetchone()
        connection.execute("SELECT set_config('budget.actor',%s,TRUE)", (self.actor_id,))
        if _actor.get() and self.actor_id != self.owner_id:
            if not connection.execute(
                "SELECT 1 FROM budget_members WHERE budget_id = %s AND user_id = %s", (self.owner_id, self.actor_id)
            ).fetchone():
                raise ValueError("Доступ к бюджету отозван.")

    def list_budgets(self, user_id):
        self.ensure_owner(user_id)
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT u.owner_id AS id, COALESCE(p.name, 'Личный бюджет') AS name,
                          COALESCE(m.role, 'owner') AS role
                   FROM users u LEFT JOIN budget_members m ON m.budget_id=u.owner_id AND m.user_id=%s
                   LEFT JOIN budget_profiles p ON p.budget_id=u.owner_id
                   WHERE u.owner_id=%s OR m.user_id=%s ORDER BY u.owner_id""",
                    (user_id, user_id, user_id),
                ).fetchall()
            ]

    def create_shared_budget(self, name):
        name = str(name).strip()
        if not name or len(name) > 80:
            raise ValueError("Название бюджета: от 1 до 80 символов.")
        actor, budget = self.actor_id, f"shared:{uuid4()}"
        self.ensure_owner(budget)
        with self._connect() as connection:
            connection.execute("INSERT INTO budget_profiles VALUES (%s,%s)", (budget, name))
            connection.execute("INSERT INTO budget_members VALUES (%s,%s,'owner')", (budget, actor))
        return budget

    def select_budget(self, budget_id):
        if budget_id not in {row["id"] for row in self.list_budgets(self.actor_id)}:
            raise ValueError("Нет доступа к бюджету.")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO budget_selection VALUES (%s,%s) ON CONFLICT(user_id) DO UPDATE SET budget_id=excluded.budget_id",
                (self.actor_id, budget_id),
            )

    def invite_member(self):
        token = secrets.token_urlsafe(24)
        with self._connect() as connection:
            self._lock_budget(connection)
            self._require_owner(connection)
            if not connection.execute("SELECT 1 FROM budget_profiles WHERE budget_id=%s", (self.owner_id,)).fetchone():
                raise ValueError("Приглашения доступны в отдельном совместном бюджете.")
            connection.execute(
                "INSERT INTO budget_invites VALUES (%s,%s,%s,NULL)",
                (sha256(token.encode()).hexdigest(), self.owner_id, now_iso() + timedelta(days=2)),
            )
        return token

    def accept_invite(self, token):
        with self._connect() as connection:
            invite = connection.execute(
                "SELECT * FROM budget_invites WHERE token_hash=%s FOR UPDATE", (sha256(token.encode()).hexdigest(),)
            ).fetchone()
            if not invite or invite["expires_at"] < now_iso() or invite["used_by"] not in (None, self.actor_id):
                raise ValueError("Приглашение истекло или уже использовано.")
            connection.execute('SELECT owner_id FROM users WHERE owner_id=%s FOR UPDATE',(invite['budget_id'],)).fetchone()
            if invite['used_by']:
                if connection.execute('SELECT 1 FROM budget_members WHERE budget_id=%s AND user_id=%s',(invite['budget_id'],self.actor_id)).fetchone():
                    return invite['budget_id']
                raise ValueError('Приглашение уже использовано; после отзыва доступа нужно новое приглашение.')
            connection.execute(
                "INSERT INTO budget_members VALUES (%s,%s,'member') ON CONFLICT DO NOTHING",
                (invite["budget_id"], self.actor_id),
            )
            connection.execute(
                "UPDATE budget_invites SET used_by=%s WHERE token_hash=%s", (self.actor_id, invite["token_hash"])
            )
        return invite["budget_id"]

    def budget_members(self):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT user_id, role FROM budget_members WHERE budget_id=%s AND user_id NOT LIKE 'shared:%%' ORDER BY role,user_id",
                    (self.owner_id,),
                ).fetchall()
            ]

    def budget_authors(self):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT author_id AS user_id FROM budget_entries
                WHERE owner_id=%s AND author_id IS NOT NULL AND author_id NOT LIKE 'shared:%%'
                UNION SELECT user_id FROM budget_members WHERE budget_id=%s AND user_id NOT LIKE 'shared:%%'
                ORDER BY user_id""",
                    (self.owner_id, self.owner_id),
                ).fetchall()
            ]

    def remove_member(self, user_id):
        with self._connect() as connection:
            self._lock_budget(connection)
            if user_id != self.actor_id:
                self._require_owner(connection)
            member = connection.execute(
                "SELECT role FROM budget_members WHERE budget_id=%s AND user_id=%s", (self.owner_id, user_id)
            ).fetchone()
            if not member or member["role"] == "owner":
                raise ValueError("Владелец не может выйти; его доступ нельзя отозвать.")
            connection.execute(
                "DELETE FROM budget_members WHERE budget_id=%s AND user_id=%s", (self.owner_id, user_id)
            )
            connection.execute(
                "DELETE FROM budget_selection WHERE budget_id=%s AND user_id=%s", (self.owner_id, user_id)
            )

    def reimbursements(self, purchase_id=None, start=date.min, end=date.max):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT r.id, r.expense_entry_id, r.operation_date, r.amount, r.name,
                          p.operation_date AS purchase_date, p.name AS purchase_name, p.category, p.subcategory
                   FROM budget_entries r JOIN budget_entries p ON p.id=r.expense_entry_id AND p.owner_id=r.owner_id
                   WHERE r.owner_id=%s AND (%s::bigint IS NULL OR p.id=%s)
                     AND (r.operation_date BETWEEN %s AND %s OR p.operation_date BETWEEN %s AND %s)
                   ORDER BY r.operation_date,r.id""",
                    (self.owner_id, purchase_id, purchase_id, start, end, start, end),
                ).fetchall()
            ]

    def refund_candidates(self, start, end, query="", page=0):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM effective_entries WHERE owner_id=%s AND operation_type='expense'
                   AND operation_date BETWEEN %s AND %s AND personal_amount>0 AND name ILIKE %s
                   ORDER BY operation_date DESC,id DESC LIMIT 6 OFFSET %s""",
                    (self.owner_id, start, end, f"%{query}%", max(0, page) * 5),
                ).fetchall()
            ]

    def receipt_candidates(self, page=0):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM budget_entries
                WHERE owner_id=%s AND operation_type IN ('income','reimbursement','transfer')
                ORDER BY operation_date DESC,id DESC LIMIT 6 OFFSET %s""",
                    (self.owner_id, max(0, page) * 5),
                ).fetchall()
            ]

    def receipt_duplicates(self, operation):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM budget_entries WHERE owner_id=%s
                AND operation_type IN ('income','reimbursement','transfer') AND operation_date=%s AND amount=%s
                ORDER BY id LIMIT 6""",
                    (self.owner_id, operation.date, amount_decimal(abs(operation.amount))),
                ).fetchall()
            ]

    def reuse_receipt_source(self, source_hash, entry_id):
        with self._connect() as connection:
            self._lock_budget(connection)
            source = connection.execute(
                "SELECT * FROM operations WHERE owner_id=%s AND operation_hash=%s", (self.owner_id, source_hash)
            ).fetchone()
            self._require_edit(connection, source)
            entry = connection.execute(
                "SELECT * FROM budget_entries WHERE owner_id=%s AND id=%s", (self.owner_id, entry_id)
            ).fetchone()
            self._require_edit(connection, entry)
            operation = ParsedOperation.from_json(source["operation_json"])
            if (
                source["status"] != "pending"
                or entry["operation_type"] not in {"income", "reimbursement", "transfer"}
                or entry["operation_date"] != operation.date
                or entry["amount"] != amount_decimal(abs(operation.amount))
            ):
                raise ValueError("Записи не совпадают по дате и сумме либо источник уже обработан.")
            connection.execute(
                "UPDATE operations SET status='auto_written',workbook_row=%s,status_note='linked existing receipt' WHERE owner_id=%s AND operation_hash=%s",
                (entry_id, self.owner_id, source_hash),
            )
            connection.execute(
                "DELETE FROM pending_actions WHERE owner_id=%s AND operation_hash=%s", (self.owner_id, source_hash)
            )

    def _validate_purchase(self, connection, incoming, purchase_id):
        purchase = connection.execute(
            "SELECT * FROM budget_entries WHERE owner_id=%s AND id=%s", (self.owner_id, purchase_id)
        ).fetchone()
        self._require_edit(connection, purchase)
        if purchase["operation_type"] != "expense":
            raise ValueError("Возврат можно связать только с расходом.")
        if incoming["operation_date"] < purchase["operation_date"]:
            raise ValueError("Дата поступления раньше покупки. Исправь дату.")
        used = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM budget_entries WHERE owner_id=%s AND expense_entry_id=%s AND id<>%s",
            (self.owner_id, purchase_id, incoming.get("id", -1)),
        ).fetchone()["total"]
        if used + amount_decimal(incoming["amount"]) > purchase["amount"]:
            raise ValueError(f"Можно вернуть ещё {purchase['amount']-used:.2f} ₽. Выбери другую трату или назначение.")
        return purchase

    def assign_receipt(self, entry_id, purpose, purchase_id=None, category=None):
        if purpose not in {"income", "reimbursement", "transfer"}:
            raise ValueError("Неизвестное назначение поступления.")
        with self._connect() as connection:
            self._lock_budget(connection)
            incoming = connection.execute(
                "SELECT * FROM budget_entries WHERE owner_id=%s AND id=%s", (self.owner_id, entry_id)
            ).fetchone()
            self._require_edit(connection, incoming)
            if incoming["operation_type"] not in {"income", "reimbursement", "transfer"}:
                raise ValueError("Эта запись не является поступлением.")
            if purpose == "reimbursement":
                self._validate_purchase(connection, incoming, purchase_id)
            elif purpose == "income":
                if not connection.execute(
                    "SELECT 1 FROM categories WHERE owner_id=%s AND kind='income' AND name=%s",
                    (self.owner_id, category),
                ).fetchone():
                    raise ValueError("Выбери категорию дохода.")
            connection.execute(
                """UPDATE budget_entries SET operation_type=%s, expense_entry_id=%s,
                category=%s,subcategory=NULL,updated_at=%s WHERE owner_id=%s AND id=%s""",
                (
                    purpose,
                    purchase_id if purpose == "reimbursement" else None,
                    category if purpose == "income" else None,
                    now_iso(),
                    self.owner_id,
                    entry_id,
                ),
            )

    def confirm_receipt(
        self,
        operation: ParsedOperation,
        key,
        purpose,
        purchase_id=None,
        category=None,
        bank="manual",
        source_hash=None,
    ):
        """Use the source identity once; create and assign within the same transaction."""
        amount = amount_decimal(operation.amount)
        with self._connect() as connection:
            self._lock_budget(connection)
            existing = connection.execute(
                "SELECT id FROM budget_entries WHERE owner_id=%s AND operation_hash=%s", (self.owner_id, key)
            ).fetchone()
            if existing:
                return int(existing["id"])
            if source_hash:
                source = connection.execute(
                    "SELECT * FROM operations WHERE owner_id=%s AND operation_hash=%s FOR UPDATE",
                    (self.owner_id, source_hash),
                ).fetchone()
                if not source or source["status"] != "pending":
                    raise ValueError("Поступление уже обработано.")
                self._require_edit(connection, source)
            if purpose == "reimbursement":
                self._validate_purchase(connection, {"amount": amount, "operation_date": operation.date}, purchase_id)
            elif purpose == "income":
                if not connection.execute(
                    "SELECT 1 FROM categories WHERE owner_id=%s AND kind='income' AND name=%s",
                    (self.owner_id, category),
                ).fetchone():
                    raise ValueError("Выбери категорию дохода.")
            elif purpose != "transfer":
                raise ValueError("Выбери назначение поступления.")
            row = connection.execute(
                """INSERT INTO budget_entries
                (owner_id,author_id,source,operation_hash,operation_date,operation_type,amount,category,name,bank,expense_entry_id,created_at,updated_at)
                VALUES (%s,%s,'receipt',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    self.owner_id,
                    self.actor_id,
                    key,
                    operation.date,
                    purpose,
                    amount,
                    category if purpose == "income" else None,
                    operation.name,
                    bank,
                    purchase_id if purpose == "reimbursement" else None,
                    now_iso(),
                    now_iso(),
                ),
            ).fetchone()
            if source_hash:
                connection.execute(
                    "UPDATE operations SET status='auto_written',workbook_row=%s,status_note=%s WHERE owner_id=%s AND operation_hash=%s",
                    (row["id"], purpose, self.owner_id, source_hash),
                )
                connection.execute(
                    "DELETE FROM pending_actions WHERE owner_id=%s AND operation_hash=%s", (self.owner_id, source_hash)
                )
            return int(row["id"])

    def frequent_categories(self, today):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT category,subcategory,COUNT(*) AS count
                FROM budget_entries WHERE owner_id=%s AND operation_type='expense' AND operation_date BETWEEN %s AND %s
                AND category IS NOT NULL AND subcategory IS NOT NULL
                GROUP BY category,subcategory ORDER BY count DESC, MAX(operation_date) DESC,MAX(id) DESC,category,subcategory LIMIT 5""",
                    (self.owner_id, today - timedelta(days=89), today),
                ).fetchall()
            ]

    def set_category_color(self, category_id, color):
        import re

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Цвет в формате #4c78a8.")
        with self._connect() as connection:
            self._lock_budget(connection)
            self._require_owner(connection)
            connection.execute(
                "UPDATE categories SET color=%s WHERE owner_id=%s AND id=%s",
                (color.lower(), self.owner_id, category_id),
            )

    def category_colors(self):
        with self._connect() as connection:
            return {
                row["name"]: row["color"]
                for row in connection.execute(
                    "SELECT name,color FROM categories WHERE owner_id=%s AND kind='expense'", (self.owner_id,)
                ).fetchall()
            }

    def configure_period(self, day, opening, today=None):
        start, _ = financial_period(today or date.today(), day)
        try:
            value = Decimal(str(opening).replace(" ", "").replace(",", "."))
        except InvalidOperation:
            raise ValueError("Начальная сумма должна быть числом.") from None
        if (
            not value.is_finite()
            or abs(value) > Decimal("999999999999.99")
            or value != value.quantize(Decimal("0.01"))
        ):
            raise ValueError("Начальная сумма должна быть указана с точностью до копейки.")
        with self._connect() as connection:
            self._lock_budget(connection)
            self._require_owner(connection)
            connection.execute(
                "INSERT INTO analytics_settings(owner_id) VALUES (%s) ON CONFLICT DO NOTHING", (self.owner_id,)
            )
            connection.execute(
                "UPDATE analytics_settings SET period_day=%s,opening_amount=%s WHERE owner_id=%s",
                (day, value, self.owner_id),
            )
            connection.execute(
                "INSERT INTO period_openings VALUES (%s,%s,%s) ON CONFLICT(owner_id,period_start) DO UPDATE SET amount=excluded.amount",
                (self.owner_id, start, value),
            )

    def planning_summary(self, today):
        from ..analytics import period_metrics

        settings = self.analytics_settings()
        start, end = financial_period(today, settings.get("period_day", 1))
        metrics = period_metrics(self.analytics_rows(start, today), settings, start, today)
        upcoming = self.recurring_instances(end)
        reserve = sum(
            (
                row["amount"]
                for row in upcoming
                if start <= row["due_date"] <= end and row["operation_type"] == "expense" and row["mandatory"]
            ),
            Decimal(0),
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT amount FROM period_openings WHERE owner_id=%s AND period_start=%s", (self.owner_id, start)
            ).fetchone()
        opening = row["amount"] if row else Decimal(0)
        available = opening + metrics["income"] + metrics["reimbursements"] - metrics["paid"] - reserve
        days = (end - today).days + 1
        return {
            **metrics,
            "start": start,
            "end": end,
            "opening": opening,
            "reserve": reserve,
            "available": available,
            "daily": max(Decimal(0), available) / days,
            "days_left": days,
            "upcoming": upcoming,
        }

    def create_recurring(
        self,
        name,
        amount,
        operation_type,
        category,
        subcategory,
        starts_on,
        frequency,
        reminder_time,
        timezone_name,
        chat_id,
        ends_on=None,
    ):
        amount = amount_decimal(amount)
        ZoneInfo(timezone_name)
        clock = time.fromisoformat(reminder_time)
        if (
            clock.tzinfo
            or clock.second
            or clock.microsecond
            or frequency not in {"week", "month"}
            or operation_type not in {"expense", "income"}
        ):
            raise ValueError("Периодичность: week/month; время: ЧЧ:ММ; тип: expense/income.")
        book = self.category_book()
        valid = (
            book.is_valid_expense(category, subcategory)
            if operation_type == "expense"
            else book.is_valid_income(category)
        )
        if not valid or not name.strip() or (ends_on and ends_on < starts_on):
            raise ValueError("Проверь название, категорию и даты шаблона.")
        with self._connect() as connection:
            self._lock_budget(connection)
            row = connection.execute(
                """INSERT INTO recurring_templates(owner_id,author_id,name,amount,operation_type,category,subcategory,starts_on,ends_on,frequency,reminder_time,timezone,chat_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    self.owner_id,
                    self.actor_id,
                    name.strip(),
                    amount,
                    operation_type,
                    category,
                    subcategory,
                    starts_on,
                    ends_on,
                    frequency,
                    clock,
                    timezone_name,
                    chat_id,
                ),
            ).fetchone()
        return row["id"]

    def recurring_templates(self):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recurring_templates WHERE owner_id=%s AND NOT deleted ORDER BY starts_on,id",
                    (self.owner_id,),
                ).fetchall()
            ]

    def edit_recurring_schedule(self, template_id, starts_on, frequency, clock, ends_on, today):
        reminder = time.fromisoformat(clock)
        if frequency not in {"week", "month"} or starts_on < today or (ends_on and ends_on < starts_on):
            raise ValueError("Новая дата начала должна быть сегодня или позже; периодичность: неделя/месяц.")
        with self._connect() as connection:
            self._lock_budget(connection)
            row = connection.execute(
                "SELECT * FROM recurring_templates WHERE owner_id=%s AND id=%s AND NOT deleted",
                (self.owner_id, template_id),
            ).fetchone()
            self._require_edit(connection, row)
            connection.execute(
                "DELETE FROM recurring_instances WHERE owner_id=%s AND template_id=%s AND due_date>=%s AND status='pending'",
                (self.owner_id, template_id, today),
            )
            connection.execute(
                "UPDATE recurring_templates SET starts_on=%s,ends_on=%s,frequency=%s,reminder_time=%s WHERE owner_id=%s AND id=%s",
                (starts_on, ends_on, frequency, reminder, self.owner_id, template_id),
            )

    def recurring_subscribers(self):
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT DISTINCT t.owner_id,t.chat_id,t.timezone,c.user_id
                FROM recurring_templates t JOIN telegram_chats c ON c.chat_id=t.chat_id
                WHERE NOT t.paused AND NOT t.deleted AND (t.owner_id='telegram:'||c.user_id::text OR EXISTS
                  (SELECT 1 FROM budget_members m WHERE m.budget_id=t.owner_id AND m.user_id='telegram:'||c.user_id::text))"""
                ).fetchall()
            ]

    def recurring_access_budget(self, instance_id):
        with self._connect() as connection:
            row = connection.execute(
                """SELECT i.owner_id FROM recurring_instances i
                WHERE i.id=%s AND (i.owner_id=%s OR EXISTS(SELECT 1 FROM budget_members m
                  WHERE m.budget_id=i.owner_id AND m.user_id=%s))""",
                (instance_id, self.actor_id, self.actor_id),
            ).fetchone()
            if not row:
                raise ValueError("Нет доступа к платежу.")
            return row["owner_id"]

    def claim_recurring_notifications(self, ids):
        with self._connect() as connection:
            return [
                r["id"]
                for r in connection.execute(
                    """UPDATE recurring_instances SET notification_claimed_at=now() WHERE owner_id=%s AND id=ANY(%s)
                       AND NOT notified AND (notification_claimed_at IS NULL OR notification_claimed_at<now()-INTERVAL '5 minutes') RETURNING id""",
                    (self.owner_id, ids),
                ).fetchall()
            ]

    def release_recurring_notifications(self, ids):
        with self._connect() as connection:
            connection.execute(
                "UPDATE recurring_instances SET notification_claimed_at=NULL WHERE owner_id=%s AND id=ANY(%s)",
                (self.owner_id, ids),
            )

    def finish_recurring_notifications(self, ids):
        with self._connect() as connection:
            connection.execute(
                "UPDATE recurring_instances SET notified=TRUE,notification_claimed_at=NULL WHERE owner_id=%s AND id=ANY(%s)",
                (self.owner_id, ids),
            )

    def edit_recurring(self, template_id, action, amount=None):
        with self._connect() as connection:
            self._lock_budget(connection)
            row = connection.execute(
                "SELECT * FROM recurring_templates WHERE owner_id=%s AND id=%s AND NOT deleted",
                (self.owner_id, template_id),
            ).fetchone()
            self._require_edit(connection, row)
            if action in {"pause", "resume", "delete"}:
                connection.execute(
                    "UPDATE recurring_templates SET paused=%s,deleted=%s WHERE owner_id=%s AND id=%s",
                    (action != "resume", action == "delete", self.owner_id, template_id),
                )
            elif action == "amount":
                value = amount_decimal(amount)
                connection.execute(
                    "UPDATE recurring_templates SET amount=%s WHERE owner_id=%s AND id=%s",
                    (value, self.owner_id, template_id),
                )
                connection.execute(
                    "UPDATE recurring_instances SET amount=%s WHERE owner_id=%s AND template_id=%s AND status='pending' AND due_date>=%s",
                    (value, self.owner_id, template_id, datetime.now(ZoneInfo(row['timezone'])).date()),
                )
            else:
                raise ValueError("Неизвестное действие с шаблоном.")

    def recurring_instances(self, end, include_recorded=False):
        with self._connect() as connection:
            self._lock_budget(connection)
            templates = connection.execute(
                "SELECT * FROM recurring_templates WHERE owner_id=%s AND NOT paused AND NOT deleted", (self.owner_id,)
            ).fetchall()
            for template in [] if include_recorded else templates:
                stop = min(end, template["ends_on"] or end)
                for due in recurring_dates(template["starts_on"], stop, template["frequency"]):
                    connection.execute(
                        "INSERT INTO recurring_instances(owner_id,template_id,due_date,amount) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (self.owner_id, template["id"], due, template["amount"]),
                    )
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT i.*,t.name,t.operation_type,t.category,t.subcategory,t.mandatory,t.author_id,t.chat_id,t.timezone,t.reminder_time
                FROM recurring_instances i JOIN recurring_templates t ON t.id=i.template_id AND t.owner_id=i.owner_id
                WHERE i.owner_id=%s AND i.due_date<=%s
                  AND ((%s AND i.status IN ('recorded','skipped')) OR (i.status='pending' AND NOT t.paused AND NOT t.deleted))
                ORDER BY i.due_date,i.id""",
                    (self.owner_id, end, include_recorded),
                ).fetchall()
            ]

    def confirm_recurring(self, instance_id, action="record", amount=None, entry_id=None, confirmed_match=False):
        with self._connect() as connection:
            self._lock_budget(connection)
            row = connection.execute(
                """SELECT i.*,t.operation_type,t.name,t.category,t.subcategory,t.author_id,t.mandatory,t.paused,t.deleted
                FROM recurring_instances i JOIN recurring_templates t ON t.id=i.template_id AND t.owner_id=i.owner_id
                WHERE i.owner_id=%s AND i.id=%s FOR UPDATE OF i""",
                (self.owner_id, instance_id),
            ).fetchone()
            self._require_edit(connection, row)
            if row["status"] != "pending":
                return row["entry_id"]
            if row["paused"] or row["deleted"]:
                raise ValueError("Шаблон приостановлен или удалён.")
            if action == "skip":
                connection.execute("UPDATE recurring_instances SET status='skipped' WHERE id=%s", (instance_id,))
                return None
            value = amount_decimal(amount if amount is not None else row["amount"])
            if entry_id is None:
                entry_id = connection.execute(
                    """INSERT INTO budget_entries(owner_id,author_id,source,operation_hash,operation_date,operation_type,amount,category,subcategory,name,is_mandatory,created_at,updated_at)
                    VALUES (%s,%s,'recurring',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        self.owner_id,
                        self.actor_id,
                        f"recurring:{instance_id}",
                        row["due_date"],
                        row["operation_type"],
                        value,
                        row["category"],
                        row["subcategory"],
                        row["name"],
                        row["mandatory"],
                        now_iso(),
                        now_iso(),
                    ),
                ).fetchone()["id"]
            else:
                entry = connection.execute(
                    "SELECT * FROM budget_entries WHERE owner_id=%s AND id=%s", (self.owner_id, entry_id)
                ).fetchone()
                self._require_edit(connection, entry)
                if entry["operation_type"] != row["operation_type"]:
                    raise ValueError("Тип записи не совпадает с платежом.")
                if not confirmed_match and (entry["operation_date"] != row["due_date"] or entry["amount"] != value):
                    raise ValueError(
                        "Дата или сумма записи отличается от платежа. Требуется подтверждение сопоставления."
                    )
                if connection.execute("SELECT 1 FROM recurring_instances WHERE entry_id=%s", (entry_id,)).fetchone():
                    raise ValueError("Эта запись уже связана с платежом.")
                value = entry["amount"]
            connection.execute(
                "UPDATE recurring_instances SET status='recorded',entry_id=%s,amount=%s WHERE id=%s",
                (entry_id, value, instance_id),
            )
            return entry_id

    def move_subcategory(self, subcategory_id, target_id, confirm=False):
        with self._connect() as connection:
            self._lock_budget(connection)
            self._require_owner(connection)
            self._check_category_edit_busy(connection)
            source = connection.execute(
                "SELECT s.*,c.name AS category FROM subcategories s JOIN categories c ON c.id=s.category_id WHERE c.owner_id=%s AND s.id=%s",
                (self.owner_id, subcategory_id),
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM categories WHERE owner_id=%s AND id=%s AND kind='expense'", (self.owner_id, target_id)
            ).fetchone()
            if not source or not target or source["category_id"] == target_id:
                raise ValueError("Выбери другую существующую категорию.")
            if connection.execute(
                "SELECT 1 FROM subcategories WHERE category_id=%s AND lower(name)=lower(%s)",
                (target_id, source["name"]),
            ).fetchone():
                raise ValueError("В целевой категории уже есть такая подкатегория. Переименуй её перед переносом.")
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM budget_entries WHERE owner_id=%s AND category=%s AND subcategory=%s",
                (self.owner_id, source["category"], source["name"]),
            ).fetchone()["n"]
            if not confirm:
                return count
            for table in ("budget_entries", "learned_expense_categories", "recurring_templates"):
                connection.execute(
                    f"UPDATE {table} SET category=%s WHERE owner_id=%s AND category=%s AND subcategory=%s",
                    (target["name"], self.owner_id, source["category"], source["name"]),
                )
            settings = connection.execute(
                "SELECT mandatory_rules,limits FROM analytics_settings WHERE owner_id=%s FOR UPDATE", (self.owner_id,)
            ).fetchone()
            if settings:
                rules = settings["mandatory_rules"]
                old = [source["category"], source["name"]]
                if old in rules:
                    rules.remove(old)
                    rules.append([target["name"], source["name"]])
                elif [source["category"], ""] in rules:
                    rules.append([target["name"], source["name"]])
                connection.execute(
                    "UPDATE analytics_settings SET mandatory_rules=%s WHERE owner_id=%s", (Jsonb(rules), self.owner_id)
                )
            connection.execute("UPDATE subcategories SET category_id=%s WHERE id=%s", (target_id, subcategory_id))
            connection.execute(
                """UPDATE operations SET operation_json=jsonb_set(operation_json,'{category}',%s)
                WHERE owner_id=%s AND operation_json->>'category'=%s AND operation_json->>'subcategory'=%s""",
                (Jsonb(target["name"]), self.owner_id, source["category"], source["name"]),
            )
            for row in connection.execute(
                "SELECT operation_hash,prompt FROM pending_actions WHERE owner_id=%s AND prompt LIKE 'manual:%%'",
                (self.owner_id,),
            ).fetchall():
                try:
                    state = json.loads(row["prompt"][7:])
                except ValueError:
                    continue
                if state.get("category") == source["category"] and state.get("subcategory") == source["name"]:
                    state["category"] = target["name"]
                state["choices"] = [
                    [target["name"] if cat == source["category"] and sub == source["name"] else cat, sub]
                    for cat, sub in state.get("choices", [])
                ]
                connection.execute(
                    "UPDATE pending_actions SET prompt=%s WHERE owner_id=%s AND operation_hash=%s",
                    ("manual:" + json.dumps(state, ensure_ascii=False), self.owner_id, row["operation_hash"]),
                )
            return count
