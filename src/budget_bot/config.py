from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _str_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _allowed_users(value: Optional[str]) -> Set[int]:
    if not value:
        return set()
    return {int(part.strip()) for part in value.split(",") if part.strip()}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_user_ids: Set[int]
    telegram_allow_all: bool
    telegram_api_base_url: str
    telegram_file_base_url: str
    telegram_proxy_url: str
    telegram_timeout_seconds: int
    use_env_proxy: bool
    llm_provider: str
    openai_api_key: str
    openai_model: str
    openai_proxy_url: str
    openai_timeout_seconds: int
    gemini_api_key: str
    gemini_model: str
    database_url: str
    export_dir: Path
    default_timezone: str
    reminder_enabled: bool
    reminder_default_time: str
    api_token: str
    max_upload_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_user_ids=_allowed_users(os.getenv("TELEGRAM_ALLOWED_USER_IDS")),
            telegram_allow_all=_bool_env("TELEGRAM_ALLOW_ALL", False),
            telegram_api_base_url=os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/"),
            telegram_file_base_url=os.getenv("TELEGRAM_FILE_BASE_URL", "https://api.telegram.org/file").rstrip("/"),
            telegram_proxy_url=_str_env("TELEGRAM_PROXY_URL"),
            telegram_timeout_seconds=_int_env("TELEGRAM_TIMEOUT_SECONDS", 60),
            use_env_proxy=_bool_env("USE_ENV_PROXY", True),
            llm_provider=os.getenv("LLM_PROVIDER", "openai").strip().lower(),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            openai_proxy_url=_str_env("OPENAI_PROXY_URL") or _str_env("TELEGRAM_PROXY_URL"),
            openai_timeout_seconds=_int_env("OPENAI_TIMEOUT_SECONDS", 90),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            database_url=os.getenv("DATABASE_URL", "postgresql://budget_bot:budget_bot@127.0.0.1:5433/budget_bot"),
            export_dir=Path(os.getenv("BUDGET_EXPORT_DIR", "./data/exports")).expanduser(),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow"),
            reminder_enabled=_bool_env("REMINDER_ENABLED", True),
            reminder_default_time=os.getenv("REMINDER_DEFAULT_TIME", "21:00").strip(),
            api_token=os.getenv("BUDGET_API_TOKEN", ""),
            max_upload_bytes=_int_env("MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
        )


def validate_llm_settings(settings: Settings) -> None:
    provider = settings.llm_provider
    if provider == "openai":
        if not settings.openai_api_key.strip():
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return
    if provider == "gemini":
        if not settings.gemini_api_key.strip():
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        return
    if provider == "mock":
        return
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r}. Use openai, gemini, or mock.")
