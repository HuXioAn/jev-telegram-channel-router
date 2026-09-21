"""入口：python -m tgfilter"""
from __future__ import annotations

import logging

from telegram import Update

from .bot.app import build_application
from .config import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.load()
    if not settings.bot_token:
        raise SystemExit(
            "BOT_TOKEN 未配置：复制 .env.example 为 .env 并填写（bot 由 @BotFather 创建）。")
    settings.ensure_db_dir()
    app = build_application(settings)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
