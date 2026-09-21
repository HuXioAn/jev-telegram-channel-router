"""管理员命令单测：权限、总览、用户详情、封禁/恢复、配额。"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import Chat, Message, Update, User

from conftest import make_template
from tgfilter.bot import admin
from tgfilter.config import Settings
from tgfilter.store import Store

ADMIN_ID = 999
USER_ID = 7


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def _make(tmp_path, admin_ids=(ADMIN_ID,)):
    store = Store(str(tmp_path / "t.db"))
    settings = Settings(admin_user_ids=admin_ids)
    svc = SimpleNamespace(store=store, settings=settings)
    bot = FakeBot()
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"services": svc}),
        bot=bot, user_data={}, args=None)
    return store, bot, context


def _msg_update(user_id: int) -> Update:
    return Update(update_id=1, message=Message(
        message_id=1, date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, first_name="x", is_bot=False), text="/admin"))


async def _run(context, user_id: int, args: list[str]) -> str:
    update = _msg_update(user_id)
    update.message.set_bot(context.bot)
    context.args = args
    await admin.cmd_admin(update, context)
    return context.bot.sent[-1][1]


async def test_admin_denied_for_non_admin(tmp_path):
    _, _, context = _make(tmp_path)
    assert "未知命令" in await _run(context, USER_ID, [])


async def test_admin_overview_and_users(tmp_path):
    store, _, context = _make(tmp_path)
    store.add_user(USER_ID, "u1")
    store.add_subscription(user_id=USER_ID, source="c", template=make_template(),
                           dest_kind="dm", dest_chat_id=USER_ID, dest_title="私聊",
                           interval_minutes=20, last_seen_id=1)
    store.record_usage(USER_ID, "jev", 4)
    text = await _run(context, ADMIN_ID, [])
    assert "管理员总览" in text and "Jev判定 4" in text
    text = await _run(context, ADMIN_ID, ["users"])
    assert str(USER_ID) in text and "订阅 1" in text


async def test_admin_block_pauses_subs_and_notifies(tmp_path):
    store, bot, context = _make(tmp_path)
    store.add_user(USER_ID, "u1")
    sub_id = store.add_subscription(user_id=USER_ID, source="c", template=make_template(),
                                    dest_kind="dm", dest_chat_id=USER_ID, dest_title="私聊",
                                    interval_minutes=20, last_seen_id=1)
    text = await _run(context, ADMIN_ID, ["block", str(USER_ID)])
    assert "已停用" in text
    assert store.get_user(USER_ID)["status"] == "blocked"
    assert store.get_subscription(sub_id)["enabled"] == 0
    assert any(chat_id == USER_ID and "暂停" in t for chat_id, t in bot.sent)
    text = await _run(context, ADMIN_ID, ["unblock", str(USER_ID)])
    assert "已恢复" in text
    assert store.get_user(USER_ID)["status"] == "active"


async def test_admin_block_admin_refused(tmp_path):
    store, _, context = _make(tmp_path)
    store.add_user(ADMIN_ID)
    assert "不能" in await _run(context, ADMIN_ID, ["block", str(ADMIN_ID)])


async def test_admin_quota_and_user_detail(tmp_path):
    store, _, context = _make(tmp_path)
    store.add_user(USER_ID, "u1")
    text = await _run(context, ADMIN_ID, ["quota", str(USER_ID), "jev", "50"])
    assert "50" in text
    assert store.get_user(USER_ID)["quota_jev_monthly"] == 50
    store.record_usage(USER_ID, "jev", 4)
    text = await _run(context, ADMIN_ID, ["user", str(USER_ID)])
    assert "配额" in text and "本月 Jev 已用：4 / 50" in text and "Jev判定 4" in text


async def test_admin_bad_args_reports_help(tmp_path):
    _, _, context = _make(tmp_path)
    text = await _run(context, ADMIN_ID, ["user"])
    assert "参数错误" in text


async def test_admin_disabled_without_admin_ids(tmp_path):
    """未配置 ADMIN_USER_IDS 时管理员命令对任何人都不生效。"""
    _, _, context = _make(tmp_path, admin_ids=())
    assert "未知命令" in await _run(context, ADMIN_ID, [])
