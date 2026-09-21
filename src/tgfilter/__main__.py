"""Entry point: python -m tgfilter"""
from __future__ import annotations

import logging

from telegram import Update

from .bot.app import build_application
from .config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep polling heartbeats out of the log
    settings = Settings.load()
    if not settings.bot_token:
        raise SystemExit(
            "BOT_TOKEN is not set: copy .env.example to .env and fill it in "
            "(create a bot via @BotFather).")
    settings.ensure_db_dir()
    app = build_application(settings)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
