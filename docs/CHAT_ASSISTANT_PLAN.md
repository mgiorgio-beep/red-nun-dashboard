# Dashboard chat assistant — plan

2026-09-26. Status: **plan, nothing built.** Decisions marked ❓ need Mike.

## What it is

A "Ask Red Nun" chat panel on every Management App page. Type a question in
plain English ("why is Dennis food cost up this quarter?", "what do we owe
Martignetti?", "which Chatham checks are still outstanding?") and get an answer
built from the live database, with the numbers linked back to the page they
came from.

It is the Telegram bot (`bot/bot.py`, "Jarvis") brought into the browser, with
the dashboard's own accounting engines behind it.

## Ground rules it must keep (from CLAUDE.md)

- **Rule 11, no silent API spend** — every Anthropic call is a message the user
  typed and sent. No background summaries, no auto-greeting, no "insights" on
  page load.
- **Rule 9, two gunicorn workers** — conversation state lives in the DB, never
  in a Python dict (the bot's `_CONVOS` dict is fine there, not here).
- **Rule 1, `@login_required`** on every route.
- Numbers come from the same engines the pages use (`build_profit_loss`,
  `_reconciliation_state`, `register_flow`, …) — the assistant never writes its
  own SQL sums for figures a page already shows, so chat and screen can't disagree.

## Architecture

```
sidebar.js ──injects──> chat panel (every page; floating button, slide-out)
                            │  POST /api/assistant/chat  (SSE stream)
                            ▼
routes/assistant_routes.py  (assistant_bp, @login_required)
    ├─ loads conversation from DB, appends user turn
    ├─ Anthropic tool-use loop (model from ASSISTANT_MODEL env)
    │     tools ──> ai/assistant_tools.py  (shared with bot/bot.py)
    └─ streams text + "cards" back, saves turns + tool calls to DB
```

- **Panel UI** — built by `sidebar.js` (Rule 10: nav/chrome lives there), so it
  lands on every page from one place. Desktop: right-side drawer, 420px.
  Mobile: full-screen sheet. Dark theme tokens from the design system.
- **Page context** — the panel sends the page it's on, plus location and period
  if the page has them (P&L sends its start/end). "Why is this number high?" on
  the P&L then means *this* P&L.
- **Streaming** — Server-Sent Events so the answer types out; tool calls show
  as small "Looking up bank register…" lines.
- **Answers carry links** — a tool result can include a `link`
  (e.g. `/static/profit_loss.html?location=dennis&period=ytd`,
  `/bank-reconcile?period=…`); the panel renders them as buttons.

### New tables

- `assistant_conversations` (id, user_id, title, created_at, updated_at)
- `assistant_messages` (id, conversation_id, role, content_json, tokens_in,
  tokens_out, cost_usd, created_at)
- `assistant_tool_calls` (id, message_id, tool, args_json, ok, ms) — audit trail

### Shared tools (`ai/assistant_tools.py`)

Move the bot's tool definitions + `execute_tool` into a shared module; the bot
imports it (keeps its Telegram-only tools: server restart, SSH diagnostics,
thermostat), the web assistant gets a curated list.

**Phase 1 — read only**

| Tool | Backed by |
|---|---|
| `get_pl(location, start, end)` | `reports.profit_loss.build_profit_loss` (with footnotes) |
| `drill_pl_line(...)` | `reports.profit_loss.drill` |
| `get_daily_sales` / `get_labor_summary` | existing bot tools / `reports/analytics.py` |
| `search_invoices(vendor, dates, amount, number)` | `scanned_invoices` + items |
| `vendor_balance(vendor, location)` | AP / Bill Pay |
| `search_register(text, amount, dates, account)` | bank register rows |
| `reconcile_status(account, period)` | `_reconciliation_state` (signed? delta? outstanding list) |
| `sales_journal_status(location, dates)` | `qb_journal_entries` (ready / posted / needs_attention) |
| `food_cost` / `recipe_cost(recipe)` | existing bot tools, `recipe_costing` |
| `open_items()` | existing bot tool |

**Phase 3 — actions, always behind a confirm card**

The model *proposes*; the panel shows a card ("Code 3 Cintas lines to Linens —
$542.30 — Confirm / Cancel"); only the Confirm click executes, through the same
route the page button uses. Candidates: code a bank line, pull a bill from the
inbox (`ingest_bill`), mark an invoice paid, generate a sales JE.
**Never from chat:** QBO pushes, bank sign-off, deleting anything, service
restarts, SSH.

## Cost control

- Model from `ASSISTANT_MODEL` env; default Sonnet (fast, cheap), switch per
  message to Opus with a "think harder" toggle.
- Prompt caching on the system prompt + tool list (the big, fixed part).
- Hard caps: max 8 tool rounds per message, `max_tokens` 2048 per reply,
  history trimmed to the last ~20 turns (clean user-turn cut, as the bot does).
- Tokens and $ stored per message; a small "today: $0.42" in the panel footer
  and a daily cap ❓ that disables the panel until tomorrow when hit.

## Phases

1. **Read-only assistant, admin only (~2 sessions).** Shared tools module,
   blueprint + tables, SSE, panel in `sidebar.js`, page context, links,
   spend meter. Test: 20 real questions Mike asks, each answer checked against
   the page it cites.
2. **Conversation list + search (~½ session).** Past chats in the panel;
   rename/delete.
3. **Confirmed actions (~1–2 sessions).** Confirm-card mechanism + the first
   3–4 actions above, each with an audit row.
4. **Manager access (~1 session).** Location-scoped users (Matt → Chatham,
   Alexis → Dennis) see only their location's data; no accounting tools.
5. **Retire overlap.** Point Telegram and the web panel at the one tools module
   so a fix lands in both.

## Decisions for Mike ❓

1. **Who gets it first** — just you (admin), or managers too?
2. **Daily spend cap** — suggest $5/day to start.
3. **Actions** — which write actions do you actually want from chat (phase 3)?
4. **Model** — Sonnet by default with an Opus toggle, OK?
5. **Telegram** — keep Jarvis running alongside, sharing the same tools?
