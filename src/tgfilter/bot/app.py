"""PTB Application 构建与到期订阅调度。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from telegram import Bot, BotCommand, BotCommandScopeChat, Update
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, BaseUpdateProcessor,
                          CallbackQueryHandler, ChatMemberHandler, CommandHandler,
                          ConversationHandler, MessageHandler, TypeHandler, filters)

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

# 注册到 Telegram 的命令菜单（客户端输入框的 “/” 列表）
BOT_COMMANDS = [
    BotCommand("new", "新建订阅"),
    BotCommand("list", "我的订阅（选条目后暂停/试跑/编辑/删除）"),
    BotCommand("test", "试跑一次（样张发往订阅目标）"),
    BotCommand("help", "使用说明"),
    BotCommand("cancel", "取消当前操作"),
    BotCommand("start", "开始使用"),
]

CONCURRENT_UPDATES = 12  # 全局并行更新上限（同一聊天仍严格串行，见 PerChatUpdateProcessor）


class PerChatUpdateProcessor(BaseUpdateProcessor):
    """多用户并发：不同用户互不阻塞；同一聊天内更新严格串行，保证向导状态不乱序。

    PTB 自带的 SimpleUpdateProcessor 只做全局信号量限流，同一聊天的多次
    更新可能交叠执行（快速连点按钮/连发消息时向导会乱序），故在其上再加
    一层「每聊天锁」。
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
        if len(self._locks) > 4096:  # 丢弃空闲锁，防长期运行缓慢膨胀
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await coroutine

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
        try:
            await app.bot.set_my_commands(BOT_COMMANDS)
        except TelegramError:
            logger.warning("set_my_commands 失败（命令菜单未注册）", exc_info=True)
        # 管理员专属菜单：/admin 只出现在管理员自己的客户端里
        for admin_id in settings.admin_user_ids:
            try:
                await app.bot.set_my_commands(
                    BOT_COMMANDS + [BotCommand("admin", "管理员面板")],
                    scope=BotCommandScopeChat(chat_id=admin_id))
            except TelegramError:
                logger.warning("set_my_commands(admin=%s) 失败", admin_id, exc_info=True)
        app.job_queue.run_repeating(_tick, interval=TICK_SECONDS, first=10,
                                    name="due-subscriptions")

    async def post_shutdown(app: Application) -> None:
        http: httpx.AsyncClient | None = app.bot_data.get("http")
        if http is not None:
            await http.aclose()

    app = (ApplicationBuilder().token(settings.bot_token)
           .concurrent_updates(PerChatUpdateProcessor(CONCURRENT_UPDATES))
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("new", h.cmd_new),
            CallbackQueryHandler(h.cmd_new, pattern=r"^ui:new$"),
            CallbackQueryHandler(h.on_src_add, pattern=r"^ms:add:"),
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
            h.WAIT_INTERVAL: [CallbackQueryHandler(h.on_interval_choice, pattern=r"^iv:")],
        },
        fallbacks=[CommandHandler("cancel", h.cmd_cancel)],
        allow_reentry=True,
    ))
    app.add_handler(CommandHandler("start", h.cmd_start))
    app.add_handler(CommandHandler("help", h.cmd_help))
    app.add_handler(CommandHandler("list", h.cmd_list))
    app.add_handler(CommandHandler("test", h.cmd_test))
    app.add_handler(CommandHandler("admin", admin_handlers.cmd_admin))
    app.add_handler(CallbackQueryHandler(h.on_ui, pattern=r"^ui:(list|help)$"))
    app.add_handler(CallbackQueryHandler(h.on_sub_action, pattern=r"^sub:"))
    # 订阅编辑（管理器/频率/模板；会话进行中时由会话内同名处理器先行接管）
    app.add_handler(CallbackQueryHandler(h.on_src_manager, pattern=r"^ms:"))
    app.add_handler(CallbackQueryHandler(h.on_dest_manager, pattern=r"^md:"))
    app.add_handler(CallbackQueryHandler(h.on_edit_interval, pattern=r"^eiv:"))
    app.add_handler(CallbackQueryHandler(h.on_edit_interval_set, pattern=r"^eivs:"))
    app.add_handler(ChatMemberHandler(h.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    # 私聊兜底：未识别的文本/未知命令 → 固定提示（必须排在全部业务处理器之后）
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.TEXT | filters.COMMAND), h.on_plain_text))
    # 独立分组：每条更新记一行日志，用于排查「消息到底有没有到」
    app.add_handler(TypeHandler(Update, h.log_update), group=1)
    app.add_error_handler(h.on_error)
    return app
