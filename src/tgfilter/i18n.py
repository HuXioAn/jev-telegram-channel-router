"""Bilingual user-interface text (English / Chinese) and lookup helpers.

All user-facing copy lives here so the rest of the codebase stays free of
embedded UI strings. Each user picks a language with /lang; the instance
default is set by the admin (/admin lang <en|zh>). Unknown locales fall back
to English.
"""
from __future__ import annotations

LANGS: tuple[str, ...] = ("en", "zh")
LANG_LABELS = {"en": "🇬🇧 English", "zh": "🇨🇳 中文"}

# Command menu entries (Telegram "/" list), per language: (command, description).
COMMANDS: dict[str, list[tuple[str, str]]] = {
    "en": [
        ("new", "Create a subscription"),
        ("list", "My subscriptions (open, pause, test, edit, delete)"),
        ("test", "Dry-run once (sample sent to targets)"),
        ("lang", "Switch language (English / 中文)"),
        ("help", "How to use"),
        ("cancel", "Cancel current operation"),
        ("start", "Get started"),
    ],
    "zh": [
        ("new", "新建订阅"),
        ("list", "我的订阅（选条目后暂停/试跑/编辑/删除）"),
        ("test", "试跑一次（样张发往订阅目标）"),
        ("lang", "切换语言（中文 / English）"),
        ("help", "使用说明"),
        ("cancel", "取消当前操作"),
        ("start", "开始使用"),
    ],
}

ADMIN_COMMAND_DESC = {"en": "Admin panel", "zh": "管理员面板"}

_EN: dict[str, str] = {
    # ------------------------------------------------------------- general
    "welcome": (
        "👋 I'm the channel-filter bot.\n\n"
        "How it works: subscribe to any public channel → every new post is "
        "judged by Jev → matches are delivered to your DM or channels.\n\n"
        "· /new — create a subscription (multiple sources → multiple targets)\n"
        "· /list — my subscriptions (open an item to pause / test / edit / delete)\n"
        "· /test <id> — dry-run once (sample is sent to the subscription targets)\n"
        "· /lang — switch language (English / 中文)\n"
        "· /help — how to use"
    ),
    "help": (
        "📖 How to use\n\n"
        "1. Create a subscription (/new)\n"
        "  1. Send a channel username or link (public channels only, e.g. @Financial_Express);\n"
        "     keep sending to add more, then tap “✅ Done”\n"
        "  2. Describe in one sentence what you want to filter; or paste a JSON template (advanced)\n"
        "  3. Confirm the template → pick targets (DM / channels, multi-select) → created\n\n"
        "2. Edit a subscription (/list → tap an item → ✏️ Edit)\n"
        "  Change sources, targets, or re-describe the filter at any time.\n\n"
        "3. Deliver to a channel\n"
        "  Add the bot as an admin of your channel first, then pick that channel as a target.\n\n"
        "4. Dry-run before going live\n"
        "  /test <id> judges the latest posts and sends a sample (up to 6 hits, 🧪 header)\n"
        "  to the subscription targets so you can check the actual output. It does not\n"
        "  advance cursors and is not a real delivery.\n\n"
        "Notes:\n"
        "· Only public channels with web preview are supported; channels that disabled\n"
        "  preview cannot be fetched;\n"
        "· All channels refresh on one global schedule (admin-configured) and each is\n"
        "  fetched and judged once per round, so there is no per-subscription interval;\n"
        "· Only posts newer than the subscription are delivered; long downtime may miss\n"
        "  posts beyond the preview window;\n"
        "· This bot never uses a userbot and never reads private channels."
    ),
    "fallback": (
        "🤖 I don't understand that yet.\n\n"
        "· /new — create a subscription\n"
        "· /list — manage subscriptions\n"
        "· /help — how to use"
    ),
    "unknown_cmd": "Unknown command. Send /help for usage.",
    "canceled": "Canceled.",
    "error": "⚠️ Something went wrong: {err}",
    "unconfigured": (
        "⚠️ This instance is not configured (missing TYPESAFE_API_KEY). "
        "Please contact the admin."
    ),
    "blocked": "⛔ Your access has been suspended. Contact the admin to restore it.",
    "private_only": "🔒 Please use {cmd} in a private chat with me (your data is only visible to you).",
    "new_private_only": "Please create subscriptions in a private chat: send /new there.",
    "toast_refreshed": "Refreshed",
    "lang_prompt": "🌐 Choose your language / 选择语言 (current: {current})",
    "lang_set": "✅ Language switched to English.",
    # ------------------------------------------------------------ /new
    "ask_source": (
        "Send the channel(s) to subscribe to (add as many as you like):\n"
        "· @channel_name\n"
        "· or link https://t.me/channel_name\n\n"
        "Tap “✅ Done” when finished to continue. (Send /cancel to abort.)"
    ),
    "bad_source": "❌ Cannot parse the channel reference: {err}\nTry again, or send /cancel to abort.",
    "source_unreachable": "❌ Cannot reach this channel: {err}\nCheck the name and retry, or send /cancel to abort.",
    "src_manager_head": "📡 Source channels (tap ❌ to remove; keep sending names/links to add)",
    "src_manager_next": "Tap “✅ Done” when finished to continue.",
    "src_manager_back": "Tap “✅ Done” to go back.",
    "source_duplicate": "This channel is already in the source list.",
    "none_label": "(none)",
    "more_items": " +{n} more",
    "list_sep": ", ",
    "ask_describe": (
        "🗣 Describe in one sentence what you want to filter (natural language is fine).\n"
        "Example: “Major China-related news, no celebrity gossip”.\n\n"
        "Advanced: paste a JSON template directly.\nSend /cancel to abort."
    ),
    "compiling": "⏳ Compiling the filter template…",
    "compile_failed": "❌ Template compilation failed: {err}\nTry rephrasing, or send /cancel.",
    "llm_not_configured": (
        "⚠️ No LLM configured (OPENAI_API_KEY), so natural-language compilation is "
        "unavailable. You can paste a JSON template (advanced), or ask the admin to "
        "configure it."
    ),
    "bad_template_json": "❌ Template JSON validation failed: {err}\nFix it and retry, or /cancel.",
    "template_confirm": (
        "📋 Please confirm the filter template:\n\n{summary}\n\n"
        "Confirm to continue to target selection; you can also re-describe it or let the AI adjust it."
    ),
    "template_confirm_edit": (
        "📋 Please confirm the new filter template:\n\n{summary}\n\n"
        "Confirming will overwrite the current subscription filter; you can also re-describe it or let the AI adjust it."
    ),
    "ask_adjust": "🔧 Tell me how to adjust it (one sentence), e.g. “raise the importance threshold to 2.0”.",
    "dest_manager_head": (
        "📬 Delivery targets (multi-select):\n"
        "· “Add DM” works right away; sending to a channel requires adding the bot as its admin first\n"
        "· Tap ❌ to remove a selected target"
    ),
    "dest_manager_next": "Tap “✅ Done” to continue once selected.",
    "dest_manager_back": "Tap “✅ Done” to go back once adjusted.",
    "dest_manager_empty": "(no target yet — pick at least one)",
    "dest_duplicate": "This target is already in the list.",
    "need_one_dest": "⚠️ Keep at least one target.",
    "need_one_source": "⚠️ Keep at least one source channel.",
    "dest_channel_invalid": "❌ The bot is not an admin of “{title}” (it may have been removed), so it cannot post there.",
    "dest_channel_not_yours": (
        "❌ Cannot select “{title}”: the channel was not added by you, or the bot is no "
        "longer in it. If it is your channel, add the bot as an admin first."
    ),
    "dest_user_not_admin": (
        "❌ You are not an admin of “{title}”, so messages cannot be delivered there. "
        "If you were removed, re-add yourself and retry."
    ),
    "dest_dm_title": "DM",
    "sub_created": (
        "✅ Subscription #{sub_id} created!\n\n"
        "· Sources: {sources}\n"
        "· Rule: {rule}\n"
        "· Targets: {dests}\n\n"
        "Only new posts will be delivered from now on. Try /test {sub_id} for a dry-run."
    ),
    "subs_limit": "⚠️ Limit of {n} subscriptions per user reached. Delete unused ones first (/list).",
    # ------------------------------------------------------------ /list
    "no_subs": "You have no subscriptions yet. Send /new to create one.",
    "list_head": "📋 My subscriptions ({n} total)",
    "list_hint": "Tap a button to open an item:",
    "status_running": "✅ Active",
    "status_paused": "⏸ Paused",
    "rule_label": "Rule",
    "sub_line_format": "#{id} | {sources} → {dests}\n{status} | {rule_label}: {rule}",
    "pick_line_format": "{index}. #{id} | {sources} → {dests} | {state}",
    "pick_more": " +{n} more",
    "sub_deleted": "🗑 Subscription #{sub_id} deleted.",
    # ------------------------------------------------------------ /test
    "test_need_id": "You have several subscriptions; specify one: /test <id> (see /list)",
    "test_sub_not_found": "Subscription #{sub_id} not found (see /list for your subscriptions).",
    "testing": "⏳ Running a dry-run (fetching recent posts and judging them; the sample goes to the targets)…",
    "test_result_title": "🧪 Dry-run result (subscription #{sub_id})",
    "test_sources": "· Sources: {sources}",
    "test_stats": "· Sampled: {fetched} | matched: {matched} | failed: {failed}",
    "test_sent": "· Sample ({n} hits) sent to targets: {dests}",
    "test_examples_head": "Examples (newest first):",
    "test_none": "(No match in the recent sample — loosen the thresholds, adjust the description, or try another channel.)",
    "test_line": "· [@{source} | {timestamp}] ({values}) {snippet}…",
    # ------------------------------------------------------------ edit
    "edit_title": "✏️ Edit subscription #{sub_id}",
    "edit_hint": "Choose what to change:",
    "edit_done": "✅ Updated.",
    "edit_template_ask": (
        "🧩 Re-describe the filter (confirming overwrites the current template):\n"
        "Example: “Major China-related news, no celebrity gossip”.\n\n"
        "You can also paste a JSON template. Send /cancel to abort."
    ),
    "edit_source_ask": (
        "Send the channel(s) to add:\n"
        "· @channel_name\n"
        "· or link https://t.me/channel_name\n\n"
        "New channels start delivering from the latest post. Send /cancel to finish."
    ),
    "chat_added": "✅ Channel “{title}” registered. You can now pick it in the /new targets list.",
    # ------------------------------------------------------------ buttons
    "btn_new": "📝 New subscription",
    "btn_list": "📋 My subscriptions",
    "btn_help": "📖 Help",
    "btn_tpl_use": "✅ Use this template",
    "btn_tpl_redo": "✏️ Re-describe",
    "btn_tpl_adjust": "🔧 Ask AI to adjust",
    "btn_done": "✅ Done",
    "btn_add_dm": "📬 Add DM",
    "btn_add_channel": "📢 Add {title}",
    "btn_refresh": "🔄 Refresh channel list",
    "btn_pause": "⏸ Pause",
    "btn_resume": "▶️ Resume",
    "btn_test": "🧪 Dry-run",
    "btn_edit": "✏️ Edit",
    "btn_delete": "🗑 Delete",
    "btn_back_list": "⬅️ Back to list",
    "btn_back": "⬅️ Back",
    "btn_sources": "📡 Sources",
    "btn_dests": "📬 Targets",
    "btn_template": "🧩 Filter template",
    "btn_rm": "❌ {name}",
    # ------------------------------------------------------- formatting
    "fmt_and": " AND ",
    "fmt_or": " OR ",
    "fmt_undefined": "(undefined)",
    "fmt_template_title": "🧩 Template: {name}",
    "fmt_unnamed": "(unnamed)",
    "fmt_q_line": "{index}. “{title}” ({type})",
    "fmt_instructions": "   Question: {text}",
    "fmt_yes": "   Yes: {value}",
    "fmt_no": "   No: {value}",
    "fmt_options": "   Options: ",
    "fmt_levels": "   Levels: ",
    "fmt_match": "🎯 Match condition: {rule}",
    "chunk_mark": "({i}/{n})",
    "test_mark": "🧪 Sample (not a real delivery)\n\n",
    # ------------------------------------------------------- pipeline
    "deliver_failed": "Delivery failed ({label}): {err}",
    "preview_quota_exhausted": "Monthly judgment quota exhausted (dry-runs count against it too).",
    "quota_paused": "User {uid} exhausted the judgment quota; subscriptions auto-paused: {paused}",
    # ------------------------------------------------------------ admin
    "admin_help": (
        "🛠 Admin commands\n"
        "/admin — overview (users/subscriptions/usage)\n"
        "/admin users [n] — user list (default 30)\n"
        "/admin user <id> — user detail (usage, quota, subscriptions)\n"
        "/admin usage [days] — usage summary by user (default 30 days)\n"
        "/admin watches — channel refresh schedule (cursor/watchers/last fetch)\n"
        "/admin interval <minutes> — set the global refresh interval (1–1440)\n"
        "/admin block <id> | unblock <id> — suspend / restore\n"
        "/admin quota <id> sub <n> — subscription cap (0 = default)\n"
        "/admin quota <id> jev <n> — monthly judgment quota (0 = unlimited)\n"
        "/admin lang <en|zh> — default UI language for new users\n"
        "/admin note <id> <text>"
    ),
    "admin_private_only": "Please use admin commands in a private chat.",
    "admin_bad_uid": "argument {n} must be a user id",
    "admin_param_error": "⚠️ Bad arguments: {err}\n\n{help}",
    "admin_overview_title": "🛠 Admin overview",
    "admin_users_line": "👥 Users {total} (active {active} | blocked {blocked})",
    "admin_subs_line": "📡 Subscriptions {total} (running {enabled} | paused {paused})",
    "admin_period_today": "Today",
    "admin_period_7d": "7d",
    "admin_period_30d": "30d",
    "admin_period_all": "All time",
    "admin_usage_line": "📊 {label}: {rollup}",
    "admin_top5_title": "🏆 Top 5 usage (30d):",
    "admin_none": "none",
    "admin_lang_line": "🌐 Default language (/admin lang <en|zh>): {language}",
    "admin_lang_set": "✅ Default language set to {language}. Users can override it with /lang.",
    "admin_lang_bad": "Usage: /admin lang <en|zh>",
    "admin_watch_list_title": "📡 Channel refresh schedule",
    "admin_watch_line": "· @{channel} | watchers {watchers} | cursor {cursor} | last {last}",
    "admin_watch_never": "never",
    "admin_interval_line": "⏱ Global refresh interval (/admin interval <minutes>): every {minutes} min",
    "admin_interval_show": "⏱ All channels refresh every {minutes} minutes.\nSet: /admin interval <minutes> (1–1440)",
    "admin_interval_set": "✅ Global refresh interval set: every {minutes} minutes (all channels).",
    "admin_interval_bad_minutes": "Minutes must be an integer between 1 and 1440",
    "admin_users_title": "👥 Users ({n} total, showing {shown}; usage = 30d)",
    "admin_user_line": "{mark} {uid} {name} | subs {subs} | {rollup}",
    "admin_users_none": "(no users yet)",
    "admin_user_not_found": "❌ No user {uid}",
    "admin_user_title": "👤 User {uid}",
    "admin_user_status": "Status: {status} | joined: {created}",
    "admin_status_blocked": "⛔ blocked",
    "admin_status_active": "✅ active",
    "admin_user_quota": "Quota: subscription cap {max_subs} | monthly judgments {quota}",
    "admin_default": "default",
    "admin_unlimited": "unlimited",
    "admin_consumed_month": "Judgments consumed this month: {used} / {quota}",
    "admin_month_tokens": "Monthly tokens: in {in_tok} → out {out_tok}",
    "admin_note_line": "Note: {note}",
    "admin_subs_head": "📡 Subscriptions ({n}):",
    "admin_sub_line": "· #{id} {mark} {sources} → {dests}",
    "admin_recent_head": "🕘 Recent events:",
    "admin_recent_line": "· {ts} {label}×{qty}{tokens}{ref}{detail}",
    "admin_usage_title": "📊 Usage summary (last {days}d | users {users} | total {rollup})",
    "admin_no_records": "(no records)",
    "admin_cannot_target_admin": "❌ Cannot apply this action to an admin account.",
    "admin_block_done": "⛔ Suspended {uid} (paused {paused} subscriptions; user notified).",
    "admin_block_notice": "⛔ Your access has been suspended by the admin. Contact the admin if you have questions.",
    "admin_unblock_done": "✅ Restored access for {uid}; their subscriptions stay paused — resume via /list.",
    "admin_quota_usage_error": "Usage: /admin quota <id> sub|jev <n>",
    "admin_quota_sub_done": "✅ {uid}: subscription cap = {value}.",
    "admin_quota_jev_done": "✅ {uid}: monthly judgment quota = {value}.",
    "admin_note_empty": "note text is empty",
    "admin_note_done": "✅ Note updated for {uid}.",
    "admin_system_name": "🛰 Channel-shared (system)",
    "kind_jev": "Jev",
    "kind_consumed": "Jev consumed",
    "kind_llm": "LLM",
    "kind_run": "Runs",
    "kind_fetch": "Fetches",
    "kind_deliver": "Sent",
    "tok_suffix": " (tok {in_tok}→{out_tok})",
    "paren": " ({text})",
    "watch_error": "⚠️ Channel @{channel} refresh failed: {err}",
}

_ZH: dict[str, str] = {
    # ------------------------------------------------------------- general
    "welcome": (
        "👋 我是「频道过滤器」机器人。\n\n"
        "用法：订阅任意公开频道 → 用 Jev 判定每条新消息 → 命中后推送到你的私聊或频道。\n\n"
        "· /new — 新建订阅（可多源 → 多目的地）\n"
        "· /list — 我的订阅（点选条目后暂停/试跑/编辑/删除）\n"
        "· /test <编号> — 试跑一次（样张发往订阅目标）\n"
        "· /lang — 切换语言（中文 / English）\n"
        "· /help — 使用说明"
    ),
    "help": (
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
        "· 所有频道按统一的全局节奏刷新（管理员可配置；同一频道每轮只抓取、判定一次），无需逐条设置频率；\n"
        "· 只推送订阅之后出现的新消息，停机过久可能漏掉超出预览窗口的消息；\n"
        "· 本机器人不使用 userbot，不读取私有频道。"
    ),
    "fallback": (
        "🤖 我还没学会处理这类消息。\n\n"
        "· /new — 新建订阅\n"
        "· /list — 管理订阅\n"
        "· /help — 使用说明"
    ),
    "unknown_cmd": "未知命令。发送 /help 查看用法。",
    "canceled": "已取消。",
    "error": "⚠️ 出错了：{err}",
    "unconfigured": "⚠️ 服务未配置 TYPESAFE_API_KEY，暂时无法工作。请联系管理员。",
    "blocked": "⛔ 你的使用权限已被暂停。如需恢复请联系管理员。",
    "private_only": "🔒 请在与我的私聊中使用 {cmd}（订阅数据只对你自己可见）。",
    "new_private_only": "请在私聊中使用 /new 创建订阅。",
    "toast_refreshed": "已刷新",
    "lang_prompt": "🌐 选择语言 / Choose your language（当前：{current}）",
    "lang_set": "✅ 语言已切换为中文。",
    # ------------------------------------------------------------ /new
    "ask_source": (
        "请发送要订阅的频道（可添加多个）：\n"
        "· @频道名\n"
        "· 或链接 https://t.me/频道名\n\n"
        "加完后点「✅ 完成」进入下一步。（发送 /cancel 取消）"
    ),
    "bad_source": "❌ 频道引用无法解析：{err}\n请重试，或发送 /cancel 取消。",
    "source_unreachable": "❌ 无法访问该频道：{err}\n请检查频道名后重试，或发送 /cancel 取消。",
    "src_manager_head": "📡 源频道（点 ❌ 移除；继续发送频道名/链接即可添加）",
    "src_manager_next": "加完后点「✅ 完成」进入下一步。",
    "src_manager_back": "点「✅ 完成」返回。",
    "source_duplicate": "该频道已在源列表中。",
    "none_label": "（无）",
    "more_items": " 等 {n} 个",
    "list_sep": "、",
    "ask_describe": (
        "🗣 用一句话描述你想筛选出什么内容（自然语言即可）。\n"
        "例如：「中国相关的重磅消息，排除娱乐八卦」。\n\n"
        "高级用法：直接粘贴 JSON 模板。\n发送 /cancel 取消。"
    ),
    "compiling": "⏳ 正在编译筛选模板…",
    "compile_failed": "❌ 模板编译失败：{err}\n请换个说法重试，或发送 /cancel。",
    "llm_not_configured": (
        "⚠️ 未配置 LLM（OPENAI_API_KEY），无法用自然语言编译模板。\n"
        "你可以直接粘贴 JSON 模板（高级用法），或联系管理员配置后再试。"
    ),
    "bad_template_json": "❌ 模板 JSON 校验失败：{err}\n请修正后重试，或 /cancel。",
    "template_confirm": (
        "📋 请确认筛选模板：\n\n{summary}\n\n"
        "确认后继续选择推送目的地；也可以重新描述，或让 AI 调整。"
    ),
    "template_confirm_edit": (
        "📋 请确认新的筛选模板：\n\n{summary}\n\n"
        "确认后将覆盖当前订阅的筛选条件；也可以重新描述，或让 AI 调整。"
    ),
    "ask_adjust": "🔧 请告诉我怎么调整（一句话即可），例如：「重要度门槛提高到 2.0」。",
    "dest_manager_head": (
        "📬 推送目的地（可多选）：\n"
        "· 「加私聊」直接可用；发到频道需先把机器人添加为该频道的管理员\n"
        "· 点 ❌ 移除已选目的地"
    ),
    "dest_manager_next": "选好后点「✅ 完成」进入下一步。",
    "dest_manager_back": "改好后点「✅ 完成」返回。",
    "dest_manager_empty": "（还没选目的地——至少要选一个）",
    "dest_duplicate": "该目的地已在列表中。",
    "need_one_dest": "⚠️ 至少要保留一个目的地。",
    "need_one_source": "⚠️ 至少要保留一个源频道。",
    "dest_channel_invalid": "❌ 机器人不是「{title}」的管理员（可能已被移除），无法发送到该频道。",
    "dest_channel_not_yours": (
        "❌ 无法选择「{title}」：该频道不由你添加，或机器人已不在其中。"
        "若是你的频道，请先把机器人添加为管理员。"
    ),
    "dest_user_not_admin": (
        "❌ 你现在不是「{title}」的管理员，无法把消息发到该频道。"
        "如果你已被移出管理员，请重新添加后再试。"
    ),
    "dest_dm_title": "私聊",
    "sub_created": (
        "✅ 订阅 #{sub_id} 已创建！\n\n"
        "· 源频道：{sources}\n"
        "· 规则：{rule}\n"
        "· 目的地：{dests}\n\n"
        "从现在起只推送新消息。可以先 /test {sub_id} 试跑看看效果。"
    ),
    "subs_limit": "⚠️ 每人最多 {n} 个订阅，你已达到上限。请先删除不用的订阅（/list）。",
    # ------------------------------------------------------------ /list
    "no_subs": "你还没有订阅。发送 /new 创建一个。",
    "list_head": "📋 我的订阅（共 {n} 条）",
    "list_hint": "点按钮选择要操作的条目：",
    "status_running": "✅ 运行中",
    "status_paused": "⏸ 已暂停",
    "rule_label": "规则",
    "sub_line_format": "#{id}｜{sources} → {dests}\n{status}｜{rule_label}：{rule}",
    "pick_line_format": "{index}. #{id}｜{sources} → {dests}｜{state}",
    "pick_more": " 等 {n} 个",
    "sub_deleted": "🗑 订阅 #{sub_id} 已删除。",
    # ------------------------------------------------------------ /test
    "test_need_id": "你有多个订阅，请指定编号：/test <订阅编号>（用 /list 查看）",
    "test_sub_not_found": "未找到订阅 #{sub_id}（用 /list 查看你的订阅）。",
    "testing": "⏳ 正在试跑（拉取最近消息逐条判定，样张将发往目标）…",
    "test_result_title": "🧪 试跑结果（订阅 #{sub_id}）",
    "test_sources": "· 源频道：{sources}",
    "test_stats": "· 抽样：{fetched} 条｜命中：{matched} 条｜判定失败：{failed} 条",
    "test_sent": "· 试跑样张（{n} 条）已发到目标：{dests}",
    "test_examples_head": "命中示例（最新在前）：",
    "test_none": "（最近样本中没有命中——可以放宽阈值、调整描述，或换个频道试试）",
    "test_line": "· [@{source}｜{timestamp}]（{values}）{snippet}…",
    # ------------------------------------------------------------ edit
    "edit_title": "✏️ 编辑订阅 #{sub_id}",
    "edit_hint": "选择要修改的部分：",
    "edit_done": "✅ 已更新。",
    "edit_template_ask": (
        "🧩 请重新描述筛选条件（确认后覆盖当前模板）：\n"
        "例如：「中国相关的重磅消息，排除娱乐八卦」。\n\n"
        "也可以直接粘贴 JSON 模板。发送 /cancel 取消。"
    ),
    "edit_source_ask": (
        "请发送要添加的频道：\n"
        "· @频道名\n"
        "· 或链接 https://t.me/频道名\n\n"
        "新频道从当前最新消息开始推送。发送 /cancel 结束。"
    ),
    "chat_added": "✅ 已登记频道「{title}」。现在可以在 /new 的目的地列表中选择它。",
    # ------------------------------------------------------------ buttons
    "btn_new": "📝 新建订阅",
    "btn_list": "📋 我的订阅",
    "btn_help": "📖 帮助",
    "btn_tpl_use": "✅ 使用这个模板",
    "btn_tpl_redo": "✏️ 重新描述",
    "btn_tpl_adjust": "🔧 让 AI 调整",
    "btn_done": "✅ 完成",
    "btn_add_dm": "📬 加私聊",
    "btn_add_channel": "📢 加 {title}",
    "btn_refresh": "🔄 刷新频道列表",
    "btn_pause": "⏸ 暂停",
    "btn_resume": "▶️ 恢复",
    "btn_test": "🧪 试跑",
    "btn_edit": "✏️ 编辑",
    "btn_delete": "🗑 删除",
    "btn_back_list": "⬅️ 返回列表",
    "btn_back": "⬅️ 返回",
    "btn_sources": "📡 源频道",
    "btn_dests": "📬 目的地",
    "btn_template": "🧩 筛选模板",
    "btn_rm": "❌ {name}",
    # ------------------------------------------------------- formatting
    "fmt_and": " 且 ",
    "fmt_or": " 或 ",
    "fmt_undefined": "（未定义）",
    "fmt_template_title": "🧩 模板：{name}",
    "fmt_unnamed": "（未命名）",
    "fmt_q_line": "{index}. 「{title}」（{type}）",
    "fmt_instructions": "   问题：{text}",
    "fmt_yes": "   是：{value}",
    "fmt_no": "   否：{value}",
    "fmt_options": "   选项：",
    "fmt_levels": "   等级：",
    "fmt_match": "🎯 命中条件：{rule}",
    "chunk_mark": "（{i}/{n}）",
    "test_mark": "🧪 试跑样张（非正式推送）\n\n",
    # ------------------------------------------------------- pipeline
    "deliver_failed": "投递失败（{label}）：{err}",
    "preview_quota_exhausted": "本月判定配额已用完（试跑同样计入配额）。",
    "quota_paused": "用户 {uid} 判定配额用尽，已自动暂停订阅 {paused}",
    # ------------------------------------------------------------ admin
    "admin_help": (
        "🛠 管理员命令\n"
        "/admin — 总览（用户/订阅/用量）\n"
        "/admin users [n] — 用户列表（默认 30）\n"
        "/admin user <id> — 用户详情（用量、配额、订阅）\n"
        "/admin usage [days] — 按用户用量汇总（默认 30 天）\n"
        "/admin watches — 频道刷新调度（游标/订阅数/上次抓取）\n"
        "/admin interval <分钟> — 设置全局刷新间隔（1–1440）\n"
        "/admin block <id> 或 unblock <id> — 停用 / 恢复\n"
        "/admin quota <id> sub <n> — 订阅数上限（0=恢复默认）\n"
        "/admin quota <id> jev <n> — 每月判定配额（0=不限）\n"
        "/admin lang <en|zh> — 新用户默认界面语言\n"
        "/admin note <id> <备注>"
    ),
    "admin_private_only": "请在私聊中使用管理员命令。",
    "admin_bad_uid": "第 {n} 个参数应为用户 id",
    "admin_param_error": "⚠️ 参数错误：{err}\n\n{help}",
    "admin_overview_title": "🛠 管理员总览",
    "admin_users_line": "👥 用户 {total}（活跃 {active}｜停用 {blocked}）",
    "admin_subs_line": "📡 订阅 {total}（运行 {enabled}｜暂停 {paused}）",
    "admin_period_today": "今日",
    "admin_period_7d": "7 天",
    "admin_period_30d": "30 天",
    "admin_period_all": "累计",
    "admin_usage_line": "📊 {label}：{rollup}",
    "admin_top5_title": "🏆 30 天用量 Top 5：",
    "admin_none": "无",
    "admin_lang_line": "🌐 默认语言（/admin lang <en|zh>）：{language}",
    "admin_lang_set": "✅ 默认语言已设置为 {language}（用户可用 /lang 覆盖）。",
    "admin_lang_bad": "用法：/admin lang <en|zh>",
    "admin_watch_list_title": "📡 频道刷新调度",
    "admin_watch_line": "· @{channel}｜订阅 {watchers}｜游标 {cursor}｜上次 {last}",
    "admin_watch_never": "未抓取",
    "admin_interval_line": "⏱ 全局刷新间隔（/admin interval <分钟>）：每 {minutes} 分钟",
    "admin_interval_show": "⏱ 所有频道每 {minutes} 分钟刷新一次。\n设置：/admin interval <分钟>（1–1440）",
    "admin_interval_set": "✅ 全局刷新间隔已设置为每 {minutes} 分钟（所有频道）。",
    "admin_interval_bad_minutes": "分钟数应为 1–1440 的整数",
    "admin_users_title": "👥 用户列表（共 {n}，显示 {shown}；用量=30 天）",
    "admin_user_line": "{mark} {uid} {name}｜订阅 {subs}｜{rollup}",
    "admin_users_none": "（暂无用户）",
    "admin_user_not_found": "❌ 没有用户 {uid}",
    "admin_user_title": "👤 用户 {uid}",
    "admin_user_status": "状态：{status}｜注册：{created}",
    "admin_status_blocked": "⛔ 已停用",
    "admin_status_active": "✅ 活跃",
    "admin_user_quota": "配额：订阅上限 {max_subs}｜月判定额度 {quota}",
    "admin_default": "默认",
    "admin_unlimited": "不限",
    "admin_consumed_month": "本月判定已消费：{used} / {quota}",
    "admin_month_tokens": "本月 token：in {in_tok} → out {out_tok}",
    "admin_note_line": "备注：{note}",
    "admin_subs_head": "📡 订阅（{n}）：",
    "admin_sub_line": "· #{id} {mark} {sources} → {dests}",
    "admin_recent_head": "🕘 最近记录：",
    "admin_recent_line": "· {ts} {label}×{qty}{tokens}{ref}{detail}",
    "admin_usage_title": "📊 用量汇总（最近 {days} 天｜用户 {users}｜合计 {rollup}）",
    "admin_no_records": "（无记录）",
    "admin_cannot_target_admin": "❌ 不能对管理员账号执行该操作。",
    "admin_block_done": "⛔ 已停用 {uid}（自动暂停 {paused} 个订阅，已尝试通知本人）。",
    "admin_block_notice": "⛔ 你的使用权限已被管理员暂停；如有疑问请联系管理员。",
    "admin_unblock_done": "✅ 已恢复 {uid} 的使用权限；其订阅仍保持暂停，可在 /list 中恢复。",
    "admin_quota_usage_error": "用法：/admin quota <id> sub|jev <n>",
    "admin_quota_sub_done": "✅ 已设置 {uid}：订阅数上限 = {value}。",
    "admin_quota_jev_done": "✅ 已设置 {uid}：每月 Jev 判定配额 = {value}。",
    "admin_note_empty": "备注内容为空",
    "admin_note_done": "✅ 已更新 {uid} 的备注。",
    "admin_system_name": "🛰 频道共享（系统）",
    "kind_jev": "Jev判定",
    "kind_consumed": "判定消费",
    "kind_llm": "LLM编译",
    "kind_run": "执行",
    "kind_fetch": "抓取",
    "kind_deliver": "推送",
    "tok_suffix": "（tok {in_tok}→{out_tok}）",
    "paren": "（{text}）",
    "watch_error": "⚠️ 频道 @{channel} 刷新出错：{err}",
}

_TABLES: dict[str, dict[str, str]] = {"en": _EN, "zh": _ZH}


def resolve(code: str | None) -> str:
    """Map a locale-ish string to a supported language code (zh* → zh, else en)."""
    return "zh" if code and code.lower().startswith("zh") else "en"


def t(lang: str, key: str, **fmt: object) -> str:
    """Look up a localized string; falls back to English for unknown keys."""
    table = _TABLES.get(lang, _EN)
    text = table.get(key) or _EN[key]
    return text.format(**fmt) if fmt else text
