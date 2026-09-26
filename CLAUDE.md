# Red Nun Dashboard — Claude Code Guide

## ⚠️ REPO & WORKFLOW — READ THIS FIRST

### The one repo that matters
```
GitHub:  https://github.com/mgiorgio-beep/red-nun-dashboard
Local:   C:\Users\giorg\code\red-nun-dashboard    (Windows working copy — plain NTFS, NOT in Google Drive)
Docs:    G:\My Drive\Red NUn Dashboard           (documents/data ONLY — NO git operations here, see below)
Server:  /opt/red-nun-dashboard                   (BOTH Beelinks — Chatham SER5 + Dennis ME Mini)
Live at: https://dashboard.rednun.com             (Chatham, full dashboard)
         https://dennis.rednun.com/staff          (Dennis, staff/TV app only)
```
Both Beelinks pull from the same repo. They run **different services** and use **machine-local config** so they stay fully independent (see "Two Beelinks — Keep Them Separate" below).

### SSH to servers
```
Chatham:  ssh -p 2222 rednun@ssh.rednun.com        # local IP 10.1.10.83
Dennis:   ssh -p 2222 rednun@10.1.10.84            # on-site
          ssh -p 2222 rednun@ssh-dennis.rednun.com # remote (Cloudflare tunnel, IP-proof)
```

### ⚠️ Git moved OUT of Google Drive (2026-07-23)
The Windows working copy used to live at `G:\My Drive\Red NUn Dashboard`. That caused two days of
constant failures: Google Drive holds file locks while git works (hanging `git status`/`commit`/`stash`,
stale `index.lock`), and the Beelink `rclone bisync` silently REVERTED working-tree files to stale copies
after a rebase. Rules now:
- **ALL git work happens in `C:\Users\giorg\code\red-nun-dashboard`.** Never run git inside the Drive folder.
- The Drive folder keeps documents, ledgers, and data (MASTER_PLAN.md, briefs, email_ledger, exports).
  Its leftover `.git` is retired — do not commit or push from it.
- **Never trust a file in the Drive folder to match GitHub** — bisync can serve stale copies. Base all
  code edits on the C: working copy (or a fresh GitHub read), not on Drive contents.
- If git ever must touch a Drive-synced folder (e.g. vendor-scrapers, still at
  `G:\My Drive\Red NUn Dashboard\vendor-scrapers` pending the same migration): PAUSE Google Drive
  syncing first (tray icon → gear → Pause), resume after.

### Deploy workflow
```
1. Edit locally in C:\Users\giorg\code\red-nun-dashboard
2. git add <exact paths> / git commit / git push
3. Chatham: cd /opt/red-nun-dashboard && git pull && sudo systemctl restart rednun
4. Dennis:  cd /opt/red-nun-dashboard && git pull && sudo systemctl restart rednun-staff
```
Restart the service that runs on each box: `rednun` (Chatham, full dashboard) vs
`rednun-staff` (Dennis, staff/TV app). A shared-code change usually means pulling on both.

### Auto-deploy timer (Chatham)
`rednun-autodeploy.timer` (→ `rednun-autodeploy.service`, script at `/usr/local/bin/rednun-autodeploy.sh`, **outside the repo**) runs every 2 min as root: `git fetch`, and if local `main` is behind `origin/main` **and the working tree is clean**, it does `git pull --ff-only` then restarts `rednun.service` (skipping the restart when the pull only touched `docs/` or `*.md`). It never forces and never stashes — a dirty tree, diverged history, or failed pull just logs to the journal (`journalctl -u rednun-autodeploy`) and does nothing.

**Claude Code running on this server MAY edit tracked files, but must `git commit` AND `git push` them the same session so the tree stays clean — the auto-deploy timer skips pulls whenever the tree is dirty.** Always check `git status` / `git diff` first; if you can't push from this box, surface the diff for review instead of leaving uncommitted edits behind.

### Cowork agent — push, don't pull
`rednun-autodeploy.timer` is the ONLY thing that deploys to `/opt` on Chatham. The Cowork agent (commits authored "Red Nun Cowork") must NOT run `git pull`, `git stash`, or any auto-stash against `/opt/red-nun-dashboard` — it races the timer and has silently orphaned uncommitted work before (an April auto-stash sat lost until rescued). Cowork's job ends at `git push` to GitHub; the timer fast-forwards `/opt` within 2 min (no stash, no force). The Drive↔Beelink `rclone bisync` cron is unaffected — it only syncs `~/cowork/red-nun-dashboard`, never `/opt` or git.

---

## What This Is
Custom restaurant operations dashboard replacing MarginEdge ($363/mo).
Consolidates POS, labor, accounting, vendor invoices, and TV displays into a single Flask app.
Two locations: Dennis Port & Chatham, Cape Cod, MA.

## App Naming
- **Management App** — Main dashboard (`dashboard.rednun.com`). Invoices, sales analytics, bill pay, product setup, recipes, menu analysis, inventory. Blueprints registered in `web/server.py`, frontend in `web/static/*.html`.
- **Staff App** — Staff-facing side (`dashboard.rednun.com/staff`). Specials board (TV display + editor), Sonos, venue watchdog, PWA. Self-contained blueprint in `staff/staff.py`.
- **TV Control App** — **Separate from Staff App.** Controls Roku TVs and DirecTV boxes in Chatham. Lives at `/opt/tv_control/`, runs as `rednun-tv.service` on port 5000. See "TV Control App" section below. (Specials Board is served by the Staff App, not this one.)

## Tech Stack
- **Server:** Beelink SER5, Chatham. SSH: `ssh -p 2222 rednun@ssh.rednun.com`. Local: 10.1.10.83.
- **Backend:** Python / Flask / Gunicorn (port 8080, workers=2) / Nginx. Service: `rednun.service` runs `gunicorn -w 2 -b 0.0.0.0:8080 web.server:app`.
- **Bot service:** `rednun-agent.service` runs `python bot/bot.py` (Telegram bot using Anthropic SDK).
- **Database:** SQLite WAL mode. Path: `/var/lib/rednun/toast_data.db`. ~1.8GB.
  - ⚠️ **The env var is `DB_PATH`, NOT `TOAST_DB_PATH`.** `integrations/toast/data_store.py`
    reads `os.getenv("DB_PATH", "toast_data.db")`. This file previously documented
    `TOAST_DB_PATH`; setting that name is silently ignored, which on 2026-08-27 sent a
    test run's writes straight into the live database (repaired from backup).
  - ⚠️ **Never `cp` the live DB.** It is 1.8GB in WAL mode and actively written, so `cp`
    yields a TORN snapshot that silently disagrees with live. Use
    `sqlite3 /var/lib/rednun/toast_data.db ".backup /tmp/x.db"`.
- **Frontend:** Vanilla HTML/JS/CSS, dark theme (#020617 bg).
- **AI/OCR:** Anthropic Claude API (claude-sonnet-4, max_tokens 16384). Used for invoice OCR and inventory vision.
- **Data Sources:** Toast POS, 7shifts, QuickBooks Online, Honeywell.
- **Sonos Amp:** 10.1.10.242 (Dining Room), controlled via `soco`.
- **Two Fire TVs (Chatham, bar + dining):** Load specials from `http://10.1.10.83:8080/staff/specials/tv`.

## Repo Layout
```
web/                Flask app entry (server.py), templates, static
routes/             Flask blueprints (auth, invoices, billpay, catalog, products, ...)
bot/                Telegram bot (bot.py)
ai/                 Inventory AI (audio, vision, reconcile) + pmix matcher
reports/            analytics, audit_dashboard, forecast, invoice_anomaly, pour_cost
staff/              Staff app (specials editor, TV power, Sonos, watchdog)
monitoring/         Server health checks, ddns updater
scraping/           Sports guide (Fanzo, ESPN, odds fetchers)
integrations/
  toast/            toast_client, sync, data_store
  sevenshifts/      sevenshifts_client
  quickbooks/       qb_*.py, payroll, JE push, check printing assets
  invoices/         processor + watchers/{drive, local, email_invoice, email}
  google/           gmail_auth, auth_drive
  recipes/          recipe_costing, recipe_autopopulate
  vendors/          vendor_item_matcher
  thermostat/       thermostat, thermostat_fetch
  sonos/            (Sonos integration via SoCo)
scripts/archive/    One-off historical scripts (fix_, patch_, deploy_, migrate_, etc.)
deploy/             deploy.sh, deploy_sports.sh, deploy_invoices.sh
docs/               PROJECT_BRIEF.md, session summaries, briefs
data/               schema_v2.sql (rest of data/ is gitignored runtime state)
tests/              test_ai_inventory
```

## Key Files
- `web/server.py` — Main Flask app, blueprint registration, core routes
- `web/static/manage.html` — Main dashboard SPA (product management, ~8,449 lines)
- `web/static/sidebar.js` — Dynamic sidebar builder (edit this to change nav, NOT manage.html)
- `web/static/invoices.html` — Invoice scanner + history + Create Invoice modal (desktop-primary)
- `web/static/payments.html` — Vendor payments / AP page
- `web/static/plan.html` — Interactive project plan
- `routes/auth_routes.py` — Login, invite, roles, `@login_required` decorator
- `routes/billpay_routes.py` — Bill pay API (AP invoices, checks, payroll)
- `routes/invoice_routes.py` — Invoice upload/review/confirm/create-manual/import-csv (invoice_bp)
- `routes/inventory_routes.py` — Existing 1,045-line manual inventory system — **DO NOT MODIFY**
- `routes/vendor_routes.py` — Vendor CRUD
- `routes/catalog_routes.py`, `routes/product_mapping_routes.py`, `routes/storage_routes.py`
- `integrations/toast/toast_client.py` — Toast API (CRITICAL timezone logic inside)
- `integrations/toast/data_store.py` — `get_connection()`, order storage, business day logic
- `integrations/invoices/processor.py` — Claude Vision invoice scanning, CSV parsing, thumbnail generation
- `integrations/vendors/vendor_item_matcher.py` — Post-confirm vendor item matching
- `reports/analytics.py` — Revenue/labor/cost SQL queries
- `reports/invoice_anomaly.py` — Post-confirm anomaly detection
- `integrations/recipes/recipe_costing.py` — Recipe cost calculation
- `monitoring/ddns.py` — Cloudflare DDNS updater (loads secrets from `.env`) — updates A records for `dashboard`, `wheelhouse`, `skywatch`, `northfla`, and `ssh` subdomains. Hard-blocks `rednun.com` apex and `www`
- `data/schema_v2.sql` — Canonical schema
- `session_journal.json` — Session state tracker (READ FIRST every session, if present)

## Database Access
ALWAYS use `get_connection()` from `integrations/toast/data_store.py`. Do NOT use `sqlite3.connect()` directly. The connection returns Row objects that support dict-style access.

---

## 🛑 CRITICAL — DO NOT BREAK

1. **Auth middleware** on all routes — `@login_required` decorator from `routes/auth_routes.py`
2. **Timezone logic in `toast_client.py`** — 4AM ET business day boundary, uses `ZoneInfo('America/New_York')`. Do not revert.
3. **Void/delete filter in `analytics.py`** — Queries filter deleted/voided orders via `json_extract` on `raw_json`. Do not remove.
4. **Late-night reassignment in `data_store.py`** — Orders before 4AM ET → previous business day. Do not change.
5. **WAL mode on SQLite** — Required for concurrent reads. Keep it.
6. **Toast `businessDate` field** — Use Toast's own `businessDate` from `raw_json`. Do not compute ourselves.
7. **`inventory_routes.py`** — 1,045-line manual inventory system. Do not modify, refactor, or rename.
8. **Gunicorn port** — 8080, NOT 8000. Test: `curl http://127.0.0.1:8080/`
9. **Workers=2** — needed for concurrent requests. Two-worker shared state must use files or DB; in-process dicts diverge.
10. **Sidebar** — Built by `web/static/sidebar.js`, NOT `manage.html`. Edit `sidebar.js` to change nav.
11. **No silent API spend** — All Anthropic API calls must be triggered by explicit user action (button click, form submit). No background pollers, no scheduled API calls.

---

## ⛔ DNS — NEVER TOUCH rednun.com

**DO NOT modify the `rednun.com` DNS record or the `www.rednun.com` CNAME under any circumstances.**

- `www.rednun.com` is a CNAME to `sites.toasttab.com` and MUST stay **DNS-only (proxied: False)** in Cloudflare. Proxying it through Cloudflare breaks Toast online ordering completely.
- `rednun.com` points to the restaurant's web host (162.120.94.90) — not this server. Do not touch it.
- **Toast online ordering going down = direct revenue loss.** This mistake previously cost $1,000 in lost orders.
- The DDNS script (`monitoring/ddns.py`) updates A records for `dashboard`, `wheelhouse`, `skywatch`, `northfla`, and `ssh` only. It must NEVER touch `rednun.com` or `www.rednun.com` — a hardcoded `FORBIDDEN` set enforces this.

---

## Two Locations
- Dennis Port: `location = 'dennis'`
- Chatham: `location = 'chatham'`
- All data queries accept optional `?location=` param.

## Design System
- Background: `#020617` (slate-950)
- Cards: `#0f172a` (slate-900)
- Borders: `#1e293b` (slate-800)
- Text: `#e2e8f0` (slate-200)
- Green (positive/done): `#22c55e`
- Red (alert/negative): `#ef4444`
- Amber (warning/in-progress): `#f59e0b`
- Blue (info/links): `#38bdf8`
- Font: System sans-serif, JetBrains Mono on plan page
- Mobile-first, Apple PWA meta tags

## Responsive Design Pattern
All UI pages treat mobile and desktop as separate layouts, not stretched versions.

- **Mobile** (<768px): card-based, thumb-friendly, big tap targets, minimal columns
- **Desktop** (≥768px): table-based where data is involved, tighter rows, more columns, full width

Pattern:
- Write mobile as base
- Add `@media (min-width: 768px)` block for desktop overrides
- Content max-width on desktop: **1100px** (not 600px or 700px)
- Never use a single fixed max-width for both

**Desktop-primary pages** (mobile is fallback): Invoice History, Product Mapping, Recipe Editor, Order Guide, Analytics
**Mobile-primary pages** (desktop shows simplified view): Smart Count / AI Inventory, Specials Admin, Mobile Ops Dashboard

---

## Database Tables (41 total)

### Toast POS Data
- `orders` — 63,597 rows. guid PK, business_date (YYYYMMDD), raw_json
- `order_items` — 259,190 rows
- `payments` — 66,472 rows
- `employees` — 528 rows
- `time_entries` — 7,872 rows (labor)
- `sync_log` — 3,526 rows

### Invoice System
- `scanned_invoices` — **884 invoices, all confirmed** (2019-03-22 .. 2026-08-31), with
  **8,994 line items, every one carrying a `category_type`** (verified live 2026-08-27;
  this said 131 for a long time). (OCR + CSV + manual). Columns: invoice_type (one_time/recurring/credit), recurring_frequency, recurring_day, source (scanned/manual/csv), payment_status, needs_reconciliation, discrepancy. Vendor breakdown: US Foods 31, PFG 34, Martignetti 18, SG 11, Craft Collective 20, Colonial 8, L. Knife 5, others 4.
- `scanned_invoice_items` — Line items (product_name, quantity, unit, unit_price, total_price, category_type, pack_size, canonical_product_name, auto_linked)

### Product & Inventory (existing manual system)
- `product_inventory_settings` — **360 rows**
- `products` — **1,767 rows**
- `product_name_map` — **1,468 rows**
- `vendors` — 52 rows (**PRESERVED**)
- `storage_locations` — 11 rows (Walk-in, Dry Storage, Bar, Freezer, Front Line per location + shed) (**PRESERVED**)
- `storage_sections` — 1 row
- `count_sessions` — **51 rows**
- `count_items` — **194 rows**
- `inventory_counts` — 0 rows (wiped)
- `product_storage_locations` — 83 rows
- `bottle_weights` — 151 rows (liquor tare weights) (**PRESERVED**)
- `recipes` — **345 rows** (**PRESERVED**)
- `recipe_ingredients` — **1,482 rows** (**PRESERVED**)
- `gl_accounts` — **541 rows** (chatham 284, dennis 257; **524 carry a `qbo_id`**)
- `qb_journal_entries` — **896 rows**, `qb_journal_line_items` — **13,766 rows**.
  Sales JEs are BUILT DAILY AND BALANCED. **Exactly one has been posted to QBO:**
  Dennis 2026-08-20, `RNDP08202026` (QBO id 29599, entered 2026-08-22, $8,053.82;
  checked line by line 2026-09-24 — every line on a sales, tender, tax, discount or
  Tip Bank account, none on labor). `reports/sales_journal.py::push_to_qbo()` is the
  only push path in the codebase.
  - **MarginEdge already posted the daily sales JEs** (`MJ…ME`) to both QBO companies
    through **Chatham 2026-05-07 / Dennis 2026-05-03** (read from QBO 2026-09-24).
    Every dashboard JE on or before those dates is `superseded_me`; `ME_LAST_JE` in
    `sales_journal.py` also refuses to push them, and a rebuild never overwrites a
    `posted` or `superseded_me` day (`TERMINAL_STATUSES`). Pushable days start
    Chatham 5/08, Dennis 5/04.
  - Payroll JEs are NOT pushed by code (the per-run QBO journal is a CSV download).
    Three were keyed into QBO by hand under the old bank-credit method: Chatham
    `PR-12262025`, Dennis `12262025` (both dated 2025-12-26) and Dennis `01092026`
    (2026-01-09). Mike corrected 01092026 in QBO himself as `01092026-TIP` (tip
    line only: Dr Tip Bank / Cr Tip Wages 7,799.61); its bank credits ARE Dennis's
    1/09 bank side in QBO, so the 1/09 impound and checks #9647–9653 must never
    be pushed again.
  - **Red Buoy's (Chatham's) QBO writes are OPEN again (Mike signed off every
    Chatham account id, 2026-09-26).** 197 Chatham gl_accounts had carried QBO ids
    copied from Dennis's chart; all were rebuilt from Red Buoy's own chart
    (logged `qbo_id_remap`) — the last, Tip Bank, 175 (= Red Buoy's American
    Express CC) -> 188. The switch stays in `integrations/quickbooks/push_guard.py`
    (`CHATHAM_QBO_WRITES_BLOCKED`); throw it again if the chart drifts. The 131
    open Chatham sales JEs (5/08–9/25) were rebuilt on the new ids.
  - **Tip Bank is a LIABILITY** (Mike, 2026-09-26). Both QBO companies still type
    it Other Current Asset / Employee Cash Advances; that QBO change is Mike's
    or the accountant's to make.
  - A sales JE whose declared-cash-tips read from Toast fails is built
    `needs_attention`, never `ready` with the tips silently zeroed.
  - Pending QBO work lives in the Drive folder's `QBO_CATCHUP_LIST.md`.

> ⚠️ **Row counts in this file go stale fast and have been badly wrong before.** The
> numbers above were read live on 2026-08-27; the previous set described a wiped state
> and understated the build by ~50x, which made the project look far less finished than
> it was. Re-read the DB before trusting any count here.

**Accounting work: see `docs/ACCOUNTING_FINISH_LINE.md`** — the plan of record for the
QBO deliverable and the bank-reconciliation path. Update that file rather than writing
a new brief.

### Bank close (as of 2026-09-23)

- Learner guard: a check row teaches a rule only from a readable OCR'd payee, a card row only from the merchant, and `CHECK nnnn` / OCR garbage never become rules (`_readable_check_payee`, `set_row_gl_account`). 74 such rules were deleted on 2026-09-23 (`gl_repair_log` kind `rule_delete`). If per-transaction rules reappear, the guard regressed.
- **Tie-out is by cleared date.** A register row counts on the day the bank cleared it
  (`cleared_date` = statement line date), whatever the book date. `register_flow()` in
  `routes/register_routes.py` is the one sum over the four sources; the register's
  opening/bank balance, `_reconciliation_state()` (preview + close) and the `R` stamp on
  sign-off all use it. Outstanding at period end is cumulative (rows dated on or before
  period end not cleared by then); book = bank + outstanding. All 16 periods Jan–Aug 2026
  tie; the register opening equals the statement beginning on every one. **All 16 are
  signed off (2026-09-25), stored figures equal live.** Voiding or merging an item that
  sits on a signed period's outstanding list changes that period — do stale-item cleanup
  in the current period, or the page shows the older periods amber "Re-sign".
- Bank Reconcile page (`web/static/bank_reconcile.html`): every figure comes from the
  server preview (`_reconciliation_state`) — never recompute a delta client-side from
  register rows (those are by book date; it showed false red deltas until 2026-09-25).
  The outstanding roll-forward (`_outstanding_rollforward`) compares the prior period's
  SIGNED list with live rows and flags voids/edits since the signature.
- `POST /api/bank-reconcile/import-all {account_id}` imports every unimported statement in
  order with the continuity check (balance to the cent, contiguous dates; a break stops the
  run), one transaction per period, then check OCR, invariant audit and tie-out. The
  "Import all pending" button on Import Statement calls it. `/import` shares the same loop
  (`_import_upload_rows`). Never re-import a period that has rows.
- Dedupe (`/api/bank-reconcile/dedupe`, `all_periods: true`): a statement outflow merges
  into an **uncleared** Bill Pay row or manual payroll check of the same amount within the
  tolerance; the book row takes the **statement** date; the deleted row is saved in
  `register_merge_audit` with `match_rule`. Signed-off periods are skipped unless asked.
- The matcher never pairs a book row the bank already cleared in another period
  (`cleared_elsewhere`); `_mark_cleared` keeps the first date unless `force=True`.
- Zero-net payroll checks are not register rows. Direct Deposit rows never are.
- Invoice lines with no real category (NON_COGS / OTHER / TAX) resolve through
  **`gl_vendor_mapping`** (per location, vendor-name prefix key, longest wins) before
  `gl_category_mapping` — Mike, 2026-09-26; seeded by
  `scripts/vendor_gl_mapping_2026_09_26.py`. Keg/container deposit returns → Beer COGS
  (as DEPOSIT, inside F&B cost); US Foods fees → Food COGS; Chatham's 7shifts annual
  invoice → Prepaid Expenses (its bank payment is amortized). "Other Business Expenses"
  should stay empty: a line there means a vendor needs a mapping. The P&L, its drill
  and the uncoded footnote all read `_INVOICE_LINES_SQL` in `reports/profit_loss.py`.
- Single-entity vendors (Mike, 2026-09-24; `integrations/vendors/vendor_entity.py`): Fore & Aft and
  Nickerson are Chatham's only, Barrows is Dennis's only. Invoice intake holds one filed on the other
  entity (ingest guard rule 5); on the other entity's bank a line is intercompany (loan account), never
  an expense. The matchers never pair a payment with another entity's bank line.
- The P&L counts a Bill Pay row with no invoice behind it, coded to an expense, as banked opex
  (`_unbacked_billpay`) — merging a statement line into such a row used to drop its cost.
- Dennis reuses check numbers (two check stocks), and a later statement's extraction overwrites
  `web/static/check_images/acct2_check_<n>.png`. To see an older check, re-extract from that month's
  PDF into a scratch dir (`extract_checks` with `CHECK_IMAGE_DIR` pointed elsewhere).
- GL: machine codings are `gl_status='suggested'`; a rule exists only when Mike confirmed
  the coding; `needs_review` marks rows the classifier refused (Kickfin float candidates).
  Venmo is never tips (Bands; $350 = Trivia). PayPal is never ruled — the learner refuses.
- Job reports (`JOB0NN_*.md`), Mike's coding list, session summaries and the bank-close
  skill (`skills/rednun-bank-close/SKILL.md`) live in the Drive folder
  `/home/rednun/cowork/red-nun-dashboard/`. Job scripts queue in its `deploy/queue/`.
- Tests: `tests/test_bank_close_cleared_date.py` (synthetic fixture, always runnable) and
  `tests/test_bank_register.py` (live DB; four sales-tax / check-number tests are known
  data-state failures as of 2026-09-23 — see `SESSION_2026-09-23_SUMMARY.md`).

### AI Inventory
- `ai_inventory_sessions` — draft/review/confirmed
- `ai_inventory_items` — dual-stream confidence data
- `ai_inventory_history` — variance tracking over time

## Existing Inventory System Architecture
Manual counting system already built in `routes/inventory_routes.py`:
- Blueprint: `inventory_bp` at `/api/inventory/*`
- Products CRUD, Vendors, Stock adjustments, Recipes with ingredients
- Count sheets with storage locations and sections
- Batch counting, count history, reorder support
- Uses `count_sessions` + `count_items`

AI vision system is a separate addition:
- Blueprint: `ai_inventory_bp` at `/api/ai-inventory/*`
- New files prefixed `inventory_ai_*`
- Confirmed AI counts ALSO write to `count_sessions`/`count_items` so manual system sees them in history

## Blueprint Registration
Match the existing pattern in `web/server.py`:
```python
from routes.invoice_routes import invoice_bp
from routes.inventory_routes import inventory_bp
app.register_blueprint(invoice_bp)
app.register_blueprint(inventory_bp)
```

---

## Invoice System Architecture
- **Desktop:** Invoice History is default. "Scan Invoice" hidden on desktop.
- **Mobile:** Scan Invoice is default (camera workflow).
- **Add Invoice** button (desktop): Dropdown with "Upload Invoice" (reuses OCR) and "Create Invoice" (manual modal).
- **Create Invoice modal:** 3-step flow — invoice type → details + line items → success.
- **OCR pipeline:** Upload → Claude Vision → validation → auto-confirm or manual review → reconciliation if discrepancy.
- **CSV import:** `POST /api/invoices/import-csv?vendor={hint}&location={loc}&filename={name}` — auto-confirms, generates invoice-style thumbnail.
- **Multi-page PDFs:** Sent as native PDF to Claude API. NOT split into JPEGs.
- **Post-confirm processing (background thread):** vendor item matching, anomaly detection, recipe costing.
- **Manual invoices:** `POST /api/invoices/create-manual` — inserted as `status=confirmed`, `source=manual`.
- **CSV thumbnail images:** `generate_csv_thumbnail()` in invoice processor renders 800px dark-theme invoice-style image via Pillow, saved to `invoice_thumbnails/csv_{id}.jpg`.
- **Vendor name normalization:** `_VENDOR_ALIASES` dict maps OCR variants to canonical names (e.g., "Artignetti" → "Martignetti").

### CSV Import Routing (`/api/invoices/import-csv`)
- `vendor=vtinfo_lknife` or `vendor=vtinfo_colonial` → `parse_vtinfo_csv_invoice()`
- `vendor=pfg` → `parse_pfg_csv_invoice()` (returns list — one CSV can contain multiple invoices)
- Default → `parse_csv_invoice()` (US Foods, single invoice)
- All CSV imports auto-confirm and generate invoice-style thumbnail images.

## Anthropic API Pattern
All AI calls use the Anthropic Claude API. See `integrations/invoices/processor.py` for the canonical pattern.
- API key from `ANTHROPIC_API_KEY` in `.env`
- Claude Vision for image analysis (max_tokens: 16384)
- Multi-page PDFs sent as native PDF documents
- Vendor-specific OCR guidance in prompt (US Foods, Southern Glazer's, Performance Foodservice)
- Two-pass extraction: initial OCR + math error verification
- Do NOT introduce OpenAI or other providers
- **No silent API spend** — only explicit user action triggers calls

---

## Telegram Bot (`bot/bot.py`, `rednun-agent.service`)

Claude-powered ops bot. Single process (NOT gunicorn), so in-process state is
safe here — unlike the main app, where Critical Rule #9 applies.

- **Conversation memory:** `_CONVOS`, an in-process dict keyed by Telegram user
  id, 30-min idle TTL, 24-message rolling window. Trimming always cuts to a
  clean user-text turn so a `tool_result` is never orphaned (the Anthropic API
  rejects a leading tool_result). Briefings and the HTTP `/ask` path pass no
  history and stay one-shot.
- **Bill inbox (live since 2026-07-31, documented 2026-08-22):** the bot can
  reach into **Mike's own mailbox over IMAP** and pull a one-off bill (insurance,
  etc.) into the invoice OCR pipeline.
  - Creds: `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` from `.env`. Read-only usage —
    it fetches, it never deletes or moves mail.
  - `find_bill_in_inbox(sender, days)` — searches for recent mail from a sender
    carrying a PDF/image attachment. Returns candidates with UIDs. Read-only.
  - `ingest_bill(uid, location)` — POSTs the attachment to
    `/api/invoices/scan` on `127.0.0.1:8080`, which the invoice blueprint's
    `before_request` trusts as on-box automation. Lands in the review queue.
  - **`location` is REQUIRED and the tool refuses to guess.** Read it off the
    bill-to entity: Red Buoy Inc / Red Nun Chatham = `chatham`; Red Nun Public
    House / Red Nun Dennis = `dennis`. It previously defaulted to Dennis
    silently, which mis-files bills with nothing downstream to correct it.
  - **It only gets bills IN.** Creating or printing a check is a separate step
    that is not wired up. The system prompt forbids claiming otherwise.
  - This is an Anthropic-spend path (OCR), but user-initiated per Rule #11 —
    Mike asks for a specific bill. Do not make it autonomous or scheduled.

---

## Service Management
```bash
sudo systemctl restart rednun         # main app
sudo systemctl restart rednun-agent   # Telegram bot
sudo systemctl restart rednun-tv      # TV control app
sudo systemctl status rednun          # status
journalctl -u rednun -f               # live logs
journalctl -u rednun -n 50            # last 50 lines
sudo nginx -t && sudo systemctl restart nginx
```

Python venv: `/opt/red-nun-dashboard/venv/bin/python3`

## Cron Jobs
- Toast sync: every 10 min during business hours (`run_sync.sh`)
- Email poller: every 5 min *(known bug: duplicated — two identical entries)*
- Drive invoice watcher: every 5 min
- Thermostat fetch: every 5 min
- Sports guide scraper: daily 10 AM
- Vendor scrapers: daily 7 AM (`run_all.sh`)
- Nightly backup: 3 AM (tar + DB copy, 14-day retention)

## Backup Policy
Every time you back up the DB to `/opt/backups/`, **delete all previous `.db` backups** after confirming the new one exists and is reasonable size. Disk hit 93.5% full when old backups accumulated.

Order (use the SQLite backup API — a plain `cp` of the live WAL database gives a torn snapshot):
1. `sqlite3 /var/lib/rednun/toast_data.db ".backup /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db"`
2. Verify: `ls -lh /opt/backups/toast_data_*.db` and `sqlite3 <new> "pragma integrity_check"`
3. Delete older: `find /opt/backups/ -name "*.db" ! -name "toast_data_YYYYMMDD_HHMM.db" -delete`
4. Confirm: `ls -lh /opt/backups/`

Also: back up before any schema change or large migration.

## Before Making Changes
1. Read `session_journal.json` (if present) for current state
2. Back up the DB (pattern above)
3. Do NOT modify existing files unless explicitly instructed
4. Match existing code style exactly
5. Test: `sudo systemctl restart rednun && curl http://127.0.0.1:8080/`
6. Restart after Python or template changes — Gunicorn caches templates. Verify with `curl` before relaunching TVs.

---

## Specials Boards — Two Boards, Day-Driven

The Staff App serves the chalkboard TVs and keeps **two** separate boards, both
machine-local and gitignored under `data/`:

| Board | File | Shown |
|-------|------|-------|
| Regular specials | `data/specials.json` | Every day except Sunday |
| Sunday game day | `data/specials_sunday.json` | Sunday's business day |

- **The switch is automatic** — `_active_board()` in `staff/staff.py` picks the
  board; nobody toggles anything on the day. The Sunday window follows the same
  **4AM ET business day** as the rest of the app (`_is_game_day()`), so Sunday
  night service past midnight still shows game day and it reverts Monday 4AM.
- **Falls back safely:** if `specials_sunday.json` doesn't exist, Sunday shows the
  regular board. The Sunday board is only live once it has been saved once.
- **Editor:** `/staff/specials/edit` has a REGULAR / SUNDAY GAME DAY switcher and
  opens on whichever board is live. It always passes `?board=` explicitly;
  `POST /staff/api/board` with no `?board=` defaults to **regular**, never the
  active board, so an old client can't overwrite the game-day menu by surprise.
- **`GET /staff/api/board`** with no param returns the active board (this is what
  the TVs poll). `_rv` is `"<board>:<mtime>"` — the board name is in there so the
  Sunday changeover always registers as a new revision and open TVs reload.
- **Two Toast syncs, different shapes:**
  - `POST /staff/api/board/sync-toast` — the regular board. Knows the Soups and
    Specials groups specifically.
  - `POST /staff/api/board/sync-toast-menu` — pulls one **named whole menu**
    (default `SUNDAY_MENU_NAME = 'Sunday Game Day'`, in-house only) and flattens
    all of its groups, nested ones included, into a flat item list. Prices keep
    cents when they have them (`$12.50`), unlike the specials sync's whole dollars.
- Board names are whitelisted in `BOARD_PATHS` — the name selects a file, so it
  must never be interpolated into a path.

> The Management App also has `web/static/specials_admin.html` on `/api/specials`.
> That is a **separate, older** system and is NOT what the TVs show.

---

## TV Control App (`/opt/tv_control/`)

**Separate service from the Staff App.** Controls Roku TVs and DirecTV boxes in Chatham.

- **Service:** `rednun-tv.service` (port 5000, gunicorn, 2 workers)
- **Restart:** `sudo systemctl restart rednun-tv`
- **Fire TV (Chatham):** IP 10.1.10.20, uses Downloader app (com.esaba.downloader) for fullscreen kiosk
- **TV Power Config:** `/opt/tv_control/data/tv_power.json` (Fire TV IP, schedule)
- **Watchdog:** Background thread in `tv_power.py` — checks every 2 min, auto-recovers specials display, scheduled on/off 11am–9pm

**Note:** Specials Board itself is served by the Staff App at `/staff/specials/tv` on port 8080, not by the TV Control App. The TV Control App handles the hardware side (powering TVs on/off, DirecTV/Roku control).

---

## Vendor Scrapers (`~/vendor-scrapers/`)
Playwright-based scrapers that log into vendor portals, download invoices, and import them via the dashboard API. Persistent browser profiles, auto-login on session expiry.

**Separate repo (not in this one):**
```
GitHub:  https://github.com/mgiorgio-beep/vendor-scrapers  (private)
Local:   G:\My Drive\Red NUn Dashboard\vendor-scrapers     (gitignored from the dashboard repo)
Server:  /home/rednun/vendor-scrapers                      (where cron runs from)
```
Dev flow matches the dashboard repo: edit locally in Drive, `git commit`, `git push`, then on the server `cd ~/vendor-scrapers && git pull`. All vendor scrapers (including US Foods, which used to live separately at `~/usfoods-scraper/`) now live in this one repo.

- **Orchestrator:** `~/vendor-scrapers/run_all.sh` — runs all 7 scrapers sequentially, then `import_downloads.py`
- **Cron:** `0 7 * * *` daily at 7 AM
- **Import pipeline:** `~/vendor-scrapers/common/import_downloads.py`
  - CSV vendors (US Foods, PFG, VTInfo) → `POST /api/invoices/import-csv`
  - PDF vendors (SG, Martignetti, Craft Collective) → `POST /api/invoices/scan` for OCR

### Scrapers

| Vendor | Dir | Type | Locations | Portal |
|--------|-----|------|-----------|--------|
| US Foods | `~/vendor-scrapers/usfoods/` (symlinked from `~/usfoods-scraper/usfoods_invoice_scraper.py`) | CSV | Chatham, Dennis | order.usfoods.com |
| PFG | `~/vendor-scrapers/pfg/` | CSV | Chatham, Dennis | customerfirstsolutions.com |
| VTInfo (Colonial only) | `~/vendor-scrapers/vtinfo/` | CSV | Chatham, Dennis | apps.vtinfo.com |
| L. Knife | **RETIRED 2026-08-26** — invoices arrive by EMAIL (noreply@vtinfo.com), ingested by the email poller | PDF | Chatham (AR034), Dennis (AR035) | — |
| Southern Glazer's | `~/vendor-scrapers/southern-glazers/` | PDF | Chatham, Dennis (separate logins) | portal2.ftnirdc.com |
| Martignetti | `~/vendor-scrapers/martignetti/` | PDF | Both (single login) | martignettiexchange.com |
| Craft Collective | `~/vendor-scrapers/craft-collective/` | PDF | Dennis only | termsync.com |

### Key Implementation Details
- **L. Knife (scraper RETIRED 2026-08-26):** the Connect-portal scraper is deleted from vendor-scrapers (archived at `~/vendor-scrapers/lknife_retired_20260826.tar.gz`); L. Knife emails all invoices for both locations and `email_invoice_poller.py` picks them up every 5 min. PDF facts still apply to the emailed invoices: each invoice is a 3-page PDF; line items with negative quantities are keg/cooperage returns (credits). Use the "Invoice Total" at the bottom of page 1 as the net `total_amount` — not "Total Sales". AR034 = Chatham, AR035 = Dennis Port. L. Knife OCR guidance lives in `integrations/invoices/processor.py` (search for "L. KNIFE & SON (NEW CONNECT-PORTAL PDFs)").
- **SG PDF download:** AngularJS portal — extract `InvoiceId` from `angular.element(link).scope().row.entity.InvoiceId` + JWT from sessionStorage, call `/api/GetExternalInvoice` directly via `requests`. Popup URL is always `:` (about:blank), don't use it.
- **Craft Collective PDF:** Download directly from listing URL (`/payments/{id}/download_invoice_pdf`) via `requests` with browser cookies. Invoices showing "Request" instead of "View" have no PDF — skip.
- **VTInfo CSV filenames:** Metadata encoded as `vtinfo_{vendor}_{location}_{invoicenum}_{YYYYMMDD}.csv`. Parser extracts via regex.
- **PFG CSV:** Can contain multiple invoices. Parser returns a list.
- **Session management:** Each scraper stores cookies in `storage_state*.json`, auto-logs in on expiry using credentials from `~/vendor-scrapers/.env`.
- **Dedup:** Check local `data/downloaded_invoices.json` AND dashboard `/api/invoices/existing`.
- **SG date-less rows:** Summary/statement rows — scraper skips automatically.

### Env Vars (in `~/vendor-scrapers/.env`)
`SG_USER`/`SG_PASS`, `SG_USER_DENNIS`/`SG_PASS_DENNIS`, `MART_USER`/`MART_PASS`, `CC_USER`/`CC_PASS`, `VTINFO_USER`/`VTINFO_PASS`, `PFG_USER`/`PFG_PASS`, `USF_USER`/`USF_PASS`

---

## Service Accounts
- **dashboard@rednun.com** — Service account for all automated operations:
  - `gmail_token.pickle` — Gmail API (email alerts for applications, availability forms)
  - `google_token.pickle` — Google Drive + Sheets API (PDF uploads, spreadsheet mgmt)
  - Owns all Drive folders (applications, availability)
- Previously used `invoice@rednun.com` — renamed to `dashboard@rednun.com` April 2026.

## Hiring / Application System
- **Form URL:** https://dashboard.rednun.com/hiring
- **Blueprint:** `application_routes.py` (`application_bp`)
- **Template:** `templates/application.html`
- **Drive folders** (owned by dashboard@rednun.com):
  - Root: Red Nun Employee Docs (`1qzgYOEHub5CXlo7_S-CKvL8cWGU4r8fN`)
  - Chatham: `1ZIbKK9xp8hKsHgVpmQ5gefS1tK1O4Qer`
  - Dennis Port: `119iaRcv98V4tycrvs2SBx12CvFRqOua1`
  - Availability: `1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA`
- **Spreadsheet:** "Applications 2026" in root (tabs: Sheet1, Chatham, Dennis Port, Both)
- **Email routing:** Chatham → matt@rednun.com · Dennis Port → alexis@rednun.com · Both/Neither → both
- **Auto-highlight:** Apps listing Mike/Michael Giorgio as reference get orange highlight
- **PDF naming:** `Lastname_Firstname.pdf`
- **Resume handling:** PDF resumes merged as page 2 of application PDF

## Availability System
- **Form URL:** https://dashboard.rednun.com/availability
- **Blueprint:** `availability_routes.py` (`availability_bp`)
- **Template:** `templates/availability.html`
- **Drive folder:** Availability (`1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA`)
- **Email alert:** matt@rednun.com on submission
- **PDF naming:** `Lastname_Firstname.pdf`
- **Auto-close:** 10-second countdown after submission

---

## Security
- ALL secrets in `.env` (chmod 600, gitignored). NEVER hardcode API keys in source.
- `google_credentials.json` — chmod 600, gitignored
- `monitoring/ddns.py` loads Cloudflare token from `.env` (`CF_API_TOKEN`, `CF_ZONE_ID`, `CF_SSH_RECORD_ID`)
- Flask `SECRET_KEY` from `.env`
- Never commit `toast_data.db` to git
- No secrets in static HTML/JS

---

## Known Issues (updated Jun 11, 2026)
1. ~~`analytics.py` line 331 missing voided/deleted filter~~ — RESOLVED/stale: that line is in `get_labor_by_role` (time_entries — no void concept; orders-side labor queries already filter). Separately, 2026-06-11 added ORDER-level void/delete exclusion to `get_sales_mix`, `get_pour_cost_by_category`, `get_bartender_pour_variance` (item-level `voided=0` missed whole-check voids).
2. Net sales discrepancy: ~$181 off vs Toast ($4,242 vs $4,061 for Chatham Feb 13) — NOT explained by the void filter (daily revenue already filtered); suspect refunds/service-charge handling, still open
3. Database size: 1.1GB — orders/order_items/payments are the bulk. Consider pruning >13 months + VACUUM.
4. Duplicate cron: `email_poller.py` runs twice every 5 min
5. VTInfo scraper: Location picker and "View and Pay Invoices" button not found — portal UI may have changed. Needs manual investigation.
6. SG Dennis #400097: Statement/summary row ($3,190.52, no date) — correctly skipped by date filter but still shows in portal. Not a real invoice.

---

## Dennis Port Beelink (Staff/TV App) — LIVE

Second Beelink (ME Mini N95) at the Dennis Port location, mirroring the Chatham SER5. **Live and running.** It serves only the staff/TV specials app — NOT the full dashboard.

- **Local IP:** `10.1.10.84` (DHCP reservation, MAC `78:55:36:04:10:5d`)
- **SSH (on-site):** `ssh -p 2222 rednun@10.1.10.84`
- **SSH (remote):** WAN port-forward, or the IP-proof Cloudflare tunnel `ssh-dennis.rednun.com`
- **Live at:** `https://dennis.rednun.com/staff` (staff + TV specials only)
- **App path:** `/opt/red-nun-dashboard` (same repo as Chatham)
- **Runs:** `staff_server.py` (loads only `staff_bp` + `tv_power_bp`) via gunicorn under systemd service **`rednun-staff`** on port 8080 — distinct from Chatham's full `rednun` service.
- **Cloudflare tunnel:** `dennis` (ID `82009d56-ad32-4d7f-86ad-66e426b70f7e`): `dennis.rednun.com` → `localhost:8080`, `ssh-dennis.rednun.com` → `ssh://localhost:2222`.
- **Sports guide:** scraped only on Chatham. Dennis pulls it nightly at **5:15 AM** via cron (scp from Chatham → `data/sports_guide.json`).

### Service management (Dennis)
```bash
sudo systemctl restart rednun-staff   # staff/TV app (Dennis)
sudo systemctl status  rednun-staff
journalctl -u rednun-staff -f
```

### Location default (how Dennis knows it's Dennis)
`staff/staff.py` reads a **per-machine env var**, NOT a committed file:
```python
LOCATION = os.environ.get('RN_LOCATION', 'chatham').strip().lower()   # line ~34
...
location = (request.json or {}).get('location', LOCATION)             # line ~200 (Toast specials sync)
```
`RN_LOCATION=dennis` is set in the Dennis `rednun-staff` systemd unit. Chatham leaves it unset → falls back to `chatham` (unchanged). An explicit `location` in the request still overrides. This is deliberately an env var (not a file in git) so one box can never push its location onto the other.

## ⛔ Two Beelinks — Keep Them Separate

The two Beelinks share the **repo** but must operate **totally independently**. We have already been burned by cross-contamination: installing TV control from Dennis once **overwrote Chatham's config**. Rules:

1. **Machine-local config is per-box and NOT in git:** `data/tvs.json` (gitignored) and the `RN_LOCATION` env var (set in each box's systemd unit). Each Beelink owns its own copy. **Never commit a machine's `tvs.json` and pull it onto the other** — that is exactly how the earlier overwrite happened.
   - **How TV config stays separate:** `staff/staff.py` `_load_tvs()` reads that box's own `data/tvs.json`; if the file is absent it falls back to `DEFAULT_TVS_DENNIS` or `DEFAULT_TVS_CHATHAM` keyed on `RN_LOCATION`. So both the saved TV list and the fallback defaults are per-location. Edit TVs per box via `…/staff → Manage TVs`, which writes only that box's local `tvs.json`.
   - There is no `data/site_config.json` in use — location is driven entirely by the `RN_LOCATION` env var, not a committed/synced file. (Earlier handoff notes mentioned `site_config.json`; that approach was dropped in favor of the env var.)
2. **Do NOT run `monitoring/ddns.py` on Dennis.** Its `.env` `CF_SSH_RECORD_ID` was copied from Chatham and points at Chatham's `ssh.rednun.com` record — running it on Dennis would overwrite Chatham's DNS with the Dennis IP and break Chatham SSH. Dennis doesn't need DDNS; the Cloudflare tunnel handles IP changes.
3. **Different services:** Chatham = `rednun` (full dashboard). Dennis = `rednun-staff` (staff/TV only). Restart the right one.
4. **TODO (rotate secret):** the `CF_API_TOKEN` in `.env` was exposed in a chat transcript. Generate a new token in Cloudflare and update `.env` on both Beelinks. Not urgent, but it's a live DNS-edit token.

### On-site setup still pending (do not attempt remotely)
- Enter Dennis DirecTV / Roku / Samsung TV IPs in `dennis.rednun.com/staff` → Manage TVs (`data/tvs.json`).
- Set up Fire TV ADB pointing at the Dennis Fire TVs.

---

## Planned / Not Yet Built

### Claude Code Channels on Beelink
Goal: flat-subscription replacement for per-token API costs. Not yet set up.
