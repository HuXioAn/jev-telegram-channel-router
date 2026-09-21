"""全部用户可见文案（集中管理，便于修改/翻译）。"""
from __future__ import annotations

from ..formatting import match_summary
from ..models import format_answer_value

WELCOME = (
    "👋 我是「频道过滤器」机器人。\n\n"
    "用法：订阅任意公开频道 → 用 Jev 判定每条新消息 → 命中后推送到你的私聊或频道。\n\n"
    "· /new — 新建订阅\n"
    "· /list — 管理订阅\n"
    "· /test <编号> — 试跑一次（只给你看，不发消息）\n"
    "· /help — 使用说明"
)

HELP = (
    "📖 使用说明\n\n"
    "一、新建订阅（/new）\n"
    "  1. 发送频道用户名或链接（仅公开频道，如 @Financial_Express）\n"
    "  2. 用一句话描述你想筛选什么；也可以直接粘贴 JSON 模板（高级用法）\n"
    "  3. 确认模板 → 选目的地（私聊 / 频道）→ 选检查频率\n\n"
    "二、推送到频道\n"
    "  先把机器人添加为你频道的管理员，再在向导的「目的地」里选择该频道。\n\n"
    "三、先试跑再正式跑\n"
    "  /test <编号> 会拉取频道最近消息做一次判定演示，不会发送任何消息。\n\n"
    "注意：\n"
    "· 只支持公开频道的网页预览；若频道关闭了预览则无法抓取；\n"
    "· 只推送订阅之后出现的新消息，停机过久可能漏掉超出预览窗口的消息；\n"
    "· 本机器人不使用 userbot，不读取私有频道。"
)

ASK_SOURCE = (
    "请发送要订阅的频道：\n"
    "· @频道名\n"
    "· 或链接 https://t.me/频道名\n\n"
    "（仅支持公开频道；发送 /cancel 取消）"
)
BAD_SOURCE = "❌ 频道引用无法解析：{err}\n请重试，或发送 /cancel 取消。"
SOURCE_UNREACHABLE = "❌ 无法访问该频道：{err}\n请检查频道名后重试，或发送 /cancel 取消。"
SOURCE_OK = (
    "✅ 频道已确认：{title}（@{channel}）\n"
    "最新消息 id：{head}｜示例：{sample}\n\n"
    "下一步——"
)
ASK_DESCRIBE = (
    "🗣 用一句话描述你想筛选出什么内容（自然语言即可）。\n"
    "例如：「中国相关的重磅消息，排除娱乐八卦」。\n\n"
    "高级用法：直接粘贴 JSON 模板。\n发送 /cancel 取消。"
)
COMPILING = "⏳ 正在编译筛选模板…"
COMPILE_FAILED = "❌ 模板编译失败：{err}\n请换个说法重试，或发送 /cancel。"
LLM_NOT_CONFIGURED = (
    "⚠️ 未配置 LLM（OPENAI_API_KEY），无法用自然语言编译模板。\n"
    "你可以直接粘贴 JSON 模板（高级用法），或联系管理员配置后再试。"
)
BAD_TEMPLATE_JSON = "❌ 模板 JSON 校验失败：{err}\n请修正后重试，或 /cancel。"
TEMPLATE_CONFIRM = (
    "📋 请确认筛选模板：\n\n{summary}\n\n"
    "确认后继续选择推送目的地；也可以重新描述，或让 AI 调整。"
)
ASK_ADJUST = "🔧 请告诉我怎么调整（一句话即可），例如：「重要度门槛提高到 2.0」。"
ASK_DEST = (
    "📬 选择推送目的地：\n"
    "· 「发到我的私聊」直接可用\n"
    "· 发到频道：先把机器人添加为该频道的管理员（频道 → 管理 → 管理员 → 添加本机器人），"
    "加好后点「🔄 刷新列表」。"
)
DEST_CHANNEL_INVALID = "❌ 机器人不是「{title}」的管理员（可能已被移除），无法发送到该频道。"
ASK_INTERVAL = "⏱ 选择检查频率（多久扫一次频道找新消息）："
SUB_CREATED = (
    "✅ 订阅 #{sub_id} 已创建！\n\n"
    "· 源频道：@{source}\n"
    "· 规则：{rule}\n"
    "· 目的地：{dest}\n"
    "· 频率：每 {interval} 分钟\n\n"
    "从现在起只推送新消息。可以先 /test {sub_id} 试跑看看效果。"
)
NO_SUBS = "你还没有订阅。发送 /new 创建一个。"
TEST_NEED_ID = "你有多个订阅，请指定编号：/test <订阅编号>（用 /list 查看）"
TEST_SUB_NOT_FOUND = "未找到订阅 #{sub_id}（用 /list 查看你的订阅）。"
TESTING = "⏳ 正在试跑（拉取最近消息并逐条判定）…"
CHAT_ADDED = "✅ 已登记频道「{title}」。现在可以在 /new 的目的地列表中选择它。"
CANCELED = "已取消。"
ERROR = "⚠️ 出错了：{err}"
UNCONFIGURED = "⚠️ 服务未配置 TYPESAFE_API_KEY，暂时无法工作。请联系管理员。"
FALLBACK = (
    "🤖 我还没学会处理这类消息。\n\n"
    "· /new — 新建订阅\n"
    "· /list — 管理订阅\n"
    "· /help — 使用说明"
)


def sub_line(sub: dict, template) -> str:
    status = "✅ 运行中" if sub["enabled"] else "⏸ 已暂停"
    dest = sub["dest_title"] or str(sub["dest_chat_id"])
    return (f"#{sub['id']}｜@{sub['source']} → {dest}\n"
            f"{status}｜每 {sub['interval_minutes']} 分钟｜规则：{match_summary(template)}")


def test_result(res, sub: dict, template) -> str:
    lines = [
        f"🧪 试跑结果（订阅 #{res.sub_id}）",
        f"· 抽样：{res.fetched} 条｜命中：{res.matched} 条｜判定失败：{res.failed} 条",
    ]
    if res.error:
        lines.append(f"⚠️ {res.error}")
    if res.sample:
        lines.append("")
        lines.append("命中示例（最新在前）：")
        for post, answers in res.sample:
            timestamp = post.date.strftime("%m-%d %H:%M") if post.date else "?"
            values = "｜".join(
                f"{question.title or qid} {format_answer_value(question, answers.get(qid))}"
                for qid, question in template.questions.items())
            snippet = post.text.replace("\n", " ")[:80]
            lines.append(f"· [{timestamp}]（{values}）{snippet}…")
    elif not res.error:
        lines.append("")
        lines.append("（最近样本中没有命中——可以放宽阈值、调整描述，或换个频道试试）")
    return "\n".join(lines)
