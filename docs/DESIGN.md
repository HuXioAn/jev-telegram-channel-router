# tg-filter-bot — Plan (PLAN)

> Standalone Telegram Bot: subscribe to any public channel → Jev semantic filtering → matching messages routed to DM / channel.
> This document records the design decisions; the implementation lives in `src/tgfilter/`.

## 1. Goals / Non-goals

**Goals**
- Every user can customize: source channels (any public channel), filter criteria (a natural-language description → compiled by an LLM into a reusable Jev template), and routing destinations (their own DM / channels they administer).
- Integrate Jev (TypeSafe System One) for per-message judging; integrate any OpenAI-compatible LLM solely for the "natural language → Jev template" compilation step.
- Complete, usable, maintainable: a clear engineering structure, unit tests, an elegant and concise implementation.

**Non-goals (explicitly out of scope for v1)**
- No natural-language conversational interaction: the Bot is entirely command/button-driven with fixed copy; the LLM only compiles templates and never produces user-visible free-form text.
- Do not read private channels, do not use userbot/MTProto (compliance boundary, see §9).
- No complex agent loop; LLM calls are single-turn structured input/output (at most one corrective retry).

## 2. Technology Choices

| Decision | Choice | Rationale |
| --- | --- | --- |
| Bot framework | **python-telegram-bot (PTB) v21+** | The most mainstream Python TG library; built-in JobQueue scheduling, ConversationHandler wizards, complete typing |
| HTTP | **httpx (AsyncClient)** | One library covers all three call sites — t.me scraping + Jev + LLM; supports MockTransport (test-friendly) |
| Data | **SQLite (stdlib sqlite3, WAL)** | Single file, zero ops; few entities (users/subscriptions/channels), no ORM needed |
| Validation | **pydantic v2** | Template schema, LLM output validation, config models |
| Scheduling | PTB **JobQueue**, polling due subscriptions every 60s | Adding/removing or pausing/resuming subscriptions needs no timer bookkeeping; missed runs are caught up automatically |
| Testing | pytest + pytest-asyncio + httpx.MockTransport | Core logic is fully testable without a bot token |

## 3. Architecture

```
Telegram user ──/new wizard──┐
                             ▼
                    ┌─────────────┐  natural-language      ┌────────────────┐
                    │ Bot layer   │  description           │ LLM template   │
                    │ (handlers)  │ ─────────────────────▶ │ compiler       │
                    └──────┬──────┘ ◀───────────────────── │ (OpenAI-       │
                           │        Jev template (JSON)    │  compatible)   │
                           │                               └────────────────┘
                           │ save subscription (SQLite)
                           ▼
          ┌───────────────────────────────────────────────┐
          │ Scheduler: JobQueue scans "due subscriptions" │
          │ every 60s                                     │
          └───────────────────────┬───────────────────────┘
                                  ▼
   Fetcher (t.me/s preview) ─▶ Jev classifier (per message, concurrent) ─▶ rule matching
                                                     ─▶ summary synthesis ─▶ delivery layer
                                                                             (DM / channel)
```

## 4. Module Breakdown

| Module | Responsibility |
| --- | --- |
| `config.py` | Environment-variable loading (BOT_TOKEN / TYPESAFE_API_KEY / OPENAI_* / DB_PATH…) |
| `models.py` | Domain models: Post, Jev template (Question/Condition/Template) and `evaluate()` rule evaluation |
| `channel_fetch.py` | Public-channel web-preview fetching: channel reference normalization, `head()`, `fetch_since()`, HTML parsing |
| `jev.py` | Jev SystemOne client: single/batch classification, concurrency semaphore + 429/529 backoff retry |
| `llm.py` | Template compiler: description (+ revision feedback) → LLM call → JSON → pydantic validation (one retry on failure) |
| `formatting.py` | Template summary (for user confirmation), summary-message synthesis and Telegram chunking |
| `db.py` | SQLite access: users / chats (channels where the bot is an admin) / subscriptions / logs |
| `pipeline.py` | One run of a single subscription: fetch → classify → match → synthesize → deliver → advance cursor |
| `delivery.py` | Sending: DM / channel; error classification (blocked by user, no permission…) |
| `bot/` | PTB layer: `app.py` (build/schedule), `handlers.py` (commands and wizards), `keyboards.py`, `messages.py` (all copy) |
| `scripts/smoke.py` | Real smoke test with no bot token required: really scrapes t.me + really calls Jev |

## 5. Data Model (SQLite)

- `users(id PK, username, status, max_subs, quota_jev_monthly, note, created_at)`
- `chats(chat_id PK, kind, title, added_by, added_at)` — channels/groups the bot has been added to and can post in (from `my_chat_member` updates)
- `subscriptions(id PK, user_id, template_json, enabled, last_run_at, created_at)` — the subscription itself (**n sources → m destinations**)
- `sub_sources(id PK, sub_id, source, last_seen_id, UNIQUE(sub_id, source))` — source channels; each has its own cursor, advanced independently
- `sub_dests(id PK, sub_id, kind[dm|channel], chat_id, title, UNIQUE(sub_id, chat_id))` — destination list (DMs/channels can be mixed)
- `logs(id PK, sub_id, ts, kind, detail)` — run records/delivery results (debugging and auditing)
- `usage(id PK, ts, user_id, sub_id, kind, qty, input_tokens, output_tokens, detail)` — usage (counts + real tokens)

Old databases (single-source, single-destination schema) are migrated automatically at startup: `subscriptions` is rebuilt and the data is split into `sub_sources` / `sub_dests`.

## 6. User Flow (/new wizard + subscription editing)

1. `/new` → send a channel (`@name` / `t.me/name` / `t.me/s/name`) → verify that the preview is reachable; **you can keep sending more channels**, then tap "✅ Done" to move on
2. Send a one-sentence natural-language filter description (e.g. "major China-related news, excluding entertainment gossip") → LLM compiles → template summary → [✅ Use / ✏️ Describe again / 🔧 Let AI adjust]
3. Destination **multi-select**: 📬 DM / channel list (requires the bot to be a channel admin and the channel to have been added by the user); add and remove repeatedly, keeping at least one
4. Choose a frequency (10/20/30/60 minutes, default 20)
5. Save the subscription; each source channel's cursor is initialized to that channel's current head (only new messages are pushed); a hint notes that `/test` can be used for a trial run

**Editing**: `/list` → any subscription → "✏️ Edit" → 📡 source channels (add/remove) | 📬 destinations (add/remove) | ⏱ frequency | 🧩 filter template (re-describe → compile → confirm to save). All changes are written to the DB immediately; neither sources nor destinations may be emptied.

Other commands: `/list` (subscription management: pause/resume/trial run/edit/delete), `/test <id>` (trial run: pull the latest ~100 messages, send samples to the subscription's targets), `/cancel`, `/help`.

## 7. Jev Template Schema (LLM output = runtime input)

```json
{
  "name": "China major news",
  "questions": {
    "china":      {"type": "noul",  "title": "relevant", "instructions": "…is it directly related to China?", "criteria": {"true": "…", "false": "…"}},
    "importance": {"type": "score", "title": "important", "instructions": "…how important is it?", "criteria": ["routine", "normal", "fairly high", "major"]}
  },
  "match": {"logic": "all", "conditions": [
    {"question": "china", "op": ">=", "value": 0.7},
    {"question": "importance", "op": ">=", "value": 1.8}
  ]}
}
```

- Question-answer primitives: `noul` (yes/no probability), `choice` (mutually exclusive options), `score` (ordered levels weighted into a score).
- Match predicates: `all/any` × `>= <= == in not_in`.
- Questions must be self-contained (Jev has no external context); `title` is used for display in the template summary (the confirmation copy shown to the user; push bodies no longer carry a judgment-value prefix).

## 8. LLM Compiler (single-turn, structured)

- System prompt pre-loaded with: schema documentation + Jev question design rules (self-contained, binarized, concrete criteria, language follows the user).
- Output forced to JSON (prefer `response_format=json_object`; automatically degrade if the endpoint does not support it); strip ``` fences.
- Validation failure: feed the pydantic error message back to the model and retry **once**; if it still fails, report an error to the user.
- When the user pastes a JSON template directly (Power Mode), skip the LLM and validate and enable it as-is.

## 9. Limitations and Compliance (must be communicated to users)

- **Public channels only**; fetching goes through the `t.me/s/` web preview (the official public interface), with polite, rate-limited scraping.
- Channel previews may be disabled by the official platform (black box); the preview retention window is limited (roughly the latest 250,000 messages for large channels, sliding), so long outages can miss messages.
- Private channels / userbots are never supported (ToS violation).
- Sending to a channel requires the bot to be an **admin**; DM requires that the user has started the bot.

## 10. Test Strategy

- Unit: template evaluation, HTML parsing and reference normalization, Jev client (MockTransport: 200/429/error), LLM compiler (including the repair retry), summary synthesis and chunking, DB CRUD and due-scan, full pipeline flow (the fake trio).
- Smoke (no bot token required): really scrape a public channel + really call Jev (`scripts/smoke.py`).
- Keep the Bot interaction layer (handlers) thin: state lives in `user_data`, all copy is centralized in `messages.py`, logic lives in testable core modules.

## 11. Milestones

1. Scaffolding + plan (this document)
2. Core modules (fetch / jev / llm / models / formatting / db / pipeline / delivery)
3. Bot layer (wizard, subscription management, scheduling)
4. All tests green + smoke passing
5. (Pending bot token) real integration: /start, wizard, first push, channel delivery

## 12. Union Judging and Channel-Level Scheduling (union judging, 2026-09-21 design)

### Background
- Old model: each subscription fetches independently and judges independently. The same message in the same channel is judged once per template,
  and every subscription re-fetches the same channel.
- Measured (2026-09-21, 4 subscriptions all watching the financial news digest channel): 1,839 judgments in one day, 1.51M input tokens,
  the vast majority of which were the same batch of messages × different templates judged repeatedly.
- Jev judgments are issued together with the "question set" and results cannot be reused directly across templates; but you can "judge the union of questions once and project per template".
  A/B measurement (6 real messages × 4 templates): calls 4→1, input −34%, matching conclusions 24/24 identical;
  the model's own rerun noise is ±0.01, so merging adds no extra distortion.

### Goals
- A given message (regardless of how many subscriptions or users watch it) results in exactly one Jev call; results are cached and routed per template.
- Implicit to users (no perceived change other than the established product change "per-item frequency configuration is removed").
- Low coupling, high cohesion: a new standalone module; minimal footprint on existing code.

### Core Model: Two-Level Cursors
1) Watch (channel-level, shared across users)
   - `watches(channel PK, last_seen_id, last_fetch_at)`
   - The set = the union of the source channels of all enabled subscriptions; idempotently materialized on each tick (`sync_watches`); retired once it has no watchers.
   - Refresh interval: one global interval for all channels (`settings.fetch_interval_minutes`, boot default `DEFAULT_INTERVAL_MINUTES`); admins change it at runtime with `/admin interval <minutes>`.
   - `last_seen_id` is the "fetch cursor": how far that channel has been fetched and judged.
2) Route (subscription-level, private)
   - `sub_sources.last_seen_id` is repurposed semantically as the "consumption cursor": this subscription's consumption/delivery progress for that channel's messages.

Message lifecycle (per channel refresh):
Fetch new messages (watch cursor) → idempotent judging: for messages not yet judged, call Jev
once with the "union of the questions of all active templates on that channel"; answers are projected per template into the `judgments`
cache → for each watching subscription: take new messages according to the consumption cursor,
evaluate the cached answers with its own template → on a match, synthesize a summary and deliver →
advance the consumption cursor; finally advance the watch cursor.

### Key Design Decisions
- Time decoupling: judging cadence = channel-level; delivery = event-driven (routed as soon as a batch arrives); subscriptions no longer have their own clock,
  and the "multiple items whose times are out of sync" problem is gone as well.
- Cache: `judgments(channel, post_id, payload, ts)`, payload={template fingerprint: {local question: answer}};
  reads need no union metadata. Check the cache before judging → reruns/multiple subscriptions are naturally idempotent.
- Template fingerprint: hash of the normalized (sorted keys) **question set** payload; changing match rules/name does not affect the cache; editing questions → new fingerprint → takes effect only for new messages (same as the status quo).
- Question-level dedup: questions with completely identical payloads across different templates are asked only once; the question id = hash of the question payload.
- Union cap: when the number of questions in a single call exceeds `judge_max_questions` (default 24), split into multiple calls grouped by template
  (still far fewer than N×).
- Failure semantics (same as the status quo): a failed judgment is skipped + logged, and the cursor advances as usual; channel-level failures notify the admin.
- Quota accounting: user quotas change to "messages consumed" (the number of judged messages the user's subscriptions actually consumed, deduplicated by message);
  real cost (calls/tokens) is recorded in usage at the channel level (user_id=0, detail=channel).
- Trial runs (/test) unchanged: still judged independently with that subscription's template (no cache writes).

### Data Model Changes
- Add two tables, `watches` and `judgments` (judgments are periodically cleaned up with a 7-day TTL).
- Migration: materialize watches (cursor = the minimum of that channel's subscription cursors → guarantees no messages are lost);
  the per-channel / per-subscription `interval_minutes` columns are dropped — one global interval replaces them (`settings.fetch_interval_minutes`).
- usage gains kind=`consumed` (messages consumed by the user); channel-level jev rows use user_id=0.

### Module Breakdown
- Add `judging.py`: fingerprints, union planning (dedup/sharding), ensure_judged (cache + call + projection persisted).
- `pipeline.py`: `run()` → `run_watch()` (fetch→judge→route); `preview()` unchanged.
- `bot/app.py`: the tick scans due watches; the concurrency key = channel.
- `bot/`: the wizard drops the frequency step; the edit menu drops the frequency item; lists/details drop the interval display;
  `/admin` gains watches management.
- `config.py`: `default_interval_minutes=20` (boot default for the global refresh interval; `/admin interval` overrides it at runtime), `judge_max_questions=24`.

### User-Visible Changes
- `/new` no longer asks for a frequency; items no longer show "every N minutes"; the edit menu drops "⏱ Frequency".
- Push timeliness follows the single global refresh interval (admin-configurable with `/admin interval`).
- Filtering, destinations, trial runs, push format, and the rest of subscription management are completely unchanged; the merge itself is invisible to users.

### Test Strategy
- judging unit tests: fingerprint stability / question dedup / sharding / cache hits / correct projection;
  key assertion: K templates watching one channel → Jev is called exactly once per message.
- pipeline unit tests: multiple subscriptions on the same channel routing independently (cursors/matches independent), multi-destination delivery, idempotent reruns,
  catch-up delivery for lagging subscriptions, quota consumption and auto-pause.
- store unit tests: watches materialization/retirement/expiry, judgments read/write and cleanup, lossless migration of old databases.
- Full interaction regression for the wiring smoke test; e2e scripts updated in sync (drop the iv: step).
- `scripts/shadow_union.py`: compare "merged vs separate" on real traffic (consistency + usage).
