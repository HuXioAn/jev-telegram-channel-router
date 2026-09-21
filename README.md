<p align="center">
  <img src="assets/logo.png" alt="Jev Telegram Channel Router logo" width="200">
</p>

# Jev Telegram Channel Router

[![CI](https://github.com/HuXioAn/jev-telegram-channel-router/actions/workflows/ci.yml/badge.svg)](https://github.com/HuXioAn/jev-telegram-channel-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Subscribe to **any public Telegram channel**, filter every new post with **Jev (TypeSafe) judgments**, and **route the matches** to your private chat or channels you manage.

> 中文版：[README.zh-CN.md](README.zh-CN.md)

## Core idea: Jev is the router

[**Jev**](https://typesafe.ai) turns plain-language questions into calibrated answers (`noul` probability, `score` grade, `choice` option). The whole bot is built on one loop: **you describe what deserves attention → it compiles into a reusable Jev template → every new post is judged → matches are routed to you.**

**One post, one judgment.** Refreshing is scheduled per *source channel*, not per subscription: the questions of all active templates on a channel are merged (deduplicated and cached), so each post costs **one Jev call** no matter how many subscribers watch that channel.

- **Multi-user & isolated** — everyone brings their own sources, filters, and destinations.
- **Natural-language setup** — describe it, dry-run it, let AI adjust it. Single-shot compilation; no agent chat.
- **n sources → m destinations** — DM and/or channels you administer, each source with its own cursor.
- **Metered** — real token counts per user; admin console with quotas and blocking.
- **White-hat only** — official Bot API + public `t.me/s/` previews; no userbot, no private channels.
- **Bilingual UI** — English / 中文, per user (`/lang`) or per instance (`/admin lang`).

## How it works

```
per source channel, ticked every 60s (due-based):
  incremental fetch → union judgment: 1 Jev call per post, shared by every
  subscription on that channel → per-template match (cached, 7-day TTL)
  → matches routed to DM / channels
```

More in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

```bash
git clone git@github.com:HuXioAn/jev-telegram-channel-router.git
cd jev-telegram-channel-router
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # fill in BOT_TOKEN and TYPESAFE_API_KEY
                          # (optionally an OpenAI-compatible LLM key — see .env.example)
python -m tgfilter        # run
```

Then message the bot: `/start` → `/new`. To deliver into a channel, add the bot as a channel admin first.

As a service (starts on boot, auto-restarts):

```bash
cp deploy/tg-filter-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tg-filter-bot
```

## Commands

| Command | What it does |
| --- | --- |
| `/start` | Entry point |
| `/new` | New subscription: pick channels → describe what to filter → confirm → destinations |
| `/list` | Manage subscriptions: pause / resume / dry-run / edit / delete |
| `/test <id>` | Dry-run: judge the latest posts and send a sample to the destinations |
| `/lang` | Switch UI language (English / 中文) |
| `/help`, `/cancel` | Help / abort the wizard |

## More

- **Full guide** — template JSON format, wizard & editing details, admin commands, metering, constraints: [docs/USAGE.md](docs/USAGE.md)
- **Architecture** — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · **Design notes** — [docs/DESIGN.md](docs/DESIGN.md)
- **License** — MIT
