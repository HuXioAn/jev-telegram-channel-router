"""全部用户可见文案（集中管理，便于修改/翻译）。"""
from __future__ import annotations

from ..formatting import match_summary
from ..models import format_answer_value

WELCOME = (
    "👋 我是「频道过滤器」机器人。\n\n"
    "用法：订阅任意公开频道 → 用 Jev 判定每条新消息 → 命中后推送到你的私聊或频道。\n\n"
    "· /new — 新建订阅（可多源 → 多目的地）\n"
    "· /list — 我的订阅（点选条目后暂停/试跑/编辑/删除）\n"
    "· /test <编号> — 试跑一次（样张发往订阅目标）\n"
    "· /help — 使用说明"
)

HELP = (
    "📖 使用说明\n\n"
    "一、新建订阅（/new）\n"
    "  1. 发送频道用户名或链接（仅公开频道，如 @Financial_Express）；\n"
    "     可以连续发送多个频道，加完后点「✅ 完成」\n"
    "  2. 用一句话描述你想筛选什么；也可以直接粘贴 JSON 模板（高级用法）\n"
    "  3. 确认模板 → 选目的地（私聊 / 频道，可多选）→ 完成创建\n\n"
    "二、修改订阅（/list → 点选条目 → ✏️ 编辑）\n"
    "  可随时增删源频道、增删目的地、重新描述筛选条件（模板）。\n\n"
    "三、推送到频道\n"
    "  先把机器人添加为你频道的管理员，再在向导的「目的地」里选择该频道。\n\n"
    "四、先试跑再正式跑\n"
    "  /test <编号> 会拉取频道最近消息做一次判定演示，并把样张（最多 6 条、带 🧪 标头）\n"
    "  发到订阅的目标，用来核对实际推送效果；不推进游标、不算正式推送。\n\n"
    "注意：\n"
    "· 只支持公开频道的网页预览；若频道关闭了预览则无法抓取；\n"
    "· 频道的刷新节奏由系统统一调度（同一频道只抓取、判定一次），无需逐条设置频率；\n"
    "· 只推送订阅之后出现的新消息，停机过久可能漏掉超出预览窗口的消息；\n"
    "· 本机器人不使用 userbot，不读取私有频道。"
)

ASK_SOURCE = (
    "请发送要订阅的频道（可添加多个）：\n"
    "· @频道名\n"
    "· 或链接 https://t.me/频道名\n\n"
    "加完后点「✅ 完成」进入下一步。（发送 /cancel 取消）"
)
BAD_SOURCE = "❌ 频道引用无法解析：{err}\n请重试，或发送 /cancel 取消。"
SOURCE_UNREACHABLE = "❌ 无法访问该频道：{err}\n请检查频道名后重试，或发送 /cancel 取消。"
SRC_MANAGER_HEAD = "📡 源频道（点 ❌ 移除；继续发送频道名/链接即可添加）"
SRC_MANAGER_NEXT = "加完后点「✅ 完成」进入下一步。"
SRC_MANAGER_BACK = "点「✅ 完成」返回。"
SOURCE_DUPLICATE = "该频道已在源列表中。"


def src_manager_text(sources: list[str], *, next_step: bool) -> str:
    body = "\n".join(f"· @{s}" for s in sources) or "（无）"
    hint = SRC_MANAGER_NEXT if next_step else SRC_MANAGER_BACK
    return f"{SRC_MANAGER_HEAD}\n\n{body}\n\n{hint}"


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
TEMPLATE_CONFIRM_EDIT = (
    "📋 请确认新的筛选模板：\n\n{summary}\n\n"
    "确认后将覆盖当前订阅的筛选条件；也可以重新描述，或让 AI 调整。"
)
ASK_ADJUST = "🔧 请告诉我怎么调整（一句话即可），例如：「重要度门槛提高到 2.0」。"
DEST_MANAGER_HEAD = (
    "📬 推送目的地（可多选）：\n"
    "· 「加私聊」直接可用；发到频道需先把机器人添加为该频道的管理员\n"
    "· 点 ❌ 移除已选目的地"
)
DEST_MANAGER_NEXT = "选好后点「✅ 完成」进入下一步。"
DEST_MANAGER_BACK = "改好后点「✅ 完成」返回。"
DEST_MANAGER_EMPTY = "（还没选目的地——至少要选一个）"
DEST_DUPLICATE = "该目的地已在列表中。"
NEED_ONE_DEST = "⚠️ 至少要保留一个目的地。"
NEED_ONE_SOURCE = "⚠️ 至少要保留一个源频道。"
DEST_CHANNEL_INVALID = "❌ 机器人不是「{title}」的管理员（可能已被移除），无法发送到该频道。"
SUB_CREATED = (
    "✅ 订阅 #{sub_id} 已创建！\n\n"
    "· 源频道：{sources}\n"
    "· 规则：{rule}\n"
    "· 目的地：{dests}\n\n"
    "从现在起只推送新消息。可以先 /test {sub_id} 试跑看看效果。"
)
NO_SUBS = "你还没有订阅。发送 /new 创建一个。"
LIST_HEAD = "📋 我的订阅（共 {n} 条）"
LIST_HINT = "点按钮选择要操作的条目："
TEST_NEED_ID = "你有多个订阅，请指定编号：/test <订阅编号>（用 /list 查看）"
TEST_SUB_NOT_FOUND = "未找到订阅 #{sub_id}（用 /list 查看你的订阅）。"
TESTING = "⏳ 正在试跑（拉取最近消息逐条判定，样张将发往目标）…"
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
PRIVATE_ONLY = "🔒 请在与我的私聊中使用 {cmd}（订阅数据只对你自己可见）。"
DEST_CHANNEL_NOT_YOURS = (
    "❌ 无法选择「{title}」：该频道不由你添加，或机器人已不在其中。"
    "若是你的频道，请先把机器人添加为管理员。")
DEST_USER_NOT_ADMIN = (
    "❌ 你现在不是「{title}」的管理员，无法把消息发到该频道。"
    "如果你已被移出管理员，请重新添加后再试。")
SUBS_LIMIT = "⚠️ 每人最多 {n} 个订阅，你已达到上限。请先删除不用的订阅（/list）。"
BLOCKED = "⛔ 你的使用权限已被暂停。如需恢复请联系管理员。"
EDIT_TITLE = "✏️ 编辑订阅 #{sub_id}"
EDIT_HINT = "选择要修改的部分："
EDIT_DONE = "✅ 已更新。"
EDIT_TEMPLATE_ASK = (
    "🧩 请重新描述筛选条件（确认后覆盖当前模板）：\n"
    "例如：「中国相关的重磅消息，排除娱乐八卦」。\n\n"
    "也可以直接粘贴 JSON 模板。发送 /cancel 取消。"
)
EDIT_SOURCE_ASK = (
    "请发送要添加的频道：\n"
    "· @频道名\n"
    "· 或链接 https://t.me/频道名\n\n"
    "新频道从当前最新消息开始推送。发送 /cancel 结束。"
)


def _join(items: list[str], cap: int = 3) -> str:
    if not items:
        return "（无）"
    shown = "、".join(items[:cap])
    return shown + (f" 等 {len(items)} 个" if len(items) > cap else "")


def sub_line(sub: dict, template) -> str:
    status = "✅ 运行中" if sub["enabled"] else "⏸ 已暂停"
    sources = _join([f"@{s['source']}" for s in sub["sources"]])
    dests = _join([d["title"] or str(d["chat_id"]) for d in sub["dests"]])
    return (f"#{sub['id']}｜{sources} → {dests}\n"
            f"{status}｜规则：{match_summary(template)}")


def sub_pick_line(index: int, sub: dict) -> str:
    """列表选择视图里的一行（无规则详情，避免过长）。"""
    state = "✅ 运行中" if sub["enabled"] else "⏸ 已暂停"
    sources = _join([f"@{s['source']}" for s in sub["sources"]], cap=2)
    dests = _join([d["title"] or str(d["chat_id"]) for d in sub["dests"]], cap=2)
    return f"{index}. #{sub['id']}｜{sources} → {dests}｜{state}"


def edit_menu_text(sub: dict, template) -> str:
    return f"{EDIT_TITLE.format(sub_id=sub['id'])}\n\n{sub_line(sub, template)}\n\n{EDIT_HINT}"


def dest_manager_text(dests: list[str], *, next_step: bool) -> str:
    body = "\n".join(f"· {d}" for d in dests) or DEST_MANAGER_EMPTY
    hint = DEST_MANAGER_NEXT if next_step else DEST_MANAGER_BACK
    return f"{DEST_MANAGER_HEAD}\n\n{body}\n\n{hint}"


def test_result(res, sub: dict, template) -> str:
    lines = [
        f"🧪 试跑结果（订阅 #{res.sub_id}）",
        f"· 源频道：{_join(['@' + s['source'] for s in sub['sources']])}",
        f"· 抽样：{res.fetched} 条｜命中：{res.matched} 条｜判定失败：{res.failed} 条",
    ]
    if res.sent:
        dests = _join([d["title"] or str(d["chat_id"]) for d in sub["dests"]])
        lines.append(f"· 试跑样张（{len(res.sample)} 条）已发到目标：{dests}")
    if res.error:
        lines.append(f"⚠️ {res.error}")
    if res.sample:
        lines.append("")
        lines.append("命中示例（最新在前）：")
        for source, post, answers in res.sample:
            timestamp = post.date.strftime("%m-%d %H:%M") if post.date else "?"
            values = "｜".join(
                f"{question.title or qid} {format_answer_value(question, answers.get(qid))}"
                for qid, question in template.questions.items())
            snippet = post.text.replace("\n", " ")[:80]
            lines.append(f"· [@{source}｜{timestamp}]（{values}）{snippet}…")
    elif not res.error:
        lines.append("")
        lines.append("（最近样本中没有命中——可以放宽阈值、调整描述，或换个频道试试）")
    return "\n".join(lines)
