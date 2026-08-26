from __future__ import annotations

from pathlib import Path

import pytest

from budget_bot import cli
from budget_bot.config import Settings, validate_llm_settings


def test_validate_llm_settings_requires_openai_key() -> None:
    settings = Settings(
        telegram_bot_token="",
        telegram_allowed_user_ids=set(),
        telegram_allow_all=False,
        telegram_api_base_url="https://api.telegram.org",
        telegram_file_base_url="https://api.telegram.org/file",
        telegram_proxy_url="",
        telegram_timeout_seconds=60,
        use_env_proxy=True,
        llm_provider="openai",
        openai_api_key="",
        openai_model="gpt-4o-mini",
        openai_proxy_url="",
        openai_timeout_seconds=90,
        gemini_api_key="",
        gemini_model="gemini-2.5-flash",
        database_url="postgresql://x:x@127.0.0.1:1/x",
        export_dir=Path("."),
        default_timezone="Europe/Moscow",
        reminder_enabled=True,
        reminder_default_time="21:00",
        api_token="",
        max_upload_bytes=1,
    )
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        validate_llm_settings(settings)


def test_cli_migrate_does_not_build_llm_context(monkeypatch, capsys) -> None:
    called = {}

    class FakeSettings:
        database_url = "postgresql://user:pass@localhost/db"

    monkeypatch.setattr("sys.argv", ["budget-bot", "migrate"])
    monkeypatch.setattr("budget_bot.config.Settings.from_env", lambda: FakeSettings())
    monkeypatch.setattr("budget_bot.migrations.runner.upgrade_head", lambda database_url: called.setdefault("url", database_url))

    cli.main()

    assert called == {"url": "postgresql://user:pass@localhost/db"}
    assert "Migrations applied." in capsys.readouterr().out
