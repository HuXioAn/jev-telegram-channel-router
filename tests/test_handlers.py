"""bot.handlers 离线单测：频道登记事件（合成 ChatMemberUpdated + 假 bot）。"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import (CallbackQuery, Chat, ChatMemberAdministrator,
                      ChatMemberLeft, ChatMemberMember, ChatMemberOwner,
                      ChatMemberUpdated, Message, Update, User)

from conftest import make_template
from tgfilter.bot import handlers as h
from tgfilter.store import Store

BOT_ID = 8884453670
USER_ID = 8094970668
CHAT_ID = -1009876543210


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edited: list[str] = []
        self.members: dict[int, str] = {}  # user_id → 状态（get_chat_member 桩数据）

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        return SimpleNamespace(status=self.members.get(user_id, "left"))

    async def edit_message_text(self, *args, **kwargs):
        self.edited.append(kwargs.get("text", args[0] if args else ""))

    async def answer_callback_query(self, *args, **kwargs):
        pass


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
        bot=bot, user_data={})
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


def _text_update(text: str = "你好") -> Update:
    return Update(update_id=2, message=Message(
        message_id=10, date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, first_name="Anton", is_bot=False), text=text))


async def test_plain_text_gets_fallback_hint(tmp_path):
    """私聊纯文本不再石沉大海：兜底回复引导性提示。"""
    store, bot, context = _make(tmp_path)
    update = _text_update()
    update.message.set_bot(bot)
    await h.on_plain_text(update, context)
    assert bot.sent and "还没学会" in bot.sent[0][1]


def _group_update(text: str = "/list") -> Update:
    return Update(update_id=3, message=Message(
        message_id=11, date=datetime.now(timezone.utc),
        chat=Chat(id=-1001234567890, type="supergroup"),
        from_user=User(id=USER_ID, first_name="Anton", is_bot=False), text=text))


def test_resolve_dest_chat_only_for_owner(tmp_path):
    """目的地频道归属校验：非添加者不可选（多用户隔离）。"""
    store, bot, context = _make(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    assert h.resolve_dest_chat(store, USER_ID, CHAT_ID)["title"] == "测试频道"
    assert h.resolve_dest_chat(store, USER_ID + 1, CHAT_ID) is None
    assert h.resolve_dest_chat(store, USER_ID, CHAT_ID + 999) is None


async def test_group_commands_refuse_private_data(tmp_path):
    """群里执行 /list：只提示去私聊，不泄露任何订阅数据。"""
    store, bot, context = _make(tmp_path)
    store.add_subscription(user_id=USER_ID, source="chan", template=make_template(),
                           dest_kind="dm", dest_chat_id=USER_ID, dest_title="私聊",
                           interval_minutes=20, last_seen_id=1)
    update = _group_update()
    update.message.set_bot(bot)
    await h.cmd_list(update, context)
    assert bot.sent and "私聊" in bot.sent[0][1]
    assert all("chan" not in text for _, text in bot.sent)


async def test_channel_demotion_removes_registration(tmp_path):
    """频道内被降权（管理员 → 普通成员）时移除登记，目的地列表不留脏项。"""
    store, bot, context = _make(tmp_path)
    await h.on_my_chat_member(_member_update("channel", _admin()), context)
    assert store.get_chat(CHAT_ID) is not None
    await h.on_my_chat_member(
        _member_update("channel", ChatMemberMember(user=_bot_user())), context)
    assert store.get_chat(CHAT_ID) is None


def _cb_update(data: str) -> Update:
    msg = Message(message_id=20, date=datetime.now(timezone.utc),
                  chat=Chat(id=USER_ID, type="private"),
                  from_user=User(id=BOT_ID, first_name="bot", is_bot=True), text="x")
    query = CallbackQuery(id="42", from_user=User(id=USER_ID, first_name="Anton",
                                                  is_bot=False),
                          chat_instance="ci", data=data, message=msg)
    return Update(update_id=5, callback_query=query)


def _attach_bot(update: Update, bot: FakeBot) -> None:
    query = update.callback_query
    query.set_bot(bot)
    if query.message:
        query.message.set_bot(bot)


async def test_dest_choice_accepts_channel_admin(tmp_path):
    """bot 与用户均为频道管理员 → 通过并进入频率选择。"""
    store, bot, context = _make(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    bot.members = {BOT_ID: "administrator", USER_ID: "administrator"}
    update = _cb_update(f"dst:ch:{CHAT_ID}")
    _attach_bot(update, bot)
    state = await h.on_dest_choice(update, context)
    assert state == h.WAIT_INTERVAL
    assert context.user_data["dest_chat_id"] == CHAT_ID


async def test_dest_choice_rejects_user_no_longer_admin(tmp_path):
    """用户不再是频道管理员时拒绝（防陈旧权限）。"""
    store, bot, context = _make(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    bot.members = {BOT_ID: "administrator", USER_ID: "member"}
    update = _cb_update(f"dst:ch:{CHAT_ID}")
    _attach_bot(update, bot)
    state = await h.on_dest_choice(update, context)
    assert state == h.WAIT_DEST
    assert any("不是「测试频道」的管理员" in text for text in bot.edited)


async def test_subscription_cap_per_user(tmp_path):
    """每人订阅数上限：达到上限后不再新建。"""
    store, bot, context = _make(tmp_path)
    template = make_template()
    for index in range(h.MAX_SUBS_PER_USER):
        store.add_subscription(user_id=USER_ID, source=f"c{index}", template=template,
                               dest_kind="dm", dest_chat_id=USER_ID, dest_title="私聊",
                               interval_minutes=20, last_seen_id=1)
    update = _cb_update("iv:20")
    _attach_bot(update, bot)
    state = await h.on_interval_choice(update, context)
    assert state == h.ConversationHandler.END
    assert any("上限" in text for text in bot.edited)
    assert len(store.list_subscriptions(user_id=USER_ID)) == h.MAX_SUBS_PER_USER
