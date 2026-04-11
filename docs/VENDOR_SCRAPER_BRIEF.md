# Vendor Invoice Scraper + Auto-Import Pipeline — Claude Code Brief

## Context

Red Nun operates two restaurant locations: **Red Nun Chatham #200** and **Red Nun Dennisport #100**. The dashboard at `dashboard.rednun.com` runs on a Beelink SER5 server at `/opt/rednun/`. It already has a full invoice processing pipeline (OCR via Claude Vision, math validation, vendor matching, product mapping, auto-confirm). Invoices currently arrive via manual upload or email polling (`invoice@rednun.com`).

The goal: **automate invoice collection** from 7 vendor portals using Playwright headless scrapers running on the same Beelink, then **auto-import** downloaded files into the dashboard pipeline — no manual upload, no email, no human intervention.

## What's Already Done

A working **US Foods scraper** exists at `~/usfoods-scraper/usfoods_invoice_scraper.py` on the Beelink. It:

- Uses Playwright async API with persistent browser context (session cookies in `./browser_profile/`)
- Runs headless on the Beelink (Python 3.12, Chromium)
- Scrapes both **invoices and credit memos** from the All Invoices page
- Downloads each as **CSV Full** format via the three-dot menu -> Download -> CSV Full radio -> Download button
- Switches between companies (Knockout Pizza, Red Nun Chatham #200, Red Nun Dennisport #100) using the header dropdown (`ion-button.usf-outline-green-button` -> `ion-popover` -> `ion-item`)
- Tracks downloaded invoices in `./data/downloaded_invoices.json` (keys: `invoice_123456` or `credit_789012`)
- Saves files as `usfoods_invoice_606658_20260322.csv` or `usfoods_credit_2990080_20260322.csv`
- Session exported from Windows PC using `export_session.py` (Playwright sync API, headed mode for login), then transferred to Beelink

## Part 1: CSV/Structured Data Import Endpoint

### What Exists

The dashboard already has:

- `POST /api/invoices/import-iif` — imports QuickBooks IIF tab-separated files, auto-confirms at 100% confidence (Session 31)
- `invoice_processor.py` — `parse_iif_invoice()`, `save_invoice()` with `source` field, OCR pipeline, math validation
- `vendor_item_matcher.py` — PATH 0: `vendor_item_code` exact match (highest priority), then fuzzy name matching
- `product_helpers.py` — `parse_pack_size()`, `get_or_create_product()`, `upsert_vendor_item()`
- `local_invoice_watcher.py` — watches `invoice_images/` folder, feeds PDFs/images into OCR pipeline
- `email_invoice_poller.py` — handles PDFs, ZIPs, IIF files, multi-page splits
- Location auto-detection from ship-to address (Session 23)
- US Foods specific OCR prompt guidance in `invoice_processor.py`

### What's Needed

**A new `POST /api/invoices/import-csv` endpoint** (similar pattern to `import-iif`) that:

1. Accepts a US Foods "CSV Full" file
2. Parses it to extract: vendor name, invoice number, invoice date, ship-to address, line items (product code, description, pack/size, qty ordered, qty shipped, unit price, extension/total)
3. Maps to the existing `save_invoice()` schema with `source='csv'`
4. Runs `vendor_item_matcher` for product matching (use the US Foods item code as `vendor_item_code` — PATH 0 exact match)
5. Runs `parse_pack_size()` on pack/size fields
6. Auto-confirms at 100% confidence (structured data = no OCR uncertainty)
7. Detects location from ship-to address using existing `detect_location_from_address()`. The scraper also knows which company it downloaded for (Chatham vs Dennisport), so the import script should pass `location` explicitly as a parameter (e.g., `?location=chatham`) as a reliable fallback. Company-to-location mapping: "Red Nun Chatham #200" = chatham, "Red Nun Dennisport #100" = dennis
8. Handles credits (negative amounts) — the CSV will have credit memos mixed in

**A new `POST /api/invoices/import-pdf-file` endpoint** (or reuse local_invoice_watcher pattern) that:

1. Accepts a PDF file path on disk
2. Feeds it through the existing OCR pipeline (same as `local_invoice_watcher.py`)
3. This is for vendors that only offer PDF downloads

**A local import script** (`import_downloads.py` or integrate into each scraper) that:

1. Watches the scraper download directories
2. For CSV files: calls `/api/invoices/import-csv`
3. For PDF files: copies to `invoice_images/` for `local_invoice_watcher.py` to pick up, OR calls the OCR pipeline directly
4. Moves processed files to an `./archived/` folder
5. Logs success/failure

### US Foods CSV Format

The "CSV Full" download from US Foods is a standard CSV with columns like:

```
Customer Number, Customer Name, Invoice Number, Invoice Date, PO Number, Order Number, Ship Date, Item Number, Brand, Description, Pack, Size, UOM, Qty Ordered, Qty Shipped, Weight, Catch Weight, Unit Price, Extension, ...
```

Key fields to map:
- `Item Number` -> `vendor_item_code` (for PATH 0 exact matching)
- `Description` -> item description
- `Pack` + `Size` + `UOM` -> pack_size string for `parse_pack_size()`
- `Qty Shipped` -> quantity (use shipped, not ordered)
- `Unit Price` -> unit_price
- `Extension` -> total_price
- `Invoice Number` -> invoice_number
- `Invoice Date` -> invoice_date
- `Customer Name` / address fields -> location detection

## Part 2: Additional Vendor Scrapers

Each vendor needs a Playwright scraper module following the same pattern as US Foods. All run on the Beelink with persistent browser contexts (session cookies).

### Architecture

```
~/vendor-scrapers/
  usfoods/
    scraper.py          # existing, working
    export_session.py   # run on Windows PC to capture login
    browser_profile/    # Playwright persistent context
    downloads/          # raw downloaded files
    data/               # tracking JSON
  pfg/
    scraper.py
    export_session.py
    browser_profile/
    downloads/
    data/
  vtinfo/              # L. Knife + Colonial (same portal)
    scraper.py
    export_session.py
    browser_profile/
    downloads/
    data/
  southern-glazers/
    scraper_chatham.py  # separate login
    scraper_dennis.py   # separate login
    export_session_chatham.py
    export_session_dennis.py
    browser_profile_chatham/
    browser_profile_dennis/
    downloads/
    data/
  martignetti/
    scraper.py
    export_session.py
    browser_profile/
    downloads/
    data/
  craft-collective/
    scraper.py
    export_session.py
    browser_profile/
    downloads/
    data/
  common/
    base_scraper.py     # shared utilities
    import_downloads.py # watches all download dirs, imports to dashboard
  run_all.sh            # master cron script
```

### CRITICAL: Duplicate Avoidance — Check Dashboard Before Downloading

**Every scraper MUST query the dashboard database before downloading an invoice.** The dashboard's `scanned_invoices` table already tracks every imported invoice by `vendor_name` + `invoice_number`. Before downloading any invoice from a vendor portal, the scraper should:

1. **On startup**, call a dashboard API endpoint (e.g., `GET /api/invoices/existing?vendor=US+Foods`) that returns a set of already-imported invoice numbers for that vendor
2. **For each invoice in the portal list**, check if that invoice number is already in the set — if yes, **skip it entirely** (don't download, don't process)
3. **Also maintain the local `downloaded_invoices.json` tracking file** as a secondary check (covers the case where a file was downloaded but not yet imported)

This means the dashboard needs a **new lightweight API endpoint**:

```
GET /api/invoices/existing?vendor={vendor_name}
```

Returns: `{"invoice_numbers": ["606658", "607123", "2990080", ...]}` — all invoice numbers already in `scanned_invoices` for that vendor (any status: pending, confirmed, etc.)

**Why both checks?** The local JSON tracks what's been downloaded (prevents re-downloading during the same cron cycle before import runs). The dashboard API tracks what's actually been imported (prevents downloading invoices that were manually uploaded or arrived via email). Together they ensure zero duplicates.

The US Foods scraper already has local tracking (`./data/downloaded_invoices.json`). All new scrapers must implement both local tracking AND dashboard API checking. **Update the US Foods scraper to also check the dashboard API.**

### Vendor Details

#### 1. PFG / Performance Food Group
- **Portal**: https://www.customerfirstsolutions.com/
- **Auth**: 1 login, standard username/password, no 2FA
- **Download formats**: PDF (download button on invoice popup). Portal also supports CSV/Excel/TXT but the natural flow produces PDF.
- **Locations**: Company switcher in **top-right of main screen**. Default is Chatham ("Red Nun Bar & Grill Chat"). Click to get dropdown with radio buttons to switch to "Red Nun Dennis Port". Switching takes you back to home screen.
- **Scraper approach**: Login -> switch company if needed -> navigate to invoices -> download PDF for each -> feed through OCR pipeline. Try CSV/Excel download if available (auto-confirm), otherwise PDF through OCR.

**Click-by-click flow:**
1. Login -> **home screen** appears (usually defaults to Chatham)
2. **Company switcher**: top-right, shows "Red Nun Bar & Grill Chat" — click to get dropdown with radio buttons, select "Red Nun Dennis Port" to switch (returns to home screen)
3. Top navigation bar: click **"Invoices"** link -> opens a dropdown menu
4. In the dropdown, click **"Invoices"** again -> opens invoice list with all invoices
5. Each invoice row has **three dots** on the right -> click to open selector
6. Selector shows **"Download Invoice"** and "Proof of Delivery" -> click "Download Invoice"
7. **Popup window** opens showing the invoice -> has **"Print"** and **"Download"** buttons
8. Click **"Download"** -> triggers save as PDF
9. Repeat for each new invoice
10. Switch company (radio button in top-right dropdown) -> back to home screen -> repeat from step 3

**Key elements to find in DOM:**
- Company switcher (top-right, text like "Red Nun Bar & Grill Chat")
- Radio button dropdown for company selection
- "Invoices" link in top navigation bar
- "Invoices" in the dropdown submenu
- Three-dot menu on each invoice row
- "Download Invoice" option in the three-dot menu
- "Download" button in the popup window (triggers PDF save)

**Location mapping:**
- "Red Nun Bar & Grill Chat" (or similar) -> chatham
- "Red Nun Dennis Port" (or similar) -> dennis

#### 2. L. Knife & Sons + Colonial Beverage
- **Portal**: https://apps.vtinfo.com/retailer-portal/ (VIP Retailer Portal)
- **Auth**: 1 shared login covers both vendors, standard username/password, no 2FA
- **Download formats**: CSV (cloud download icon on individual invoice view)
- **Locations**: Has location dropdown in top-right corner of home screen
- **Scraper approach**: Single scraper handles both vendors since same portal/login

**Click-by-click flow:**
1. Login page -> enter credentials -> lands on vendor selection screen
2. Select "L. Knife" (or "Colonial") from vendor list — this must be done first after login
3. Home screen appears — **location dropdown in top-right corner** (select Chatham or Dennisport)
4. Click **"View and Pay Invoices"** tab — opens list of current invoices
5. Click on an **invoice number** in the list — opens the full invoice detail view
6. Top-left of invoice detail: **cloud icon with down arrow** — click to download CSV of that invoice
7. Repeat for each invoice, then switch vendor (Colonial) and repeat the whole flow

**Key elements to find in DOM:**
- Vendor selector (after login)
- Location dropdown (top-right)
- "View and Pay Invoices" tab/link
- Invoice number links in the list
- Cloud download icon (top-left of invoice detail)

#### 3. Southern Glazer's (ex-Horizon)
- **Portal**: https://portal2.ftnirdc.com/en/72752
- **Auth**: **2 separate logins**. mike@rednun.com = Chatham, mgiorgio@rednun.com = Dennis. Standard username/password, no 2FA
- **Download formats**: PDF only (download from invoice detail splash page)
- **Scraper approach**: Two separate browser profiles. Each logs in independently. PDFs go through OCR pipeline.

**Click-by-click flow:**
1. Login page -> enter credentials -> **lands on invoice list page**
2. Click on an **invoice number** in the list -> **opens a NEW BROWSER WINDOW/TAB** with the PDF
3. Download the PDF from the new window (Playwright: listen for `context.on('page')` event to catch the new tab, then use `page.pdf()` or intercept the download)
4. Close the new tab, return to invoice list
5. Repeat for each new invoice
6. Run again with the other login for the other location

**Key elements to find in DOM:**
- Invoice list table/rows
- Invoice number links
- PDF download button on the splash/preview page
- Invoice number, date, amount for tracking

**Auth mapping:**
- mike@rednun.com -> chatham
- mgiorgio@rednun.com -> dennis

**Browser profiles:**
- `browser_profile_chatham/` (mike@rednun.com session)
- `browser_profile_dennis/` (mgiorgio@rednun.com session)

#### 4. Martignetti
- **Portal**: https://martignettiexchange.com/profile/login?backurl=/profile/invoices/
- **Auth**: 1 login, standard username/password, no 2FA
- **Download formats**: PDF only (no CSV/Excel option)
- **Locations**: Both locations in same account. **"Red Nun"** = Dennis location, **"Red Nun Bar & Grill"** = Chatham location
- **Scraper approach**: Login -> lands directly on invoices page -> download PDFs -> feed through OCR pipeline

**Click-by-click flow:**
1. Login page -> enter credentials -> **lands straight on invoices list** (no extra navigation needed)
2. All invoices for both locations are in the same list
3. Each row has a **"PDF" column** with a paper icon (folded corner top-left) — click it to start PDF download
4. Repeat for each new invoice
5. Downloaded PDFs go through the existing OCR pipeline (not CSV import — these are PDFs)

**Key elements to find in DOM:**
- Invoice list table/rows
- PDF download icon in each row (paper with folded corner)
- Invoice number, date, amount in each row for tracking
- Customer/location name in each row ("Red Nun" vs "Red Nun Bar & Grill") for location detection

**Location mapping:**
- "Red Nun" -> dennis
- "Red Nun Bar & Grill" -> chatham

#### 5. Craft Collective
- **Portal**: https://www.termsync.com/ (TermSync/Esker invoice portal)
- **Auth**: 1 login (mgiorgio@rednun.com), standard username/password, no 2FA
- **Download formats**: PDF only (opens in new window from invoice detail page)
- **Locations**: **Dennis only** — Craft Collective does NOT serve the Chatham location. All invoices under this account are for Red Nun Dennisport #100
- **Scraper approach**: Login -> navigate to invoice listing -> download PDFs -> feed through OCR pipeline. Filter to current year only.

**Click-by-click flow:**
1. Login (mgiorgio@rednun.com) -> **vendor home screen** appears
2. Top-left navigation: "Dashboard" and "Messages" highlighted in red. Also on that line: **"Account Statement"** and **"Invoices"** (with dropdown arrow)
3. Hover on "Invoices" dropdown -> shows **"Invoice Listing"** and **"Credits Listing"**
4. Click **"Invoice Listing"** -> opens all invoices list
5. **IMPORTANT: Filter to current year only** — there are old 2025 invoices in the list, skip those
6. Click on an **invoice number** in the list -> opens invoice detail page
7. Under **"Available Actions"** section: find the line that says **"View Invoice PDF"** with a small PDF thumbnail icon
8. Click "View Invoice PDF" -> **opens NEW BROWSER WINDOW/TAB** with PDF splash page (Playwright: listen for `context.on('page')` event)
9. On the PDF splash page: **download or print** button -> download the PDF
10. Close the new tab, return to invoice list
11. Repeat for each new invoice
12. Also scrape **"Credits Listing"** (same dropdown) for credit memos — same download flow

**Key elements to find in DOM:**
- "Invoices" dropdown in top navigation
- "Invoice Listing" and "Credits Listing" menu items
- Invoice list table/rows (with date filtering — skip pre-2026)
- Invoice number links in the list
- "Available Actions" section on invoice detail page
- "View Invoice PDF" link with PDF thumbnail
- Download button on the PDF splash page (new window)

**Note:** The "Credits Listing" is a separate page from "Invoice Listing" — scraper needs to visit both.

### Authentication — No Passwords in Code

**CRITICAL: No vendor passwords are stored in code, config files, environment variables, or anywhere on the Beelink.** Authentication is handled entirely through Playwright persistent browser contexts:

1. Mike runs `export_session.py` on his Windows PC — it opens the vendor portal in a real browser
2. Mike logs in manually (types password himself)
3. Playwright saves session cookies/state to `browser_profile/` directory
4. The browser profile is transferred to the Beelink
5. The headless scraper reuses the saved session cookies — no password needed

**Session expiry handling:** Each scraper must detect expired sessions (check for login page redirect, "sign in" text, etc.) and exit with a clear message like the US Foods scraper does:
```
SESSION EXPIRED — login required.
Run export_session.py on your home PC to refresh,
then transfer browser_profile/ to the Beelink.
```

For Southern Glazer's (2 logins), Mike exports two sessions — one for each email/location.

### Scraper Development Process (for each vendor)

1. **Export session**: Create `export_session.py` (Playwright sync API, headed mode) that opens the portal, lets Mike log in, saves browser profile. Run on Windows PC.
2. **Transfer session**: Tar+gzip browser_profile, transfer to Beelink via reverse SSH tunnel (HTTP server method).
3. **Explore DOM**: Run a headless debug script to dump page structure, find invoice list elements, download buttons, company/location switchers.
4. **Build scraper**: Follow US Foods pattern — navigate to invoices, scrape list, download each new one, track in JSON.
5. **Determine format**: Check if CSV/Excel download is available. If yes, use structured import (auto-confirm). If PDF only, feed through OCR pipeline.
6. **Test**: Run headless on Beelink, verify downloads, verify import into dashboard.

### Session Export Pattern

Each `export_session.py` follows this template:

```python
from playwright.sync_api import sync_playwright
from pathlib import Path

PORTAL_URL = "https://vendor-portal.com/login"
PROFILE_DIR = Path("./browser_profile")

def main():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1600, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(PORTAL_URL, wait_until="networkidle")
        input("\n>>> Log in to the portal, then press Enter here to save session...")
        context.close()
    print("Done - session saved to", PROFILE_DIR)

if __name__ == "__main__":
    main()
```

**Important**: Must use `sync_playwright` (not async) because Mike's Windows PC runs Python 3.14 which has asyncio compatibility issues with Playwright's async API.

## Part 3: Cron Automation

### Schedule

```cron
# Run all vendor scrapers daily at 6 AM ET
0 6 * * * cd ~/vendor-scrapers && ./run_all.sh >> /var/log/vendor-scrapers.log 2>&1

# Import downloaded files every 15 minutes
*/15 * * * * cd ~/vendor-scrapers && python3 common/import_downloads.py >> /var/log/vendor-import.log 2>&1
```

### run_all.sh

```bash
#!/bin/bash
echo "=== Vendor Scraper Run: $(date) ==="

# US Foods
cd ~/vendor-scrapers/usfoods && python3 scraper.py 2>&1

# PFG
cd ~/vendor-scrapers/pfg && python3 scraper.py 2>&1

# L. Knife + Colonial (VIP)
cd ~/vendor-scrapers/vtinfo && python3 scraper.py 2>&1

# Southern Glazer's (two logins)
cd ~/vendor-scrapers/southern-glazers && python3 scraper_chatham.py 2>&1
cd ~/vendor-scrapers/southern-glazers && python3 scraper_dennis.py 2>&1

# Martignetti
cd ~/vendor-scrapers/martignetti && python3 scraper.py 2>&1

# Craft Collective
cd ~/vendor-scrapers/craft-collective && python3 scraper.py 2>&1

echo "=== Done: $(date) ==="
```

## Part 4: Dashboard Integration Points

### Existing Code to Reference

| File | What It Does | Relevant For |
|------|-------------|--------------|
| `invoice_processor.py` | OCR, save_invoice(), parse_iif_invoice(), detect_location_from_address() | CSV import endpoint, location detection |
| `invoice_routes.py` | /api/invoices/* endpoints, import-iif | New import-csv endpoint pattern |
| `vendor_item_matcher.py` | PATH 0 vendor_item_code match, fuzzy matching | Matching CSV line items to products |
| `product_helpers.py` | parse_pack_size(), get_or_create_product() | Pack size parsing from CSV fields |
| `local_invoice_watcher.py` | Watches invoice_images/ folder | PDF import path |
| `email_invoice_poller.py` | Gmail polling, PDF/ZIP/IIF handling | Reference for multi-format handling |

### Database Tables

- `scanned_invoices` — main invoice table. Key columns: vendor_name, invoice_number, invoice_date, total_amount, tax_amount, location, source (scanned/iif/csv/manual), status (pending/confirmed), confidence_score, is_duplicate
- `scanned_invoice_items` — line items. Key columns: description, quantity, unit_price, total_price, category, vendor_item_code, pack_size
- `vendor_items` — vendor-specific product records. Key columns: vendor_id, vendor_item_code, description, pack_size, unit_price, match_confidence
- `products` — canonical products (634 rows). Key columns: name, display_name, category, yield_pct

### Vendor Names in Dashboard

These exact names must be used (from `invoice_processor.py` vendor aliases):

- **US Foods** (vendor in DB)
- **Performance Foodservice** (PFG's brand name in invoices)
- **L. Knife & Son, Inc.** (check exact name)
- **Colonial Spirits** or **Colonial Beverage** (check exact name)
- **Southern Glazer's Beverage Company** (aliases: SOUTHERN GLAZER'S, Southern Glazers)
- **Martignetti Companies** (alias: Artignetti)
- **Craft Collective** (check exact name)

### Priority Order — ONE VENDOR AT A TIME

**CRITICAL: Build and fully test each vendor before moving to the next.** Do NOT scaffold multiple scrapers at once. The workflow for each vendor is: build scraper -> test scraper -> build/verify import into dashboard -> confirm invoices appear correctly -> then move to next vendor.

1. **US Foods CSV import endpoint** — scraper already working, CSVs already downloading. Build the `/api/invoices/import-csv` endpoint + `import_downloads.py` to auto-import those CSVs into the dashboard. Test end-to-end: CSV -> import -> confirmed invoice in dashboard with correct line items, vendor matching, and location.
2. **PFG scraper + import** — export session, build scraper, test downloads, verify PDF import through OCR pipeline.
3. **L. Knife & Sons + Colonial scraper + import** — export session (covers both vendors), build scraper, test CSV downloads, verify import.
4. **Southern Glazer's scraper + import** — export both sessions (Chatham + Dennis), build scraper, test PDF downloads, verify OCR import.
5. **Martignetti scraper + import** — export session, build scraper, test PDF downloads, verify OCR import.
6. **Craft Collective scraper + import** — export session, build scraper, test PDF downloads, verify OCR import.

**After each vendor:** Mike will verify the invoices look correct in the dashboard before proceeding to the next vendor.

## Part 5: Automated Invoice Payment via Vendor Portals (Future Phase)

Once scraping and import are working for all vendors, the next phase is **automating invoice payments** through the same vendor portals, triggered from the Bill Pay module in dashboard.rednun.com.

### What Exists (Bill Pay — Session 41)

The dashboard already has a full Bill Pay module at `/opt/rednun/`:

- `billpay_routes.py` — Outstanding invoices, AP aging summary, vendor setup CRUD, payment recording, void, check printing (single + batch), CSV export
- `check_printer.py` — PDF check generation for DocuGard top-check voucher stock with MICR E-13B glyph rendering and signature overlay
- `static/manage.html` — 4 Bill Pay views: Outstanding (aging cards, filterable invoice table, select + pay), Payments history, Vendor Setup (remittance, terms, method), Check Setup
- Database tables: `vendor_bill_pay`, `ap_payments`, `ap_payment_invoices`, `check_config`
- Invoice columns: `due_date`, `amount_paid`, `balance`, `payment_status`

### What's Needed

**A payment automation layer** that connects the dashboard Bill Pay module to vendor portal payment flows:

1. **Mark invoices for payment in the dashboard** — Mike selects invoices in the Bill Pay Outstanding view and approves payment
2. **Payment script per vendor** — Playwright automation that logs into the vendor portal and submits payment for the selected invoices
3. **Payment confirmation** — Script confirms payment went through on the vendor portal, then updates the dashboard (`mark_invoice_paid()`)

### Vendor Payment Flows (to be explored)

Each vendor portal has its own payment flow. These need DOM exploration similar to the invoice scraping:

| Vendor | Portal | Payment Method | Notes |
|--------|--------|---------------|-------|
| **US Foods** | order.usfoods.com | TBD — likely has "Pay" option in three-dot menu or "Go To Make A Payment" button on invoices page | Already saw "Go To Make A Payment" and "Pay" in popover options |
| **PFG** | customerfirstsolutions.com | TBD | Three-dot menu had "Proof of Delivery" — may also have payment option |
| **L. Knife / Colonial** | apps.vtinfo.com | TBD — "View and **Pay** Invoices" tab suggests payment is built in | Tab name implies payment flow exists |
| **Southern Glazer's** | portal2.ftnirdc.com | TBD | Separate logins per location, so payment account should be straightforward |
| **Martignetti** | martignettiexchange.com | TBD | **WARNING: see bank account note below** |
| **Craft Collective** | termsync.com | TBD — TermSync is specifically designed for payments | TermSync's core feature is AR payments |

### Vendor-Specific Payment Gotchas

**Martignetti — Bank Account Selection:**
Martignetti has both operating accounts (Chatham + Dennis) visible in the same login, but the payment screen **does NOT auto-select the correct bank account based on the invoice location**. It defaults to the Dennis bank account. When paying a Chatham invoice, you must **manually switch to the Chatham bank account** before submitting payment. The automation script MUST:
1. Determine which location the invoice belongs to ("Red Nun" = Dennis, "Red Nun Bar & Grill" = Chatham)
2. Select the correct bank account on the payment screen before confirming
3. **NEVER submit payment with the wrong bank account** — verify the selected bank matches the invoice location

**L. Knife / Colonial — Location-Scoped Payments:**
L. Knife and Colonial require switching locations (top-right dropdown) to view and pay invoices. Each location has only 1 stored payment method. This is simpler than Martignetti — the scraper already switches locations to see invoices, so payment will use the correct stored method automatically. No bank account selection needed.

**US Foods — Company Switcher Scoped:**
Same as L. Knife/Colonial — you switch companies in the portal to see each location's invoices. Each company has its own payment method. No bank account selection needed.

**PFG — Company Switcher Scoped:**
Same pattern — radio button company switcher (top-right). Each location has its own payment method. No bank account selection needed.

**Southern Glazer's — Login Scoped:**
Separate logins per location (mike@rednun.com = Chatham, mgiorgio@rednun.com = Dennis). Each login has only 1 stored payment method. Simplest case — the login determines everything.

**Craft Collective (TermSync) — TBD:**
Payment flow needs exploration. TermSync is designed for payments so it likely has a straightforward pay button.

Document each vendor's payment flow carefully during testing, especially around bank account / payment method selection per location.

### Architecture

```
Dashboard Bill Pay                    Vendor Portal
┌─────────────────┐                  ┌──────────────────┐
│ Mike selects     │                  │                  │
│ invoices to pay  │──── API call ───>│ Playwright script │
│ in Outstanding   │                  │ submits payment  │
│ view             │                  │ on vendor portal │
│                  │<── confirmation ─│                  │
│ mark_invoice_paid│                  │                  │
└─────────────────┘                  └──────────────────┘
```

### Implementation Approach

1. **New endpoint**: `POST /api/billpay/submit-portal-payment` — accepts vendor name + invoice numbers, triggers the appropriate vendor payment script
2. **Per-vendor payment scripts** — separate Playwright modules, one per vendor, that handle the portal payment flow (select invoices, enter payment details, confirm)
3. **Payment method per vendor** — stored in `vendor_bill_pay` table (ACH, credit card, etc.). The script uses whichever method is configured.
4. **Safety**: Require explicit confirmation before submitting payment. Log every action. Never auto-pay without Mike's approval.
5. **Fallback**: If automated payment fails, log the error and leave the invoice unpaid in the dashboard so Mike can pay manually through the portal.

### Priority

This is a **FUTURE PHASE** — build it AFTER all vendor scrapers and invoice imports are working and verified. Payment automation is higher risk (moving money) and needs careful testing. Start with one vendor (probably TermSync/Craft Collective since that platform is designed for payments) and expand from there.

## Part 6: Session Health Monitoring & Login Recovery

Persistent browser sessions WILL expire. When they do, the scraper lands on a login page instead of the invoice page. We need: (1) detection, (2) alerting, (3) a recovery workflow.

### Detection — Every Scraper Must Check

After navigating to the expected invoice page, every scraper must verify it actually got there and didn't land on a login/auth page instead. Pattern:

```python
async def check_session_health(page, vendor_name):
    """Returns True if session is valid, False if login page detected."""
    # Check for common login indicators
    login_indicators = [
        'input[type="password"]',
        'form[action*="login"]',
        'form[action*="signin"]',
        '#login', '.login-form', '#signin',
        'button:has-text("Log In")', 'button:has-text("Sign In")',
    ]
    for selector in login_indicators:
        if await page.query_selector(selector):
            await mark_session_expired(vendor_name)
            return False
    return True
```

Each vendor scraper should also have vendor-specific checks (e.g., US Foods: check for `ion-button.usf-outline-green-button` which only exists when logged in). If the session check fails, the scraper should:

1. **Stop immediately** — do NOT attempt to log in (no passwords stored)
2. **Mark the vendor as expired** in a status file AND the dashboard database
3. **Send an alert email** to Mike
4. **Exit cleanly** with a non-zero exit code so `run_all.sh` can log the failure

### Status Tracking — Dashboard Database

New table:

```sql
CREATE TABLE IF NOT EXISTS vendor_session_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'unknown',  -- 'healthy', 'expired', 'unknown'
    last_successful_scrape TEXT,              -- ISO datetime
    last_failure TEXT,                        -- ISO datetime
    failure_reason TEXT,                      -- 'session_expired', 'timeout', 'portal_error', etc.
    invoices_scraped_last_run INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
```

Each scraper updates this table at the end of every run — whether it succeeded or failed. The `run_all.sh` script should update status to 'expired' if a scraper exits with non-zero code.

### Alert Email

When a session expires, send an email to Mike (mgiorgio@rednun.com) using the existing email infrastructure on the Beelink:

```
Subject: [Red Nun Dashboard] ⚠️ {Vendor Name} session expired — login needed

The {vendor_name} scraper could not access the invoice portal because the
login session has expired.

Vendor: {vendor_name}
Portal: {portal_url}
Last successful scrape: {last_success_date}
Invoices pending since: {days_since} days

TO FIX:
1. On your Windows PC, run: python export_session_{vendor}.py
2. Log in when the browser opens
3. Close the browser after login completes
4. Transfer the session to the Beelink (the script will give you the command)

The scraper will automatically resume on the next cron run once the
session is refreshed.
```

Use Python `smtplib` or the existing Gmail OAuth setup in `email_invoice_poller.py` — whichever is simpler. If Gmail OAuth is already configured for sending (not just receiving), reuse that.

### Dashboard Status Page

Add a **"Vendor Sessions"** panel to the dashboard (could be a section in the existing admin/settings area, or a new route like `/vendor-status`). Shows:

| Vendor | Status | Last Scrape | Invoices Last Run | Action |
|--------|--------|-------------|-------------------|--------|
| US Foods | 🟢 Healthy | 2 hours ago | 3 new | — |
| PFG | 🔴 Expired | 3 days ago | 0 | Re-login needed |
| L. Knife | 🟢 Healthy | 2 hours ago | 1 new | — |

This is a read-only status page — it pulls from `vendor_session_status` table. No login buttons on the dashboard (logins happen on Mike's PC via export_session scripts).

### API Endpoint for Scraper Status Updates

```
POST /api/vendor-sessions/update
Body: {
    "vendor_name": "US Foods",
    "status": "healthy",          // or "expired"
    "invoices_scraped": 3,
    "failure_reason": null         // or "session_expired", "timeout", etc.
}
```

Each scraper calls this at the end of its run. The endpoint updates `vendor_session_status` and triggers the alert email if status changed to "expired".

### Recovery Workflow (Mike's Steps)

When Mike gets the alert email:

1. **Open PowerShell** on his Windows PC
2. **Run**: `python ~/vendor-scrapers/{vendor}/export_session.py` (or wherever it's saved locally)
3. **A Chromium window opens** — Mike logs into the vendor portal manually
4. **Close the browser** — the script saves the session cookies to `browser_profile/`
5. **Transfer to Beelink** — the script prints the exact `scp` or HTTP transfer command to run
6. **Done** — the next cron run will pick up the fresh session and resume scraping

The export_session scripts should be written to make this as simple as possible — ideally a single command that handles everything including the transfer. Print clear instructions. Mike is not a developer, so the recovery flow should be as close to "double-click and log in" as possible.

### Session Lifetime Expectations

Most vendor portals keep sessions alive for 7-30 days with persistent cookies. Some may expire sooner. After the scrapers are running, Mike will get a feel for how often each vendor's session needs refreshing. If a vendor expires frequently (every few days), consider:

- Running the scraper more often (keeps the session active via regular use)
- Checking if the portal has a "remember me" or "keep me logged in" option during session export

## CRITICAL: Existing Production Codebase — Do NOT Overwrite

**The dashboard at `/opt/rednun/` is a LIVE PRODUCTION system.** Follow these rules:

1. **Never overwrite or replace existing files.** Add new endpoints, new functions, new routes — do not modify existing working code unless absolutely necessary for integration.
2. **Add new endpoints** to existing route files (e.g., add `/api/invoices/import-csv` to `invoice_routes.py`) or create new blueprint files. Do NOT restructure or refactor existing code.
3. **Add new import functions** (e.g., `parse_csv_invoice()`) alongside existing ones (e.g., `parse_iif_invoice()`) in `invoice_processor.py`. Follow the same patterns.
4. **Reuse existing functions** — `save_invoice()`, `detect_location_from_address()`, `vendor_item_matcher`, `parse_pack_size()`, etc. are all working. Call them, don't rewrite them.
5. **Do NOT modify the database schema** unless adding new columns. Never drop or alter existing columns.
6. **Test changes** with `sudo systemctl restart rednun` after modifying dashboard code (gunicorn caches old code).
7. **The US Foods scraper at `~/usfoods-scraper/` is working and downloading invoices + credits.** Do not rewrite it. The `import_downloads.py` script should read from its `./downloads/` directory.
8. **Read the existing code first** before writing anything. Understand the patterns in `invoice_processor.py`, `invoice_routes.py`, and `vendor_item_matcher.py` before adding to them.
9. **Back up the database** before any schema changes: `cp /opt/rednun/toast_data.db /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db`

## Environment

- **Server**: Beelink SER5, Ubuntu 24.04, Python 3.12, at `/opt/rednun/` (dashboard) and `~/usfoods-scraper/` (current scraper location)
- **Dashboard**: Flask + Gunicorn + Nginx, SQLite (`toast_data.db`), Cloudflare proxy
- **Playwright**: Already installed with Chromium on the Beelink
- **Mike's PC**: Windows, Python 3.14, used for session exports only (must use sync_playwright)
- **SSH access**: `ssh -p 2222 rednun@ssh.rednun.com`
- **File transfer**: Reverse SSH tunnel (`ssh -p 2222 -R 9999:127.0.0.1:8888 rednun@ssh.rednun.com`) + Python HTTP server on Windows
