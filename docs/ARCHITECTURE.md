# Architecture

This document describes how the bot is put together. For the design rationale
and history see [DESIGN.md](DESIGN.md).

## Overview

One process (a python-telegram-bot `Application`) plus one SQLite file. No
external services beyond the APIs it calls:

- **Telegram Bot API** — receiving updates, delivering results;
- **`t.me/s/` public previews** — fetching channel posts (no Telegram client,
  no userbot);
- **Jev (TypeSafe)** — judging every fetched post;
- **an OpenAI-compatible LLM** (optional) — compiling natural language into Jev
  templates.

The bot answers with status and results only. It never holds conversations and
never runs an agent loop.

## Component map

| Module | Role |
| --- | --- |
| `config.py` | `.env` → frozen `Settings` dataclass |
| `models.py` | `Post`, `Question`, `Condition`, `Template` + rule evaluation |
| `channel_fetch.py` | `t.me/s/` fetching: reference normalization, HTML parsing, paging with rate limiting |
| `jev.py` | Jev client: concurrent `classify_questions` calls, 429/529 backoff, usage extraction |
| `llm.py` | Template compiler: one-shot chat call, JSON-mode degradation, one auto-fix retry |
| `judging.py` | Union judging engine: template fingerprints, question-union (dedup/sharding), projection, cache |
| `store.py` | SQLite layer: users, chats, subscriptions, watches, judgments, logs, usage, settings |
| `pipeline.py` | Channel-round pipeline: `run_watch()` / `preview()`; metering and quota enforcement |
| `delivery.py` | Sender abstraction: retries, rate-limit handling, error mapping (DM / channel) |
| `formatting.py` | One message per matching post ("text + link", dry-run marker) |
| `services.py` | Service container built in `post_init`, kept in `application.bot_data` |
| `bot/app.py` | Application wiring, per-chat update serialization, due-watch ticker |
| `bot/handlers.py` | Commands, the `/new` wizard, callbacks, membership events |
| `bot/admin.py` | `/admin` console |
| `bot/messages.py` | All user-facing copy, resolved through `i18n.t()` |
| `i18n.py` | Bilingual copy tables (en/zh), `t()`, `resolve()`, command menus |

## Data flow

```
tick (60s) → due_watches() → one task per channel (deduped by _running)
   │
   ├─ fetch new posts since watch.last_seen_id   (t.me/s/, paged, 0.5–2s apart)
   ├─ union judge: questions of all active templates on the channel
   │     ├─ fingerprint each template (stable hash of the template JSON)
   │     ├─ collect (question payload → answer id) map, dedup by payload
   │     ├─ shard if > JUDGE_MAX_QUESTIONS
   │     ├─ one Jev call per post, concurrency JEV_CONCURRENCY
   │     └─ store judgments (channel, post, fingerprint) with a 7-day TTL
   ├─ per subscription: consume-cursor slice → project answers → evaluate match
   │     └─ hits → 24h same-destination near-duplicate check → one message per new post
   └─ advance the channel fetch cursor
```

### Union judging

The scheduler's unit is the **source channel**, not the subscription. Within
one round:

1. every active template on the channel (any user) is fingerprinted;
2. questions from all templates are merged into one union, deduplicated by
   payload (two templates asking the identical question share one answer);
3. the union is split into shards of at most `JUDGE_MAX_QUESTIONS` (default 24);
4. Jev is asked **once per post for the whole union** (per shard);
5. answers are cached per (channel, post, template-fingerprint), so a template
   edit changes only its own fingerprint and does not invalidate others;
6. each subscription projects the answers onto its own template and evaluates
   its match conditions.

Consequences: cost scales with **posts × shards**, not posts × subscriptions;
identical questions across users cost one call; caching makes cursor rollbacks
and re-runs free. Decision parity with per-subscription judging is verified on
real posts by `scripts/shadow_union.py`.

### Scheduling and cursors

- `sync_watches()` materializes the union of source channels of all enabled
  subscriptions into the `watches` table (idempotent; a new channel's cursor
  starts at the minimum cursor among its subscriptions).
- A repeating job ticks every 60 s, materializes, prunes expired judgments and
  picks `due_watches()` — channels not fetched within the single global refresh
  interval (admin-adjustable via `/admin interval <minutes>`).
- Each due channel runs as its own task; `_running` prevents overlap per
  channel. All other users proceed in parallel.
- Two cursors decouple timing: the **fetch cursor** (per channel) follows new
  posts; each subscription's **consume cursor** (per source) decides where that
  subscription starts reading, so a new subscription backfills its first window
  and existing ones never double-read.

## Storage

Single SQLite file (`DB_PATH`), WAL-adjacent single-writer access through a
lock in `store.py`. Tables:

| Table | Contents |
| --- | --- |
| `users` | status (active/blocked), per-user caps/quotas, note, UI language |
| `chats` | known channels/groups with owner (`added_by`) — the basis of destination validation |
| `subscriptions` | template JSON, sources + consume cursors, destinations, enabled flag |
| `watches` | one row per source channel: fetch cursor, last fetch |
| `judgments` | (channel, post_id, template fingerprint) → answers JSON, `created_at` (7-day TTL) |
| `logs` | operational events (`delivered`, `classify_failed`, `delivery_error`, `quota_exhausted`, …) |
| `usage` | one row per metered item: kind, user, sub, counts, real input/output tokens, detail |
| `settings` | key/value instance settings (e.g. `default_lang`) |

Schema evolution is handled by idempotent migrations at startup.

## Delivery and formatting

- `Sender.send()` retries on Telegram rate limits with backoff; `Forbidden`
  ("bot not in the channel / user never started it") and `BadRequest` are
  mapped to a per-destination failure so one broken destination never blocks
  the others.
- Each matching source post is sent as one Telegram message: post text, newline,
  linked original and source; no combined digest or per-item prefixes. Oversized
  posts are clipped to the message budget and retain a link to the full post.
- Dry-runs (`/test`) use the same one-post-per-message format with a `🧪 sample`
  marker on each example; they neither advance cursors nor count as deliveries.

## Internationalization

- `i18n.py` holds two complete copy tables (`en`, `zh`) plus `t(lang, key, **fmt)`
  with English fallback; `resolve()` normalizes locale codes.
- Language resolution order: personal choice (`users.lang`, set by `/lang`) →
  instance setting (`settings.default_lang`, set by `/admin lang`) →
  `DEFAULT_LANG` from `.env` → `en`.
- Bot command menus are registered per language scope in `post_init`; admins
  additionally get `/admin` in their own chat scope (both languages).
- Templates, logs and internal diagnostics stay English; only the bot UI is
  localized.

## Security & isolation

- Subscriptions, destinations and channel selections are validated per owner:
  a user can only pick a channel they themselves added the bot to, and only
  edit their own subscriptions (`/list`, `/test` are private-chat only).
- Destination re-validation happens at selection time (the user must still be
  a channel admin) and delivery failures degrade per destination.
- Admin commands are limited to `ADMIN_USER_IDS`, private chat only.
- Fetching is white-hat only: official Bot API + public previews; no userbot,
  no private channels, no member list scraping.
- Secrets live in `.env` only; nothing sensitive is written to the database.

## Metering & quotas

- Every execution path (including dry-runs) writes `usage` rows with the real
  token counts returned by the APIs.
- Channel-level shared costs (fetch, jev) are recorded under `user_id=0` with
  the channel name as detail; personal consumption is recorded as `consumed`
  (deduplicated across a user's subscriptions) and is the basis of monthly
  quotas.
- When a user's monthly quota is exhausted mid-round, the round finishes (the
  judgments were already paid for) and the user's subscriptions are paused with
  a notice; nothing is silently overspent.

## Failure handling

- Jev: 429/529 → exponential backoff; per-post failures are counted, logged
  (`classify_failed`) and skipped without blocking the rest of the batch.
- Fetch: errors record `last_fetch_at` (no per-minute retry storms) and notify
  admins at most once per 6 h per channel.
- Delivery: per-destination errors are collected into the round result; admins
  get a throttled notification.
- Startup cleanly rebuilds state from SQLite; there is no in-memory state that
  matters across restarts.
