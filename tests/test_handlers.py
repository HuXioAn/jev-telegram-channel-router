"""bot.handlers offline unit tests: channel registration events (synthetic ChatMemberUpdated + fake bot)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import (CallbackQuery, Chat, ChatMemberAdministrator,
                      ChatMemberLeft, ChatMemberMember, ChatMemberOwner,
                      ChatMemberUpdated, Message, Update, User)

from conftest import make_template
from tgfilter.bot import handlers as h
from tgfilter.config import Settings
from tgfilter.store import Store

BOT_ID = 8884453670
USER_ID = 8094970668
CHAT_ID = -1009876543210


class FakeBot:
    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.sent_markups: list = []    # reply_markup passed to send_message
        self.edited: list[str] = []
        self.edited_markups: list = []  # reply_markup passed to edit_message_text
        self.members: dict[int, str] = {}  # user_id → status (get_chat_member stub data)
        self.answers: list[tuple[str | None, bool]] = []  # callback query answers (text, show_alert)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        self.sent_markups.append(kwargs.get("reply_markup"))

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        return SimpleNamespace(status=self.members.get(user_id, "left"))

    async def edit_message_text(self, *args, **kwargs):
        self.edited.append(kwargs.get("text", args[0] if args else ""))
        self.edited_markups.append(kwargs.get("reply_markup"))

    async def answer_callback_query(self, *args, **kwargs):
        self.answers.append((kwargs.get("text"), bool(kwargs.get("show_alert"))))


class FakeFetcher:
    """Fetcher stub that only provides head (used to add channels in the wizard/edit flows)."""

    def __init__(self, head_id: int = 500, error: str | None = None):
        self.head_id = head_id
        self.error = error
        self.head_calls: list[str] = []

    async def head(self, channel: str):
        from tgfilter.channel_fetch import ChannelError, ChannelInfo

        self.head_calls.append(channel)
        if self.error:
            raise ChannelError(self.error)
        return ChannelInfo(channel=channel, title=channel, head_id=self.head_id, posts=[])


class FakeCompiler:
    """Template compiler stub: returns a fixed template + fixed usage."""

    def __init__(self, template=None):
        self.template = template or make_template()

    async def compile(self, text, feedback=None, previous=None):
        return self.template, {"input_tokens": 900, "output_tokens": 60, "calls": 1}


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
        application=SimpleNamespace(bot_data={
            "services": SimpleNamespace(store=store,
                                        settings=Settings(default_lang="zh"))}),
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
    """The client "/" menu must cover every handler command, in every language."""
    from tgfilter import i18n

    expected = {"start", "new", "list", "test", "lang", "help", "cancel"}
    for lang in i18n.LANGS:
        names = {cmd for cmd, _ in i18n.COMMANDS[lang]}
        assert names == expected
        assert all(3 <= len(desc) <= 256 for _, desc in i18n.COMMANDS[lang])


def _text_update(text: str = "你好") -> Update:
    return Update(update_id=2, message=Message(
        message_id=10, date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, first_name="Anton", is_bot=False), text=text))


async def test_plain_text_gets_fallback_hint(tmp_path):
    """Plain text in a DM no longer falls into the void: a fallback reply offers guidance."""
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
    """Destination channel ownership check: non-adders cannot select it (multi-user isolation)."""
    store, bot, context = _make(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    assert h.resolve_dest_chat(store, USER_ID, CHAT_ID)["title"] == "测试频道"
    assert h.resolve_dest_chat(store, USER_ID + 1, CHAT_ID) is None
    assert h.resolve_dest_chat(store, USER_ID, CHAT_ID + 999) is None


async def test_group_commands_refuse_private_data(tmp_path):
    """Running /list inside a group: only points the user to the DM, leaking no subscription data."""
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
    """Demoted inside the channel (administrator → regular member): the registration is removed so the destination list keeps no stale entries."""
    store, bot, context = _make(tmp_path)
    await h.on_my_chat_member(_member_update("channel", _admin()), context)
    assert store.get_chat(CHAT_ID) is not None
    await h.on_my_chat_member(
        _member_update("channel", ChatMemberMember(user=_bot_user())), context)
    assert store.get_chat(CHAT_ID) is None


def _cb_update(data: str, user_id: int = USER_ID) -> Update:
    msg = Message(message_id=20, date=datetime.now(timezone.utc),
                  chat=Chat(id=USER_ID, type="private"),
                  from_user=User(id=BOT_ID, first_name="bot", is_bot=True), text="x")
    query = CallbackQuery(id="42", from_user=User(id=user_id, first_name="Anton",
                                                  is_bot=False),
                          chat_instance="ci", data=data, message=msg)
    return Update(update_id=5, callback_query=query)


def _attach_bot(update: Update, bot: FakeBot) -> None:
    query = update.callback_query
    query.set_bot(bot)
    if query.message:
        query.message.set_bot(bot)


async def test_dest_manager_accepts_channel_admin(tmp_path):
    """Both the bot and the user are channel admins → the channel is accepted as a destination (creation flow)."""
    store, bot, context = _make(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    bot.members = {BOT_ID: "administrator", USER_ID: "administrator"}
    context.user_data["dests"] = []
    update = _cb_update(f"md:ch:new:{CHAT_ID}")
    _attach_bot(update, bot)
    state = await h.on_dest_manager(update, context)
    assert state == h.WAIT_DEST
    assert context.user_data["dests"][0]["chat_id"] == CHAT_ID
    assert context.user_data["dests"][0]["title"] == "测试频道"


async def test_dest_manager_rejects_user_no_longer_admin(tmp_path):
    """Rejected once the user is no longer a channel admin (guards against stale permissions)."""
    store, bot, context = _make(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    bot.members = {BOT_ID: "administrator", USER_ID: "member"}
    context.user_data["dests"] = []
    update = _cb_update(f"md:ch:new:{CHAT_ID}")
    _attach_bot(update, bot)
    state = await h.on_dest_manager(update, context)
    assert state == h.WAIT_DEST
    assert context.user_data["dests"] == []
    assert any("不是「测试频道」的管理员" in text for text in bot.edited)


async def test_dest_manager_rejects_last_removal(tmp_path):
    """Destinations may not be emptied: removing the last one pops a warning and deletes nothing."""
    store, bot, context = _make(tmp_path)
    context.user_data["dests"] = [{"kind": "dm", "chat_id": USER_ID, "title": "私聊"}]
    update = _cb_update("md:rm:new:0")
    _attach_bot(update, bot)
    state = await h.on_dest_manager(update, context)
    assert state == h.WAIT_DEST
    assert context.user_data["dests"]  # not deleted
    assert bot.answers[-1] == ("⚠️ 至少要保留一个目的地。", True)


async def test_subscription_cap_per_user(tmp_path):
    """Per-user subscription cap: no new subscription once the cap is reached."""
    store, bot, context = _make(tmp_path)
    template = make_template()
    for index in range(h.MAX_SUBS_PER_USER):
        store.add_subscription(user_id=USER_ID, source=f"c{index}", template=template,
                               dest_kind="dm", dest_chat_id=USER_ID, dest_title="私聊",
                               interval_minutes=20, last_seen_id=1)
    context.user_data["sources"] = [{"source": "chan_new", "head_id": 500}]
    context.user_data["dests"] = [{"kind": "dm", "chat_id": USER_ID, "title": "私聊"}]
    context.user_data[h.K_TEMPLATE] = template.model_dump()
    update = _cb_update("md:done:new")
    _attach_bot(update, bot)
    state = await h.on_dest_manager(update, context)
    assert state == h.ConversationHandler.END
    assert any("上限" in text for text in bot.edited)
    assert len(store.list_subscriptions(user_id=USER_ID)) == h.MAX_SUBS_PER_USER


async def test_blocked_user_commands_refused(tmp_path):
    """After an admin blocks the user: /list and /test only return a notice and expose no functionality."""
    store, bot, context = _make(tmp_path)
    store.add_user(USER_ID, "ant")
    store.set_user_fields(USER_ID, status="blocked")
    update = _text_update("/list")
    update.message.set_bot(bot)
    await h.cmd_list(update, context)
    assert bot.sent and "暂停" in bot.sent[0][1]
    update = _text_update("/test")
    update.message.set_bot(bot)
    await h.cmd_test(update, context)
    assert "暂停" in bot.sent[-1][1]


# --------------------------------------------------------- subscription list (picker menu)
def _kb_datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _quick_sub(store, name: str, title: str = "私聊", kind: str = "dm",
               chat_id: int = USER_ID) -> int:
    return store.add_subscription(user_id=USER_ID, source=name, template=make_template(),
                                  interval_minutes=20, dest_kind=kind,
                                  dest_chat_id=chat_id, dest_title=title,
                                  last_seen_id=1)


async def test_list_is_single_pick_message(tmp_path):
    """/list becomes a single message plus an entry picker (no longer one row of buttons per subscription)."""
    store, bot, context = _make(tmp_path)
    id1 = _quick_sub(store, "chan_a")
    id2 = _quick_sub(store, "chan_b", "测试频道", kind="channel", chat_id=CHAT_ID)
    update = _text_update("/list")
    update.message.set_bot(bot)
    await h.cmd_list(update, context)
    assert len(bot.sent) == 1  # a single picker message, not one per subscription
    text = bot.sent[0][1]
    assert "我的订阅" in text and f"#{id1}" in text and f"#{id2}" in text
    assert _kb_datas(bot.sent_markups[0]) == [f"sub:open:{id1}", f"sub:open:{id2}"]


async def test_sub_pick_opens_entry_actions(tmp_path):
    """Picking an entry → that entry's detail view + action buttons (including `⬅️ Back to list`)."""
    store, bot, context = _make(tmp_path)
    sub_id = _quick_sub(store, "chan_a")
    update = _cb_update(f"sub:open:{sub_id}")
    _attach_bot(update, bot)
    await h.on_sub_action(update, context)
    assert f"#{sub_id}" in bot.edited[-1] and "运行中" in bot.edited[-1]
    datas = _kb_datas(bot.edited_markups[-1])
    assert f"sub:pause:{sub_id}" in datas and f"sub:delete:{sub_id}" in datas
    assert "sub:list" in datas


async def test_sub_back_list_returns_to_picker(tmp_path):
    """`⬅️ Back to list` returns to the picker menu."""
    store, bot, context = _make(tmp_path)
    sub_id = _quick_sub(store, "chan_a")
    update = _cb_update("sub:list")
    _attach_bot(update, bot)
    await h.on_sub_action(update, context)
    assert "我的订阅" in bot.edited[-1]
    assert _kb_datas(bot.edited_markups[-1]) == [f"sub:open:{sub_id}"]


async def test_sub_pick_rejects_foreign_subscription(tmp_path):
    """Picking someone else's subscription id → handled as not found."""
    store, bot, context = _make(tmp_path)
    sub_id = store.add_subscription(user_id=USER_ID + 1, source="chan_x",
                                    template=make_template(), interval_minutes=20,
                                    dest_kind="dm", dest_chat_id=USER_ID + 1,
                                    dest_title="私聊", last_seen_id=1)
    update = _cb_update(f"sub:open:{sub_id}")
    _attach_bot(update, bot)
    await h.on_sub_action(update, context)
    assert "未找到订阅" in bot.edited[-1]


async def test_sub_delete_returns_to_fresh_picker(tmp_path):
    """After deletion it returns to a freshly built picker menu (no hollow shell left with action buttons)."""
    store, bot, context = _make(tmp_path)
    id1 = _quick_sub(store, "chan_a")
    id2 = _quick_sub(store, "chan_b", "测试频道", kind="channel", chat_id=CHAT_ID)
    update = _cb_update(f"sub:delete:{id1}")
    _attach_bot(update, bot)
    await h.on_sub_action(update, context)
    assert "已删除" in bot.edited[-1] and f"#{id2}" in bot.edited[-1]
    assert _kb_datas(bot.edited_markups[-1]) == [f"sub:open:{id2}"]


async def test_llm_compile_records_usage(tmp_path):
    """Template compilation counts as llm usage: qty = number of API calls, plus the real input/output tokens."""
    store, bot, context = _make(tmp_path)

    context.application.bot_data["services"].compiler = FakeCompiler()
    update = _text_update("只要是与中国相关的消息")
    update.message.set_bot(bot)
    state = await h.on_describe(update, context)
    assert state == h.CONFIRM_TEMPLATE
    assert store.usage_rollup(user_id=USER_ID)["llm"] == {
        "count": 1, "in": 900, "out": 60}


# ------------------------------------------------------------ wizard (n sources → m destinations)
def _edit_env(tmp_path):
    """An existing subscription with 1 source and 1 DM destination, for the edit-flow tests."""
    store, bot, context = _make(tmp_path)
    sub_id = store.add_subscription(
        user_id=USER_ID, template=make_template(), interval_minutes=20,
        sources=[{"source": "chan_a", "last_seen_id": 100}],
        dests=[{"kind": "dm", "chat_id": USER_ID, "title": "私聊"}])
    return store, bot, context, sub_id


async def test_create_flow_multi_sources_and_dests(tmp_path):
    """Full wizard flow: two source channels + several destinations → one n:n subscription."""
    store, bot, context = _make(tmp_path)
    services = context.application.bot_data["services"]
    services.fetcher = FakeFetcher()
    services.compiler = FakeCompiler()

    for name in ("chan_a", "chan_b"):
        update = _text_update(f"@{name}")
        update.message.set_bot(bot)
        assert await h.on_source(update, context) == h.WAIT_SOURCE
    assert [s["source"] for s in context.user_data["sources"]] == ["chan_a", "chan_b"]

    update = _cb_update("ms:done:new")
    _attach_bot(update, bot)
    assert await h.on_src_manager(update, context) == h.WAIT_DESCRIBE

    update = _text_update("中国相关的重磅消息")
    update.message.set_bot(bot)
    assert await h.on_describe(update, context) == h.CONFIRM_TEMPLATE

    update = _cb_update("tpl:confirm")
    _attach_bot(update, bot)
    assert await h.on_template_choice(update, context) == h.WAIT_DEST

    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    bot.members = {BOT_ID: "administrator", USER_ID: "administrator"}
    update = _cb_update(f"md:ch:new:{CHAT_ID}")
    _attach_bot(update, bot)
    assert await h.on_dest_manager(update, context) == h.WAIT_DEST
    update = _cb_update("md:dm:new")
    _attach_bot(update, bot)
    assert await h.on_dest_manager(update, context) == h.WAIT_DEST

    update = _cb_update("md:done:new")
    _attach_bot(update, bot)
    assert await h.on_dest_manager(update, context) == h.ConversationHandler.END

    subs = store.list_subscriptions(user_id=USER_ID)
    assert len(subs) == 1
    sub = subs[0]
    assert [s["source"] for s in sub["sources"]] == ["chan_a", "chan_b"]
    assert [s["last_seen_id"] for s in sub["sources"]] == [500, 500]  # start from the head
    assert [d["chat_id"] for d in sub["dests"]] == [CHAT_ID, USER_ID]
    assert sub["interval_minutes"] == services.settings.default_interval_minutes
    assert sub["enabled"] == 1
    assert any("已创建" in text for text in bot.edited)


# ------------------------------------------------------------------ edit flow
async def test_edit_menu_offers_source_dest_template(tmp_path):
    """✏️ Edit → edit menu: source channels / destinations / filter template (frequency was dropped)."""
    store, bot, context, sub_id = _edit_env(tmp_path)
    update = _cb_update(f"sub:edit:{sub_id}")
    _attach_bot(update, bot)
    await h.on_sub_action(update, context)
    assert any("编辑订阅" in text for text in bot.edited)
    datas = _kb_datas(bot.edited_markups[-1])
    assert f"ms:menu:{sub_id}" in datas and f"md:menu:{sub_id}" in datas
    assert f"etpl:{sub_id}" in datas
    assert not any(data.startswith("eiv") for data in datas)


async def test_edit_template_overwrites(tmp_path):
    """Edit template: etpl → describe again → confirm overwrite and save; the conversation ends."""
    store, bot, context, sub_id = _edit_env(tmp_path)
    new_template = make_template().model_copy(update={"name": "新模板"})
    context.application.bot_data["services"].compiler = FakeCompiler(new_template)

    update = _cb_update(f"etpl:{sub_id}")
    _attach_bot(update, bot)
    assert await h.on_edit_template(update, context) == h.WAIT_DESCRIBE
    assert context.user_data[h.K_EDIT_SUB] == str(sub_id)

    update = _text_update("换个筛选条件")
    update.message.set_bot(bot)
    assert await h.on_describe(update, context) == h.CONFIRM_TEMPLATE
    assert any("覆盖" in text for _, text in bot.sent)  # confirmation copy carries editing semantics

    update = _cb_update("tpl:confirm")
    _attach_bot(update, bot)
    assert await h.on_template_choice(update, context) == h.ConversationHandler.END
    from tgfilter.store import template_of as _tpl

    assert _tpl(store.get_subscription(sub_id)).name == "新模板"
    assert h.K_EDIT_SUB not in context.user_data
    assert any("已更新" in text for text in bot.edited)


async def test_edit_sources_add_remove(tmp_path):
    """Edit source channels: add (cursor = current head), remove; emptying is not allowed."""
    store, bot, context, sub_id = _edit_env(tmp_path)
    context.application.bot_data["services"].fetcher = FakeFetcher(head_id=777)

    update = _cb_update(f"ms:add:{sub_id}")
    _attach_bot(update, bot)
    assert await h.on_src_add(update, context) == h.WAIT_SOURCE

    update = _text_update("@chan_b")
    update.message.set_bot(bot)
    assert await h.on_source(update, context) == h.WAIT_SOURCE
    sub = store.get_subscription(sub_id)
    assert [s["source"] for s in sub["sources"]] == ["chan_a", "chan_b"]
    assert sub["sources"][1]["last_seen_id"] == 777

    update = _cb_update(f"ms:rm:{sub_id}:{sub['sources'][1]['id']}")
    _attach_bot(update, bot)
    await h.on_src_manager(update, context)
    assert [s["source"] for s in store.get_subscription(sub_id)["sources"]] == ["chan_a"]

    sub = store.get_subscription(sub_id)
    update = _cb_update(f"ms:rm:{sub_id}:{sub['sources'][0]['id']}")
    _attach_bot(update, bot)
    await h.on_src_manager(update, context)
    assert len(store.get_subscription(sub_id)["sources"]) == 1  # not emptied
    assert bot.answers[-1] == ("⚠️ 至少要保留一个源频道。", True)


async def test_edit_dests_add_and_remove(tmp_path):
    """Edit destinations: add a channel (permissions re-checked), remove; emptying is not allowed."""
    store, bot, context, sub_id = _edit_env(tmp_path)
    store.upsert_chat(CHAT_ID, "channel", "测试频道", USER_ID)
    bot.members = {BOT_ID: "administrator", USER_ID: "administrator"}

    update = _cb_update(f"md:ch:{sub_id}:{CHAT_ID}")
    _attach_bot(update, bot)
    await h.on_dest_manager(update, context)
    sub = store.get_subscription(sub_id)
    assert [d["chat_id"] for d in sub["dests"]] == [USER_ID, CHAT_ID]

    update = _cb_update(f"md:rm:{sub_id}:{sub['dests'][1]['id']}")
    _attach_bot(update, bot)
    await h.on_dest_manager(update, context)
    assert [d["chat_id"] for d in store.get_subscription(sub_id)["dests"]] == [USER_ID]

    sub = store.get_subscription(sub_id)
    update = _cb_update(f"md:rm:{sub_id}:{sub['dests'][0]['id']}")
    _attach_bot(update, bot)
    await h.on_dest_manager(update, context)
    assert len(store.get_subscription(sub_id)["dests"]) == 1  # not emptied
    assert bot.answers[-1] == ("⚠️ 至少要保留一个目的地。", True)


async def test_edit_refuses_other_users_sub(tmp_path):
    """Multi-user isolation: someone else's subscription cannot be edited (guessing the id is useless)."""
    store, bot, context, sub_id = _edit_env(tmp_path)
    update = _cb_update(f"etpl:{sub_id}", user_id=USER_ID + 1)
    _attach_bot(update, bot)
    assert await h.on_edit_template(update, context) == h.ConversationHandler.END
    assert any("未找到" in text for text in bot.edited)
    assert h.K_EDIT_SUB not in context.user_data
