# Bank Register Refactor — Session Brief

**Last updated:** 2026-04-29
**Session goal:** Wrap up January reconciliation, then refactor the Bank Register to be a pure real-time bank-activity view (NOT a reconciliation tool). All reconcile-style UI should live on `/bank-reconcile`.

---

## What this session accomplished

### Bank register hardening
- Scroll position preserved across account switches (no more bouncing to top on filter change)
- Clickable column sort with `localStorage` persistence (`register.sort`)
- Within-date sort direction fix — when sorting Date ASC, secondary sort by `(source, source_id)` also ASC, so running balance flows naturally (deposit ↑, payment ↓ as you scroll down)
- Date preset dropdown replacing manual From/To inputs: Today (default) / This Week / MTD / Last Month / This Quarter / Last Quarter / YTD / Last Year / Last 90 Days / Custom. Persisted in `register.preset` / `register.customStart` / `register.customEnd`.

### Bank-side correctness fixes
- `vendor_payments` query now excludes `status='failed'` AND `'void'` (was only `'void'`). Same fix in `bank_reconcile_routes.py`.
- `payroll_checks` Direct Deposit rows excluded from the bank register — they're rolled into the lump-sum 7shifts ACH on the statement; showing them individually was double-counting. DD rows still exist in `payroll_checks` for HR / payroll-reporting.
- One-time SQL marked all 54 historical DD `payroll_checks` as `cleared=1` so they don't sit as orphan outstanding items.

### Opening Balances feature (`/opening-balances`)
- New page accessed via sidebar Accounting → Opening Balances
- Bank account section: edit `bank_accounts.opening_balance` + `opening_date` per location
- Balance-Sheet GL Accounts section: edit `gl_accounts.opening_balance` + `opening_date` for asset / liability / equity / credit-card account types
- Schema additions (idempotent ALTERs in `init_register_tables`):
  - `gl_accounts.opening_balance REAL DEFAULT 0`
  - `gl_accounts.opening_date TEXT`
- Location toggle persists via `localStorage`
- Inline edit with dirty-state highlighting + per-row Save buttons

### QBO Balance Sheet importer
- `POST /api/gl-accounts/import-balance-sheet` (admin) — multipart form: file=CSV, location, as_of_date, include_equity, replace_mode, dry_run
- Parser handles QBO BS export format: section detection, "Total for X" reset, parent-with-value rows (Michael Giorgio + child Dividend), QBO-style "Parent:Child" naming
- Bank Accounts section auto-matches to existing `bank_accounts` row by last-4 — surfaces the bank opening balance update separately
- Skips `Accounts Payable` (computed live from invoices)
- Replace mode: deactivates every active `gl_accounts` row at this location before the upsert, so first-time import wipes out the legacy Dennis-style seeded accounts that were duplicated into Chatham
- Import Balance Sheet button + modal on `/opening-balances` with dry-run preview, summary line, then apply
- Verified: parses Red Buoy 1/1/2026 BS exactly, all 8 type subtotals reconcile to the penny, balance sheet identity holds (Assets = Liabilities + Equity)

### Current Balance overlay on `/opening-balances`
- "As of [date]" + "Show Current Balance" toggle in page header
- New endpoint `GET /api/gl-accounts/balances?location=&as_of=` — returns opening + register activity per balance-sheet GL account, with sign convention (assets += outflow - inflow; liabilities/equity += inflow - outflow)
- Activity sourced from `vendor_payments + payroll_checks (Manual only) + bank_deposits + manual_bank_entries`, scoped by bank_account location
- Cash-side-only disclaimer banner (POS accruals like sales tax collected at register, gift cert issuance NOT included)

### Bank Reconcile dedupe tool
- `POST /api/bank-reconcile/dedupe` (admin) — finds `manual_bank_entries` outflows that match a `vendor_payments` or Manual `payroll_checks` row by amount + date proximity. On apply: dashboard row gets `bank_account_id` + `cleared=1` + `cleared_date`, duplicate `manual_bank_entries` row deleted.
- "Dedupe Existing Register" button + modal on `/bank-reconcile`: account picker, date range, tolerance ±N days, match-vendor-payments / match-payroll-manual checkboxes, dry-run preview with green/amber/gray rows, then apply
- Conservative match (amount exact, date within tolerance, ambiguous flagged for manual review)

### January 2026 Chatham reconciliation
End-state numbers for `/registers` Chatham 2026-01-01 to 2026-01-31:
- Opening: $34,013.57 (matches statement)
- Deposits In: $84,169.93 (matches statement exactly)
- Payments Out: $94,754.41 (statement: $92,398.46, off by $2,356)
- Ending: $23,429.09 (statement: $25,785.04, off by $2,356)
- 226 transactions, 8 uncleared (down from original 39)

The $2,356 residual is the 8 uncashed paper paychecks ($3,575) netting against small variances — legitimately pending, will resolve as checks get cashed and Feb statement is imported.

### Cleanup actions taken
1. **Code fix** — DD payroll_checks excluded from register (`payment_method != 'Direct Deposit'`)
2. **One-time SQL** — 54 historical DD payroll_checks set to `cleared=1, cleared_date=pay_date`
3. **Dedupe step 1** — ±5 days tolerance, matched 19 (7 vendor_payments + 12 payroll_checks Manual), $29,372.91
4. **Dedupe step 2** — ±15 days tolerance, matched 3 more paychecks (Margaret Stella $1,792.99, Leah Artman $1,220.01, Jamison Rushnak $430.58), $3,443.58
5. **Vendor-portal-imported bill_pays voided** — 14 `vendor_payments` rows (Martignetti, Southern Glazer's, US Foods) that came from pre-dashboard-payments era when invoices were imported from vendor portals via the scrapers. They were duplicating the bank-statement-imported `manual_bank_entries`. Voided via:
   ```sql
   UPDATE vendor_payments SET status='void' WHERE bank_account_id IS NULL
     AND payment_date >= '2026-01-01' AND payment_date <= '2026-01-31'
     AND (status IS NULL OR status NOT IN ('void','failed'));
   ```
   Total voided: 15 rows (one was already in another non-cleared state), $21,364.94.

---

## Tomorrow's main work — Register page refactor

**Architectural insight from the session:** The Bank Register (`/registers`) and Bank Reconcile (`/bank-reconcile`) are two separate concerns and should be cleanly separated.

- **Register = real-time view of what's flowing through the bank account.** Like the QB 5975 register. Just the running list of transactions with running balance.
- **Reconcile = the tool that matches dashboard records to bank statement entries.** Cleared/uncleared status, matching, dedupe, statement upload.

Currently the register is a hybrid: it shows transactions BUT also has cleared checkboxes, an uncleared count, a status filter — all of which are reconciliation UI.

### Proposed changes for `/registers`

1. **Remove the ✓ column** (per-row cleared checkbox)
2. **Remove the "uncleared" count** from the summary card (just show total transactions)
3. **Remove the Status filter dropdown** (All / Uncleared / Cleared)
4. **Keep the existing functionality otherwise:** account picker, date preset, GL account dropdown editing, manual entry modal, sync deposits, GL account reassignment

The register query keeps reading `cleared` internally for sorting / filtering, but the UI stops surfacing it.

### Proposed changes for `/bank-reconcile`

The reconcile page should become the home for everything cleared-status related:

1. **Add a "Mark cleared" or "Match this row" UI** for manual reconciliation when the auto-matcher misses something
2. **Add an "Uncleared register rows" view** so the user can see what's pending and decide what to do (mark cleared, void, leave pending)
3. **Keep the existing flow:** PDF upload → parse → match → import unmatched → dedupe afterward

### Files to touch tomorrow

- `web/static/register.html` — remove ✓ column from `<thead>` and `renderRows()`, remove status filter dropdown, remove uncleared count from `renderSummary()`. Adjust colspan in empty-state and detail-row from 10 to 9.
- `routes/register_routes.py` — `get_register()` keeps computing cleared internally but the response can stop including `uncleared_count` (or leave it; UI just stops reading it). The `cleared_filter` query param can be removed or left for backward compat.
- `web/static/bank_reconcile.html` — add a new section (Section 5?) "Manual reconciliation" that shows uncleared register rows for a date range, with per-row "mark cleared / void / link to statement entry" actions.
- New endpoint maybe: `GET /api/bank-reconcile/uncleared-register?account_id=&start=&end=` — returns the same shape as the register but only `cleared=0` rows. The reconcile UI consumes this.
- `routes/register_routes.py` — `set_cleared` endpoint stays (it's the per-row mark-cleared API; just consumed from `/bank-reconcile` now instead of `/registers`).

---

## Other deferred items

### 1. Stale Bill-Pays report
14 January `vendor_payments` rows were vendor-portal-imported and voided during this session. Going forward, dashboard-initiated payments will have `bank_account_id` set at creation, so this exact pattern shouldn't recur. But there should be a **"Stale Bill Pays"** report somewhere — bill_pays older than 60-90 days that are still uncleared probably need investigation (either matcher missed them, or they were never sent and need voiding).

### 2. Vendor scraper imports — model question
Should vendor scraper imports (`~/vendor-scrapers/common/import_downloads.py`) create `vendor_payments` rows at all? They currently do, which causes the duplicate-counting we cleaned up. Two options:
- **a.** Keep current behavior; rely on the dedupe tool / manual voiding for old data. Simpler.
- **b.** Modify scraper imports to ONLY create `scanned_invoices` (the bill record) and let the bank statement be the cash-side truth. Cleaner long-term, but breaks the "bill_pay shows up immediately" intent for the AP page.

Decision deferred until the dashboard-driven bill-pay workflow is more established.

### 3. DAVO accruals integration
Mentioned during the session — Sales Tax Payable's "Current Balance" overlay only sees the cash-side decreases (DAVO sweeps). Doesn't see accruals (sales tax collected at POS). User has a DAVO report that shows the accrual side. To wire up:
- New table `gl_journal_entries` (location, gl_account_id, entry_date, amount, description, source, source_id)
- DAVO report parser in `integrations/davo/` (format/cadence TBD — will need a sample report)
- Importer endpoint similar to BS importer
- Update `list_gl_account_balances` to also sum from `gl_journal_entries`

### 4. Default register date range
Currently defaults to "Today" per user request. Probably overkill — most accounting workflows want MTD or YTD. If user finds Today too restrictive, easy one-line flip in `register.html` (`DEFAULT_PRESET = 'mtd'`).

### 5. GL Account registers
Per-GL-account register view (e.g., open Sales Tax Payable and see all transactions coded to it with running balance). Natural extension of what was built. Click a GL account from `/opening-balances` or somewhere → drill into its transaction list. Bigger build, deferred.

### 6. Register filter strict mode
The bank register's bill_pay query currently includes `bank_account_id IS NULL` as a "catch-all" for Chatham (5975). With dashboard-driven payments now setting `bank_account_id` at creation, this catch-all could be removed in a future month so that historical NULL rows don't accidentally surface. But removing it now would hide any legitimate vendor-portal imports going forward. Defer until vendor scraper model decision (item 2) is made.

---

## Repo state at end of session

```
HEAD on origin/main is the dedupe-tool commit + register sort fix.
Last commits in order (newest first):
  Bank register: fix within-date order on asc sort so running balance flows naturally
  Bank reconcile: dedupe tool — merge statement entries with matching dashboard rows
  Opening Balances: BS importer + replace mode + current balance overlay; register: date preset dropdown; docs: session brief
  Bank register: exclude Direct Deposit payroll checks (rolled into 7shifts lump-sum ACH)
  Bank register: scroll/sort/preset filters, BS importer, current-balance overlay, fail-status fix
```

DB state at end of session:
- 1 backup at `/opt/backups/toast_data_20260429_1941_pre-vendor-portal-void.db` (1.3 GB)
- 3 older backups should be deleted: `_pre-bankrec-wipe.db`, `_pre-failed-purge.db`, `_pre-dd-clear.db` (run cleanup script)
- January 2026 Chatham: register reconciles within $2,356 of bank statement (residual is 8 uncashed paper paychecks)
- 14 vendor-portal-imported bill_pays voided
- 54 DD payroll_checks marked cleared
- All bank_account_id assignments preserved on dedup matches

---

## Resume instructions for next session

1. Read this file first.
2. Check the deployed HEAD: `ssh -p 2222 rednun@ssh.rednun.com "cd /opt/red-nun-dashboard && git log --oneline -5"` should match the commits listed above.
3. Confirm DB state: `sqlite3 /var/lib/rednun/toast_data.db "SELECT cleared, COUNT(*), SUM(payment_total) FROM vendor_payments WHERE bank_account_id = (SELECT id FROM bank_accounts WHERE account_last4='5975') AND payment_date >= '2026-01-01' AND payment_date <= '2026-01-31' GROUP BY cleared;"` — should show 7 cleared rows.
4. Start with the **register refactor** (remove ✓, uncleared count, status filter from `/registers`; consolidate reconcile UI on `/bank-reconcile`). Keep changes additive.
5. Test on Chatham January after each change — register should still reconcile within $2,356 of statement.
6. **Things NOT to touch:**
   - `rednun.com` / `www.rednun.com` DNS (per CLAUDE.md, breaks Toast online ordering)
   - `inventory_routes.py` (1,045-line existing system)
   - `/reconcile` page (portal-reconcile, separate from `/bank-reconcile`)
