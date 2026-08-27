# Accounting suite — the finish line

**Written 2026-08-27. This is the plan of record.** Every number below was read
live from `/var/lib/rednun/toast_data.db` on 2026-08-27, not from CLAUDE.md —
which is badly stale on this subject (it claims `products` = 0 rows; there are
1,767).

Update this file instead of writing a new brief. It exists so no session has to
re-derive the state of play.

---

## 1. The finish line, in one sentence

**A monthly close you run in an afternoon:** Toast sales, vendor invoices,
payroll, and bank statements all land in the dashboard; every bank account ties
to its statement; the dashboard shows labor %, food cost %, and prime cost you
can steer by; and one action hands your accountant a QBO deliverable that ties
to those same numbers.

"Done" means all seven of these are true:

1. Each day's Toast sales post to QBO as a balanced journal entry, automatically.
2. Each vendor invoice becomes a GL-coded AP bill in QBO.
3. Each payroll run becomes a payroll JE in QBO.
4. Each bank account ties to its statement every month, signed off, rows locked `R`.
5. The dashboard reports labor %, food cost %, and prime cost per location per period.
6. Those dashboard numbers equal what QBO shows. If they diverge, something says so.
7. Every check in the chain can actually go red. No status field that cannot detect its own failure.

You do not use QBO day to day. QBO is the **delivery format** for the accountant
and investors. The dashboard stays the place you run the business.

---

## 2. Where we actually are

Rough completion: **~65%**, and the shape of the remaining work is better than
expected. It is mostly *wiring up and feeding what already exists*, not building
new systems.

| Gate | What | Status |
|---|---|---|
| **0** | Toast sync alive | 🟢 **FIXED 2026-08-27** (credential rotated; sync_log 32 complete / 0 error) |
| **1** | Sales → QBO | 🟢 ~90% (built, running daily, **never switched on**) |
| **2** | AP / invoices → QBO | 🟡 ~40% |
| **3** | Payroll → QBO | 🟡 ~30% |
| **4** | Bank reconciliation | 🟡 Dennis ~70%, Chatham ~15% |
| **5** | Business targets (labor/food/prime) | 🟡 ~60%, gated on Gate 0 |
| **6** | Checks that can go red | 🟡 ~30% |

### Gate 0 — FIXED 2026-08-27

Was the only true blocker; sales and labor had been frozen at business_date
20260821 with `sync_log` running 372 errors to 4 completes.

**Cause: the credential was simply dead.** Mike rotated the client secret and
everything came back on the first try — login 200, `expiresIn` 86400, and the
token's own `scope` string contains `labor:read` and `labor.employees:read`,
which definitively kills the "missing Labor scope" theory the original brief
floated. Dennis 274 employees / 18 time entries, Chatham 282 / 18, orders OK on
both. `sync_log` now reads **32 complete, 0 error**.

Gap backfilled. Dennis 08-24 and 08-25 are genuinely near-empty — a direct API
probe bypassing the exception swallow shows Toast itself reports 1 and 0 orders,
and the week reads Wed 90 / Thu 96 / Fri 206 / Sat 165 / Sun 100 / Mon ~0 /
Tue 0 / Wed 88. **Dennis is closed Mondays and Tuesdays.** Do not chase that as
a sync hole.

Two durable fixes so it cannot hide again:

1. `get_all_orders_for_date` swallowed per-chunk exceptions and returned `[]`;
   `sync_orders_for_date` then logged `_log_sync(...)` with its **default**
   `status="complete"`. Five clean-looking days, nothing stored. It now raises,
   naming how many chunk-reads failed and how many orders arrived before the gap.
2. `sync_log` gained a **`message`** column, and all four error paths write
   `str(e)` into it. Verified by pointing the client at a bad secret on a scratch
   copy: raises `RuntimeError`, logs `status='error'` plus the 401 text. The same
   input previously produced `status='complete'`.

**Still open on this path** (brief §5): no retry/backoff in `_get_token`; the
scheduler rebuilds `DataSync` every 10 min so the 23h token cache never
survives a run; the cadence comment says 30 min while cron says `*/10`; the
hardcoded `23 * 3600` should use the returned `expiresIn` (86400); sustained
failure should route into the existing scraper-alert machinery.

### Gate 1 — Sales → QBO is built and switched off

This is the big surprise, and the best news in the file.

- `qb_journal_entries`: **896 entries**, `qb_journal_line_items`: **13,766 lines**
- Entries exist for 08-25 and 08-26 — the daily builder is **running now**
- Every one is `status='ready'`: built, balanced, **and never posted**
- `reports/sales_journal.py::push_to_qbo(entry_id)` exists, with an idempotency
  pre-check that adopts an existing QBO txn rather than double-posting
- There is even a weekly "unposted entries" emailer

Nothing has ever been pushed to QBO — verified: zero `qbo_txn_id`, zero
`posted`. **So there is no QBO cleanup behind any of this, and redoing any
close or repost is free.**

Remaining: pick a start month, post it, have the accountant confirm the JE shape
before bulk-posting the backlog.

### Gate 2 — AP / invoices

Strong foundation, broken plumbing.

- `scanned_invoices`: **884, all confirmed**, 2019-03-22 .. 2026-08-31
- `scanned_invoice_items`: **8,994 — every one carries a `category_type`**
- `gl_accounts`: **541** (chatham 284, dennis 257), **524 with a `qbo_id`**

But:

- `vendor_payments`: 477 rows, only **7** have `bank_account_id`, only **13**
  have `gl_account_id`
- **147** rows have a NULL `location`, so no account can be derived from them
- Consequence: Dennis's register shows **zero** bill_pay rows, so Dennis AP can
  never tie. `build_register_view` folds NULL-account rows into a register only
  for the catch-all keyed on `account_last4 == '5975'` (Chatham); Dennis is 2757.

**DONE 2026-08-27** — `scripts/backfill_vendor_payment_bank_account.py` applied,
323 rows moved. Dennis now has bill_pay rows in every month from February on
(Feb 9, Mar 14, Apr 19, May 49, Jun 20, Jul 13). Chatham January untouched:
delta 0.00, outstanding −5,151.48, book_side 31, identical to before.

The important lesson from doing it: **`location` does not determine
`bank_account_id`.** `location` is where the expense belongs; `bank_account_id`
is which bank paid. For intercompany they differ, and only the statement can
tell them apart. The first version of the script keyed on `location` and would
have moved 3 provably-Chatham-paid Dennis payments (#27/#28/#29, 8,163.13,
visible as `AR PAYMENT PERFORMANCEBOS` debits on Chatham's January statement)
out of the one period that ties to the penny.

Six intercompany pairs found and deliberately left alone — they need a
due-to/due-from entry, not a reassignment:

| vendor_payment | expense loc | cash left |
|---|---|---|
| #27, #28, #29 (PFG, Jan) | dennis | Chatham |
| #235, #295, #304 (May) | chatham | Dennis |

Evidence must be read from `bank_statement_uploads.parsed_json`, not from
surviving `manual_bank_entries` — dedupe DELETES a statement line once it is
matched to a book row. 22 of Chatham January's 218 lines no longer exist as
manual entries; `parsed_json` still holds all 218.

**Still open here:** 147 rows carry a NULL `location` and were left alone.
And Dennis's outstanding is now large and honest instead of artificially zero
(May −70,026.22 across 60 book-side rows) because the newly-attributed bill_pay
rows have never been through a dedupe pass. Every delta still ties at 0.00, so
the statement side is complete — the next step is a dedupe run per Dennis
period, which is the intended workflow, not a defect.

### Gate 3 — Payroll

- `payroll_runs`: 30. `payroll_checks`: **687**, 2025-12-22 .. 2026-08-16
- **0 of 687 carry a `gl_account_id`** → there is no payroll JE path at all
- `qb_line_mapping` exists and runs a GL backfill at startup (currently
  "0 by qbo id, 0 by role, 0 unmapped")
- Needs: map payroll components (wages, employer taxes, tips, deductions) onto
  the GL, then emit a JE per run

Also on this path: `payroll_checks.check_number` is 234/687. Of the 230
originally populated, **only 14 appear on any bank statement**. 205 of the rest
sit in periods no statement covers; **11 are Dennis rows carrying Chatham's
2000-series** (each location prints its own sequence — `check_config`: chatham
next=2245, dennis next=9749). Wrong-series numbers can never match the bank, so
they behave like an empty column while looking authoritative.

### Gate 4 — Bank reconciliation

The machinery works and is verified. The data is thin.

- Roll-forward correct on all 6 statement periods, `opening_drift = 0.00` on every one
- `C` / `R` states live; `R` rows locked, force-untick marks the period stale
- Statement coverage: **Chatham 1 statement** (January only). **Dennis 5** (Jan–May).
- **Missing: Chatham Feb–Jul (6), Dennis Jun–Jul (2).** Chatham has had no
  balance anchor since February.
- Dennis Jan and Feb "tie" but the tie is **hollow** — all 175/166 rows came
  from the imported statement with zero book-side rows, so the delta compares
  the statement against itself and cannot come out non-zero. The page now says
  so in amber.

June and July are likely overdrawn months (Dennis ended May at $3,194.35), so
they exercise the overdraft parser fix on first upload. Check the "line count
mismatch" warning on every upload.

### Gate 5 — Business targets

Better than documented. CLAUDE.md is wrong here.

- `products` **1,767** · `product_name_map` **1,468** · `product_inventory_settings` **360**
- `recipes` **345** · `recipe_ingredients` **1,482**
- `count_sessions` 51 · `count_items` 194 · `bottle_weights` 151 · `vendors` 52

So recipe-level costing is viable, not a rebuild-from-zero. Two tiers:

- **Top-line food cost %** = purchases ÷ net sales. Available today from
  invoices, once Gate 0 restores the sales denominator.
- **Theoretical vs actual** (recipe-level variance) — needs the recipe and
  count data exercised, which is a data-quality pass rather than new code.

**Labor %** needs Gate 0. Labor data is frozen at 08-21.

### Gate 6 — Checks that can go red

The recurring failure mode in this codebase is a green light that cannot turn
red. Three confirmed instances: the bank tie-out footed the statement *header*
instead of the parsed rows; the reconciliation `delta` compared a statement's
cleared rows against themselves; `sync_log` defaulted to `status="complete"` so
a dead credential logged five days of successful syncs.

Added so far: `identity_holds`, `opening_drift`, hollow-tie detection,
statement-coverage state, parsed-row-count vs statement-header count.

Still needed: sales-JE completeness check (posted JEs vs business days), AP
completeness (invoices vs bills), and an alert on sustained sync failure — the
scraper-alert machinery already exists to route it into.

---

## 3. Sequence to the finish line

Five blocks. Order matters — each unblocks the next.

**Block A — Unblock the data (needs you)**
1. Fix the Toast credential in the partner portal; confirm Labor scope on both GUIDs.
2. Add retry/backoff to `_get_token()`, share one client across the scheduler,
   record `str(e)` in `sync_log`, use Toast's `expiresIn`, alert on sustained failure.
3. Fix the silent-success bug: a chunk failure must not log `complete`.
4. Backfill the 6 missing days of sales and labor.

**Block B — Turn on the QBO sales rail**
5. Post one month of sales JEs; accountant confirms the shape.
6. Bulk-post the backlog; wire the completeness check.

**Block C — Make AP tie**
7. Apply the vendor-payment attribution fix; re-close Chatham January.
8. Resolve the 147 NULL-location payments.
9. Roll invoice line-item `category_type` up into AP-bill GL coding; emit AP bills to QBO.

**Block D — Payroll rail**
10. Map payroll components to GL; emit a JE per run; reconcile against the bank's check lines.

**Block E — Close the books, monthly**
11. Upload the 8 missing statements; reconcile every month both locations.
12. Stand up the targets dashboard: labor %, food cost %, prime cost, per location per period.
13. Assert dashboard == QBO, and alert when they diverge.

---

## 4. What is needed from you (nothing moves without these)

| # | Item | Blocks |
|---|---|---|
| 1 | **Toast credential** fixed in the partner portal | Everything above Gate 0 |
| 2 | **8 bank statement PDFs** — Chatham Feb–Jul, Dennis Jun–Jul | Gate 4, Block E |
| 3 | **What the accountant wants**: summary JEs, or full transaction detail (bills/payments/deposits as objects)? | Gate 2 + 3 scope |
| 4 | **Go/no-go** on the vendor attribution fix (it breaks rec#1 by design) | Block C |

---

## 5. Open items carried forward

- `#244` looks like a duplicate payment: US Foods, 2026-01-30, $2,795.53,
  pending, NULL account — against `#10`, same vendor/date/amount, cleared, acct 1.
- `_match_transactions` ignores payee text entirely — scores on direction +
  amount ±0.005 + date proximity, ties broken by iteration order. Now that `R`
  makes a match sticky, a wrong match costs more.
- GL provenance (`coded_by` / `coded_via`) still not done; blocks a safe rule rebuild.
- 11 Dennis payroll rows carry Chatham's check series — detected, reported, not
  auto-corrected (overwriting would discard the number printed on the paper check).

## 6. Operational gotchas that have already cost time

- **`data_store` reads `DB_PATH`, not `TOAST_DB_PATH`.** CLAUDE.md documents the
  wrong name. Setting the wrong one sends test writes into the live database.
- **Never `cp` the live DB.** It is 1.7 GB in WAL mode and actively written;
  `cp` yields a *torn* snapshot. Use `sqlite3 ... ".backup /tmp/x.db"`.
- **A bad parse is frozen in the DB.** Import reads `parsed_json` stored at
  upload time and selects rows by positional index, so a parser fix requires
  re-uploading the PDF. Re-upload upserts only while `imported_count = 0`.
- **Never touch `rednun.com` DNS.** Toast online ordering resolves through it.
