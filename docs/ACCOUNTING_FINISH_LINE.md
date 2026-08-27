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
| **2** | AP / invoices → QBO | 🟡 ~55% (attribution + settlement coding fixed 08-27) |
| **3** | Payroll → QBO | 🟡 ~30% |
| **4** | Bank reconciliation | 🟡 Dennis ~70%, Chatham ~15% |
| **5** | Business targets (labor/food/prime) | 🟡 ~60%, gated on Gate 0 |
| **6** | Checks that can go red | 🟡 ~45% |

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

#### Settlements no longer hit the P&L — DONE 2026-08-27

`audit_settlement_codings` went **43 → 0**. Recoded to Accounts Payable:
chatham 5 rows / $8,036.67, dennis 38 rows / $69,677.65 — **$77,714.32 of
double-counted expense out of the P&L.** Script:
`scripts/fix_settlement_codings.py`.

The rule: cost is recognised at INVOICE date from the invoice line items, so the
bank row that pays it is `Dr Accounts Payable / Cr Bank`. Coding it to Food COGS
as well books the cost twice. Biggest hits were Dennis Apr/May — Food COGS
$15,777 + $13,746, Food Costs-F&B $12,200, Beer COGS $4,620 — so food cost %
for those months was materially overstated.

Pre-existing, not caused by the attribution backfill (the same test fails on the
older code). The backfill just gave vendor_payments a `bank_account_id`, which is
what lets `settlement_evidence()` pair a statement line to the payment behind it.
The double counts were always there, undetectable while 470 of 477 payments had a
NULL account.

`settlement_evidence()` had the same weakness as the statement matcher — amount
+ <=5 days, `LIMIT 1`, no payee test — so it called a $400 "Bands" row a Colonial
Wholesale beer settlement. It now walks candidates by date proximity and rejects
payee conflicts. The script also carries a **capacity guard**: group by
(account, amount) and require at least as many invoice-carrying payments as rows
claiming them, because forcing a non-settlement onto AP strips a real expense —
the same error inverted. Three $23.90 Cozzini rows against five $23.90 Cozzini
payments are three genuine settlements.

Provenance is `gl_source='accrual'`, `gl_status='suggested'`. Suggested is
deliberate: `_may_learn_rule_from()` gates purely on status, so 'confirmed' would
let a rule rebuild learn "US FOODSERVICE -> Accounts Payable" and miscode that
vendor non-settlement charges.

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
- Statement coverage: **ALL 14 UPLOADED 2026-08-27** — Chatham and Dennis both
  Jan → Aug 2. Every one foots its parsed rows to its own stated ending balance to
  the penny, and both chains link with no date gaps:
  - Chatham 34,013.57 → 25,785.04 → 17,235.02 → 9,865.21 → 8,969.03 → 14,026.59 → 34,649.29 → **101,902.38**
  - Dennis 9,495.35 → 24,280.14 → 35,746.63 → 32,162.19 → 37,795.39 → 3,194.35 → 11,090.70 → **51,035.55**
- Note July period is **07-01..08-02** — CCF closed on Aug 2, not the 31st. This is
  exactly why the period selector reads uploads and not calendar months.
- **Not yet imported.** Import per statement, oldest first: *Select unmatched → Import
  selected*, with *Mark matched register rows as cleared* ticked. Expect ~200 unmatched
  lines per statement (Toast deposits, card settlements, fees) — that is normal.
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

### Statement matching now reads the payee — DONE 2026-08-27

`_match_transactions` scored on direction + amount +-0.005 + date proximity only,
ties broken by iteration order. That paired a `$260.00 SALE FORE & AFT INC.`
statement line with a `$260.00 The Caron Group` bill. Chatham June alone had 14
matches choosing among 5-7 identical-amount candidates.

Now: `match` / `conflict` / `unknown` from `_payee_agreement`, and a named-payee
**conflict vetoes the pairing**. Vetoing rather than down-ranking because the
failure modes are asymmetric — a missed veto marks the WRONG bill cleared and
drops the real statement line, so a bill the vendor never cashed reads as paid.
A false veto merely imports a duplicate, which is visible and mergeable.

`_PAYEE_ALIASES` covers vendors the bank names differently (we store "PFG", the
statement prints "AR PAYMENT PERFORMANCEBOS"). **If a legitimate vendor starts
getting vetoed, add it there rather than loosening the rule.** `_payee_tokens` is
memoised — uncached it pushed the suite past 300s; cached, all 14 statements
match 3,050 lines in 0.11s.

Result: `exact` roughly doubled, `likely` collapsed, 0 conflicts accepted
(Chatham Jun 39 → 72 exact, May 23 → 44, Apr 11 → 30).

### Check OCR — DONE 2026-08-27

Chatham February reported "30 checks, 0 payees read, nothing legible" on every
one. Cause: `rednun.service` sets `Environment=PATH=/opt/red-nun-dashboard/venv/bin`
— venv only — so `/usr/bin/tesseract` was invisible to gunicorn while resolving
fine from a shell. **Every "nothing legible" meant no read was attempted.**

`_resolve_tesseract()` now resolves the binary explicitly ($TESSERACT_BIN ->
`shutil.which` -> /usr/bin, /usr/local/bin, /opt/homebrew/bin, /snap/bin), and a
missing binary returns `ocr_unavailable` once instead of N illegible checks.

The PATH fix alone read only 1 of 14 — the rest are handwritten payroll checks.
So `identify_payroll_payee()` names a check from `payroll_checks` when OCR fails:
exactly one uncleared, unvoided, non-DD check for that entity at that amount
within 14 days. Two candidates stays unread. Reported separately as
`payees_from_books`. Chatham Feb went **0 → 18 of 30** named.

Re-run OCR on an upload: `POST /api/bank-reconcile/checks/<upload_id>` with
`{"force": true}`. Idempotent.

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
7. ~~Apply the vendor-payment attribution fix~~ DONE 08-27 (Chatham January was not
   disturbed — 14 of the 15 rows dated inside it are `void` and never render).
8. ~~Recode settlements off the P&L~~ DONE 08-27 ($77,714.32).
9. Import the 14 statements, then run a dedupe pass per Dennis period — the
   newly-attributed bill_pay rows have never been deduped, which is why Dennis
   outstanding is large (May -70,026.22) but honest.
10. Resolve the 147 NULL-location payments.
11. Roll invoice line-item `category_type` up into AP-bill GL coding; emit AP bills.

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
| 1 | ~~Toast credential~~ **DONE 08-27** — rotated, sync healthy | — |
| 2 | ~~14 bank statement PDFs~~ **DONE 08-27** — all uploaded and verified | — |
| 3 | **Accountant format: DECIDED 08-27 — summary JEs now, full detail later.** | — |
| 4 | ~~Go/no-go on attribution~~ **DONE 08-27** | — |
| 5 | **Decide on `#244`** — US Foods $2,795.53 pending, NULL account, duplicates the already-cleared `#10`. It inflates Chatham outstanding by exactly that amount. | Chatham Jan close |

**The next move, and the cheapest per dollar: post one month of sales JEs (Gate 1).**
896 entries are already built and balanced, nothing has ever gone to QBO, so there is
no cleanup risk. That is what puts a real deliverable in the accountant hands.

---

## 5. Open items carried forward

- `#244` looks like a duplicate payment: US Foods, 2026-01-30, $2,795.53,
  pending, NULL account — against `#10`, same vendor/date/amount, cleared, acct 1.
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
