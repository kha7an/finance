from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config

from ..log_config import reconfigure_logging
from .url import sqlalchemy_url


def alembic_config(database_url: Optional[str] = None) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    url = database_url or os.getenv("DATABASE_URL", "")
    if url:
        config.set_main_option("sqlalchemy.url", sqlalchemy_url(url))
    return config


def upgrade_head(database_url: Optional[str] = None) -> None:
    command.upgrade(alembic_config(database_url), "head")
    reconfigure_logging()
