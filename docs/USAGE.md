# Usage Guide

Everything beyond the README's quick start: subscription mechanics, template format,
admin commands, metering, and constraints.

## Subscriptions: n sources → m destinations

- One subscription bundles several source channels + one filter template + one or
  more destinations (your DM and/or channels you administer).
- Each source keeps its own **consume cursor**: a new subscription backfills from
  the moment it is created, existing ones never double-read.
- Refresh cadence is one global schedule for every source channel; subscriptions
  do not configure intervals. Admins set it with `/admin interval <minutes>`.

## Templates (JSON)

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

- `noul` returns a 0–1 probability; `score` a 0-based grade position; `choice` an
  option name.
- `op` supports `>=` `<=` `==` `in` `not_in`; `logic` supports `all` / `any`.
- Question ids are lowercase English; `title` is display-only and is never sent to Jev.
- Templates are generated in English (Jev's models are English-first — the official
  Models page notes lower accuracy for non-English); the posts being filtered can be
  in any language.
- The wizard's description box also accepts a raw JSON template (bypasses the LLM).

## Wizard & editing

- Send channel references one by one (`@name`, `t.me/name`, or a post link), then
  tap "✅ Done". Each source starts from the newest post at the time it was added.
- Template confirmation offers "✅ Use" / "✏️ Redescribe" / "🔧 Ask AI to adjust"
  (recompiles with the previous template + your feedback).
- Destinations: "📬 Add DM" / "📢 Add channel" (add the bot as channel admin first,
  then "🔄 Refresh channel list"). Sources and destinations can never be emptied.
- **Editing**: `/list` → pick an entry → "✏️ Edit" → change 📡 sources, 📬
  destinations, or the 🧩 template independently; every change saves immediately.
- **Dry-runs** (`/test` or the /list button) deliver a 🧪-marked sample to the real
  destinations: no cursor advance, not counted as a delivery — verify pushes exactly
  where they will land.

## Configuration

- Every variable is self-documented in [`.env.example`](../.env.example). The ones
  that matter: `BOT_TOKEN`, `TYPESAFE_API_KEY`, optional `OPENAI_*` (template
  compiler), `ADMIN_USER_IDS`, `DEFAULT_LANG`.
- The compiler talks to any OpenAI-compatible endpoint — provider examples are in
  `.env.example`. Without a key, the wizard only accepts raw JSON templates.
- Endpoints that reject JSON mode or `temperature` are handled automatically
  (parameter degradation + retry).

## Admin console

For ids in `ADMIN_USER_IDS`, private chat only (`/admin` appears in the admin's own
command menu):

| Command | What it does |
| --- | --- |
| `/admin` | Overview: users/subscriptions, usage windows, 30-day top 5 |
| `/admin users [n]` | User list with status, subscription count, 30-day usage |
| `/admin user <id>` | Detail: quota, month-to-date consumption + tokens, subscriptions, recent events |
| `/admin usage [days]` | Per-user usage summary (default 30 days) |
| `/admin watches` | Channel refresh schedule: cursor / watchers / last fetch |
| `/admin interval <minutes>` | Set the global refresh interval for all channels (1–1440) |
| `/admin lang <en\|zh>` | Instance default UI language |
| `/admin block <id>` / `unblock <id>` | Suspend / restore (auto-pauses subscriptions, notifies the user) |
| `/admin quota <id> sub <n>` | Subscription cap (0 = default 20) |
| `/admin quota <id> jev <n>` | Monthly judgment quota in consumed posts (0 = unlimited) |
| `/admin note <id> <text>` | Operator note |

- Blocked users get refusals for `/start /new /list /test` and can do nothing else.
- `DEFAULT_USER_STATUS=blocked` turns the instance invite-only; admit users with
  `/admin unblock`.
- Quotas reset monthly (UTC); after raising a quota the user can resume
  subscriptions from `/list`.

## Metering & quotas

Every execution path (dry-runs included) writes a `usage` row with the **real
token counts returned by the APIs**:

| kind | counts | tokens |
| --- | --- | --- |
| `jev` | Jev judgment calls, channel-shared (recorded under `user_id=0` + channel name; 1 = one post's union judgment) | ✅ per call |
| `consumed` | posts actually consumed by the user (deduplicated across their subscriptions) — the basis of monthly quotas | — |
| `llm` | template-compiler calls (including auto-fix retries) | ✅ |
| `fetch` | channel fetches (channel-shared) | — |
| `deliver` | delivered messages | — |

- Judgment cost is a channel-level shared cost; personal quotas count `consumed` —
  fair and currency-agnostic.
- Token counts come from the API responses (Jev `usage.*`; the LLM side accepts both
  `prompt_tokens/completion_tokens` and `input_tokens/output_tokens` naming) — not
  estimates, so costs can be computed with each provider's unit prices.
- Quota exhaustion pauses the user's subscriptions (never silently overspends);
  admins see the shared rows as "🛰 channel-shared".

## Design constraints (deliberate)

- **No conversational replies** — status and results only; unknown text/commands get
  a fixed hint.
- **Implicit union judging** — one judgment per post serves every subscription on the
  channel (union questions + cache + per-template projection), across users; users
  neither see nor configure it.
- **Isolation** — subscriptions and destinations are verified per owner; a channel
  can only be selected by the user who added the bot to it; `/list` and `/test` are
  private-chat only.
- **Concurrency** — up to 12 updates in parallel globally, strictly serial per chat;
  the send path retries with backoff on Telegram rate limits.
- **White-hat fetching** — only `t.me/s/` previews; channels without previews cannot
  be fetched (the wizard tells you immediately).
- **Sliding window** — public previews usually keep ~250k recent posts; a long outage
  may drop older ones.
- **Copy-based delivery** — posts are re-published as "text + link" copies (native
  forwarding is impossible — the bot has no rights over source channels).

## Testing

```bash
pytest -q                                    # offline unit tests
python scripts/smoke_live.py <channel>       # real network smoke (t.me + Jev), no bot token needed
python scripts/shadow_union.py <channel> [n] # union vs per-subscription parity & savings on real posts
python scripts/e2e_wizard.py <user_id>       # wizard end-to-end through a real bot (delivers to DM)
python scripts/e2e_commands.py <user_id>     # full command/wizard/button/channel-event coverage
```
