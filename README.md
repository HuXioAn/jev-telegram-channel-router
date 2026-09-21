# Jev Telegram Channel Router

[![CI](https://github.com/HuXioAn/jev-telegram-channel-router/actions/workflows/ci.yml/badge.svg)](https://github.com/HuXioAn/jev-telegram-channel-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Subscribe to **any public Telegram channel**, filter every new post with **Jev (TypeSafe) judgments**, and **route the matches** to your private chat or channels you manage.

> Read this in [中文](README.zh-CN.md)

## Why Jev? (Jev is the router)

[**Jev**](https://typesafe.ai) is TypeSafe's judgment model: instead of prompting a generative LLM, you ask it natural-language questions about a piece of text — *"Is this post about crypto? How important is it? Which category is it?"* — and it returns **calibrated answers** (`noul` probability 0–1, graded `score`, or a `choice`).

This bot is built around one idea: **use Jev as a message router**. You describe what deserves your attention in plain language; that description is compiled into a reusable **Jev template** (a question set + match conditions); and then *every new post in your source channels is judged by Jev* — posts matching your conditions are routed to your destinations, the rest are dropped. No keywords, no regexes, no reading the firehose yourself.

### One post, one judgment — no matter how many subscribers

Restating the same questions per subscriber would multiply cost by the number of subscriptions. So the execution unit is the **source channel**, not the subscription:

1. every refresh round fetches new posts **once** per channel;
2. the questions of **all active templates on that channel** (from all subscribers, across users) are merged into a **union question set**, deduplicated by content and split into shards if it exceeds `JUDGE_MAX_QUESTIONS` (default 24);
3. Jev is called **once per post** for the whole union;
4. each answer set is **projected per template**, evaluated against the template's match conditions, cached (channel + post + template fingerprint, 7-day TTL) and routed.

Merging is implicit: users never see or configure it. Measured on live channels (`scripts/shadow_union.py`), compared with "judge each subscription separately":

- decision parity: **40/40 identical matches**;
- Jev calls: **−75%** (40 → 10 for the same batch);
- input tokens: **−70%** (18.5k → 5.6k).

Other highlights:

- **Multi-user, fully isolated** — anyone can create their own subscriptions (sources, filter, destinations are per-user; channel destinations are only offered to the user who added the bot to that channel).
- **Natural-language setup** — describe what you want in a sentence; an LLM compiles it into a Jev template; you can dry-run it and ask the AI to adjust. No agent loop: a single structured JSON call, no conversational replies.
- **n sources → m destinations** — one subscription can watch several channels (each with its own cursor) and fan matches out to a DM and/or several channels.
- **Token-grade metering + admin console** — real `input_tokens`/`output_tokens` from the API responses, per user and per kind; quotas and blocking for operators.
- **White-hat fetching only** — official Bot API + the public `t.me/s/` previews. No userbot, no private channels, no member scraping.
- **Bilingual UI** — English and Chinese, switchable per user (`/lang`) and per instance (`DEFAULT_LANG`, `/admin lang`).

## How it works

```
                    ┌─ scheduled per source channel (one round each)
t.me/s/<channel>    ▼
?after=<cursor> ─ incremental fetch (rate-limited 0.5–2s) ─ union of all
                                                   template questions on the channel
                                                            │
                                                            ▼
                                              ONE Jev call per post
                                        (answers cached; retries on 429/529)
                                                            │
                                   per-template projection & match evaluation
                                                            │
                                                    hits → digest composition
                                          ┌─────────────────┴─────────────────┐
                                          ▼                                   ▼
                                  📬 your DM                   📢 your channel (bot is admin)
```

- The scheduler unit is the **source channel**: every 60 s due channels are refreshed (interval = min across the channel's subscriptions; admins tune it with `/admin watch`).
- Two cursors decouple timing: the channel-level **fetch cursor** (watch) follows new posts; each subscription's **consume cursor** decides where *it* starts reading — new subscriptions backfill, old ones never double-read.
- Judge results are cached per (channel, post, template fingerprint): reruns after a cursor rollback never re-call Jev.
- Matches are delivered as plain "text + link" blocks, blank-line separated, auto-chunked at ≤3800 chars.

## Quick start

```bash
# 1) Install (Python ≥ 3.11)
git clone git@github.com:HuXioAn/jev-telegram-channel-router.git
cd jev-telegram-channel-router
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# 2) Configure
cp .env.example .env    # fill BOT_TOKEN / TYPESAFE_API_KEY / (optional) OPENAI_*

# 3) Offline self-check (no bot token needed)
pytest -q
python scripts/smoke_live.py Financial_Express     # real t.me fetch (+ real Jev if key set)

# 4) Run
python -m tgfilter          # or: tg-filter-bot
```

### BotFather checklist

1. `/newbot` → put the token into `BOT_TOKEN`.
2. To deliver to a **channel**, add the bot as a **channel admin** (channel → Manage → Administrators → Add); no other switches are needed.
3. Group privacy settings are irrelevant — the bot never reads group messages.

### `.env` variables

| Variable | Meaning | Default |
| --- | --- | --- |
| `BOT_TOKEN` | BotFather token (required) | — |
| `TYPESAFE_API_KEY` | Jev / TypeSafe API key (required) | — |
| `TYPESAFE_BASE_URL` | Jev endpoint | `https://api.typesafe.ai/v1` |
| `OPENAI_API_KEY` | OpenAI-compatible LLM key for the natural-language compiler (empty → JSON templates only) | — |
| `OPENAI_BASE_URL` | Compatible endpoint, including version prefix (e.g. `/v1`) | `https://api.openai.com/v1` |
| `OPENAI_MODEL` | Model name | `gpt-4o-mini` |
| `DB_PATH` | SQLite path | `data/tgfilter.db` |
| `JEV_CONCURRENCY` | Jev request concurrency | `8` |
| `FETCH_PAGE_DELAY` | Delay between preview pages (seconds) | `0.6` |
| `DIGEST_CHUNK_LIMIT` | Max chars per delivered message | `3800` |
| `DEFAULT_INTERVAL_MINUTES` | Default channel refresh interval (minutes) | `20` |
| `JUDGE_MAX_QUESTIONS` | Max questions per union judgment call (beyond → per-template shards) | `24` |
| `DEFAULT_LANG` | Default UI language `en` \| `zh` (users switch with `/lang`, admins with `/admin lang`) | `en` |
| `ADMIN_USER_IDS` | Comma-separated Telegram user ids for `/admin`; empty disables admin commands | — |
| `DEFAULT_USER_STATUS` | New-user status: `active` / `blocked` (invite-only) | `active` |

### Using any OpenAI-compatible LLM

The LLM is only used for the one-shot "natural language → Jev template" compilation. Switching providers is three lines:

| Provider | `OPENAI_BASE_URL` | `OPENAI_MODEL` |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| GLM (Zhipu) | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| OpenRouter | `https://openrouter.ai/api/v1` | any (e.g. `openai/gpt-4o-mini`) |
| Anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-4-20250514` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash` |
| Ollama (local) | `http://localhost:11434/v1` | `qwen2.5:14b` |
| vLLM / LM Studio | `http://localhost:8000/v1` | your served model |

Endpoints that reject JSON mode or `temperature` are handled automatically (the client degrades parameters and retries).

## Bot commands

| Command | What it does |
| --- | --- |
| `/start` | Entry point (with menu buttons) |
| `/new` | Subscription wizard: channels (multiple) → description/template → confirm → destinations → created |
| `/list` | My subscriptions: pick an entry, then pause / resume / dry-run / edit / delete |
| `/test <id>` | Dry-run: judges the latest ~100 posts and sends a sample (≤6 posts, 🧪 header) to the subscription's destinations |
| `/lang` | Switch the UI language (English / Chinese) |
| `/help` | Usage help |
| `/cancel` | Abort the current wizard |

A subscription is **n sources → m destinations**: several channels (each with its own cursor) can fan out to a mix of DM/channel destinations.

Wizard notes:

- Send channel references one by one (`@name`, `t.me/name` or a post link), then tap "✅ Done". Each source starts from the newest post at the time it was added.
- The description box also accepts a raw **JSON template** (advanced, bypasses the LLM).
- The template confirmation page offers "✅ Use" / "✏️ Redescribe" / "🔧 Ask AI to adjust" (recompiles with the previous template + your feedback).
- Destinations are picked between "📬 Add DM" and "📢 Add channel"; for a channel, add the bot as admin first and tap "🔄 Refresh channel list". Sources and destinations can never be emptied.
- **Editing**: `/list` → pick an entry → "✏️ Edit" → change 📡 sources, 📬 destinations, or the 🧩 template independently; every change saves immediately.
- **Refresh cadence is scheduled system-wide** (one fetch + one judgment round per channel); subscriptions do not configure frequency anymore — admins tune it per channel with `/admin watch`.
- Dry-runs really deliver the sample to the subscription's destinations (marked 🧪, no cursor advance, not counted as a delivery) so you can verify the push exactly where it will land.
- **Generated templates are always written in English** — Jev's models are English-first (the official Models page notes lower accuracy for non-English), while the posts being filtered can be in any language.

## Template format (JSON)

```jsonc
{
  "name": "China headlines",
  "questions": {
    "china":  { "type": "noul",  "title": "China", "instructions": "Is this directly related to China's market/policy/companies?",
                "criteria": {"true": "China-related", "false": "Not related"} },
    "cat":    { "type": "choice", "title": "Category", "instructions": "Which category?",
                "criteria": {"Macro": "…", "Company": "…", "Industry": "…"} },
    "importance": { "type": "score", "title": "Importance", "instructions": "How important?",
                "criteria": ["Routine", "Normal", "High", "Major"] }
  },
  "match": { "logic": "all",
             "conditions": [ {"question": "china", "op": ">=", "value": 0.7},
                             {"question": "importance", "op": ">=", "value": 1.8} ] }
}
```

- `noul` returns a 0–1 probability; `score` a 0-based grade position; `choice` an option name.
- `op` supports `>=` `<=` `==` `in` `not_in`; `logic` supports `all` / `any`.
- Question ids are lowercase English; `title` is display-only and is never sent to Jev.

## Usage metering & admin console

Every user's activity is recorded per item in the `usage` table, with the **real token counts returned by the APIs**:

| kind | what it counts | tokens |
| --- | --- | --- |
| `jev` | Jev judgment calls (channel-shared: recorded under `user_id=0` + channel name; 1 = one post's union judgment in one round) | ✅ real input/output per call |
| `consumed` | judgment consumption: posts actually consumed by the user (deduplicated across the user's subscriptions; **monthly quota is based on this**) | — |
| `llm` | template-compiler API calls (including auto-fix retries) | ✅ real input/output |
| `fetch` | channel fetches (channel-shared) | — |
| `deliver` | delivered messages | — |

Because judgments are merged, judgment cost is a **channel-level shared cost** (recorded under `user_id=0`, shown to admins as "🛰 channel-shared"); individual quotas count `consumed` (posts actually consumed) — fair and currency-agnostic.

Admin commands (only for ids in `ADMIN_USER_IDS`, private chat only; `/admin` appears in the admin's own command menu):

| Command | What it does |
| --- | --- |
| `/admin` | Overview: users/subscriptions, today/7d/30d/all-time usage, 30-day top 5 |
| `/admin users [n]` | User list with status, subscription count, 30-day usage |
| `/admin user <id>` | Detail: quota, month-to-date consumption + tokens, subscriptions, per-window usage, recent events |
| `/admin usage [days]` | Per-user usage summary (default 30 days) |
| `/admin watches` | Channel refresh schedule: interval / cursor / watchers / last fetch |
| `/admin watch <channel> <minutes>` | Set a channel's refresh interval (e.g. `/admin watch Financial_Express 10`) |
| `/admin lang <en\|zh>` | Instance default UI language |
| `/admin block <id>` / `unblock <id>` | Suspend / restore a user (suspension auto-pauses their subscriptions and notifies them) |
| `/admin quota <id> sub <n>` | Subscription cap (0 = default 20) |
| `/admin quota <id> jev <n>` | Monthly judgment quota in consumed posts (0 = unlimited; auto-pauses on exhaustion) |
| `/admin note <id> <text>` | Operator note |

- Blocked users get a refusal for `/start /new /list /test` and can do nothing else.
- `DEFAULT_USER_STATUS=blocked` turns the instance invite-only; admins let users in with `/admin unblock`.
- Quotas reset monthly (UTC); after raising a quota the user can resume subscriptions from `/list`.

## Design constraints (deliberate)

- **No conversational replies** — the bot answers with status and results only; unknown text/commands get a fixed hint.
- **Implicit union judging** — one judgment per post serves every subscription on the channel (union questions + cache + per-template projection), across users; users neither see nor configure it.
- **Isolation** — subscriptions and channel destinations are verified per owner; a channel can only be selected by the user who added the bot to it; `/list` and `/test` are private-chat only.
- **Concurrency** — up to 12 updates in parallel globally, strictly serial per chat (wizard state can't interleave); the send path retries with backoff on Telegram rate limits.
- **Metered everything** — every execution path (dry-runs included) is metered: call counts + real API tokens; judgment cost is channel-shared, personal quotas count consumed posts; exhaustion pauses rather than silently overspends.
- **No agent loop** — the LLM only translates description → JSON template in one turn; one auto-fix retry on validation failure.
- **White-hat fetching** — only `t.me/s/` previews; channels with previews disabled can't be fetched (the wizard tells you immediately).
- **Sliding window** — public previews usually keep ~250k recent posts; a long outage may drop older ones.
- **Copy-based delivery** — posts are re-published as "text + link" copies (the bot has no rights over source channels, so native forwarding is impossible).

## Deployment (systemd)

Run ad hoc with `.venv/bin/python -m tgfilter`. To run as a service (starts on boot, auto-restarts):

```bash
cp deploy/tg-filter-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tg-filter-bot
systemctl status tg-filter-bot    # state
tail -f data/bot.log              # logs
```

## Testing

```bash
pytest -q                                   # offline unit tests
python scripts/smoke_live.py <channel>      # real network smoke (t.me + Jev), no bot token needed
python scripts/shadow_union.py <channel> [n]# union vs per-subscription parity & savings on real posts
python scripts/e2e_wizard.py <user_id>      # wizard end-to-end through a real bot (delivers to DM)
python scripts/e2e_commands.py <user_id>    # full command/wizard/button/channel-event coverage
```

Everything except actual Telegram I/O is verifiable offline; with a token, `/start` exercises the full path end-to-end.

## Docs & license

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, data flow, scheduling, storage, i18n.
- [docs/DESIGN.md](docs/DESIGN.md) — design notes and rationale (translated from the original Chinese plan).
- [README.zh-CN.md](README.zh-CN.md) — Chinese README.
- MIT — see [LICENSE](LICENSE).
