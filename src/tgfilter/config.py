"""Environment variables and global configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _ids(name: str) -> tuple[int, ...]:
    """Comma- (or semicolon-) separated list of Telegram user ids."""
    raw = _env(name).replace(";", ",")
    return tuple(int(part) for part in (p.strip() for p in raw.split(",")) if part.isdigit())


@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    typesafe_api_key: str = ""
    typesafe_base_url: str = "https://api.typesafe.ai/v1"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    db_path: str = "data/tgfilter.db"
    default_interval_minutes: int = 20
    jev_concurrency: int = 8
    http_timeout: float = 30.0
    fetch_page_delay: float = 0.6
    digest_chunk_limit: int = 3800
    judge_max_questions: int = 24
    default_lang: str = "en"
    admin_user_ids: tuple[int, ...] = ()
    default_user_status: str = "active"

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            bot_token=_env("BOT_TOKEN"),
            typesafe_api_key=_env("TYPESAFE_API_KEY"),
            typesafe_base_url=_env("TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1").rstrip("/"),
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
            db_path=_env("DB_PATH", "data/tgfilter.db"),
            default_interval_minutes=_int("DEFAULT_INTERVAL_MINUTES", 20),
            judge_max_questions=_int("JUDGE_MAX_QUESTIONS", 24),
            jev_concurrency=_int("JEV_CONCURRENCY", 8),
            http_timeout=_float("HTTP_TIMEOUT", 30.0),
            fetch_page_delay=_float("FETCH_PAGE_DELAY", 0.6),
            digest_chunk_limit=_int("DIGEST_CHUNK_LIMIT", 3800),
            default_lang=_env("DEFAULT_LANG", "en") or "en",
            admin_user_ids=_ids("ADMIN_USER_IDS"),
            default_user_status=_env("DEFAULT_USER_STATUS", "active") or "active",
        )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)

    def ensure_db_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
