# tg-filter-bot

订阅**任意公开 Telegram 频道** → 用 **Jev 判定模型**逐条语义过滤 → 命中消息**路由**到你的私聊或你管理的频道。

- 多用户：任何 Telegram 用户都可以创建自己的订阅（源频道、筛选条件、目的地各自独立）。
- 自然语言配置：用一句话描述想筛选什么，LLM 编译成**可反复执行的 Jev 模板**；确认后可试跑、可让 AI 调整。
- 全程无 agent loop、不自然语言回复用户——bot 只回状态与结果。
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
├── store.py           # SQLite：users/chats/subscriptions/logs
├── pipeline.py        # 单订阅执行管线（run / preview）
├── delivery.py        # 投递抽象（DM / 频道）
├── services.py        # 服务容器
└── bot/
    ├── app.py         # Application 构建 + 到期调度
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
| `/test <编号>` | 试跑：拉最近 ~100 条判定演示，不发送 |
| `/help` | 使用说明 |
| `/cancel` | 取消当前向导 |

向导要点：
- 描述框可直接粘贴 **JSON 模板**（高级用法，绕过 LLM）。
- 模板确认页可「✅ 使用」/「✏️ 重新描述」/「🔧 让 AI 调整」（带上一版模板与调整意见再编译）。
- 目的地在「📬 私聊」和「📢 频道」间选择；频道需先把 bot 添加为管理员，加好后点「🔄 刷新列表」。

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

## 设计约束（有意为之）

- **不回复自然语言**：bot 输出只有状态、模板摘要、试跑结果、推送摘要。
- **无 agent loop**：LLM 只做「描述 → JSON 模板」单轮翻译；编译失败自动带错误重试一次。
- **白道抓取**：仅 `t.me/s/` 预览；频道若关闭网页预览则无法抓取（向导会即时提示）。
- **滑动窗口**：公开预览通常只保留最近约 25 万条；停机过久可能漏掉窗口外的消息。
- **投递即复制**：推送为「原文 + 链接」的复制转发（本 bot 对源频道无任何权限，无法原生转发）。

## 部署（systemd，可选 —— 开发阶段可不启用）

开发/验证阶段直接前台或临时后台运行即可：`.venv/bin/python -m tgfilter`。正式部署时：

```bash
cp deploy/tg-filter-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tg-filter-bot
systemctl status tg-filter-bot    # 状态
tail -f data/bot.log              # 日志（追加写入）
```

服务以项目 venv 运行、开机自启、异常自动拉起；改代码后 `systemctl restart tg-filter-bot` 即可。

## 测试

```bash
pytest -q              # 全部离线单测：模型/解析/抓取分页/Jev 重试/编译/存储/管线
python scripts/smoke_live.py <频道>   # 冒烟：真网络（t.me + Jev）
```

无 bot token 时，除「Telegram 收发」外的一切均可离线验证；拿到 token 后 `/start` 即可端到端跑通。
