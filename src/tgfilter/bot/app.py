"""PTB Application 构建与到期订阅调度。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from telegram import Bot
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          ChatMemberHandler, CommandHandler, ConversationHandler,
                          MessageHandler, filters)

from ..channel_fetch import ChannelFetcher
from ..config import Settings
from ..delivery import Sender
from ..jev import JevClient
from ..llm import TemplateCompiler
from ..pipeline import Pipeline
from ..services import Services
from ..store import Store
from . import handlers as h

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
ERROR_NOTIFY_COOLDOWN_SECONDS = 6 * 3600

_running: set[int] = set()
_last_notified: dict[int, datetime] = {}


async def _execute(services: Services, sub: dict, bot: Bot) -> None:
    try:
        result = await services.pipeline.run(sub)
        if result.error:
            logger.warning("subscription #%s: %s", sub["id"], result.error)
            now = datetime.now(timezone.utc)
            last = _last_notified.get(sub["id"])
            if not last or (now - last).total_seconds() > ERROR_NOTIFY_COOLDOWN_SECONDS:
                _last_notified[sub["id"]] = now
                try:
                    await bot.send_message(
                        sub["user_id"], f"⚠️ 订阅 #{sub['id']} 运行出错：{result.error}")
                except TelegramError:
                    pass
    except Exception:
        logger.exception("subscription #%s crashed", sub["id"])
    finally:
        _running.discard(sub["id"])


async def _tick(context) -> None:
    """每分钟扫一次到期订阅；每个订阅独立任务并发执行。"""
    services: Services = context.application.bot_data["services"]
    for sub in services.store.due_subscriptions(datetime.now(timezone.utc)):
        if sub["id"] in _running:
            continue
        _running.add(sub["id"])
        asyncio.create_task(_execute(services, sub, context.application.bot))


def build_application(settings: Settings) -> Application:
    async def post_init(app: Application) -> None:
        http = httpx.AsyncClient(timeout=settings.http_timeout)
        store = Store(settings.db_path)
        fetcher = ChannelFetcher(http, settings.fetch_page_delay)
        jev = JevClient(http, settings.typesafe_api_key, settings.typesafe_base_url,
                        settings.jev_concurrency)
        compiler = TemplateCompiler(http, settings) if settings.llm_enabled else None
        pipeline = Pipeline(store, fetcher, jev, Sender(app.bot),
                            settings.digest_chunk_limit)
        app.bot_data["services"] = Services(settings, store, fetcher, jev, compiler, pipeline)
        app.bot_data["http"] = http
        app.job_queue.run_repeating(_tick, interval=TICK_SECONDS, first=10,
                                    name="due-subscriptions")

    async def post_shutdown(app: Application) -> None:
        http: httpx.AsyncClient | None = app.bot_data.get("http")
        if http is not None:
            await http.aclose()

    app = (ApplicationBuilder().token(settings.bot_token)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("new", h.cmd_new),
            CallbackQueryHandler(h.cmd_new, pattern=r"^ui:new$"),
        ],
        states={
            h.WAIT_SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_source)],
            h.WAIT_DESCRIBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_describe)],
            h.CONFIRM_TEMPLATE: [CallbackQueryHandler(h.on_template_choice, pattern=r"^tpl:")],
            h.WAIT_ADJUST: [MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_adjust)],
            h.WAIT_DEST: [CallbackQueryHandler(h.on_dest_choice, pattern=r"^dst:")],
            h.WAIT_INTERVAL: [CallbackQueryHandler(h.on_interval_choice, pattern=r"^iv:")],
        },
        fallbacks=[CommandHandler("cancel", h.cmd_cancel)],
        allow_reentry=True,
    ))
    app.add_handler(CommandHandler("start", h.cmd_start))
    app.add_handler(CommandHandler("help", h.cmd_help))
    app.add_handler(CommandHandler("list", h.cmd_list))
    app.add_handler(CommandHandler("test", h.cmd_test))
    app.add_handler(CallbackQueryHandler(h.on_ui, pattern=r"^ui:(list|help)$"))
    app.add_handler(CallbackQueryHandler(h.on_sub_action, pattern=r"^sub:"))
    app.add_handler(ChatMemberHandler(h.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(h.on_error)
    return app
