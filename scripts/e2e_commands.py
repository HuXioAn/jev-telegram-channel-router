#!/usr/bin/env python3
"""Full-feature end-to-end coverage: drive a real Application with synthetic Updates and exercise every entry point.

Coverage: /help /list /cancel /test (including a bogus id) · every wizard branch (Power Mode JSON,
re-describe, adjust, argument validation, URL variants) · subscription buttons (pause/resume/dry run/delete) ·
the three channel-destination branches (real validation failure / non-admin / admin) · channel registration and removal
(synthetic my_chat_member) · UI callbacks (menu buttons) · error fallback.

Real chain: real Bot API (messages really reach the user's DM) / real DeepSeek compilation / real Jev dry run;
only the admin branch of get_chat_member is injected as a mock (real channels are exercised after the user adds the bot).
Usage: python scripts/e2e_commands.py <user_id>
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from telegram import (CallbackQuery, Chat, ChatMemberAdministrator,  # noqa: E402
                      ChatMemberLeft, ChatMemberMember, ChatMemberOwner,
                      ChatMemberUpdated, Message, MessageEntity, Update, User)
from telegram.ext import ExtBot  # noqa: E402

from tgfilter.bot.app import build_application  # noqa: E402
from tgfilter.config import Settings  # noqa: E402
from tgfilter.store import Store  # noqa: E402

FAKE_CHAT_ID = -1009876543210
_state = {"update_id": 5000, "outbox": [], "printed": 0, "fake_member": None}
_checks: list[tuple[str, bool]] = []


def _check(label: str, ok: bool) -> None:
    _checks.append((label, bool(ok)))
    print(f"   {'✓' if ok else '✗'} {label}")


def _mark() -> int:
    return len(_state["outbox"])


def _new_texts(mark: int) -> list[str]:
    return [m.text or "" for _, m in _state["outbox"][mark:]]


def _next_id() -> int:
    _state["update_id"] += 1
    return _state["update_id"]


def _user(user_id: int) -> User:
    return User(id=user_id, first_name="Alice", is_bot=False)


def _message(user_id: int, text: str, entities=None) -> Message:
    return Message(message_id=_next_id(), date=datetime.now(timezone.utc),
                   chat=Chat(id=user_id, type="private"), from_user=_user(user_id),
                   text=text, entities=entities or [])


def _command(user_id: int, text: str) -> Message:
    head = text.split()[0]
    return _message(user_id, text, [MessageEntity(type=MessageEntity.BOT_COMMAND,
                                                  offset=0, length=len(head))])


def _callback(user_id: int, message: Message, data: str) -> Update:
    return Update(update_id=_next_id(), callback_query=CallbackQuery(
        id=str(_next_id()), from_user=_user(user_id), chat_instance="e2e-debug",
        message=message, data=data))


def _member_update(bot, user_id: int, new_member) -> Update:
    return Update(update_id=_next_id(), my_chat_member=ChatMemberUpdated(
        chat=Chat(id=FAKE_CHAT_ID, type="channel", title="测试频道"),
        from_user=_user(user_id), date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(
            user=User(id=bot.id, first_name="bot", is_bot=True)),
        new_chat_member=new_member))


def _attach(update: Update, bot) -> Update:
    update.set_bot(bot)
    for obj in (update.message, update.callback_query, update.my_chat_member):
        if obj is not None:
            obj.set_bot(bot)
    return update


async def _step(app, label: str, update: Update) -> None:
    print(f"\n[步骤] {label}")
    await app.process_update(_attach(update, app.bot))
    for kind, message in _state["outbox"][_state["printed"]:]:
        text = (message.text or "")[:80].replace("\n", " ⏎ ")
        print(f"   → {kind} #{message.message_id}: {text!r}")
    _state["printed"] = len(_state["outbox"])


def _find_last(pred) -> Message | None:
    for _, message in reversed(_state["outbox"]):
        if message.text and pred(message.text):
            return message
    return None


def _must(message: Message | None, label: str) -> Message:
    if message is None:
        raise SystemExit(f"❌ 无法定位消息：{label}")
    return message


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：python scripts/e2e_commands.py <user_id>")
    user_id = int(sys.argv[1])
    settings = Settings.load()
    store = Store(settings.db_path)
    app = build_application(settings)

    # PTB objects' __slots__ forbid instance-level monkeypatching → patch the class to record every outbound message
    original_send, original_edit = ExtBot.send_message, ExtBot.edit_message_text
    original_member = ExtBot.get_chat_member

    async def send_message(self, *args, **kwargs):
        message = await original_send(self, *args, **kwargs)
        if isinstance(message, Message):
            _state["outbox"].append(("发送", message))
        return message

    async def edit_message_text(self, *args, **kwargs):
        result = await original_edit(self, *args, **kwargs)
        if isinstance(result, Message):
            _state["outbox"].append(("编辑", result))
        return result

    async def get_chat_member(self, chat_id, user_id_, *args, **kwargs):
        if chat_id == FAKE_CHAT_ID and _state["fake_member"] is not None:
            return _state["fake_member"]
        return await original_member(self, chat_id, user_id_, *args, **kwargs)

    ExtBot.send_message, ExtBot.edit_message_text = send_message, edit_message_text
    ExtBot.get_chat_member = get_chat_member
    original_answer = CallbackQuery.answer
    CallbackQuery.answer = lambda self, *a, **k: asyncio.sleep(0, result=True)

    await app.initialize()
    await app.post_init(app)

    U = lambda text: Update(update_id=_next_id(), message=_message(user_id, text))  # noqa: E731
    CMD = lambda text: Update(update_id=_next_id(), message=_command(user_id, text))  # noqa: E731

    try:
        # ---------- A. basic commands ----------
        await _step(app, "/help", CMD("/help"))
        await _step(app, "/list（当前仅订阅 #1）", CMD("/list"))
        await _step(app, "/cancel（无进行中向导）", CMD("/cancel"))
        await _step(app, "/test 1（真实试跑：抓取 + Jev）", CMD("/test 1"))
        await _step(app, "/test 999（不存在的编号）", CMD("/test 999"))

        # ---------- E1. channel registration (synthetic: added as administrator) ----------
        bot_user = User(id=app.bot.id, first_name="bot", is_bot=True)
        mark = _mark()
        await _step(app, "频道加入事件（bot 被加为频道管理员）",
                    _member_update(app.bot, user_id, ChatMemberAdministrator(
                        user=bot_user, can_be_edited=False, is_anonymous=False,
                        can_manage_chat=True, can_delete_messages=True,
                        can_manage_video_chats=True, can_restrict_members=True,
                        can_promote_members=False, can_change_info=True,
                        can_invite_users=True, can_post_stories=False,
                        can_edit_stories=False, can_delete_stories=False,
                        can_post_messages=True)))
        chats = store.list_chats(added_by=user_id)
        _check("频道已登记进数据库", any(c["chat_id"] == FAKE_CHAT_ID for c in chats))
        _check("用户收到「已登记」通知", any("测试频道" in t for t in _new_texts(mark)))

        # ---------- C1. wizard: Power Mode + channel path ----------
        await _step(app, "/new", CMD("/new"))
        await _step(app, "发送频道 URL 变体", U("https://t.me/Financial_Express"))
        manager = _must(_find_last(lambda t: t.startswith("📡")), "源频道管理器")
        await _step(app, "点击「✅ 完成」（源频道）",
                    _callback(user_id, manager, "ms:done:new"))
        await _step(app, "发送非法 JSON（应报错并留在原地）", U("{坏掉的 json"))
        backup = ROOT / "data/sub1_template_backup.json"
        if backup.exists():
            template_json = backup.read_text()
        else:
            template_json = json.dumps({
                "name": "中国财经",
                "questions": {"china": {
                    "type": "noul", "title": "中国相关",
                    "instructions": "这条消息是否与中国（含港澳台）直接相关？"}},
                "match": {"logic": "all", "conditions": [
                    {"question": "china", "op": ">=", "value": 0.7}]}})
        await _step(app, "粘贴合法模板 JSON（Power Mode）",
                    U(json.dumps(json.loads(template_json), ensure_ascii=False)))
        confirm = _must(_find_last(lambda t: t.startswith("📋")), "模板确认消息")
        await _step(app, "点击「使用这个模板」", _callback(user_id, confirm, "tpl:confirm"))
        mark = _mark()
        await _step(app, "点击「刷新列表」（内容未变，应静默）",
                    _callback(user_id, confirm, "md:refresh:new"))
        _check("刷新列表已静默处理（无报错）",
               not any("出错了" in t for t in _new_texts(mark)))
        mark = _mark()
        await _step(app, "选择「测试频道」——真实校验（bot 实际不在该频道）",
                    _callback(user_id, confirm, f"md:ch:new:{FAKE_CHAT_ID}"))
        _check("真实校验失败被正确拦下",
               any("管理员" in t and "无法发送" in t for t in _new_texts(mark)))
        _state["fake_member"] = ChatMemberMember(user=bot_user)
        mark = _mark()
        await _step(app, "选择「测试频道」——模拟普通成员（应拦下且不崩溃）",
                    _callback(user_id, confirm, f"md:ch:new:{FAKE_CHAT_ID}"))
        _check("非管理员被拦下（无报错、未进入下一步）",
               not any("出错了" in t for t in _new_texts(mark))
               and not any("已创建" in t for t in _new_texts(mark)))
        _state["fake_member"] = ChatMemberOwner(user=bot_user, is_anonymous=False)
        await _step(app, "选择「测试频道」——模拟管理员（应通过）",
                    _callback(user_id, confirm, f"md:ch:new:{FAKE_CHAT_ID}"))
        _state["fake_member"] = None
        await _step(app, "点击「✅ 完成」（目的地）→ 直接创建",
                    _callback(user_id, confirm, "md:done:new"))
        sid = next((int(m.text.split("#", 2)[1].split(" ", 1)[0])
                    for _, m in reversed(_state["outbox"])
                    if m.text and m.text.startswith("✅ 订阅 #")), None)
        sub2 = store.get_subscription(sid) if sid else None
        _check("订阅已创建（dest=测试频道）",
               sub2 is not None and sub2["enabled"] == 1
               and [(d["kind"], d["chat_id"]) for d in sub2["dests"]]
               == [("channel", FAKE_CHAT_ID)])

        await _step(app, f"/list（应显示 #1 和 #{sid}）", CMD("/list"))
        list2 = _must(_find_last(lambda t: f"#{sid}｜" in t), "新订阅的列表消息")
        await _step(app, f"点击选择菜单里的 #{sid}", _callback(user_id, list2, f"sub:open:{sid}"))
        await _step(app, "点击「⬅️ 返回列表」", _callback(user_id, list2, "sub:list"))
        await _step(app, f"点击 #{sid}「暂停」", _callback(user_id, list2, f"sub:pause:{sid}"))
        _check("暂停生效（enabled=0）", store.get_subscription(sid)["enabled"] == 0)
        await _step(app, f"点击 #{sid}「恢复」", _callback(user_id, list2, f"sub:resume:{sid}"))
        _check("恢复生效（enabled=1）", store.get_subscription(sid)["enabled"] == 1)
        await _step(app, f"点击 #{sid}「试跑」（真实 Jev）",
                    _callback(user_id, list2, f"sub:test:{sid}"))
        await _step(app, f"点击 #{sid}「删除」", _callback(user_id, list2, f"sub:delete:{sid}"))
        _check("删除生效", store.get_subscription(sid) is None)

        # ---------- C2. wizard: natural language + re-describe + adjust + DM branch ----------
        await _step(app, "/new（自然语言分支）", CMD("/new"))
        await _step(app, "发送频道名", U("Financial_Express"))
        manager2 = _must(_find_last(lambda t: t.startswith("📡")), "源频道管理器 2")
        await _step(app, "点击「✅ 完成」（源频道）",
                    _callback(user_id, manager2, "ms:done:new"))
        await _step(app, "发送自然语言描述（LLM 编译）", U("只看中国相关的重大财经新闻"))
        confirm2 = _must(_find_last(lambda t: t.startswith("📋")), "模板确认消息 2")
        await _step(app, "点击「重新描述」", _callback(user_id, confirm2, "tpl:redo"))
        await _step(app, "重新发送描述（LLM 再编译）", U("中国相关、重要的财经新闻就行"))
        confirm3 = _must(_find_last(lambda t: t.startswith("📋")), "模板确认消息 3")
        await _step(app, "点击「微调」", _callback(user_id, confirm3, "tpl:adjust"))
        await _step(app, "发送微调意见（LLM 再编译）", U("把重要度阈值提高到 2.5"))
        confirm4 = _must(_find_last(lambda t: t.startswith("📋")), "模板确认消息 4")
        await _step(app, "点击「使用这个模板」", _callback(user_id, confirm4, "tpl:confirm"))
        await _step(app, "点击「📬 加私聊」（DM 分支）",
                    _callback(user_id, confirm4, "md:dm:new"))
        await _step(app, "/cancel（创建前取消）", CMD("/cancel"))
        _check("取消后未产生新订阅",
               len(store.list_subscriptions(user_id=user_id)) == 1)

        # ---------- C3. wizard: invalid channel name ----------
        await _step(app, "/new（非法源校验）", CMD("/new"))
        await _step(app, "发送非法频道名", U("!!! 这根本不是频道 !!!"))
        await _step(app, "/cancel", CMD("/cancel"))

        # ---------- D. UI callbacks + error fallback ----------
        last = _state["outbox"][-1][1]
        await _step(app, "菜单按钮「我的订阅」(ui:list)", _callback(user_id, last, "ui:list"))
        await _step(app, "菜单按钮「帮助」(ui:help)", _callback(user_id, last, "ui:help"))
        await _step(app, "菜单按钮「新建订阅」(ui:new)", _callback(user_id, last, "ui:new"))
        await _step(app, "/cancel", CMD("/cancel"))
        await _step(app, "畸形回调 sub:boom:x（错误兜底应回复 ⚠️）",
                    _callback(user_id, last, "sub:boom:x"))

        # ---------- E2. channel removal ----------
        await _step(app, "频道移除事件（bot 被移出）",
                    _member_update(app.bot, user_id,
                                   ChatMemberLeft(
                                       user=User(id=app.bot.id, first_name="bot", is_bot=True))))
        _check("频道已从数据库移除",
               not any(c["chat_id"] == FAKE_CHAT_ID for c in store.list_chats()))

        # ---------- summary ----------
        passed = sum(1 for _, ok in _checks if ok)
        print(f"\n{'=' * 50}\n检查项：{passed}/{len(_checks)} 通过")
        for label, ok in _checks:
            if not ok:
                print(f"  ✗ {label}")
        print("\\n[数据库终态]")
        print("  订阅:", [(s["id"], [x["source"] for x in s["sources"]],
                        [(d["kind"], d["chat_id"]) for d in s["dests"]],
                        s["enabled"])
                       for s in store.list_subscriptions(user_id=user_id)])
        print("  频道:", store.list_chats())
        print("  调度:", [(w["channel"], w["interval_minutes"], w["last_seen_id"])
                        for w in store.list_watches()])
    finally:
        ExtBot.send_message, ExtBot.edit_message_text = original_send, original_edit
        ExtBot.get_chat_member = original_member
        CallbackQuery.answer = original_answer
        _state["fake_member"] = None
        await app.shutdown()
        await app.post_shutdown(app)
    print("\n✅ 全功能合成覆盖测试完成")


if __name__ == "__main__":
    asyncio.run(main())
