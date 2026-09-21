"""bot.handlers 离线单测：频道登记事件（合成 ChatMemberUpdated + 假 bot）。"""
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
        self.sent_markups: list = []    # send_message 的 reply_markup
        self.edited: list[str] = []
        self.edited_markups: list = []  # edit_message_text 的 reply_markup
        self.members: dict[int, str] = {}  # user_id → 状态（get_chat_member 桩数据）
        self.answers: list[tuple[str | None, bool]] = []  # 回调查询的应答（文案, 是否弹窗）

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
    """只提供 head 的抓取器桩（向导/编辑里添加频道用）。"""

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
    """模板编译器桩：返回固定模板 + 固定用量。"""

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
            "services": SimpleNamespace(store=store, settings=Settings())}),
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
    """bot 与用户均为频道管理员 → 频道加入目的地（新建流程）。"""
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
    """用户不再是频道管理员时拒绝（防陈旧权限）。"""
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
    """目的地不允许删空：最后一个目的地弹出警告且不删。"""
    store, bot, context = _make(tmp_path)
    context.user_data["dests"] = [{"kind": "dm", "chat_id": USER_ID, "title": "私聊"}]
    update = _cb_update("md:rm:new:0")
    _attach_bot(update, bot)
    state = await h.on_dest_manager(update, context)
    assert state == h.WAIT_DEST
    assert context.user_data["dests"]  # 未被删除
    assert bot.answers[-1] == ("⚠️ 至少要保留一个目的地。", True)


async def test_subscription_cap_per_user(tmp_path):
    """每人订阅数上限：达到上限后不再新建。"""
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
    """被管理员停用后：/list、/test 只回提示，不提供任何功能。"""
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


# --------------------------------------------------------- 订阅列表（选择菜单）
def _kb_datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _quick_sub(store, name: str, title: str = "私聊", kind: str = "dm",
               chat_id: int = USER_ID) -> int:
    return store.add_subscription(user_id=USER_ID, source=name, template=make_template(),
                                  interval_minutes=20, dest_kind=kind,
                                  dest_chat_id=chat_id, dest_title=title,
                                  last_seen_id=1)


async def test_list_is_single_pick_message(tmp_path):
    """/list 改为单条消息 + 条目选择菜单（不再是每条订阅各带一排按钮）。"""
    store, bot, context = _make(tmp_path)
    id1 = _quick_sub(store, "chan_a")
    id2 = _quick_sub(store, "chan_b", "测试频道", kind="channel", chat_id=CHAT_ID)
    update = _text_update("/list")
    update.message.set_bot(bot)
    await h.cmd_list(update, context)
    assert len(bot.sent) == 1  # 单条选择消息，而不是每订阅一条
    text = bot.sent[0][1]
    assert "我的订阅" in text and f"#{id1}" in text and f"#{id2}" in text
    assert _kb_datas(bot.sent_markups[0]) == [f"sub:open:{id1}", f"sub:open:{id2}"]


async def test_sub_pick_opens_entry_actions(tmp_path):
    """点选条目 → 该条目的详情 + 操作按钮（含「⬅️ 返回列表」）。"""
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
    """「⬅️ 返回列表」回到选择菜单。"""
    store, bot, context = _make(tmp_path)
    sub_id = _quick_sub(store, "chan_a")
    update = _cb_update("sub:list")
    _attach_bot(update, bot)
    await h.on_sub_action(update, context)
    assert "我的订阅" in bot.edited[-1]
    assert _kb_datas(bot.edited_markups[-1]) == [f"sub:open:{sub_id}"]


async def test_sub_pick_rejects_foreign_subscription(tmp_path):
    """选中别人的订阅编号 → 按未找到处理。"""
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
    """删除后自动回到刷新过的选择菜单（不残留操作按钮的空壳）。"""
    store, bot, context = _make(tmp_path)
    id1 = _quick_sub(store, "chan_a")
    id2 = _quick_sub(store, "chan_b", "测试频道", kind="channel", chat_id=CHAT_ID)
    update = _cb_update(f"sub:delete:{id1}")
    _attach_bot(update, bot)
    await h.on_sub_action(update, context)
    assert "已删除" in bot.edited[-1] and f"#{id2}" in bot.edited[-1]
    assert _kb_datas(bot.edited_markups[-1]) == [f"sub:open:{id2}"]


async def test_llm_compile_records_usage(tmp_path):
    """模板编译计入 llm 用量：qty=API 调用次数，并记录真实 input/output token。"""
    store, bot, context = _make(tmp_path)

    context.application.bot_data["services"].compiler = FakeCompiler()
    update = _text_update("只要是与中国相关的消息")
    update.message.set_bot(bot)
    state = await h.on_describe(update, context)
    assert state == h.CONFIRM_TEMPLATE
    assert store.usage_rollup(user_id=USER_ID)["llm"] == {
        "count": 1, "in": 900, "out": 60}


# ------------------------------------------------------------ 向导（n 源 → m 目的地）
def _edit_env(tmp_path):
    """已有 1 源 1 私聊目的地的订阅，供编辑流程测试。"""
    store, bot, context = _make(tmp_path)
    sub_id = store.add_subscription(
        user_id=USER_ID, template=make_template(), interval_minutes=20,
        sources=[{"source": "chan_a", "last_seen_id": 100}],
        dests=[{"kind": "dm", "chat_id": USER_ID, "title": "私聊"}])
    return store, bot, context, sub_id


async def test_create_flow_multi_sources_and_dests(tmp_path):
    """向导全流程：两个源频道 + 多目的地 → 一条 n:n 订阅。"""
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
    assert [s["last_seen_id"] for s in sub["sources"]] == [500, 500]  # 从头部起
    assert [d["chat_id"] for d in sub["dests"]] == [CHAT_ID, USER_ID]
    assert sub["interval_minutes"] == services.settings.default_interval_minutes
    assert sub["enabled"] == 1
    assert any("已创建" in text for text in bot.edited)


# ------------------------------------------------------------------ 编辑流程
async def test_edit_menu_offers_source_dest_template(tmp_path):
    """✏️ 编辑 → 编辑菜单：源频道 / 目的地 / 筛选模板（频率已取消）。"""
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
    """编辑模板：etpl → 重新描述 → 确认覆盖保存，会话结束。"""
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
    assert any("覆盖" in text for _, text in bot.sent)  # 确认文案为编辑语义

    update = _cb_update("tpl:confirm")
    _attach_bot(update, bot)
    assert await h.on_template_choice(update, context) == h.ConversationHandler.END
    from tgfilter.store import template_of as _tpl

    assert _tpl(store.get_subscription(sub_id)).name == "新模板"
    assert h.K_EDIT_SUB not in context.user_data
    assert any("已更新" in text for text in bot.edited)


async def test_edit_sources_add_remove(tmp_path):
    """编辑源频道：添加（游标=当前头部）、移除；不允许删空。"""
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
    assert len(store.get_subscription(sub_id)["sources"]) == 1  # 未删空
    assert bot.answers[-1] == ("⚠️ 至少要保留一个源频道。", True)


async def test_edit_dests_add_and_remove(tmp_path):
    """编辑目的地：加频道（权限复核）、移除；不允许删空。"""
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
    assert len(store.get_subscription(sub_id)["dests"]) == 1  # 未删空
    assert bot.answers[-1] == ("⚠️ 至少要保留一个目的地。", True)


async def test_edit_refuses_other_users_sub(tmp_path):
    """多用户隔离：改不了别人的订阅（猜编号无效）。"""
    store, bot, context, sub_id = _edit_env(tmp_path)
    update = _cb_update(f"etpl:{sub_id}", user_id=USER_ID + 1)
    _attach_bot(update, bot)
    assert await h.on_edit_template(update, context) == h.ConversationHandler.END
    assert any("未找到" in text for text in bot.edited)
    assert h.K_EDIT_SUB not in context.user_data
