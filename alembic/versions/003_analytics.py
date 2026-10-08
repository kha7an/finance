"""Owner-scoped analytics preferences and notification deliveries."""
from alembic import op

revision = "003_analytics"
down_revision = "002_parse_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE budget_entries ADD COLUMN is_mandatory BOOLEAN NOT NULL DEFAULT FALSE;
        CREATE TABLE analytics_settings (
            owner_id TEXT PRIMARY KEY REFERENCES users(owner_id) ON DELETE CASCADE,
            exclude_mandatory BOOLEAN NOT NULL DEFAULT FALSE,
            mandatory_rules JSONB NOT NULL DEFAULT '[]',
            limits JSONB NOT NULL DEFAULT '{}',
            weekly_chat_id BIGINT,
            weekly_enabled BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE TABLE analytics_deliveries (
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            period_start DATE NOT NULL,
            delivery_key TEXT NOT NULL,
            sent_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (owner_id, kind, period_start, delivery_key)
        );
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE analytics_deliveries;
        DROP TABLE analytics_settings;
        ALTER TABLE budget_entries DROP COLUMN is_mandatory;
    """)
