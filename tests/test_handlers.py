"""bot.handlers 离线单测：频道登记事件（合成 ChatMemberUpdated + 假 bot）。"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import (Chat, ChatMemberAdministrator, ChatMemberLeft,
                      ChatMemberMember, ChatMemberOwner, ChatMemberUpdated,
                      Update, User)

from tgfilter.bot import handlers as h
from tgfilter.store import Store

BOT_ID = 8884453670
USER_ID = 8094970668
CHAT_ID = -1009876543210


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def _bot_user() -> User:
    return User(id=BOT_ID, first_name="bot", is_bot=True)


def _member_update(chat_type: str, new_member) -> Update:
    return Update(update_id=1, my_chat_member=ChatMemberUpdated(
        chat=Chat(id=CHAT_ID, type=chat_type, title="测试频道"),
        from_user=User(id=USER_ID, first_name="Anton", is_bot=False),
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(user=_bot_user()),
        new_chat_member=new_member))


def _admin() -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        user=_bot_user(), can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
        can_restrict_members=True, can_promote_members=False, can_change_info=True,
        can_invite_users=True, can_post_stories=False, can_edit_stories=False,
        can_delete_stories=False, can_post_messages=True)


def _make(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    bot = FakeBot()
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"services": SimpleNamespace(store=store)}),
        bot=bot)
    return store, bot, context


async def test_channel_admin_is_registered(tmp_path):
    store, bot, context = _make(tmp_path)
    await h.on_my_chat_member(_member_update("channel", _admin()), context)
    chat = store.get_chat(CHAT_ID)
    assert chat is not None and chat["added_by"] == USER_ID
    assert any("测试频道" in text for _, text in bot.sent)


async def test_channel_owner_is_registered(tmp_path):
    store, bot, context = _make(tmp_path)
    member = ChatMemberOwner(user=_bot_user(), is_anonymous=False)
    await h.on_my_chat_member(_member_update("channel", member), context)
    assert store.get_chat(CHAT_ID) is not None


async def test_channel_plain_member_is_not_registered(tmp_path):
    store, bot, context = _make(tmp_path)
    await h.on_my_chat_member(_member_update("channel", ChatMemberMember(user=_bot_user())), context)
    assert store.get_chat(CHAT_ID) is None


async def test_group_plain_member_is_registered(tmp_path):
    store, bot, context = _make(tmp_path)
    await h.on_my_chat_member(
        _member_update("supergroup", ChatMemberMember(user=_bot_user())), context)
    assert store.get_chat(CHAT_ID) is not None


async def test_leave_removes_chat(tmp_path):
    store, bot, context = _make(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    await h.on_my_chat_member(_member_update("channel", ChatMemberLeft(user=_bot_user())), context)
    assert store.get_chat(CHAT_ID) is None


def test_bot_commands_menu_covers_all_handlers():
    """客户端 “/” 菜单注册的命令必须覆盖全部处理器命令。"""
    from tgfilter.bot.app import BOT_COMMANDS

    names = {c.command for c in BOT_COMMANDS}
    assert names == {"start", "new", "list", "test", "help", "cancel"}
    assert all(3 <= len(c.description) <= 256 for c in BOT_COMMANDS)
