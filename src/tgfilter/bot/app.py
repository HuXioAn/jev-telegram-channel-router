"""PTB Application wiring and the channel-refresh scheduler."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from telegram import (Bot, BotCommand, BotCommandScopeChat, LinkPreviewOptions,
                      Update)
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, BaseUpdateProcessor,
                          CallbackQueryHandler, ChatMemberHandler, CommandHandler,
                          ConversationHandler, Defaults, MessageHandler, TypeHandler,
                          filters)

from .. import i18n
from ..channel_fetch import ChannelFetcher
from ..config import Settings
from ..delivery import Sender
from ..jev import JevClient
from ..llm import TemplateCompiler
from ..pipeline import Pipeline
from ..services import Services
from ..store import Store
from . import admin as admin_handlers
from . import handlers as h

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
ERROR_NOTIFY_COOLDOWN_SECONDS = 6 * 3600

CONCURRENT_UPDATES = 12  # global parallel update cap (per chat still serial, see below)


def _bot_commands(lang: str, *, admin: bool = False) -> list[BotCommand]:
    """Command menu ("/" list) for one language; optionally with /admin."""
    commands = [BotCommand(cmd, desc) for cmd, desc in i18n.COMMANDS[lang]]
    if admin:
        commands.append(BotCommand("admin", i18n.ADMIN_COMMAND_DESC[lang]))
    return commands


class PerChatUpdateProcessor(BaseUpdateProcessor):
    """Multi-user concurrency: users never block each other; one chat is serial.

    PTB's SimpleUpdateProcessor only applies a global semaphore, so updates from
    the same chat may overlap (rapid button taps / messages can reorder wizard
    state). This processor adds a per-chat lock on top.
    """

    def __init__(self, max_concurrent_updates: int = CONCURRENT_UPDATES):
        super().__init__(max_concurrent_updates)
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

    @staticmethod
    def _key(update: object) -> tuple[int, int] | None:
        if not isinstance(update, Update):
            return None
        chat = update.effective_chat
        if chat is None:
            return None
        user = update.effective_user
        return (chat.id, user.id if user else 0)

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_process_update(self, update: object, coroutine) -> None:
        key = self._key(update)
        if key is None:
            await coroutine
            return
        if len(self._locks) > 4096:  # drop idle locks so long runs don't grow forever
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await coroutine

_running: set[str] = set()
_last_notified: dict[str, datetime] = {}


async def _execute_watch(services: Services, watch: dict, bot: Bot) -> None:
    channel = watch["channel"]
    try:
        result = await services.pipeline.run_watch(watch)
        if result.error:
            logger.warning("watch %s: %s", channel, result.error)
            now = datetime.now(timezone.utc)
            last = _last_notified.get(channel)
            if not last or (now - last).total_seconds() > ERROR_NOTIFY_COOLDOWN_SECONDS:
                _last_notified[channel] = now
                for admin_id in services.settings.admin_user_ids:
                    try:
                        lang = services.store.language_for(
                            admin_id, services.settings.default_lang)
                        await bot.send_message(
                            admin_id, i18n.t(lang, "watch_error",
                                             channel=channel, err=result.error))
                    except TelegramError:
                        pass
    except Exception:
        logger.exception("watch %s crashed", channel)
    finally:
        _running.discard(channel)


async def _tick(context) -> None:
    """Scan for due source channels once a minute; each runs as its own task."""
    services: Services = context.application.bot_data["services"]
    store = services.store
    store.sync_watches()
    store.prune_judgments()
    interval = store.fetch_interval_minutes(services.settings.default_interval_minutes)
    for watch in store.due_watches(datetime.now(timezone.utc), interval):
        channel = watch["channel"]
        if channel in _running:
            continue
        _running.add(channel)
        asyncio.create_task(_execute_watch(services, watch, context.application.bot))


def build_application(settings: Settings) -> Application:
    async def post_init(app: Application) -> None:
        http = httpx.AsyncClient(timeout=settings.http_timeout)
        store = Store(settings.db_path)
        fetcher = ChannelFetcher(http, settings.fetch_page_delay,
                                 max_chars=settings.max_post_chars)
        jev = JevClient(http, settings.typesafe_api_key, settings.typesafe_base_url,
                        settings.jev_concurrency)
        compiler = TemplateCompiler(http, settings) if settings.llm_enabled else None
        pipeline = Pipeline(store, fetcher, jev, Sender(app.bot),
                            settings.digest_chunk_limit,
                            settings.judge_max_questions,
                            settings.default_lang)
        app.bot_data["services"] = Services(settings, store, fetcher, jev, compiler, pipeline)
        app.bot_data["http"] = http
        # Command menu: English is the default; Chinese is served for zh clients.
        try:
            await app.bot.set_my_commands(_bot_commands("en"))
            await app.bot.set_my_commands(_bot_commands("zh"), language_code="zh")
        except TelegramError:
            logger.warning("set_my_commands failed (command menu not registered)", exc_info=True)
        # Admin-only menu: /admin shows up only in the admins' own clients.
        for admin_id in settings.admin_user_ids:
            for code in (None, "zh"):
                try:
                    await app.bot.set_my_commands(
                        _bot_commands(code or "en", admin=True),
                        scope=BotCommandScopeChat(chat_id=admin_id),
                        language_code=code)
                except TelegramError:
                    logger.warning("set_my_commands(admin=%s, lang=%s) failed",
                                   admin_id, code, exc_info=True)
        app.job_queue.run_repeating(_tick, interval=TICK_SECONDS, first=10,
                                    name="due-watches")

    async def post_shutdown(app: Application) -> None:
        http: httpx.AsyncClient | None = app.bot_data.get("http")
        if http is not None:
            await http.aclose()

    # Link previews off instance-wide (every send/edit): pushes carry links and
    # must not sprout a preview box below the text.
    app = (ApplicationBuilder().token(settings.bot_token)
           .defaults(Defaults(
               link_preview_options=LinkPreviewOptions(is_disabled=True)))
           .concurrent_updates(PerChatUpdateProcessor(CONCURRENT_UPDATES))
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("new", h.cmd_new),
            CallbackQueryHandler(h.cmd_new, pattern=r"^ui:new$"),
            CallbackQueryHandler(h.on_src_add, pattern=r"^ms:(add|menu):"),
            CallbackQueryHandler(h.on_edit_template, pattern=r"^etpl:"),
        ],
        states={
            h.WAIT_SOURCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_source),
                CallbackQueryHandler(h.on_src_manager, pattern=r"^ms:"),
            ],
            h.WAIT_DESCRIBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_describe)],
            h.CONFIRM_TEMPLATE: [CallbackQueryHandler(h.on_template_choice, pattern=r"^tpl:")],
            h.WAIT_ADJUST: [MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_adjust)],
            h.WAIT_DEST: [CallbackQueryHandler(h.on_dest_manager, pattern=r"^md:")],
        },
        fallbacks=[CommandHandler("cancel", h.cmd_cancel)],
        allow_reentry=True,
    ))
    app.add_handler(CommandHandler("start", h.cmd_start))
    app.add_handler(CommandHandler("help", h.cmd_help))
    app.add_handler(CommandHandler("list", h.cmd_list))
    app.add_handler(CommandHandler("test", h.cmd_test))
    app.add_handler(CommandHandler("lang", h.cmd_lang))
    app.add_handler(CommandHandler("admin", admin_handlers.cmd_admin))
    app.add_handler(CallbackQueryHandler(h.on_ui, pattern=r"^ui:(list|help)$"))
    app.add_handler(CallbackQueryHandler(h.on_lang, pattern=r"^lang:set:"))
    app.add_handler(CallbackQueryHandler(h.on_sub_action, pattern=r"^sub:"))
    # Subscription editing (managers/template; during a conversation the same-named
    # in-conversation handlers take over first)
    app.add_handler(CallbackQueryHandler(h.on_src_manager, pattern=r"^ms:"))
    app.add_handler(CallbackQueryHandler(h.on_dest_manager, pattern=r"^md:"))
    app.add_handler(ChatMemberHandler(h.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    # Private-chat fallback: unrecognized text / unknown commands → fixed hint
    # (must come after every business handler)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.TEXT | filters.COMMAND), h.on_plain_text))
    # Separate group: log one line per update, for "did the message arrive" debugging
    app.add_handler(TypeHandler(Update, h.log_update), group=1)
    app.add_error_handler(h.on_error)
    return app
