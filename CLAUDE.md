# Red Nun Dashboard — Claude Code Guide

## ⚠️ REPO & WORKFLOW — READ THIS FIRST

### The one repo that matters
```
GitHub:  https://github.com/mgiorgio-beep/red-nun-dashboard
Local:   C:\Users\giorg\red-nun-dashboard        (Windows working copy)
Server:  /opt/red-nun-dashboard                   (Beelink SER5, Chatham)
Live at: https://dashboard.rednun.com
```

### SSH to server
```
ssh -p 2222 rednun@ssh.rednun.com
```
Local IP: 10.1.10.83 (for on-network access).

### Deploy workflow
```
1. Edit locally in C:\Users\giorg\red-nun-dashboard
2. git add / git commit / git push
3. On server: cd /opt/red-nun-dashboard && git pull && sudo systemctl restart rednun
```

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
- **Database:** SQLite WAL mode. Path: `/var/lib/rednun/toast_data.db` (set via `TOAST_DB_PATH` in `.env`). ~1.1GB.
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
- `monitoring/ddns.py` — Cloudflare DDNS updater (loads secrets from `.env`) — must ONLY update `dashboard.rednun.com`
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
- The DDNS script (`monitoring/ddns.py`) must only ever update `dashboard.rednun.com`. Nothing else.

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
- `scanned_invoices` — 131 invoices (OCR + CSV + manual). Columns: invoice_type (one_time/recurring/credit), recurring_frequency, recurring_day, source (scanned/manual/csv), payment_status, needs_reconciliation, discrepancy. Vendor breakdown: US Foods 31, PFG 34, Martignetti 18, SG 11, Craft Collective 20, Colonial 8, L. Knife 5, others 4.
- `scanned_invoice_items` — Line items (product_name, quantity, unit, unit_price, total_price, category_type, pack_size, canonical_product_name, auto_linked)

### Product & Inventory (existing manual system)
- `product_inventory_settings` — 0 rows (wiped). Rebuilds from confirmed invoices via auto-populate.
- `products` — 0 rows (wiped)
- `product_name_map` — 0 rows (wiped)
- `vendors` — 51 rows (**PRESERVED**)
- `storage_locations` — 11 rows (Walk-in, Dry Storage, Bar, Freezer, Front Line per location + shed) (**PRESERVED**)
- `storage_sections` — 1 row
- `count_sessions` — 0 rows (wiped)
- `count_items` — 0 rows (wiped)
- `inventory_counts` — 0 rows (wiped)
- `product_storage_locations` — 83 rows
- `bottle_weights` — 151 rows (liquor tare weights) (**PRESERVED**)
- `recipes` — 3 rows (**PRESERVED**)
- `recipe_ingredients` — 14 rows (**PRESERVED**, orphaned product_id refs — resolve when products rebuild)

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

Order:
1. `cp /var/lib/rednun/toast_data.db /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db`
2. Verify: `ls -lh /opt/backups/toast_data_*.db`
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

- **Orchestrator:** `~/vendor-scrapers/run_all.sh` — runs all 7 scrapers sequentially, then `import_downloads.py`
- **Cron:** `0 7 * * *` daily at 7 AM
- **Import pipeline:** `~/vendor-scrapers/common/import_downloads.py`
  - CSV vendors (US Foods, PFG, VTInfo) → `POST /api/invoices/import-csv`
  - PDF vendors (SG, Martignetti, Craft Collective) → `POST /api/invoices/scan` for OCR

### Scrapers

| Vendor | Dir | Type | Locations | Portal |
|--------|-----|------|-----------|--------|
| US Foods | `~/usfoods-scraper/` | CSV | Chatham, Dennis | order.usfoods.com |
| PFG | `~/vendor-scrapers/pfg/` | CSV | Chatham, Dennis | customerfirstsolutions.com |
| VTInfo (L. Knife + Colonial) | `~/vendor-scrapers/vtinfo/` | CSV | Chatham, Dennis | apps.vtinfo.com |
| Southern Glazer's | `~/vendor-scrapers/southern-glazers/` | PDF | Chatham, Dennis (separate logins) | portal2.ftnirdc.com |
| Martignetti | `~/vendor-scrapers/martignetti/` | PDF | Both (single login) | martignettiexchange.com |
| Craft Collective | `~/vendor-scrapers/craft-collective/` | PDF | Dennis only | termsync.com |

### Key Implementation Details
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

## Known Issues (carried forward from Mar 23, 2026 — re-verify)
1. `analytics.py` line 331: one `where_clauses = []` missing voided/deleted filter
2. Net sales discrepancy: ~$181 off vs Toast ($4,242 vs $4,061 for Chatham Feb 13)
3. Database size: 1.1GB — orders/order_items/payments are the bulk. Consider pruning >13 months + VACUUM.
4. Duplicate cron: `email_poller.py` runs twice every 5 min
5. VTInfo scraper: Location picker and "View and Pay Invoices" button not found — portal UI may have changed. Needs manual investigation.
6. SG Dennis #400097: Statement/summary row ($3,190.52, no date) — correctly skipped by date filter but still shows in portal. Not a real invoice.

---

## Planned / Not Yet Built

### Second Beelink (Dennis Port)
Beelink ME Mini N95 earmarked for Dennis Port. Will run the **TV/specials app for the Dennis location** (mirror of the Chatham TV Control setup). Not built yet — no service, no config, no DNS. When it happens, model it on the existing Chatham `rednun-tv.service` and `/opt/tv_control/` layout.

### Claude Code Channels on Beelink
Goal: flat-subscription replacement for per-token API costs. Not yet set up.
