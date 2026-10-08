"""Claim analytics notifications before sending."""
from alembic import op

revision = "004_analytics_claims"
down_revision = "003_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE analytics_deliveries ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'
                  CHECK (status IN ('sending', 'sent'));""")


def downgrade() -> None:
    op.execute("ALTER TABLE analytics_deliveries DROP COLUMN status;")
