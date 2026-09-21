# tg-filter-bot

订阅**任意公开 Telegram 频道** → 用 **Jev 判定模型**逐条语义过滤 → 命中消息**路由**到你的私聊或你管理的频道。

- 多用户：任何 Telegram 用户都可以创建自己的订阅（源频道、筛选条件、目的地各自独立）。
- 自然语言配置：用一句话描述想筛选什么，LLM 编译成**可反复执行的 Jev 模板**；确认后可试跑、可让 AI 调整。
- 全程无 agent loop、不自然语言回复用户——bot 只回状态与结果。
- 用量统计与管理员后台：按用户逐条计量（Jev 判定/LLM 编译/执行/抓取/推送），管理员可查询台账、封禁用户、设置配额。
- 只用官方 Bot API + `t.me/s/` 公开预览，不使用 userbot，不读取私有频道。

## 工作原理

```
t.me/s/<频道>?after=<游标>            每条消息独立调用 Jev
公开预览增量抓取（限速 0.5~2s）  ──▶  · systemone: noul/choice/score  ──▶ 模板规则命中
                                        └ 失败重试(429/529退避)              │
                                                              ┌─────────────┴─────────────┐
                                                              ▼                           ▼
                                                     📬 用户私聊                 📢 用户频道（bot 是管理员）
```

- 每个订阅一个游标（`last_seen_id`），只抓比游标新的消息；停机后自动追平。
- 判定并发执行（asyncio，默认 8 并发），单条失败只跳过该条。
- 命中多条时合并成一条摘要（原文 + 链接 + 判定值），超长自动分块（≤3800 字符/条）。
- 调度：应用内每 60 秒检查一次到期订阅，按各自 interval（10/20/30/60 分钟）执行。

## 目录结构

```
src/tgfilter/
├── config.py          # .env → Settings
├── models.py          # Post / Question / Condition / Template + 规则求值
├── channel_fetch.py   # t.me/s/ 抓取、解析、续抓（限速）
├── jev.py             # Jev(TypeSafe) 客户端：并发 + 退避重试
├── llm.py             # 自然语言 → 模板编译器（单轮调用、JSON mode 自动降级）
├── store.py           # SQLite：users/chats/subscriptions/logs/usage
├── pipeline.py        # 单订阅执行管线（run / preview）+ 用量记录与配额执行
├── delivery.py        # 投递抽象（DM / 频道）
├── services.py        # 服务容器
└── bot/
    ├── app.py         # Application 构建 + 到期调度
    ├── admin.py       # 管理员后台（/admin：用量/用户/配额）
    ├── handlers.py    # 命令、/new 向导、回调、频道成员事件
    ├── keyboards.py   # Inline 键盘
    ├── messages.py    # 全部文案（集中管理）
    └── ...
tests/                 # pytest 单测（离线，全部 mock）
scripts/smoke_live.py  # 冒烟脚本：真抓 t.me +（可选）真调 Jev，无需 bot token
```

## 快速开始

```bash
# 1) 依赖（Python ≥ 3.11）
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# 2) 配置
cp .env.example .env    # 填 BOT_TOKEN / TYPESAFE_API_KEY /（可选）OPENAI_*

# 3) 自检（无需 bot token）
pytest -q
python scripts/smoke_live.py Financial_Express

# 4) 运行
python -m tgfilter          # 或：tg-filter-bot
```

### BotFather 配置

1. `/newbot` 创建 bot，把 token 填进 `.env` 的 `BOT_TOKEN`。
2. 若要接收频道消息/成员变更事件，无需特殊开关；把 bot **加为频道管理员**即可（「频道 → 管理 → 管理员 → 添加」）。
3. 不要开启 Group Privacy 之外的特殊项；本 bot 不依赖读取群消息。

### .env 变量

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BOT_TOKEN` | BotFather 颁发的 token（必填） | — |
| `TYPESAFE_API_KEY` | Jev(TypeSafe) API key（必填） | — |
| `TYPESAFE_BASE_URL` | Jev 端点 | `https://api.typesafe.ai/v1` |
| `OPENAI_API_KEY` | OpenAI 兼容 LLM key（用于自然语言编译模板；留空则只能用 JSON 模板） | — |
| `OPENAI_BASE_URL` | 兼容端点（含版本前缀，如 `/v1`） | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | 模型名 | `gpt-4o-mini` |
| `DB_PATH` | SQLite 路径 | `data/tgfilter.db` |
| `JEV_CONCURRENCY` | Jev 并发数 | `8` |
| `FETCH_PAGE_DELAY` | 抓取翻页间隔（秒） | `0.6` |
| `DIGEST_CHUNK_LIMIT` | 单条消息字符上限 | `3800` |
| `ADMIN_USER_IDS` | 管理员（bot owner）Telegram 用户 id，逗号分隔；留空则管理员命令禁用 | — |
| `DEFAULT_USER_STATUS` | 新用户默认状态：`active` / `blocked`（邀请制） | `active` |

### 接入任意 OpenAI 兼容 LLM（示例）

LLM 只在「自然语言 → Jev 模板」这一步用到；换服务商只需改三行：

| 服务 | `OPENAI_BASE_URL` | `OPENAI_MODEL` |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Kimi（月之暗面） | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| Qwen（阿里） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| OpenRouter | `https://openrouter.ai/api/v1` | 任意（如 `openai/gpt-4o-mini`） |
| Anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-4-20250514` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash` |
| Ollama 本地 | `http://localhost:11434/v1` | `qwen2.5:14b` |
| vLLM / LM Studio | `http://localhost:8000/v1` | 自部署模型名 |

端点若不支持 JSON mode、或拒绝 `temperature`（部分推理模型），客户端会自动逐级降参重试，无需手工适配。

## Bot 命令

| 命令 | 作用 |
| --- | --- |
| `/start` | 私聊入口（含菜单按钮） |
| `/new` | 新建订阅向导：频道 → 描述/模板 → 确认 → 目的地 → 频率 |
| `/list` | 我的订阅（暂停/恢复/试跑/删除） |
| `/test <编号>` | 试跑：拉最近 ~100 条判定，并把样张（最多 6 条、带 🧪 标头）发到订阅目标 |
| `/help` | 使用说明 |
| `/cancel` | 取消当前向导 |

向导要点：
- 描述框可直接粘贴 **JSON 模板**（高级用法，绕过 LLM）。
- 模板确认页可「✅ 使用」/「✏️ 重新描述」/「🔧 让 AI 调整」（带上一版模板与调整意见再编译）。
- 目的地在「📬 私聊」和「📢 频道」间选择；频道需先把 bot 添加为管理员，加好后点「🔄 刷新列表」。
- 试跑（/list 内按钮或 /test）会把样张**真正发到订阅目标**，便于在频道里核对推送效果；样张带 🧪 标头、不推进游标、不算正式推送。
- **生成的模板内容一律为英文**（Jev 以英文为主训练语言，判定更准——官方 Models 页明确非英文精度较低）；被筛选的消息本身仍可以是中文/任何语言。

## 模板格式（JSON）

```jsonc
{
  "name": "中国重磅",
  "questions": {
    "china":  { "type": "noul",  "title": "相关", "instructions": "是否与中国市场/政策/公司直接相关？",
                "criteria": {"true": "涉及中国", "false": "不涉及"} },
    "cat":    { "type": "choice", "title": "类别", "instructions": "属于哪一类？",
                "criteria": {"宏观": "…", "公司": "…", "行业": "…"} },
    "importance": { "type": "score", "title": "重要", "instructions": "有多重要？",
                "criteria": ["日常", "一般", "较高", "重大"] }
  },
  "match": { "logic": "all",
             "conditions": [ {"question": "china", "op": ">=", "value": 0.7},
                             {"question": "importance", "op": ">=", "value": 1.8} ] }
}
```

- `noul` 返回 0~1 概率；`score` 返回 0 起始的等级位置；`choice` 返回选项名。
- `op` 支持 `>=` `<=` `==` `in` `not_in`；`logic` 支持 `all` / `any`。
- 问题 id 用英文小写；`title` 仅用于展示，不发给 Jev。

## 用量统计与管理员后台

每个用户逐条记录用量（`usage` 表，含时间、订阅号与 **API 返回的真实 token 数**）：

| kind | 计什么 | token |
| --- | --- | --- |
| `jev` | Jev 判定条数（1 条消息 = 1 次判定） | ✅ 每次判定的真实 input/output |
| `llm` | 模板编译的 API 调用次数（含自动修复重试） | ✅ 真实 input/output（多次调用累计） |
| `run` | 订阅执行轮次（含试跑） | — |
| `fetch` | 频道抓取次数 | — |
| `deliver` | 推送消息条数 | — |

token 全部取自 API 响应本身（Jev 返回 `usage.input_tokens/output_tokens`；LLM 侧兼容
`prompt_tokens/completion_tokens` 与 `input_tokens/output_tokens` 两种命名），
不是估算值——按各家的 input/output token 单价可直接折算成本。

管理员由 `.env` 的 `ADMIN_USER_IDS` 指定（仅私聊生效；`/admin` 只出现在管理员自己的命令菜单里）：

| 命令 | 作用 |
| --- | --- |
| `/admin` | 总览：用户/订阅数、今日/7/30/累计用量、30 天 Top 5 |
| `/admin users [n]` | 用户列表：状态、订阅数、30 天用量 |
| `/admin user <id>` | 详情：配额、本月 Jev 已用与 token 合计、订阅列表、各窗口用量（含 token）、最近记录 |
| `/admin usage [days]` | 按用户用量汇总（默认 30 天）——当月台账 |
| `/admin block <id>` / `unblock <id>` | 停用 / 恢复（停用会自动暂停其全部订阅并通知本人） |
| `/admin quota <id> sub <n>` | 订阅数上限（0=默认 20） |
| `/admin quota <id> jev <n>` | 每月 Jev 判定配额（0=不限；用尽自动暂停订阅并通知） |
| `/admin note <id> <备注>` | 管理备注 |

- 被停用的用户：/start /new /list /test 一律只回提示，不接受任何操作。
- `DEFAULT_USER_STATUS=blocked` 可开启邀请制：新用户默认无权限，由管理员 `/admin unblock` 放行。
- 计费：以 `jev`/`llm` 的 token 数 × 各自 input/output 单价折算；`/admin usage 30` 是当月 token 台账，`/admin user <id>` 有单人各窗口 token 与当月合计。
- 配额按月（UTC）计算；用尽自动暂停订阅并通知用户，管理员调整配额后用户可在 /list 恢复订阅（配额按判定条数计，token 仅作计量）。

## 设计约束（有意为之）

- **不回复自然语言**：bot 不做对话式回复；未识别的文本/未知命令只回一句固定提示（引导用 /help）。
- **多用户隔离**：订阅/频道目的地全部按用户校验归属；频道目的地只有「把机器人添加进频道的人」可选；/list /test 限私聊。
- **多用户并发**：全局最多 12 条更新并行（用户间互不阻塞），同一聊天严格串行（向导不乱序）；发送侧自带 Telegram 限流退避重试。
- **用量与配额**：所有执行路径（含试跑）逐条计量入账——次数 + API 真实 token；配额用尽自动暂停订阅，不静默超支。
- **无 agent loop**：LLM 只做「描述 → JSON 模板」单轮翻译；编译失败自动带错误重试一次。
- **白道抓取**：仅 `t.me/s/` 预览；频道若关闭网页预览则无法抓取（向导会即时提示）。
- **滑动窗口**：公开预览通常只保留最近约 25 万条；停机过久可能漏掉窗口外的消息。
- **投递即复制**：推送为「原文 + 链接」的复制转发（本 bot 对源频道无任何权限，无法原生转发）。

## 部署（systemd）

临时运行：`.venv/bin/python -m tgfilter`。安装为常驻服务（开机自启，崩溃自动重启）：

```bash
cp deploy/tg-filter-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tg-filter-bot
systemctl status tg-filter-bot    # 状态
tail -f data/bot.log              # 日志（追加写入）
```

服务以项目 venv 运行、开机自启、异常自动拉起；改代码后 `systemctl restart tg-filter-bot` 即可。

## 测试

```bash
pytest -q              # 离线单测：模型/解析/抓取分页/Jev 重试/编译/存储/管线/处理器
python scripts/smoke_live.py <频道>        # 冒烟：真网络（t.me + Jev），无需 bot token
python scripts/e2e_wizard.py <user_id>     # 向导端到端：合成 Update 驱动真实 bot（真发到 DM）
python scripts/e2e_commands.py <user_id>   # 全功能覆盖：命令/向导分支/按钮/频道事件/错误兜底
```

无 bot token 时，除「Telegram 收发」外的一切均可离线验证；拿到 token 后 `/start` 即可端到端跑通。
