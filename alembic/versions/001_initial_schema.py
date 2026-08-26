"""Initial schema."""

from alembic import op

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            owner_id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS categories (
            id BIGSERIAL PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')),
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(owner_id, kind, name)
        );

        CREATE TABLE IF NOT EXISTS subcategories (
            id BIGSERIAL PRIMARY KEY,
            category_id BIGINT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(category_id, name)
        );

        CREATE TABLE IF NOT EXISTS budget_accounts (
            owner_id TEXT PRIMARY KEY REFERENCES users(owner_id) ON DELETE CASCADE,
            storage_kind TEXT NOT NULL,
            workbook_path TEXT,
            spreadsheet_url TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_images (
            id BIGSERIAL PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            image_hash TEXT NOT NULL,
            telegram_file_id TEXT,
            bank TEXT,
            status TEXT NOT NULL,
            raw_response JSONB,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(owner_id, image_hash)
        );

        CREATE TABLE IF NOT EXISTS operations (
            id BIGSERIAL PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            operation_hash TEXT NOT NULL,
            image_hash TEXT NOT NULL,
            bank TEXT NOT NULL,
            operation_json JSONB NOT NULL,
            status TEXT NOT NULL,
            workbook_row INTEGER,
            status_note TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(owner_id, operation_hash)
        );

        CREATE TABLE IF NOT EXISTS pending_actions (
            id BIGSERIAL PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            operation_hash TEXT NOT NULL,
            chat_id BIGINT NOT NULL,
            message_id BIGINT,
            prompt TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(owner_id, operation_hash)
        );

        CREATE TABLE IF NOT EXISTS learned_expense_categories (
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            merchant_key TEXT NOT NULL,
            merchant_name TEXT NOT NULL,
            category TEXT NOT NULL,
            subcategory TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(owner_id, merchant_key)
        );

        CREATE TABLE IF NOT EXISTS budget_entries (
            id BIGSERIAL PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(owner_id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            operation_hash TEXT,
            export_sheet TEXT,
            export_row INTEGER,
            operation_date DATE NOT NULL,
            operation_type TEXT NOT NULL,
            amount NUMERIC(14,2) NOT NULL,
            category TEXT,
            subcategory TEXT,
            name TEXT NOT NULL,
            note TEXT,
            bank TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE(owner_id, operation_hash, export_row)
        );

        CREATE TABLE IF NOT EXISTS telegram_chats (
            chat_id BIGINT PRIMARY KEY,
            user_id BIGINT,
            owner_id TEXT REFERENCES users(owner_id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminder_settings (
            chat_id BIGINT PRIMARY KEY REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL,
            time_local TEXT NOT NULL,
            timezone TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminder_deliveries (
            chat_id BIGINT NOT NULL REFERENCES telegram_chats(chat_id) ON DELETE CASCADE,
            reminder_date DATE NOT NULL,
            sent_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (chat_id, reminder_date)
        );

        CREATE INDEX IF NOT EXISTS categories_owner_kind_idx
            ON categories(owner_id, kind, sort_order);
        CREATE INDEX IF NOT EXISTS source_images_owner_hash_idx
            ON source_images(owner_id, image_hash);
        CREATE INDEX IF NOT EXISTS operations_owner_hash_idx
            ON operations(owner_id, operation_hash);
        CREATE INDEX IF NOT EXISTS pending_actions_owner_chat_idx
            ON pending_actions(owner_id, chat_id);
        CREATE INDEX IF NOT EXISTS budget_entries_owner_date_type_idx
            ON budget_entries(owner_id, operation_date, operation_type);
        CREATE INDEX IF NOT EXISTS budget_entries_owner_category_idx
            ON budget_entries(owner_id, category, subcategory);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS reminder_deliveries;
        DROP TABLE IF EXISTS reminder_settings;
        DROP TABLE IF EXISTS telegram_chats;
        DROP TABLE IF EXISTS budget_entries;
        DROP TABLE IF EXISTS learned_expense_categories;
        DROP TABLE IF EXISTS pending_actions;
        DROP TABLE IF EXISTS operations;
        DROP TABLE IF EXISTS source_images;
        DROP TABLE IF EXISTS budget_accounts;
        DROP TABLE IF EXISTS subcategories;
        DROP TABLE IF EXISTS categories;
        DROP TABLE IF EXISTS users;
        """
    )
