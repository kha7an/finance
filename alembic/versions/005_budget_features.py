"""Reimbursements, planning and explicit shared-budget membership."""

from alembic import op

revision = "005_budget_features"
down_revision = "004_analytics_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE categories ADD COLUMN color TEXT;
        UPDATE categories SET color = '#' || substr(md5(id::text), 1, 6);
        ALTER TABLE categories ALTER COLUMN color SET NOT NULL;
        ALTER TABLE categories ALTER COLUMN color SET DEFAULT '#4c78a8';
        ALTER TABLE budget_entries ADD COLUMN author_id TEXT;
        ALTER TABLE budget_entries ADD COLUMN updated_by TEXT;
        ALTER TABLE operations ADD COLUMN author_id TEXT;
        UPDATE operations SET author_id=owner_id;
        UPDATE budget_entries SET author_id = owner_id;
        ALTER TABLE budget_entries ADD COLUMN expense_entry_id BIGINT;
        ALTER TABLE budget_entries ADD CONSTRAINT entries_owner_id_unique UNIQUE(owner_id, id);
        ALTER TABLE budget_entries ADD CONSTRAINT reimbursement_purchase_fk
            FOREIGN KEY(owner_id, expense_entry_id) REFERENCES budget_entries(owner_id, id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE budget_entries ADD CONSTRAINT reimbursement_type_check
            CHECK ((operation_type = 'reimbursement') = (expense_entry_id IS NOT NULL));
        CREATE INDEX entries_reimbursement_idx ON budget_entries(owner_id, expense_entry_id);
        CREATE VIEW effective_entries AS
            SELECT e.*, e.amount AS paid_amount, COALESCE(r.returned, 0) AS returned_amount,
                   e.amount - COALESCE(r.returned, 0) AS personal_amount
            FROM budget_entries e LEFT JOIN
                (SELECT owner_id, expense_entry_id, SUM(amount) AS returned
                 FROM budget_entries WHERE operation_type = 'reimbursement'
                 GROUP BY owner_id, expense_entry_id) r
            ON r.owner_id = e.owner_id AND r.expense_entry_id = e.id;
        ALTER TABLE analytics_settings ADD COLUMN period_day INTEGER NOT NULL DEFAULT 1 CHECK(period_day BETWEEN 1 AND 31);
        ALTER TABLE analytics_settings ADD COLUMN opening_amount NUMERIC(14,2) NOT NULL DEFAULT 0;
        CREATE TABLE period_openings (
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            period_start DATE NOT NULL, amount NUMERIC(14,2) NOT NULL,
            PRIMARY KEY(owner_id, period_start)
        );
        CREATE TABLE budget_members (
            budget_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner', 'member')),
            PRIMARY KEY(budget_id, user_id)
        );
        INSERT INTO budget_members SELECT owner_id, owner_id, 'owner' FROM users;
        CREATE TABLE budget_profiles (
            budget_id TEXT PRIMARY KEY REFERENCES users(owner_id) ON DELETE CASCADE,
            name TEXT NOT NULL
        );
        CREATE TABLE budget_selection (user_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE);
        CREATE TABLE budget_invites (
            token_hash TEXT PRIMARY KEY, budget_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL, used_by TEXT
        );
        CREATE TABLE recurring_templates (
            id BIGSERIAL PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            author_id TEXT NOT NULL, name TEXT NOT NULL, amount NUMERIC(14,2) NOT NULL CHECK(amount > 0),
            operation_type TEXT NOT NULL CHECK(operation_type IN ('expense','income')),
            category TEXT NOT NULL, subcategory TEXT, starts_on DATE NOT NULL, ends_on DATE,
            frequency TEXT NOT NULL CHECK(frequency IN ('week','month')),
            reminder_time TIME NOT NULL, timezone TEXT NOT NULL, chat_id BIGINT NOT NULL,
            mandatory BOOLEAN NOT NULL DEFAULT TRUE, paused BOOLEAN NOT NULL DEFAULT FALSE,
            deleted BOOLEAN NOT NULL DEFAULT FALSE, CHECK(ends_on IS NULL OR ends_on >= starts_on),
            UNIQUE(owner_id, id)
        );
        CREATE TABLE recurring_instances (
            id BIGSERIAL PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            template_id BIGINT NOT NULL, due_date DATE NOT NULL, amount NUMERIC(14,2) NOT NULL CHECK(amount > 0),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','recorded','skipped')),
            entry_id BIGINT, notified BOOLEAN NOT NULL DEFAULT FALSE, notification_claimed_at TIMESTAMPTZ,
            UNIQUE(template_id, due_date), UNIQUE(entry_id),
            FOREIGN KEY(owner_id, template_id) REFERENCES recurring_templates(owner_id, id),
            FOREIGN KEY(owner_id, entry_id) REFERENCES budget_entries(owner_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE telegram_updates (
            user_id TEXT NOT NULL, update_id BIGINT NOT NULL, processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(user_id, update_id)
        );
        CREATE TABLE web_tokens (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('login','session')), expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE entry_changes (
            id BIGSERIAL PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            entry_id BIGINT NOT NULL, actor_id TEXT NOT NULL, changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            before_data JSONB NOT NULL, after_data JSONB
        );
        CREATE FUNCTION validate_budget_money() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE purchase budget_entries; returned NUMERIC; earliest DATE;
        BEGIN
            PERFORM owner_id FROM users WHERE owner_id=COALESCE(NEW.owner_id,OLD.owner_id) FOR UPDATE;
            IF NOT FOUND THEN RETURN COALESCE(NEW,OLD); END IF;
            IF TG_OP='DELETE' THEN
                IF EXISTS(SELECT 1 FROM budget_entries WHERE owner_id=OLD.owner_id AND expense_entry_id=OLD.id) THEN
                    RAISE EXCEPTION 'Purchase has reimbursements';
                END IF;
                INSERT INTO entry_changes(owner_id,entry_id,actor_id,before_data)
                    VALUES(OLD.owner_id,OLD.id,COALESCE(NULLIF(current_setting('budget.actor',TRUE),''),OLD.owner_id),to_jsonb(OLD));
                RETURN OLD;
            END IF;
            NEW.author_id=COALESCE(NEW.author_id,NEW.owner_id);
            NEW.updated_by=COALESCE(NULLIF(current_setting('budget.actor',TRUE),''),NEW.author_id);
            IF NEW.amount<=0 THEN RAISE EXCEPTION 'Amount must be positive'; END IF;
            IF NEW.expense_entry_id IS NOT NULL THEN
                SELECT * INTO purchase FROM budget_entries WHERE owner_id=NEW.owner_id AND id=NEW.expense_entry_id;
                IF purchase.id IS NULL OR purchase.operation_type<>'expense' OR NEW.operation_date<purchase.operation_date THEN
                    RAISE EXCEPTION 'Invalid reimbursement purchase or date';
                END IF;
                SELECT COALESCE(SUM(amount),0) INTO returned FROM budget_entries
                    WHERE owner_id=NEW.owner_id AND expense_entry_id=NEW.expense_entry_id AND id<>NEW.id;
                IF returned+NEW.amount>purchase.amount THEN RAISE EXCEPTION 'Reimbursement exceeds purchase'; END IF;
            END IF;
            IF TG_OP='UPDATE' AND OLD.operation_type='expense' THEN
                SELECT COALESCE(SUM(amount),0),MIN(operation_date) INTO returned,earliest
                    FROM budget_entries WHERE owner_id=NEW.owner_id AND expense_entry_id=NEW.id;
                IF returned>NEW.amount OR earliest<NEW.operation_date OR (returned>0 AND NEW.operation_type<>'expense') THEN
                    RAISE EXCEPTION 'Purchase conflicts with reimbursements';
                END IF;
            END IF;
            IF TG_OP='UPDATE' THEN
                INSERT INTO entry_changes(owner_id,entry_id,actor_id,before_data,after_data)
                    VALUES(NEW.owner_id,NEW.id,NEW.updated_by,to_jsonb(OLD),to_jsonb(NEW));
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER budget_money_guard BEFORE INSERT OR UPDATE OR DELETE ON budget_entries
            FOR EACH ROW EXECUTE FUNCTION validate_budget_money();
    """
    )


def downgrade() -> None:
    # A downgrade must never silently turn reimbursements into ordinary income.
    op.execute(
        """DO $$ BEGIN IF EXISTS(SELECT 1 FROM budget_entries WHERE operation_type = 'reimbursement')
        THEN RAISE EXCEPTION 'Reassign reimbursements before downgrade'; END IF; END $$;
        DROP TRIGGER budget_money_guard ON budget_entries;
        DROP FUNCTION validate_budget_money();
        DROP TABLE entry_changes, web_tokens, period_openings, telegram_updates, recurring_instances, recurring_templates, budget_invites,
            budget_selection, budget_profiles, budget_members;
        ALTER TABLE analytics_settings DROP COLUMN period_day, DROP COLUMN opening_amount;
        DROP VIEW effective_entries;
        ALTER TABLE budget_entries DROP CONSTRAINT reimbursement_purchase_fk,
            DROP CONSTRAINT reimbursement_type_check, DROP CONSTRAINT entries_owner_id_unique,
            DROP COLUMN expense_entry_id, DROP COLUMN author_id, DROP COLUMN updated_by;
        ALTER TABLE operations DROP COLUMN author_id;
        ALTER TABLE categories DROP COLUMN color;
    """
    )
