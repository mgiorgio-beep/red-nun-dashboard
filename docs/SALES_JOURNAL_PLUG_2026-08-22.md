# Sales Journal — the `Summary: Other` plug, and a `qb_accounts` trap

**Found:** 2026-08-22, while verifying the `push_to_qbo` fix.
**Status:** open. Not investigated — captured so the lead isn't lost.
**Why it matters:** this is a data-quality problem in the dashboard's own daily
sales numbers. It is *not* a QuickBooks problem. It stands whether or not we
ever push to QBO, and the dashboard is meant to become the books.

---

## 1. The finding

`build_journal_entry()` in `reports/sales_journal.py` forces every entry to
balance. After building sales credits and tender debits it computes the residual
and dumps it into a single plug line:

```python
balance_gap = round(total_credits_built - total_debits_built, 2)
if balance_gap > 0.005:
    add_line("Summary: Other", debit=balance_gap)
elif balance_gap < -0.005:
    add_line("Summary: Other", credit=abs(balance_gap))
```

The comment says *"any residual gap is rounding"*. It is not rounding.

**`balanced == 1` is therefore meaningless as a quality signal.** Every entry
balances by construction. The plug hides how badly the day reconciles.

### Distribution across the 176 `ready` entries (2026-08-22)

| plug size | entries | share |
|---|---:|---:|
| clean (< $1) | 56 | 32% |
| small ($1–50) | 38 | 22% |
| **material ($50–500)** | **75** | **43%** |
| **large (> $500)** | **7** | **4%** |

- **$19,002.14** total absorbed by the plug across the 176 entries.
- **75 days** where the daily sales journal does not reconcile against its own
  source data by more than $50.
- 2 entries where the plug exceeds 25% of the entry.

### Worst entries

| id | location | date | plug | entry total | plug % |
|---|---|---|---:|---:|---:|
| 3514 | dennis | 2026-08-21 | $893.29 | $1,517.38 | **59%** |
| 3457 | chatham | 2026-08-06 | $856.96 | $9,097.44 | 9% |
| 3513 | chatham | 2026-08-21 | $822.52 | $1,727.92 | **48%** |
| 3242 | dennis | 2026-06-05 | $751.46 | $7,776.90 | 10% |
| 3330 | chatham | 2026-07-02 | $591.89 | $17,249.33 | 3% |

### It lands in an expense account

`qb_line_mapping` maps `Summary: Other` to account **78** per location. In the
**Dennis** realm account 78 is **Cash Over/Short**, an *Expense*. So pushing a
plugged entry books phantom expense. On entry 3514 that would have been $893.29
of invented Cash Over/Short on a day with $1,370 of gross sales.

Entry 3514 also has **no `Tenders: Cash` line at all**, while a healthy Dennis
entry (3510) does. The plug appears to be standing in for missing cash tenders.

---

## 2. The most promising lead

**The two worst plugs — 3514 (Dennis) and 3513 (Chatham) — are both dated
2026-08-21, the most recent business day, and both are ~50–59% plug.**

That is the exact symptom `monitoring/daily_journal.py`'s re-pull hack was
written to fix: the live 10-minute Toast sync freezing mid-evening, so Late
Night service (runs to 1am) and after-midnight tabs never get captured. The
day's sales credits land but the tenders don't, and the plug swallows the
difference.

**Hypothesis to test first:** plug size correlates with incomplete Toast syncs.
Join plug size per entry against `sync_log` coverage for that business date. If
it holds, the fix is upstream in sync reliability, not in the journal builder —
and the journal builder should *refuse to build* rather than plug.

`daily_journal.py` now gates that re-pull behind `--force-resync` /
`FORCE_RESYNC=1` (it previously ran unconditionally every night). Re-running it
for a plugged date and rebuilding the entry is the cheapest way to test the
hypothesis on a single day.

---

## 3. Guard added 2026-08-22

`push_to_qbo()` now refuses any entry whose plug exceeds `MAX_PLUG_TO_PUSH`
(default $1.00, override with `QB_MAX_PLUG`) and says why. This is a backstop,
not a fix — it stops a plugged entry reaching the books, it does not make the
day reconcile.

**Do not relax this threshold to make entries pushable.** A large plug means the
sales data is wrong, and posting it launders a data error into the general
ledger.

---

## 4. Trap: `qb_accounts` cannot validate Dennis mappings

`qb_accounts` has **no `location` column** and was synced exactly once
(`synced_at = 2026-04-13 18:12:47`, 221 rows, single timestamp). It therefore
holds the chart of accounts for **one realm only**.

Chatham and Dennis are **separate QuickBooks companies** with separate realm IDs
(`QB_REALM_ID_CHATHAM` / `QB_REALM_ID_DENNIS`) and **independent account-ID
numbering**. `qb_line_mapping.qbo_account` stores realm-specific IDs.

**So resolving a Dennis mapping against `qb_accounts` silently returns another
company's account names — plausible-looking nonsense, with no error.**

Worked example. Dennis entry 3514's account IDs, resolved both ways:

| id | via `qb_accounts` (wrong) | via live Dennis realm (right) |
|---|---|---|
| 77 | Dues & Subscriptions | **Sales Tax Payable** |
| 175 | American Express CC | **Tip Bank** |
| 71 | Payroll Taxes | **Credit Card Sales** |
| 78 | Postage | **Cash Over/Short** |
| 1 | Services | Services |

The wrong column looks like a catastrophic mis-mapping and is entirely an
artifact of the lookup. This cost real time on 2026-08-22 and will do so again.

**To validate a mapping, query the realm directly** — read-only:

```
GET https://quickbooks.api.intuit.com/v3/company/{realm_id}/query
    ?query=select Id, Name, AccountType from Account where Id in ('54','71',...)
    &minorversion=75
```

**Fix worth doing:** add a `location` (or `realm_id`) column to `qb_accounts`
and re-sync both realms into it, so the table can answer the question it looks
like it can already answer.

---

## 5. Related, same session

- `~/.qb_tokens_chatham.json` is ~131 days old; Intuit expires refresh tokens at
  ~101 days. Chatham cannot post until someone re-runs `qb_push.py --auth`.
  `push_to_qbo()` now falls back to the legacy token file on **staleness**
  rather than mere existence, so a present-but-dead file can't defeat the
  fallback — but the dead Chatham token still needs a real re-auth.
- Of 176 `ready` entries, exactly **1** has ever posted (id 3510, Dennis
  2026-08-20, QBO txn 29599 — the verification push). 175 remain `ready` by
  decision, not by failure.

---

## 6. Unrelated but found the same day: `payroll_checks.check_number` is not a key

The number in `payroll_checks.check_number` is NOT the number printed on the
physical check that cleared the bank.

Dennis March statement lines read `Check 9689 / 9692 / 9693 / 9695`, clearing
03/23–03/24. The `payroll_checks` rows bearing those exact numbers belong to
the **2026-05-25..06-07** pay period, are different employees, and carry
different amounts. The March checks were recorded in the dashboard under its
own sequence, **2011–2017**, so the number the bank actually printed was never
stored anywhere.

Measured across all Dennis statement check lines:

```
statement "Check NNNN" lines : 66
number AND amount agree      :  0
```

**Zero of 66.** Matching a statement line to a payroll check on check number
would have matched a March bank line to a June payroll row. Amount + date is
the reliable signal; the check number is actively misleading.

This directly contradicts the handover brief's §7 job 034, which proposes
backfilling `check_number` and matching on it. That plan needs rethinking
before it is built — and OCR'd payee names, which 034 also proposes, are a
better signal than the number precisely because the number is wrong.

Pinned by `tests/test_bank_register.py::TestCheckNumberIsNotAKey`, which fails
if any agreement ever appears — so check-number matching can only be adopted
deliberately, never by accident.
