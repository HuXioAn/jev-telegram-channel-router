"""i18n: locale resolution, table integrity, rendering and the /lang flow."""
from __future__ import annotations

import string
from datetime import datetime, timezone
from types import SimpleNamespace

from telegram import CallbackQuery, Chat, Message, Update, User

from tgfilter import i18n
from tgfilter.bot import handlers as h
from tgfilter.bot import messages as msg
from tgfilter.config import Settings
from tgfilter.store import Store

USER_ID = 7


def test_resolve_maps_locales():
    assert i18n.resolve("zh") == "zh"
    assert i18n.resolve("zh-hans") == "zh"
    assert i18n.resolve("ZH-CN") == "zh"
    assert i18n.resolve("en") == "en"
    assert i18n.resolve("fr") == "en"
    assert i18n.resolve(None) == "en"


def test_tables_share_the_same_keys():
    assert set(i18n._EN) == set(i18n._ZH)
    assert all(i18n._EN[key] and i18n._ZH[key] for key in i18n._EN)


def test_tables_use_the_same_format_placeholders():
    formatter = string.Formatter()
    for key, en in i18n._EN.items():
        zh = i18n._ZH[key]
        en_fields = {name for _, name, _, _ in formatter.parse(en) if name}
        zh_fields = {name for _, name, _, _ in formatter.parse(zh) if name}
        assert en_fields == zh_fields, f"placeholder mismatch in {key!r}"


def test_commands_menu_complete_and_bilingual():
    expected = {"start", "new", "list", "test", "lang", "help", "cancel"}
    assert {cmd for cmd, _ in i18n.COMMANDS["en"]} == expected
    assert {cmd for cmd, _ in i18n.COMMANDS["zh"]} == expected


def test_t_falls_back_to_english():
    assert i18n.t("zh", "canceled") == "已取消。"
    assert i18n.t("xx", "canceled") == "Canceled."
    assert "5" in i18n.t("en", "subs_limit", n=5)


def test_store_language_precedence(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.add_user(USER_ID)
    # nothing set → configured default
    assert store.language_for(USER_ID, "en") == "en"
    # instance default set by the admin wins over the configured default
    store.set_setting("default_lang", "zh")
    assert store.language_for(USER_ID, "en") == "zh"
    assert store.language_for(999, "en") == "zh"   # unknown user → instance default
    # personal choice wins, and locale codes are normalized
    store.set_user_lang(USER_ID, "en")
    assert store.language_for(USER_ID, "zh") == "en"
    store.set_user_lang(USER_ID, "zh-CN")
    assert store.language_for(USER_ID, "en") == "zh"


def test_messages_render_in_both_languages():
    assert "channel-filter" in msg.welcome("en")
    assert "频道过滤器" in msg.welcome("zh")
    en = msg.sub_created("en", 3, "src", "rule", "dest")
    zh = msg.sub_created("zh", 3, "src", "rule", "dest")
    assert "Subscription #3 created" in en
    assert "订阅 #3 已创建" in zh
    assert "I don't understand" in msg.fallback("en")
    assert "还没学会" in msg.fallback("zh")


class _Bot:
    id = 1

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_markups: list = []
        self.edited: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self.sent_markups.append(kwargs.get("reply_markup"))

    async def edit_message_text(self, *args, **kwargs):
        self.edited.append(kwargs.get("text", args[0] if args else ""))

    async def answer_callback_query(self, *args, **kwargs):
        pass


def _ctx(store, settings=None):
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={
            "services": SimpleNamespace(store=store,
                                        settings=settings or Settings())}),
        bot=_Bot(), user_data={})


def _text_update(text: str = "/lang") -> Update:
    return Update(update_id=1, message=Message(
        message_id=1, date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, first_name="x", is_bot=False), text=text))


def _kb_datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_lang_command_offers_both_languages(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    context = _ctx(store)
    update = _text_update()
    update.message.set_bot(context.bot)
    await h.cmd_lang(update, context)
    assert context.bot.sent
    assert _kb_datas(context.bot.sent_markups[-1]) == ["lang:set:en", "lang:set:zh"]


async def test_lang_callback_persists_and_confirms_in_new_language(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.add_user(USER_ID)
    context = _ctx(store)

    async def click(data: str) -> None:
        inner = Message(message_id=2, date=datetime.now(timezone.utc),
                        chat=Chat(id=USER_ID, type="private"),
                        from_user=User(id=1, first_name="bot", is_bot=True),
                        text="x")
        query = CallbackQuery(id="1", from_user=User(id=USER_ID, first_name="x",
                                                     is_bot=False),
                              chat_instance="ci", data=data, message=inner)
        update = Update(update_id=2, callback_query=query)
        query.set_bot(context.bot)
        inner.set_bot(context.bot)
        await h.on_lang(update, context)

    await click("lang:set:en")
    assert store.language_for(USER_ID, "zh") == "en"
    assert "Language switched" in context.bot.edited[-1]
    # switching back to Chinese confirms in Chinese
    await click("lang:set:zh")
    assert store.language_for(USER_ID, "en") == "zh"
    assert "语言已切换" in context.bot.edited[-1]


async def test_lang_command_refused_in_groups(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    context = _ctx(store)
    update = Update(update_id=3, message=Message(
        message_id=3, date=datetime.now(timezone.utc),
        chat=Chat(id=-100123, type="supergroup"),
        from_user=User(id=USER_ID, first_name="x", is_bot=False), text="/lang"))
    update.message.set_bot(context.bot)
    await h.cmd_lang(update, context)
    assert "/lang" in context.bot.sent[-1] and "private chat" in context.bot.sent[-1]
