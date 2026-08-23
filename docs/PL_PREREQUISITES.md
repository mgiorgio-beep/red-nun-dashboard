# P&L Prerequisites — named blockers, so they can't be forgotten

Each item below **gates** a specific deliverable. Nothing here blocks the Dennis
management P&L, which is clear to build.

Status as of 2026-08-23.

---

## Gates: "Chatham history P&L"

### PREREQ-1 — Restamp ~5,000 historical Chatham JE line items

`qb_journal_line_items.qbo_account` was written at JE-build time from the old,
wrong `qb_line_mapping.qbo_account` values. Every Chatham line from 2025-04
through 2026-08 therefore carries an account id that means something else in
Chatham's chart — most seriously, all credit-card tender lines point at
"Food Sales", an Income account, so a clearing **debit** sits on revenue.

The mapping itself is fixed (see PREREQ notes below); entries built from now on
resolve correctly through `gl_accounts`. **History was deliberately not
rewritten** — that was an explicit call on 2026-08-23, not an oversight.

Consequence: **any Chatham P&L covering dates before the fix will be wrong** if
it groups by the stored `qbo_account`. Two ways out, whichever is preferred:

- restamp the stored lines from the corrected mapping, or
- have the P&L join `journal_name → qb_line_mapping.gl_account_id` at read time
  and ignore the stored `qbo_account` entirely (cheaper, and arguably right —
  the stored id is a push artifact, not a fact about the books).

Amounts are unaffected either way. Only the account each line points at is
wrong, and every JE still balances.

Dennis needs none of this. Its mapping was correct throughout.

---

## Gates: "QBO export" (step 7)

### PREREQ-2 — Backfill `qbo_id` on Chatham's `Daily Sales:*` accounts

`Daily Sales:Cash Sales` (513), `Daily Sales:Credit Card Sales` (514) and
`Daily Sales:Doordash` (515) are active and correct, but carry `qbo_id = NULL`.
Seven Chatham tender lines therefore resolve internally but **cannot be pushed**
to QBO.

This is by design, not breakage: the JE builder refuses to fall back to the old
`qbo_account` once a line is on the spine, because that fallback is exactly what
would restore the wrong ids. A missing QBO id reads as "not pushable yet",
which is visible; a wrong one does not.

Fix: pull the real ids from the Chatham QBO company file and write them onto
those three accounts. Dennis is 20/20 pushable and needs nothing.

### PREREQ-3 — Chatham QBO token re-auth

The Chatham QBO connection is dead (expired token). Needed for PREREQ-2 and for
any push. Also the reason `bank_deposits` was dropped from the plan rather than
repaired — Toast settlements arrive as statement rows, which is the verified
path, and a second source of the same money invites double-counting.

### PREREQ-4 — Plug-day resolution

121 days across both entities carry a `Summary: Other` plug over $50
(66 Chatham / $13,675.19, 55 Dennis / $8,080.76). Net is far smaller
(−$4,205.87 Chatham, +$1,224.86 Dennis) because over/short cancels — which is
why a monthly net hides it and the gate is per-day.

Until resolved, these days flow into the P&L **flagged**, and the month carries
a banner naming the count and total. Do not push a month to QBO whose plug days
are unresolved.

---

## Standing principle

**Agreement, not resolvability, is the test.**

A wrong id that resolves cleanly is more dangerous than one that dangles. The
225 orphaned codings announced themselves as blank boxes; Chatham's tender
mapping resolved perfectly to the wrong account and announced nothing. Whenever
an id is inherited, validate that it agrees with what the row is supposed to
MEAN — not merely that it points at a live row.
