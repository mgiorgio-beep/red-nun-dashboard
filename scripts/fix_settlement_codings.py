#!/usr/bin/env python3
"""Recode settlement rows off P&L accounts and onto Accounts Payable.

WHY
---
These books recognise cost at INVOICE date, from the invoice's own line items:

    invoice confirmed :  Dr Food COGS         Cr Accounts Payable
    payment clears    :  Dr Accounts Payable  Cr Bank

A bank row that settles an invoice must therefore carry AP, not a P&L account.
Coding the payment to Food COGS as well books the same cost twice, and the
double count lands directly in the P&L the accountant and investors read.

`audit_settlement_codings()` finds them; nothing until now fixed the ones that
predate the API guard. As of 2026-08-27 it reports 40, e.g.

    manual_bank_entries#866  -> Food COGS
        (matches vendor payment #4 (US Foods) carrying 6 invoices)

This was pre-existing, not caused by the attribution backfill — the same test
fails on the older code. What the attribution fix did was give vendor_payments
a bank_account_id, which is what lets settlement_evidence() pair a statement
line with the payment behind it. The double counts were always there; they were
undetectable while every payment had a NULL account.

Bank rows with NO invoice behind them -- autopay utilities, card charges, bank
fees -- are the legitimate P&L source and are left alone. That distinction is
`settlement_evidence()`, not this script.

WHAT IT WRITES
--------------
For each flagged row: gl_account_id -> that entity's Accounts Payable account,
gl_source = 'accrual', gl_status = 'suggested'.

'suggested' is deliberate even though the account is certain. _may_learn_rule_from()
gates purely on gl_status, so 'confirmed' would let a rule rebuild learn
"US FOODSERVICE -> Accounts Payable" and miscode that vendor's non-settlement
charges. No human has reviewed these rows individually either.

Rows are SKIPPED, never guessed, when:
  * the entity has no active Accounts Payable account (nowhere correct to put it)
  * the row's location cannot be determined
  * the row is locked into a signed-off reconciliation (reconciliation_id set) --
    recoding it changes a closed period's P&L, so it needs the discrepancy flow.
    Pass --include-reconciled to do those too.

USAGE
-----
    venv/bin/python3 scripts/fix_settlement_codings.py                # dry run
    venv/bin/python3 scripts/fix_settlement_codings.py --apply
    venv/bin/python3 scripts/fix_settlement_codings.py --apply --include-reconciled

    # scratch copy (env var is DB_PATH, NOT TOAST_DB_PATH; use .backup --
    # plain cp of the live WAL database gives a TORN snapshot)
    sqlite3 /var/lib/rednun/toast_data.db ".backup /tmp/t.db"
    DB_PATH=/tmp/t.db venv/bin/python3 scripts/fix_settlement_codings.py --apply

Back up before --apply. This moves money between the P&L and the balance sheet.
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import get_connection, DB_PATH  # noqa: E402
from routes.register_routes import (  # noqa: E402
    audit_settlement_codings, settlement_evidence, _resolve_ap_account,
    GL_SOURCE_ACCRUAL, GL_STATUS_SUGGESTED,
)

TABLES = ("vendor_payments", "manual_bank_entries")
DATE_COL = {"vendor_payments": "payment_date", "manual_bank_entries": "entry_date"}
AMT_COL = {"vendor_payments": "payment_total", "manual_bank_entries": "amount"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--include-reconciled", action="store_true",
                    help="also recode rows locked into a signed-off period")
    args = ap.parse_args()

    print(f"DB_PATH = {DB_PATH}")
    print(f"mode    = {'APPLY (writing)' if args.apply else 'DRY RUN (no writes)'}")
    print(f"closed  = {'included' if args.include_reconciled else 'skipped'}\n")

    conn = get_connection()

    ap_by_loc = {}
    for loc in ("chatham", "dennis"):
        ap_by_loc[loc] = _resolve_ap_account(conn, loc)
        print(f"  {loc:8s} Accounts Payable -> "
              f"{ap_by_loc[loc] if ap_by_loc[loc] else 'MISSING (rows will be skipped)'}")
    for loc, acct in ap_by_loc.items():
        if acct:
            nm = conn.execute("SELECT name, account_type FROM gl_accounts WHERE id = ?",
                              (acct,)).fetchone()
            print(f"           {loc}: {nm['name']!r} ({nm['account_type']})")

    bad = audit_settlement_codings(conn)
    print(f"\n  settlements coded to a P&L account: {len(bad)}")
    if not bad:
        print("\nNothing to fix.")
        conn.close()
        return

    plan, skipped = [], []
    impact = defaultdict(float)
    for b in bad:
        t, rid = b["table"], b["id"]
        ev = settlement_evidence(conn, t, rid)
        loc = (ev or {}).get("location")
        row = conn.execute(
            f"SELECT {DATE_COL[t]} AS dt, {AMT_COL[t]} AS amt, reconciliation_id "
            f"FROM {t} WHERE id = ?", (rid,)).fetchone()
        target = ap_by_loc.get((loc or "").strip().lower())
        if not loc:
            skipped.append((b, row, "no location, cannot pick an AP account"))
            continue
        if not target:
            skipped.append((b, row, f"{loc} has no active Accounts Payable account"))
            continue
        if row["reconciliation_id"] and not args.include_reconciled:
            skipped.append((b, row, f"locked into reconciliation "
                                    f"#{row['reconciliation_id']}"))
            continue
        plan.append((b, row, loc, target))
        impact[(loc, str(row["dt"])[:7], b["gl_name"])] += abs(float(row["amt"] or 0))

    # ── CAPACITY GUARD ────────────────────────────────────────────────────
    # settlement_evidence answers per-row, so several statement rows can each
    # name the same payment as the thing they settle. That is fine when enough
    # matching payments exist -- three $23.90 Cozzini statement rows against
    # five $23.90 Cozzini payments really are three settlements -- and wrong
    # when they outnumber the payments, because then some of those rows are
    # separate charges and recoding them to AP would strip a real expense.
    #
    # So: group the plan by (account, amount) and require at least as many
    # invoice-carrying payments as rows claiming them. Excess rows are skipped,
    # newest first, rather than guessed at.
    from routes.register_routes import SETTLEMENT_MATCH_DAYS
    groups = defaultdict(list)
    for item in plan:
        b, row, loc, target = item
        if b["table"] != "manual_bank_entries":
            continue
        acct = conn.execute(
            "SELECT bank_account_id FROM manual_bank_entries WHERE id = ?",
            (b["id"],)).fetchone()["bank_account_id"]
        groups[(acct, round(abs(float(row["amt"] or 0)), 2))].append(item)

    over = []
    for (acct, amt), items in groups.items():
        if len(items) < 2:
            continue
        dates = [str(i[1]["dt"])[:10] for i in items]
        n_pay = conn.execute(
            """SELECT COUNT(*) FROM vendor_payments vp
                WHERE vp.bank_account_id = ?
                  AND ROUND(ABS(vp.payment_total), 2) = ?
                  AND (vp.status IS NULL OR vp.status NOT IN ('void','failed'))
                  AND ABS(JULIANDAY(vp.payment_date) - JULIANDAY(?)) <= ?
                  AND ((SELECT COUNT(*) FROM ap_payment_invoices api
                         WHERE api.payment_id = vp.ap_payment_id)
                     + (SELECT COUNT(*) FROM vendor_payment_invoices vpi
                         WHERE vpi.payment_id = vp.id)) > 0""",
            (acct, amt, min(dates), SETTLEMENT_MATCH_DAYS + 2)).fetchone()[0]
        if len(items) > n_pay:
            excess = sorted(items, key=lambda i: str(i[1]["dt"]), reverse=True)[
                : len(items) - n_pay]
            for e in excess:
                over.append((e, n_pay, len(items)))

    if over:
        print(f"\n  !! capacity guard: {len(over)} row(s) outnumber the payments "
              f"they claim to settle — skipping those")
        for (b, row, loc, target), n_pay, n_rows in over:
            print(f"     {b['table']}#{b['id']} {str(row['dt'])[:10]} "
                  f"{abs(float(row['amt'] or 0)):,.2f}: {n_rows} rows vs "
                  f"{n_pay} invoice-carrying payment(s)")
            skipped.append((b, row, f"only {n_pay} matching payment(s) for "
                                    f"{n_rows} claiming rows"))
        drop = {id(e[0]) for e in over}
        plan = [p for p in plan if id(p) not in drop]
        impact.clear()
        for b, row, loc, target in plan:
            impact[(loc, str(row["dt"])[:7], b["gl_name"])] += abs(float(row["amt"] or 0))

    print(f"  will recode : {len(plan)}")
    print(f"  skipped     : {len(skipped)}")

    print("\n--- P&L IMPACT (expense removed, by entity / month / account) ---")
    tot = 0.0
    for (loc, month, gl), amt in sorted(impact.items()):
        tot += amt
        print(f"    {loc:8s} {month}  {gl[:28]:28s} {amt:>12,.2f}")
    print(f"    {'':8s} {'':7s}  {'TOTAL double-counted expense removed':28s} {tot:>12,.2f}")

    print("\n--- ROWS ---")
    for b, row, loc, target in plan:
        print(f"    {b['table']:20s}#{b['id']:<5d} {str(row['dt'])[:10]} "
              f"{abs(float(row['amt'] or 0)):>10,.2f} {loc:8s} "
              f"{b['gl_name'][:22]:22s} -> AP({target})   {b['evidence'][:52]}")

    if skipped:
        print("\n--- SKIPPED ---")
        for b, row, why in skipped:
            print(f"    {b['table']:20s}#{b['id']:<5d} {b['gl_name'][:22]:22s} :: {why}")

    if not args.apply:
        print(f"\nDRY RUN — nothing written. Re-run with --apply to recode {len(plan)} rows.")
        conn.close()
        return

    n = 0
    for b, row, loc, target in plan:
        cur = conn.execute(
            f"UPDATE {b['table']} SET gl_account_id = ?, gl_source = ?, gl_status = ? "
            f"WHERE id = ?",
            (target, GL_SOURCE_ACCRUAL, GL_STATUS_SUGGESTED, b["id"]))
        n += cur.rowcount or 0
    conn.commit()
    print(f"\nRecoded {n} rows to Accounts Payable.")

    remaining = audit_settlement_codings(conn)
    print(f"audit_settlement_codings now reports: {len(remaining)}")
    for r in remaining[:10]:
        print(f"   still bad: {r['table']}#{r['id']} -> {r['gl_name']} ({r['evidence'][:44]})")
    conn.close()


if __name__ == "__main__":
    main()
