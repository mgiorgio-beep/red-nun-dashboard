# Bank Reconcile + GL Accounts — Session Brief

**Last updated:** 2026-04-29
**Session goal:** Build a bank-statement-driven reconcile workflow that imports CCF PDF statements, dedupes against the existing register, and codes statement-only rows to the right GL accounts for an internal P&L (Red Nun is no longer using QBO; the dashboard IS the books).

**Deployed at HEAD `ac45433` on `origin/main`.** All commits below are pushed and live on `dashboard.rednun.com`.

---

## Architectural model agreed

The dashboard is the source of truth for accounting. P&L is derived from existing data:

- **COGS / expenses** → sum of `scanned_invoice_items` rolled up by `category_type` (Wine COGS, Liquor COGS, Food, etc.). Already coded at OCR time on each invoice.
- **Wages / payroll taxes** → sum from `payroll_runs` totals (total_wages, total_ee_taxes, etc.). Already coded when 7shifts payroll JE is uploaded.
- **Sales** → from Toast.
- **Random ops expenses** → imported from bank statements. **This is the gap the new feature fills.**

**Therefore:** the GL on a bank-register row is the *offsetting* account (the non-bank side of the journal entry). For bills/payroll/known deposits the offset is mechanical (A/P, Payroll Liabilities, Cash/CC Sales, Sales Tax Payable). User input is only required for genuinely new statement-only transactions (debit-card swipes, Venmo, ACH bills, etc.).

| Row source | What's already booked upstream | Bank-register GL | User input? |
|---|---|---|---|
| `bill_pay` (vendor_payments) | line items in scanned_invoice_items | A/P (implicit) | No — show "→ Bill #N" link |
| `payroll` (payroll_checks) | Wages + Taxes in payroll_runs | Payroll Liabilities (implicit) | No — show "→ Payroll Run #N" link |
| `deposit` (bank_deposits, QBO sync) | Toast/POS daily journal | Credit Card Sales / Cash Sales | Auto-coded by rules |
| `manual` (manual_bank_entries from statement) | nothing — bank tx is the original entry | depends — DAVO→Sales Tax Payable, Home Depot→Repairs, etc. | Yes — code once, rule learns |

---

## What was built

### 1. CCF bank statement parser
**`integrations/bank_statements/processor.py`** — pdfplumber-based extractor tuned to Cape Cod Five layout. Handles M/DD or MM/DD dates, sub-dollar amounts (`.27`), multi-line memos, page-break repeats, "Statement Dates X thru Y" header. Verified against the real Chatham January 2026 PDF: parses all 218 transactions, totals reconcile to the penny ($34,013.57 → $25,785.04, $92,398.46 out, $84,169.93 in, zero warnings).

Public entry point: `parse_bank_statement_pdf(pdf_bytes)` → `{ bank, account_last4, period_start, period_end, beginning_balance, ending_balance, total_debits, total_credits, transactions[], warnings[] }`.

### 2. Bank Reconcile blueprint
**`routes/bank_reconcile_routes.py`** at `/api/bank-reconcile/*`:
- `POST /upload` — accepts PDF + `account_id` (multipart). Parses, validates the statement matches the selected account (compares `account_last4` from PDF header to the bank_account record — refuses upload with red error if mismatch), saves PDF to disk, runs the matcher, persists result.
- `POST /import` — body `{upload_id, indexes[], also_clear_matches}`. Inserts selected parsed rows as `manual_bank_entries`. Optionally flips `cleared=1` on matched register rows.
- `GET /uploads`, `GET /uploads/<id>`, `DELETE /uploads/<id>` — history + reopen + admin delete.
- `GET /uploads/<id>/raw-text` — diagnostic that returns the pdfplumber-extracted text. Useful for tuning the parser regex if a future statement format differs.

**Tables added:** `bank_statement_uploads` + `manual_bank_entries.statement_upload_id` column.

### 3. Bank Reconcile UI
**`web/static/bank_reconcile.html`** at `/bank-reconcile`. Numbered sections:
1. Account picker (Chatham 5975 / Dennis 2757)
2. Drag-and-drop PDF upload zone
3. Review & Import — summary cards, warnings, action bar (selection count + "Mark matched register rows as cleared" + Import button), parsed-row table with match pills (`✓ exact` / `~ likely` / `NEW`), filter by show-state, search
4. Past Uploads history table

Action bar moved ABOVE the table so it's visible without scrolling 218 rows.

### 4. Bank Register
**`web/static/register.html`** at `/registers` (sidebar: Accounting → Registers).
- Account picker pills (Chatham/Dennis)
- Date range filter (default last 90 days — note: probably should be last 6 months for end-of-quarter use)
- Cleared filter
- Sortable columns: Date, Type, Ref, Payee, GL Account, Memo, Payment, Deposit, Balance, ✓
- Bill-pay rows show "→ Bill #N" link in GL Account column (clickable, opens `/payments?payment=N`)
- Payroll rows show "→ Payroll Run #N" link
- Deposit + manual rows show editable GL dropdown grouped by account_type
- "+ GL Account" button in header — quick-adds a custom account to the current location's COA via 2 prompts (name, type)
- "Sync Toast Deposits" button — pulls deposits from QBO live (token-dependent)
- "+ Manual Entry" — adds a manual_bank_entries row directly (transfers, fees, interest, adjustments)

### 5. GL chart of accounts
**Tables added** (in `routes/register_routes.py:init_register_tables`):
- `gl_accounts` (id, qbo_id, acct_num, name, account_type, account_subtype, location, active, sort_order). Scoped per location (chatham | dennis | NULL legacy). 256 rows seeded per location from `data/coa.csv` (committed) or `/home/rednun/dennis_coa.csv` (legacy fallback).
- `gl_account_rules` (id, location, pattern, gl_account_id, created_by). Pattern is a substring (e.g. "TOAST", "DAVO"). On register row coding, a rule is auto-created and back-fills any other unassigned matching row in the same location.

**Auto-seeded default rules** at startup (idempotent — only inserts missing rules). Currently 58 across both locations: TOAST→Credit Card Sales, DEPOSIT→Cash Sales, DOORDASH→Doordash, DAVO→Sales Tax Payable, SHIFTS→Payroll Expenses, VENMO→Tip Wages, COMCAST→Cable/Phone/Internet, EVERSOURCE→Electric, NGRID→Gas, NUCO→Bar Consumables, CINTAS→Linens, UNIFIRST→Linens, EIDL→EIDL Loan, INTUIT→Bookkeeping, US FOODSERVICE→Food Costs -F&B, MARTIGNETTI→Liquor COGS, etc.

**API endpoints** (in `routes/register_routes.py`):
- `GET /api/gl-accounts?location=chatham` — list active accounts for the dropdown.
- `POST /api/gl-accounts` — create a new account (the "+ GL Account" button).
- `PUT /api/gl-accounts/<id>` — edit (name, type, deactivate).
- `POST /api/gl-accounts/refresh-from-csv?force=1` — admin: wipe + re-seed.
- `PUT /api/register/row/gl-account` — assign GL to a single row. Auto-creates a rule and back-fills similar rows in the same location.

### 6. Schema migrations (all idempotent in init_register_tables)
- `bank_accounts` (Chatham/Dennis seeded)
- `manual_bank_entries`
- `bank_deposits` (QBO sync cache)
- `vendor_payments.bank_account_id`, `.cleared`, `.cleared_date`, `.gl_account_id`
- `payroll_checks.bank_account_id`, `.cleared`, `.cleared_date`, `.gl_account_id`
- `manual_bank_entries.statement_upload_id`
- `gl_accounts`, `gl_account_rules`

### 7. Bug fixes during this session
- **Payroll `pay_date` column missing** — `payroll_checks` doesn't have it; `payroll_runs` does. Fixed both `register_routes.get_register` and `bank_reconcile_routes._load_register_rows_for_period` to JOIN on `payroll_runs.id = payroll_checks.payroll_run_id`. Stops a recurring log warning AND makes payroll appear in the register AND makes statement-import matching catch payroll checks.
- **CCF parser fixes** — single-digit months (M/DD), Statement Dates header format, "Checks/Debits" / "Deposits/Credits" totals labels, sub-dollar amounts, page-break repeats not dropping in-flight transactions.
- **Account-mismatch validation** — uploading a Chatham PDF on Dennis account is refused with a clear error message.
- **GL seed empty-path bug** — `Path("")` resolves to current dir which exists; filtered to `is_file()` only.

### 8. Refactor (HEAD `ac45433`)
**Withdrew the misguided "auto-fill GL on bill_pay from invoice categories" code.** Initial implementation tried to put Liquor COGS / Wine COGS on the bank-register row. That's wrong — the expense was already coded on the invoice when it was OCR'd. Bill_pay rows now have no GL coding (it's implicit A/P) and the dropdown is replaced with a "→ Bill #N" link. Same logic applied to payroll rows.

**Helper functions kept in code** (`_compute_bp_category_breakdown`, `_apply_invoice_category_coding`, `_resolve_gl_for_category`, `_CATEGORY_GL_MAP`) but no longer called from `get_register`. Useful if we ever decide to build proper split coding (one bill_pay row hitting multiple GL accounts proportionally for SG-style mixed invoices).

---

## Repo layout (new files)
```
integrations/bank_statements/
  __init__.py
  processor.py                      ← CCF PDF parser
routes/
  bank_reconcile_routes.py          ← /api/bank-reconcile/*
  register_routes.py                ← (existing, heavily extended)
web/static/
  bank_reconcile.html               ← /bank-reconcile page
  register.html                     ← (existing, refactored)
  sidebar.js                        ← (existing, +Bank Reconcile entry)
data/
  coa.csv                           ← committed COA (256 rows; copy of Dennis)
docs/
  BANK_RECONCILE_BRIEF_2026-04-29.md  ← this file
```

Sidebar entry added under Accounting: **Bank Reconcile** → `/bank-reconcile`.

---

## Database state right now

DB was wiped at the end of the session for a clean retry. State as of 13:23 ET:

```
bank_statement_uploads          0
manual_bank_entries             0
bank_deposits                   0
gl_account_rules               58   (re-seeded defaults)
gl_accounts                   512   (256 chatham + 256 dennis)
bank_accounts                   2   (Chatham 5975, Dennis 2757)
vendor_payments.gl_account_id   0   (none coded)
payroll_checks.gl_account_id    0   (none coded)
```

**Backup before wipe:** `/opt/backups/toast_data_20260429_1323_pre-bankrec-wipe.db` (1.3 GB).

---

## Workflow

### One-time setup (already done)
1. COA seeded from `data/coa.csv` (Dennis used for both locations until Chatham COA is provided).
2. Default GL rules seeded.
3. Bank accounts seeded.

### Adding a new GL account
**+ GL Account** button on `/registers` → 2 prompts (name, type). Persists to current location's COA. Or `POST /api/gl-accounts`.

### Reconciling a bank statement (the actual workflow)
1. Upload all 7shifts payroll runs that cleared in the period **first** (so the matcher catches checks).
2. Go to `/bank-reconcile`, pick the right bank account, drag the PDF on the upload zone.
3. Confirm summary numbers reconcile (begin → end, total in/out match the statement).
4. In Section 3, set SHOW filter to "Unmatched only".
5. Click "Select unmatched", check "Mark matched register rows as cleared", click Import.
6. Hop to `/registers`, set the date range to the statement period, code any unassigned manual entries (rules will auto-fill what they can).

### What auto-codes vs. needs user input
**Auto-codes:** TOAST deposits, plain Deposits (cash), DAVO sweeps, DOORDASH deposits, 7shifts payroll, Comcast/Eversource/NGrid, Cintas/Unifirst, NEXT INSUR, MarginEdge, NuCO2, AppleCard, EIDL, Intuit. (Whichever rules in `_DEFAULT_RULES` find their named GL account.)

**Needs user input:** anything not in the default rules — typically debit-card swipes (e.g. Webstaurant, Chatham Liquor Store), one-off ACH bills, transfers between accounts, owner draws, manual Venmo. After coding the first one, the rule remembers and future similar rows pre-fill.

---

## Deferred / known gaps

### 1. Split coding for mixed-category invoices (e.g. Southern Glazers)
Currently a single bill_pay row can only point to one GL account (or none). For SG with $400 Liquor + $200 Wine, the invoice line items are correctly categorized, so the dashboard's P&L reports are right — the bank-register row just shows "→ Bill #N" with no GL because it's already booked upstream. This is fine for the current architecture.

If we later want to push journal entries to an external system (or want bank-register-level visibility into the split), we'd need a `bill_pay_gl_splits` table (`bill_pay_id, gl_account_id, amount`). Helper functions (`_compute_bp_category_breakdown`) are already in `register_routes.py` — they compute the per-category totals from line items.

### 2. Auto-clean payroll duplicates on out-of-order upload
User confirmed they'll **upload payroll runs before bank statements** (workflow option a). If they ever upload in the wrong order, manual entries from the statement that should have matched payroll_checks will become duplicates. Cleanup would require a small pass that runs after a payroll run is recorded — delete `manual_bank_entries` where description starts `Check 1xxx`, amount equals a payroll_checks.net_pay, and date within ±7 days. Not built.

### 3. Backfill check_number from bank statement
Most existing payroll_checks rows have `check_number = NULL`. The matcher handles this via the amount + date path (matches as `~ likely`). After a confirmed import, we could write the bank statement's check# back to `payroll_checks.check_number` so future matches are `✓ exact`. Not built.

### 4. Category breakdown click-through on bill_pay rows
Removed from this iteration. If user wants visual confirmation of how a bill split (e.g. SG: Liquor $400 + Wine $200 + Beer $100), it'd be a hover tooltip or modal triggered from the "→ Bill #N" link. The data is already computed by `_compute_bp_category_breakdown`.

### 5. Default register date range
Currently last 90 days. Bank reconciliation is typically done after month-end so a January reconcile in late April is outside the default window. Consider bumping to 6 months, or anchor to the most recent uploaded statement period.

### 6. Old payments without ap_payment_id
Bill-pay rows from before the `ap_payments` system was wired (legacy data) won't have a clickable "→ Bill #N" link — they'll show "→ Bill payment" in muted text. Should be a small minority.

---

## Gotchas / lessons from this session

1. **Bash mount lag.** The Linux sandbox occasionally serves a stale snapshot of files, leading to spurious `py_compile` errors on files that are actually fine on disk. Trust the file tools (Read/Edit) — they reflect the live state. If `python -m py_compile` fails on a file you just edited, re-Read the file to verify; the file tools' view is authoritative.
2. **CRLF / LF noise on Windows working copy.** `git diff --stat` will report huge byte counts on `web/server.py` and `web/static/sidebar.js` because the Windows working copy has CRLF and the index has LF. Use `git diff --ignore-cr-at-eol` to see real changes. Has nothing to do with our edits.
3. **`.git` ownership on the server.** A `git pull` once failed with `error: insufficient permission for adding an object to repository database .git/objects` — caused by an earlier `sudo` git operation. Fix: `sudo chown -R rednun:rednun .git`.
4. **`Path("")` resolves to `.`** which exists but is a directory. Always filter `is_file()` not just `exists()` when iterating candidate paths.
5. **`COALESCE(pay_date, pay_period_end)` referencing a missing column** — SQLite errors during planning, not execution. Fixed by joining `payroll_runs` for `pay_date`.
6. **Default rules require GL account names to match exactly.** If the COA changes and a name moves (e.g. "Cash Sales" → "Cash Receipts"), the corresponding rule won't seed. Update `_DEFAULT_RULES` in `register_routes.py` accordingly.
7. **The "dennis_coa.csv" file** is misleadingly named — it's the full Red Buoy chart of accounts and is used for both locations until a separate Chatham COA is provided.

---

## Commits in this session (newest first)
```
ac45433  Refactor register: drop GL dropdown on bill_pay/payroll
5e3fe90  Fix payroll pay_date query; seed default GL rules; auto-fill from invoice categories
89a823b  Fix GL seed: skip empty/non-file paths
7544ffe  Scope GL accounts per location; seed Chatham from Dennis CSV; add manual GL account creation
e9dfba1  Move Import button above tx table
d724195  Parse sub-dollar amounts (.27)
9d2df75  Fix CCF parser; verify upload matches selected account
a8aa982  Widen register table; add raw-text diagnostic endpoint
a595e20  Add bank statement reconcile page + inline account dropdown on register
```

(Plus the wipe + restart at 13:23 ET — no commit, just a SQL cleanup.)

---

## Resume instructions for next session

1. Read this file first.
2. Check the deployed HEAD: `ssh -p 2222 rednun@ssh.rednun.com "cd /opt/red-nun-dashboard && git log --oneline -1"` should show `ac45433` or later.
3. Confirm DB state with the queries in the "Database state right now" section above.
4. The user's next step is to **retry the workflow with the clean DB**: upload the Chatham January PDF on `/bank-reconcile`, look at the match results, decide what to import.
5. Most likely next asks (in priority order):
   - Verify payroll matching works end-to-end (post-DB-wipe).
   - Build the auto-clean for out-of-order payroll uploads (deferred item #2 above) if user changes their mind on workflow.
   - Build a category breakdown tooltip on bill_pay rows (deferred item #4) if mixed-invoice visibility becomes a pain.
   - Bump default register date range (deferred item #5) — easy fix.
6. Things NOT to touch: rednun.com / www.rednun.com DNS (per CLAUDE.md, breaks Toast online ordering), `inventory_routes.py` (1,045-line existing system, do not modify), the existing `/reconcile` page (portal-reconcile, separate from `/bank-reconcile`).
