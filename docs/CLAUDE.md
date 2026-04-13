# Red Nun — Claude Code Guide

## What This Is
Custom restaurant platform replacing MarginEdge ($363/mo).
Two locations: Dennis Port & Chatham, Cape Cod, MA.
Live at: https://dashboard.rednun.com

## App Naming
- **Management App** — The main dashboard (`dashboard.rednun.com`). Invoices, sales analytics, bill pay, product setup, recipes, menu analysis, inventory. All blueprints registered in `server.py`, frontend in `static/*.html`.
- **Staff App** — Staff-facing venue controls (`dashboard.rednun.com/staff`). Sports guide, specials board (TV display + editor), TV control (DirecTV, Roku, Samsung, YouTube TV), Sonos, lights. Self-contained blueprint in `staff/staff.py` with its own templates, static files, and PWA manifest.

## Tech Stack
- **Server:** Beelink SER5, Chatham. SSH: `ssh -p 2222 rednun@ssh.rednun.com`
- **Backend:** Python / Flask / Gunicorn (port 8080) / Nginx. Venv: `/opt/rednun/venv/bin/python3`
- **Database:** SQLite WAL mode → toast_data.db (1.1GB)
- **Frontend:** Vanilla HTML/JS/CSS, dark theme (#020617 bg)
- **AI/OCR:** Anthropic Claude API (claude-sonnet-4, max_tokens 16384) — invoice OCR and inventory vision
- **Data Sources:** Toast POS, MarginEdge (legacy), 7shifts, Honeywell

## Key Files
- `server.py` — Main Flask app, blueprint registration, all core routes
- `analytics.py` — Revenue/labor/cost SQL queries
- `data_store.py` — DB connection (get_connection()), order storage, business day logic
- `toast_client.py` — Toast API integration (CRITICAL timezone fix inside)
- `invoice_processor.py` — Claude Vision invoice scanning, save/confirm/validate, product auto-populate, CSV parsing (US Foods, PFG, VTInfo), invoice image generation
- `invoice_routes.py` — Invoice upload/review/confirm/create-manual/import-csv endpoints (invoice_bp blueprint)
- `inventory_routes.py` — Manual inventory system (inventory_bp, 1,045 lines — DO NOT MODIFY)
- `vendor_item_matcher.py` — Post-confirm vendor item matching (process_invoice_items)
- `invoice_anomaly.py` — Post-confirm anomaly detection
- `recipe_costing.py` — Recipe cost calculation (cost_all_recipes)
- `catalog_routes.py` — Product catalog routes
- `product_mapping_routes.py` — Canonical product mapping
- `auth_routes.py` — Authentication (login_required decorator)
- `storage_routes.py` — Storage location management
- `sync.py` — Data sync orchestration
- `batch_ocr.py` — Bulk invoice image processing
- `drive_invoice_watcher.py` — Google Drive invoice watcher (cron every 5 min)
- `email_poller.py` — Email invoice intake (cron every 5 min)
- `ddns_update.py` — Cloudflare DDNS updater (loads secrets from .env)
- `ddns_update.sh` — Cloudflare DDNS shell version (loads secrets from .env)
- `bottle_weights_seed.py` — Seeds bottle_weights table (151 bottles seeded)
- `static/sidebar.js` — Dynamic sidebar builder (NOT manage.html)
- `static/invoices.html` — Invoice scanner + history + Create Invoice modal (desktop-primary)
- `static/manage.html` — Product management page (4,359 lines)
- `static/plan.html` — Interactive project plan
- `toast_data.db` — SQLite database
- `session_journal.json` — Session state tracker (READ THIS FIRST every session)
- `invoice_images/` — Invoice scans
- `invoice_images_archive/` — Processed invoices
- `invoice_thumbnails/` — Generated invoice thumbnails (OCR PDFs + CSV invoice images)

## Database Access
ALWAYS use `get_connection()` from `data_store.py` for database access.
Do NOT use sqlite3.connect() directly. The connection returns Row objects
that support dict-style access.

## CRITICAL — DO NOT BREAK THESE
1. **Timezone logic in toast_client.py**: 4AM ET business day boundary. Uses ZoneInfo('America/New_York'). DO NOT revert.
2. **Void/delete filter in analytics.py**: Queries filtering deleted/voided orders via json_extract on raw_json. DO NOT remove.
3. **Late-night reassignment in data_store.py**: Orders before 4AM ET → previous business day. DO NOT change.
4. **WAL mode on SQLite**: Database uses WAL for concurrent reads. Keep it.
5. **Toast businessDate field**: Use Toast's own `businessDate` from raw_json. DO NOT compute ourselves.
6. **inventory_routes.py**: Existing 1,045-line manual inventory system. DO NOT modify, refactor, or rename.
7. **Gunicorn port**: Runs on 8080, NOT 8000. Test: curl http://127.0.0.1:8080/
8. **Sidebar**: Built by static/sidebar.js, NOT manage.html. Edit sidebar.js to change nav.

## Database Tables (41 total)

### Toast POS Data
- `orders` — 63,597 rows. guid PK, business_date (YYYYMMDD format), raw_json
- `order_items` — 259,190 rows. Sales line items
- `payments` — 66,472 rows
- `employees` — 528 rows
- `time_entries` — 7,872 rows (labor data)
- `sync_log` — 3,526 rows

### Invoice System
- `scanned_invoices` — 131 invoices (OCR + CSV + manual). Columns include: invoice_type (one_time/recurring/credit), recurring_frequency, recurring_day, source (scanned/manual/csv), payment_status, needs_reconciliation, discrepancy. Vendors: US Foods (31), PFG (34), Martignetti (18), SG (11), Craft Collective (20), Colonial (8), L. Knife (5), others (4).
- `scanned_invoice_items` — Line items per invoice (product_name, quantity, unit, unit_price, total_price, category_type, pack_size, canonical_product_name, auto_linked)
- `me_invoices` — 0 rows (wiped, ME migration data gone for good)
- `me_invoice_items` — 0 rows (wiped)

### Product & Inventory (existing manual system)
- `product_inventory_settings` — 0 rows (wiped). Rebuilds from confirmed invoices via auto-populate.
- `products` — 0 rows (wiped)
- `product_name_map` — 0 rows (wiped)
- `vendors` — 51 rows (PRESERVED)
- `storage_locations` — 11 rows (Walk-in, Dry Storage, Bar, Freezer, Front Line per location + shed) (PRESERVED)
- `storage_sections` — 1 row
- `count_sessions` — 0 rows (wiped)
- `count_items` — 0 rows (wiped)
- `inventory_counts` — 0 rows (wiped)
- `product_storage_locations` — 83 rows
- `bottle_weights` — 151 rows (liquor tare weights) (PRESERVED)
- `recipes` — 3 rows (PRESERVED)
- `recipe_ingredients` — 14 rows (PRESERVED, orphaned product_id refs — resolve when products rebuild)

### AI Inventory (NEW — created by inventory build)
- `ai_inventory_sessions` — AI count sessions (draft/review/confirmed)
- `ai_inventory_items` — AI count items with dual-stream confidence data
- `ai_inventory_history` — Variance tracking over time

## Existing Inventory System Architecture
The manual counting system is ALREADY BUILT in inventory_routes.py:
- Blueprint: `inventory_bp` at `/api/inventory/*`
- Products CRUD, Vendors, Stock adjustments, Recipes with ingredients
- Count sheets with storage locations and sections
- Batch counting, count history, reorder support
- Uses `count_sessions` + `count_items` tables

The AI vision system is a SEPARATE addition:
- Blueprint: `ai_inventory_bp` at `/api/ai-inventory/*`
- New files prefixed `inventory_ai_*`
- When AI count is confirmed, it ALSO writes to count_sessions/count_items
  so the manual system sees it in its history

## Blueprint Registration (in server.py)
Check how existing blueprints are registered and follow the same pattern:
```
from invoice_routes import invoice_bp
from inventory_routes import inventory_bp
app.register_blueprint(invoice_bp)
app.register_blueprint(inventory_bp)
```
New AI blueprint adds:
```
from inventory_ai_routes import ai_inventory_bp
app.register_blueprint(ai_inventory_bp)
```

## Design System
- Background: #020617 (slate-950)
- Cards: #0f172a (slate-900)
- Borders: #1e293b (slate-800)
- Text: #e2e8f0 (slate-200)
- Green: #22c55e (done/positive)
- Red: #ef4444 (alerts/negative)
- Amber: #f59e0b (in progress/warning)
- Blue: #38bdf8 (info/links)
- Font: System sans-serif, JetBrains Mono on plan page
- Mobile-first, Apple PWA meta tags

## Anthropic API Pattern
All AI calls use Anthropic Claude API. See invoice_processor.py for the exact pattern:
- API key loaded from environment variable (ANTHROPIC_API_KEY in .env)
- Claude Vision for image analysis (max_tokens: 16384)
- Multi-page PDFs sent as native PDF documents (NOT split into JPEG pages)
- Vendor-specific OCR guidance in prompt (US Foods, Southern Glazer's, Performance Foodservice)
- Two-pass extraction: initial OCR + math error verification pass
- Do NOT introduce OpenAI or other providers

## Before Making Changes
1. Read session_journal.json for current state
2. Backup: `cp /opt/rednun/toast_data.db /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db`
3. Do NOT modify existing files unless explicitly instructed
4. Match existing code style exactly
5. Test: `systemctl restart rednun && curl http://127.0.0.1:8080/`

## Backup Policy
EVERY time you create a backup in /opt/backups/, ALWAYS delete all previous .db backups
after confirming the new one exists and has a reasonable file size. The server has limited
disk space — never leave old backups accumulating.

Pattern (always follow in this order):
1. Create new backup: `cp toast_data.db /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db`
2. Verify it exists and is the right size: `ls -lh /opt/backups/toast_data_*.db`
3. Delete all OLDER .db files in /opt/backups/ (keep only the one just created):
   `find /opt/backups/ -name "*.db" ! -name "toast_data_YYYYMMDD_HHMM.db" -delete`
4. Confirm cleanup: `ls -lh /opt/backups/`

This prevents disk from filling up (went from 93.5% used after old backups accumulated).

## Service Management
- Restart: `sudo systemctl restart rednun`
- Logs: `journalctl -u rednun -f`
- Nginx: `sudo systemctl restart nginx`
- Port: 8080 (NOT 8000)
- SSH: `ssh -p 2222 rednun@ssh.rednun.com`
- Python: `/opt/rednun/venv/bin/python3`

## TV Control App (`/opt/tv_control/`)
- **Service:** `rednun-tv.service` (port 5000, gunicorn, 2 workers)
- **Restart:** `sudo systemctl restart rednun-tv`
- **Specials Board TV:** `/specials/tv` route → `templates/specials_tv.html` (fullscreen, Downloader app)
- **Specials Editor:** `/specials/edit` route → phone-friendly editor
- **Specials Data:** `/opt/tv_control/data/specials.json`
- **TV Power Config:** `/opt/tv_control/data/tv_power.json` (Fire TV IP, schedule)
- **Watchdog:** Background thread in `tv_power.py` — checks every 2 min, auto-recovers specials display, scheduled on/off (11am-9pm)
- **Fire TV (Chatham):** IP 10.1.10.20, uses Downloader app (com.esaba.downloader) for fullscreen kiosk
- **Key:** Template changes require `sudo systemctl restart rednun-tv` (gunicorn caches templates)

## Vendor Scrapers (`~/vendor-scrapers/`)
Playwright-based scrapers that log into vendor portals, download invoices (PDF or CSV), and import them into the dashboard. All use persistent browser profiles for session management and auto-login on session expiry.

- **Orchestrator**: `~/vendor-scrapers/run_all.sh` — runs all 7 scrapers in sequence, then `import_downloads.py`
- **Cron**: `0 7 * * *` daily at 7 AM
- **Import pipeline**: `~/vendor-scrapers/common/import_downloads.py` — processes downloaded files:
  - CSV vendors (US Foods, PFG, VTInfo) → POST to `/api/invoices/import-csv`
  - PDF vendors (SG, Martignetti, Craft Collective) → POST to `/api/invoices/scan` for OCR

### Scrapers

| Vendor | Dir | Type | Locations | Portal |
|--------|-----|------|-----------|--------|
| **US Foods** | `~/usfoods-scraper/` | CSV | Chatham, Dennis | order.usfoods.com |
| **PFG** | `~/vendor-scrapers/pfg/` | CSV | Chatham, Dennis | customerfirstsolutions.com |
| **VTInfo** (L. Knife + Colonial) | `~/vendor-scrapers/vtinfo/` | CSV | Chatham, Dennis | apps.vtinfo.com |
| **Southern Glazer's** | `~/vendor-scrapers/southern-glazers/` | PDF | Chatham, Dennis (separate logins) | portal2.ftnirdc.com |
| **Martignetti** | `~/vendor-scrapers/martignetti/` | PDF | Both (single login) | martignettiexchange.com |
| **Craft Collective** | `~/vendor-scrapers/craft-collective/` | PDF | Dennis only | termsync.com |

### Key Implementation Details
- **SG PDF download**: AngularJS portal — extracts InvoiceId from `angular.element(link).scope().row.entity.InvoiceId` + JWT from sessionStorage, calls `/api/GetExternalInvoice` directly via `requests`. Popup URL is always `:` (about:blank), don't use it.
- **Craft Collective PDF download**: Downloads directly from listing page URL (`/payments/{id}/download_invoice_pdf`) via `requests` with browser cookies. No need to navigate to detail page. Invoices showing "Request" instead of "View" have no PDF available — skip them.
- **VTInfo CSV filenames**: Metadata encoded in filename `vtinfo_{vendor}_{location}_{invoicenum}_{YYYYMMDD}.csv` — not in the CSV content itself. Parser extracts via regex.
- **PFG CSV**: One CSV can contain multiple invoices. Parser returns a list, each invoice saved separately.
- **Session management**: Each scraper stores cookies/state in `storage_state*.json` and uses persistent browser profiles. Auto-login on session expiry with credentials from `~/vendor-scrapers/.env`.
- **Dedup**: Each scraper checks both local `data/downloaded_invoices.json` AND dashboard API (`/api/invoices/existing`) to avoid re-downloading.
- **SG date-less rows**: Rows with no date are summary/statement rows — scraper skips them automatically.

### Env Vars (in `~/vendor-scrapers/.env`)
- `SG_USER`, `SG_PASS` — Southern Glazer's Chatham login
- `SG_USER_DENNIS`, `SG_PASS_DENNIS` — Southern Glazer's Dennis login
- `MART_USER`, `MART_PASS` — Martignetti login
- `CC_USER`, `CC_PASS` — Craft Collective login
- `VTINFO_USER`, `VTINFO_PASS` — VTInfo login
- `PFG_USER`, `PFG_PASS` — PFG login
- `USF_USER`, `USF_PASS` — US Foods login

### CSV Import Routing (`/api/invoices/import-csv`)
- `vendor=vtinfo_lknife` or `vendor=vtinfo_colonial` → `parse_vtinfo_csv_invoice()`
- `vendor=pfg` → `parse_pfg_csv_invoice()` (returns list of invoices)
- Default (no vendor param) → `parse_csv_invoice()` (US Foods, single invoice)
- All CSV imports auto-confirm and generate invoice-style thumbnail images.

## Cron Jobs
- Toast sync: */10 min during business hours (run_sync.sh)
- MarginEdge sync: daily 10:30 AM
- Email poller: */5 min (NOTE: currently duplicated — two identical entries)
- Drive invoice watcher: */5 min
- Thermostat fetch: */5 min
- Sports guide scraper: daily 10 AM
- Vendor scrapers: daily 7 AM (run_all.sh)
- Nightly backup: 3 AM (tar + DB copy, 14-day retention)

## Security
- ALL secrets in `.env` (chmod 600, gitignored). NEVER hardcode API keys in source files.
- `google_credentials.json` — chmod 600, gitignored
- `ddns_update.py` / `ddns_update.sh` — load Cloudflare token from `.env` (CF_API_TOKEN, CF_ZONE_ID, CF_SSH_RECORD_ID)
- Flask SECRET_KEY loaded from .env
- No secrets in static HTML/JS files

## Known Issues (as of Mar 23, 2026)
1. **analytics.py line 331**: One where_clauses = [] missing voided/deleted filter
2. **Net sales discrepancy**: ~$181 off vs Toast ($4,242 vs $4,061 for Chatham Feb 13)
3. **Database size**: 1.1GB — orders/order_items/payments are the bulk. Consider pruning data older than 13 months + VACUUM
4. **Duplicate cron**: email_poller.py runs twice every 5 min
5. **VTInfo scraper**: Location picker and "View and Pay Invoices" button not found — portal may have changed UI. Needs manual investigation.
6. **SG Dennis #400097**: Statement/summary row ($3,190.52, no date) — correctly skipped by date filter but still shows in portal. Not a real invoice.

## Invoice System Architecture
- **Desktop**: Invoice History is default view. "Scan Invoice" is hidden on desktop.
- **Mobile**: Scan Invoice is default view (camera workflow).
- **Add Invoice button** (desktop): Dropdown with "Upload Invoice" (reuses OCR pipeline) and "Create Invoice" (manual entry modal).
- **Create Invoice modal**: 3-step flow — invoice type → details + line items/categories → success.
- **OCR pipeline**: Upload → Claude Vision extraction → validation → auto-confirm or manual review → reconciliation if discrepancy.
- **CSV import**: `POST /api/invoices/import-csv?vendor={hint}&location={loc}&filename={name}` — auto-confirms, generates invoice-style thumbnail image.
- **Multi-page PDFs**: Sent as native PDF documents to Claude API. NOT split into JPEG pages.
- **Post-confirm processing**: vendor item matching, anomaly detection, recipe costing (background thread).
- **Manual invoices**: `POST /api/invoices/create-manual` — inserted as status=confirmed, source=manual.
- **CSV thumbnail images**: `generate_csv_thumbnail()` in invoice_processor.py renders a full invoice-style image (800px wide, dark theme, line item table with pack/qty/price/extension, totals) using Pillow. Saved to `invoice_thumbnails/csv_{id}.jpg`. Updated via `image_path` + `thumbnail_path` in DB.
- **Vendor name normalization**: `_VENDOR_ALIASES` dict in invoice_processor.py maps OCR variants to canonical names (e.g., "Artignetti" → "Martignetti", "TIGNETTI COMPANIES" → "Martignetti").

## Responsive Design Pattern
All UI pages must treat mobile and desktop as separate layouts, not stretched versions of each other.

- **Mobile** (<768px): card-based, thumb-friendly, big tap targets, minimal columns
- **Desktop** (≥768px): table-based where data is involved, tighter rows, more columns, full page width

Pattern:
- Write mobile layout as the base
- Add `@media (min-width: 768px)` block for desktop overrides
- Content max-width on desktop: **1100px** (not 600px or 700px)
- Never use a single fixed max-width that serves both — they need different values

Pages that are **desktop-primary** (mobile is functional fallback only):
- Invoice History, Product Mapping, Recipe Editor, Order Guide, Analytics

Pages that are **mobile-primary** (desktop shows "mobile only" or simplified view):
- Smart Count / AI Inventory, Specials Admin, Mobile Ops Dashboard


## Service Accounts
- **dashboard@rednun.com** — Service account for all automated operations:
  -  — Gmail API (sending email alerts for applications, availability forms)
  -  — Google Drive + Sheets API (uploading PDFs, managing spreadsheets)
  - All Drive folders (applications, availability) are owned by this account
- **Previously** used invoice@rednun.com — renamed to dashboard@rednun.com as of April 2026

## Hiring / Application System
- **Form URL:** https://dashboard.rednun.com/hiring
- **Blueprint:**  → 
- **Template:** 
- **Drive folders (owned by dashboard@rednun.com):**
  - Root: Red Nun Employee Docs (1qzgYOEHub5CXlo7_S-CKvL8cWGU4r8fN)
  - Chatham: 1ZIbKK9xp8hKsHgVpmQ5gefS1tK1O4Qer
  - Dennis Port: 119iaRcv98V4tycrvs2SBx12CvFRqOua1
  - Availability: 1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA
- **Spreadsheet:** Applications 2026 in root folder (tabs: Sheet1, Chatham, Dennis Port, Both)
- **Email routing:** Chatham → matt@rednun.com, Dennis Port → alexis@rednun.com, Both/Neither → both
- **Auto-highlight:** Apps listing Mike/Michael Giorgio as reference get orange highlight
- **PDF naming:** Lastname_Firstname.pdf
- **Resume handling:** PDF resumes merged as page 2 of application PDF

## Availability System
- **Form URL:** https://dashboard.rednun.com/availability
- **Blueprint:**  → 
- **Template:** 
- **Drive folder:** Availability (1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA)
- **Email alert:** matt@rednun.com on submission
- **PDF naming:** Lastname_Firstname.pdf
- **Auto-close:** 10-second countdown after submission


## Service Accounts
- **dashboard@rednun.com** — Service account for all automated operations:
  - gmail_token.pickle — Gmail API (sending email alerts for applications, availability forms)
  - google_token.pickle — Google Drive + Sheets API (uploading PDFs, managing spreadsheets)
  - All Drive folders (applications, availability) are owned by this account
- **Previously** used invoice@rednun.com — renamed to dashboard@rednun.com as of April 2026

## Hiring / Application System
- **Form URL:** https://dashboard.rednun.com/hiring
- **Blueprint:** application_routes.py (application_bp)
- **Template:** templates/application.html
- **Drive folders (owned by dashboard@rednun.com):**
  - Root: Red Nun Employee Docs (1qzgYOEHub5CXlo7_S-CKvL8cWGU4r8fN)
  - Chatham: 1ZIbKK9xp8hKsHgVpmQ5gefS1tK1O4Qer
  - Dennis Port: 119iaRcv98V4tycrvs2SBx12CvFRqOua1
  - Availability: 1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA
- **Spreadsheet:** "Applications 2026" in root folder (tabs: Sheet1, Chatham, Dennis Port, Both)
- **Email routing:** Chatham -> matt@rednun.com, Dennis Port -> alexis@rednun.com, Both/Neither -> both
- **Auto-highlight:** Apps listing Mike/Michael Giorgio as reference get orange highlight
- **PDF naming:** Lastname_Firstname.pdf
- **Resume handling:** PDF resumes merged as page 2 of application PDF

## Availability System
- **Form URL:** https://dashboard.rednun.com/availability
- **Blueprint:** availability_routes.py (availability_bp)
- **Template:** templates/availability.html
- **Drive folder:** Availability (1fKLC6ZiIRrI7KkMqObDkFcK03H-cBgNA)
- **Email alert:** matt@rednun.com on submission
- **PDF naming:** Lastname_Firstname.pdf
- **Auto-close:** 10-second countdown after submission

## ⛔ DNS — NEVER TOUCH rednun.com
**DO NOT modify the  DNS record or the  CNAME under any circumstances.**

-  is a CNAME to  and MUST remain **DNS-only (proxied: False)** in Cloudflare. Proxying it through Cloudflare breaks Toast online ordering completely.
-  points to the restaurant's web host (162.120.94.90) — not this server. Do not touch it.
- **Toast online ordering going down = direct revenue loss.** This mistake cost ,000 in lost orders.
- The only DNS the DDNS script should ever update is .


## NEVER TOUCH rednun.com DNS
**DO NOT modify the `rednun.com` DNS record or `www.rednun.com` CNAME under any circumstances.**

- `www.rednun.com` is a CNAME to `sites.toasttab.com` and MUST stay **proxied: False** (DNS-only) in Cloudflare. Proxying it through Cloudflare breaks Toast online ordering completely.
- `rednun.com` points to the restaurant web host (162.120.94.90) — not this server. Do not touch it.
- Toast ordering going down = direct revenue loss. This mistake cost $1,000 in lost orders.
- The DDNS script (`monitoring/ddns.py`) must only ever update `dashboard.rednun.com`. Nothing else.
