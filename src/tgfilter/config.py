"""环境变量与全局配置。"""
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
    min_interval_minutes: int = 5
    jev_concurrency: int = 8
    http_timeout: float = 30.0
    fetch_page_delay: float = 0.6
    digest_chunk_limit: int = 3800

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
            min_interval_minutes=_int("MIN_INTERVAL_MINUTES", 5),
            jev_concurrency=_int("JEV_CONCURRENCY", 8),
            http_timeout=_float("HTTP_TIMEOUT", 30.0),
            fetch_page_delay=_float("FETCH_PAGE_DELAY", 0.6),
            digest_chunk_limit=_int("DIGEST_CHUNK_LIMIT", 3800),
        )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)

    def ensure_db_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
