<p align="center">
  <img src="assets/logo.png" alt="Jev Telegram Channel Router Logo" width="200">
</p>

# Jev Telegram Channel Router（中文版）

[![CI](https://github.com/HuXioAn/jev-telegram-channel-router/actions/workflows/ci.yml/badge.svg)](https://github.com/HuXioAn/jev-telegram-channel-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

订阅**任意公开 Telegram 频道** → 用 **Jev（TypeSafe）判定**逐条过滤 → 命中消息**路由**到你的私聊或你管理的频道。

比如：
- **过滤**——只盯信息量大的财经频道，但只收「重要」的那几条。
- **分类**——把加密货币的帖子推到加密货币频道，宏观政策的推到新闻频道。
- **聚合**——同时关注多个频道，把所有 AI 相关的消息合并成一条流发进你的私聊。

> English：[README.md](README.md)

## 核心思路：Jev 就是路由器

[**Jev**](https://typesafe.ai) 把自然语言问题变成校准过的答案（`noul` 概率 / `score` 等级 / `choice` 选项）。整个 bot 就一个闭环：**你描述什么值得看 → 编译成可复用的 Jev 模板 → 每条新帖都由 Jev 判定 → 命中就路由给你。**

**一条消息只判一次。** 刷新按**源频道**调度而非按订阅：频道上所有活跃模板的问题合并（去重 + 缓存），因此无论多少订阅者，每条新帖只花**一次 Jev 调用**。

- **多用户隔离**——各自独立的源、筛选与目的地。
- **自然语言配置**——一句话描述，可试跑、可让 AI 调整；单轮编译，无对话式 agent。
- **n 源 → m 目的地**——私聊和/或你管理的频道；每源独立游标。
- **token 计量**——按用户记录真实 token；管理后台支持配额与封禁。
- **白道抓取**——仅官方 Bot API + 公开 `t.me/s/` 预览；不用 userbot、不碰私有频道。
- **中英双语界面**——`/lang` 自选，`/admin lang` 改实例默认。

## 工作原理

```
按源频道调度（60 秒 tick，到期即刷）：
  增量抓取 → 联合判定：每条新帖一次 Jev 调用，频道内全部订阅共享
  → 按各模板求值（缓存 7 天）→ 命中路由到私聊 / 频道
```

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

```bash
git clone https://github.com/HuXioAn/jev-telegram-channel-router.git
cd jev-telegram-channel-router
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # 填 BOT_TOKEN 和 TYPESAFE_API_KEY
                          # （可选）LLM key——见下方说明
python -m tgfilter        # 运行
```

`.env` 里的 LLM key（`OPENAI_*`，任意 OpenAI 兼容端点）为**可选**：如果希望用自然语言描述筛选条件、由 LLM 编译成 Jev 模板，就需要配置它；不配置时，向导里只能直接粘贴 JSON 模板。

然后给 bot 发 `/start` → `/new`。要推送到频道，先把 bot 加为频道管理员。

常驻服务（开机自启、崩溃自动重启）：

```bash
cp deploy/tg-filter-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tg-filter-bot
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `/start` | 私聊入口 |
| `/new` | 新建订阅：选频道 → 描述筛选什么 → 确认 → 选目的地 |
| `/list` | 管理订阅：暂停 / 恢复 / 试跑 / 编辑 / 删除 |
| `/test <编号>` | 试跑：判定最近消息，并把样张发到订阅目标 |
| `/lang` | 切换界面语言（English / 中文） |
| `/help`、`/cancel` | 帮助 / 取消向导 |

## 更多

- **完整使用指南**——模板 JSON 格式、向导与编辑细节、管理命令、计量口径、设计约束：[docs/USAGE.md](docs/USAGE.md)
- **架构**——[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · **设计笔记**——[docs/DESIGN.md](docs/DESIGN.md)
- **许可**——MIT
