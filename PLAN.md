# tg-filter-bot — 计划（PLAN）

> 独立 Telegram Bot：订阅任意公开频道 → Jev 语义过滤 → 命中消息路由到 DM / 频道。
> 本文档是设计决策的记录；实现见 `src/tgfilter/`。

## 1. 目标 / 非目标

**目标**
- 任何用户可自定义：源频道（任意公开频道）、筛选条件（自然语言描述 → LLM 编译为可复用的 Jev 模板）、路由目的地（自己的 DM / 自己管理的频道）。
- 接入 Jev（TypeSafe System One）做逐条判定；接入任意 OpenAI 兼容 LLM 仅用于「自然语言 → Jev 模板」的编译。
- 完整、好用、可维护：清晰的工程结构、单元测试、优雅简洁的实现。

**非目标（v1 明确不做）**
- 不做自然语言对话式交互：Bot 全部为命令/按钮式固定文案；LLM 只做模板编译，不产出用户可见的自由文本。
- 不读取私有频道、不使用 userbot/MTProto（合规边界，见 §9）。
- 不做复杂的 agent loop；LLM 调用是单轮结构化输入/输出（必要时一次纠错重试）。

## 2. 技术选型

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| Bot 框架 | **python-telegram-bot (PTB) v21+** | 最主流的 Python TG 库；内置 JobQueue 调度、ConversationHandler 向导、类型完善 |
| HTTP | **httpx (AsyncClient)** | 一个库覆盖 t.me 抓取 + Jev + LLM 三处调用；支持 MockTransport（测试友好） |
| 数据 | **SQLite（stdlib sqlite3, WAL）** | 单文件、零运维；实体少（用户/订阅/频道），无需 ORM |
| 校验 | **pydantic v2** | 模板 schema、LLM 输出校验、配置模型 |
| 调度 | PTB **JobQueue** 每 60s 轮询到期订阅 | 订阅增删/停启无需维护定时器簿记；漏跑自动补 |
| 测试 | pytest + pytest-asyncio + httpx.MockTransport | 无 bot token 也可完整测试核心逻辑 |

## 3. 架构

```
Telegram 用户 ──/new 向导──┐
                           ▼
                    ┌─────────────┐   自然语言描述     ┌───────────────┐
                    │  对话层 Bot  │ ───────────────▶ │ LLM 模板编译器 │
                    │  (handlers) │ ◀─────────────── │ (OpenAI 兼容)  │
                    └──────┬──────┘   Jev 模板(JSON)  └───────────────┘
                           │ 保存订阅(SQLite)
                           ▼
          ┌─────────────────────────────────────────┐
          │ 调度器：JobQueue 每 60s 扫描"到期订阅"      │
          └────────────────┬────────────────────────┘
                           ▼
   抓取器(t.me/s 预览) ─▶ Jev 分类器(逐条,并发) ─▶ 规则匹配 ─▶ 摘要合成 ─▶ 投递层
                                                                   (DM / 频道)
```

## 4. 模块划分

| 模块 | 职责 |
| --- | --- |
| `config.py` | 环境变量加载（BOT_TOKEN / TYPESAFE_API_KEY / OPENAI_* / DB_PATH…） |
| `models.py` | 领域模型：Post、Jev 模板（Question/Condition/Template）与 `evaluate()` 规则求值 |
| `channel_fetch.py` | 公开频道网页预览抓取：频道引用规范化、`head()`、`fetch_since()`、HTML 解析 |
| `jev.py` | Jev SystemOne 客户端：单条/批量分类，并发信号量 + 429/529 退避重试 |
| `llm.py` | 模板编译器：描述(+调整意见) → 调用 LLM → JSON → pydantic 校验（失败重试一次） |
| `formatting.py` | 模板摘要（给用户确认用）、摘要消息合成与 Telegram 分块 |
| `db.py` | SQLite 存取：users / chats(机器人是管理员的) / subscriptions / logs |
| `pipeline.py` | 单订阅一次执行：抓取→分类→匹配→合成→投递→推进游标 |
| `delivery.py` | 发送：DM / 频道；错误归类（被拉黑、无权限…） |
| `bot/` | PTB 层：`app.py`(构建/调度)、`handlers.py`(命令与向导)、`keyboards.py`、`messages.py`(全部文案) |
| `scripts/smoke.py` | 无需 bot token 的真实冒烟：真抓 t.me + 真调 Jev |

## 5. 数据模型（SQLite）

- `users(id PK, username, status, max_subs, quota_jev_monthly, note, created_at)`
- `chats(chat_id PK, kind, title, added_by, added_at)` — 机器人被加入且可发言的频道/群（来自 my_chat_member 更新）
- `subscriptions(id PK, user_id, template_json, interval_minutes, enabled, last_run_at, created_at)` — 订阅本体（**n 源 → m 目的地**）
- `sub_sources(id PK, sub_id, source, last_seen_id, UNIQUE(sub_id, source))` — 源频道；每条独立游标，独立推进
- `sub_dests(id PK, sub_id, kind[dm|channel], chat_id, title, UNIQUE(sub_id, chat_id))` — 目的地列表（私聊/频道可混）
- `logs(id PK, sub_id, ts, kind, detail)` — 运行记录/投递结果（调试与审计）
- `usage(id PK, ts, user_id, sub_id, kind, qty, input_tokens, output_tokens, detail)` — 用量（次数 + 真实 token）

旧库（单源单目的地结构）在启动时自动迁移：`subscriptions` 重建 + 数据拆分进 `sub_sources` / `sub_dests`。

## 6. 用户流程（/new 向导 + 订阅编辑）

1. `/new` → 发送频道（`@name` / `t.me/name` / `t.me/s/name`）→ 验证预览可用；**可继续发送多个频道**，点「✅ 完成」进入下一步
2. 发送一句自然语言筛选描述（例：“中国相关的重磅消息，排除娱乐八卦”）→ LLM 编译 → 模板摘要 → [✅使用 / ✏️重新描述 / 🔧让 AI 调整]
3. 目的地**多选**：📬 私聊 / 频道列表（要求机器人是频道管理员、且频道为本人添加），可反复增删，至少保留一个
4. 选频率（10/20/30/60 分钟，默认 20）
5. 保存订阅；每条源频道的游标初始化为该频道当前头部（只推新消息）；提示 `/test` 可先试跑

**编辑**：`/list` 任意订阅 →「✏️ 编辑」→ 📡 源频道（增/删）｜📬 目的地（增/删）｜⏱ 频率｜🧩 筛选模板（重新描述→编译→确认即保存）。所有修改即时落库；源/目的地均不允许删空。

其他命令：`/list`（订阅管理：暂停/恢复/试跑/编辑/删除）、`/test <id>`（试跑：拉最近 ~100 条，样张发往订阅目标）、`/cancel`、`/help`。

## 7. Jev 模板 schema（LLM 输出 = 运行时输入）

```json
{
  "name": "中国重磅消息",
  "questions": {
    "china":      {"type": "noul",  "title": "相关", "instructions": "…是否与中国直接相关？", "criteria": {"true": "…", "false": "…"}},
    "importance": {"type": "score", "title": "重要", "instructions": "…有多重要？", "criteria": ["日常", "一般", "较高", "重大"]}
  },
  "match": {"logic": "all", "conditions": [
    {"question": "china", "op": ">=", "value": 0.7},
    {"question": "importance", "op": ">=", "value": 1.8}
  ]}
}
```

- 问答原语：`noul`(是/否概率)、`choice`(互斥选项)、`score`(有序等级加权分)。
- 匹配谓词：`all/any` × `>= <= == in not_in`。
- 问题需自包含（Jev 不具外部上下文）；`title` 用于摘要展示与推送行展示。

## 8. LLM 编译器（单轮、结构化）

- System prompt 内置：schema 文档 + Jev 问题设计规范（自包含、二值化、判据具体、语言跟随用户）。
- 输出强制 JSON（优先 `response_format=json_object`；端点不支持则自动降级）；剥离 ``` 围栏。
- 校验失败：将 pydantic 错误信息回填给模型，重试 **1 次**；仍失败则报错给用户。
- 用户直接粘贴 JSON 模板（Power Mode）时跳过 LLM，直接校验启用。

## 9. 限制与合规（必须向用户传达）

- **只支持公开频道**；抓取走 `t.me/s/` 网页预览（官方公开界面），限速礼貌抓取。
- 频道预览可能被官方禁用（黑盒）；预览保留窗口有限（大频道约最近 25 万条，滑动），长停机会漏。
- 私有频道 / userbot 一律不支持（违反 ToS）。
- 发送到频道要求机器人是**管理员**；DM 要求用户已启动过机器人。

## 10. 测试策略

- 单元：模板求值、HTML 解析与引用规范化、Jev 客户端（MockTransport：200/429/错误）、LLM 编译器（含修复重试）、摘要合成与分块、DB CRUD 与到期扫描、pipeline 全流程（fake 三件套）。
- 冒烟（无需 bot token）：真抓公开频道 + 真调 Jev（`scripts/smoke.py`）。
- Bot 交互层（handlers）保持薄：状态存 `user_data`，文案集中在 `messages.py`，逻辑在可测的核心模块。

## 11. 里程碑

1. 脚手架 + 计划（本文档）
2. 核心模块（fetch / jev / llm / models / formatting / db / pipeline / delivery）
3. Bot 层（向导、订阅管理、调度）
4. 测试全绿 + 冒烟通过
5. （待 bot token）真实联调：/start、向导、首次推送、频道投递
