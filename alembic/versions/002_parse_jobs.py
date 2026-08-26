"""Parse jobs queue."""

from alembic import op

revision = "002_parse_jobs"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS parse_jobs (
            id BIGSERIAL PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            chat_id BIGINT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
            job_kind TEXT NOT NULL CHECK (job_kind IN ('single', 'album')),
            payload JSONB NOT NULL,
            error_message TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS parse_jobs_status_created_idx
            ON parse_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS parse_jobs_owner_chat_idx
            ON parse_jobs(owner_id, chat_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS parse_jobs;")
