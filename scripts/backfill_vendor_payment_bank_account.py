#!/usr/bin/env python3
"""Assign vendor_payments.bank_account_id from location.

WHY
---
Dennis's bank register shows ZERO bill_pay rows, so Dennis AP can never tie to
the register. Cause: `vendor_payments.bank_account_id` is almost never
populated -- 470 of 477 rows are NULL -- and `build_register_view` folds NULL
rows into a register only for the catch-all account, which is keyed on
`account_last4 == '5975'` (Chatham). Dennis is 2757, so every unassigned Dennis
vendor payment lands in CHATHAM's register and none land in Dennis's.

Current state (2026-08-27):

    loc=(null)    acct=NULL   147 rows   375,838.73
    loc=chatham   acct=NULL   168 rows   154,955.40
    loc=chatham   acct=1        4 rows    10,049.02
    loc=dennis    acct=NULL   155 rows   179,727.29
    loc=dennis    acct=1        3 rows     8,163.13   <-- explicitly wrong

payroll_checks does NOT have this problem (486 chatham -> acct 1, 201 dennis ->
acct 2), which is why payroll rows appear in both registers and bill pay does
not. Same defect family as the `chatham` fallback at auto_pay.py:504 /
payment_routes.py:705, which picks a bank account and check stock.

⚠️  THIS BREAKS A CLOSED PERIOD, BY DESIGN
------------------------------------------
3 of Chatham January's 8 bill_pay rows are Dennis-located payments totalling
8,163.13 (vendor_payments #27, #28, #29 -- all PFG). Moving them to Dennis
removes 8,163.13 of outflow from Chatham's January book, so bank_reconciliation
rec#1 -- currently the only reconciliation whose tie is meaningful (delta 0.00,
outstanding -5,151.48 across 9 items) -- will no longer tie and must be
re-closed.

That is the correct consequence of a correct fix, not a reason to skip it. But
run it deliberately, and re-close Chatham January afterwards. Order:

    1. this script --apply
    2. re-close Chatham Jan on /bank-reconcile (it will show a non-zero delta
       until the moved rows are accounted for)
    3. Dennis Jan/Feb now carry real book-side rows, so their ties can finally
       fail -- re-close them too, and their "hollow tie" warning should clear

SCOPE
-----
Writes bank_account_id ONLY where `location` says unambiguously which account:

    location='dennis'  -> the account with last4 2757
    location='chatham' -> the account with last4 5975

Leaves alone (reported, never guessed):
  * location IS NULL (147 rows) -- nothing to derive an account from
  * rows already pointing at the account their location implies

By default it does NOT touch rows inside a closed reconciliation period; pass
--include-closed to move those too (which is what actually fixes Chatham Jan).

USAGE
-----
    venv/bin/python3 scripts/backfill_vendor_payment_bank_account.py
    venv/bin/python3 scripts/backfill_vendor_payment_bank_account.py --apply --include-closed

    # scratch copy (the env var is DB_PATH, not TOAST_DB_PATH; use .backup,
    # plain cp of the live WAL DB gives a TORN snapshot)
    sqlite3 /var/lib/rednun/toast_data.db ".backup /tmp/t.db"
    DB_PATH=/tmp/t.db venv/bin/python3 scripts/backfill_vendor_payment_bank_account.py

Back up before --apply.
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import get_connection, DB_PATH  # noqa: E402

LAST4_BY_LOCATION = {"dennis": "2757", "chatham": "5975"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--include-closed", action="store_true",
                    help="also move rows inside a closed reconciliation period "
                         "(required to fix Chatham January)")
    args = ap.parse_args()

    print(f"DB_PATH = {DB_PATH}")
    print(f"mode    = {'APPLY (writing)' if args.apply else 'DRY RUN (no writes)'}")
    print(f"closed  = {'included' if args.include_closed else 'skipped'}\n")

    conn = get_connection()

    acct_by_last4 = {}
    for r in conn.execute("SELECT id, short_name, account_last4 FROM bank_accounts"):
        if r["account_last4"]:
            acct_by_last4[r["account_last4"]] = dict(r)
    target_acct = {}
    for loc, last4 in LAST4_BY_LOCATION.items():
        a = acct_by_last4.get(last4)
        if not a:
            print(f"  !! no bank_account with last4 {last4} for location {loc}; aborting")
            conn.close()
            return
        target_acct[loc] = a["id"]
        print(f"  {loc:8s} -> acct {a['id']} {a['short_name']!r} (last4 {last4})")

    # closed periods, so we can flag rows whose account move disturbs a sign-off
    closed = defaultdict(list)
    for r in conn.execute(
            "SELECT bank_account_id, period_start, period_end, id, status "
            "FROM bank_reconciliations WHERE status = 'reconciled'"):
        closed[r["bank_account_id"]].append(
            (r["period_start"], r["period_end"], r["id"]))

    def closed_rec(acct, day):
        for a, b, rid in closed.get(acct, []):
            if day and a <= day <= b:
                return rid
        return None

    # ── BANK EVIDENCE, which outranks `location` ──────────────────────────
    # `location` says where the EXPENSE belongs. `bank_account_id` must say
    # which bank actually paid. For an intercompany payment those differ, and
    # only the statement can tell them apart.
    #
    # Proven case: vendor_payments #27/#28/#29 are location='dennis' but appear
    # as "AR PAYMENT PERFORMANCEBOS" debits on CHATHAM's January statement at
    # exactly their amounts. That cash left Chatham. Assigning them to Dennis
    # by location would have moved 8,163.13 out of Chatham January and
    # destroyed a reconciliation that ties to the penny.
    #
    # Evidence is read from bank_statement_uploads.parsed_json, NOT from
    # surviving manual_bank_entries: dedupe DELETES the statement line once it
    # is matched to a book row, so 22 of Chatham January's 218 lines no longer
    # exist as manual entries. parsed_json still holds all of them.
    import json as _json
    from datetime import date as _date

    evidence = defaultdict(list)   # cents -> [(acct_id, tx_date)]
    for u in conn.execute("SELECT bank_account_id, parsed_json "
                          "FROM bank_statement_uploads "
                          "WHERE parsed_json IS NOT NULL"):
        try:
            txs = (_json.loads(u["parsed_json"]) or {}).get("transactions", []) or []
        except Exception:
            continue
        for t in txs:
            d = float(t.get("debit") or 0)
            if d > 0:
                evidence[int(round(d * 100))].append(
                    (u["bank_account_id"], t.get("date")))

    def evid_accounts(amount, pay_date, window=45):
        """Accounts whose statement shows a debit at this amount near this date."""
        out = set()
        pd = None
        try:
            pd = _date.fromisoformat(pay_date) if pay_date else None
        except (TypeError, ValueError):
            pass
        for acct, tdate in evidence.get(int(round(float(amount or 0) * 100)), []):
            if pd is None or not tdate:
                out.add(acct)
                continue
            try:
                lag = (_date.fromisoformat(tdate) - pd).days
            except (TypeError, ValueError):
                out.add(acct)
                continue
            if -7 <= lag <= window:
                out.add(acct)
        return out

    rows = [dict(r) for r in conn.execute("""
        SELECT id, vendor, location, payment_date, payment_total,
               bank_account_id, status, reconciliation_id, cleared
          FROM vendor_payments
         ORDER BY payment_date, id
    """)]

    to_move, skipped_null_loc, already_ok, disturbs = [], [], [], []
    intercompany, no_evidence = [], []
    for r in rows:
        loc = (r["location"] or "").strip().lower()
        if loc not in target_acct:
            skipped_null_loc.append(r)
            continue
        by_loc = target_acct[loc]

        ev = evid_accounts(r["payment_total"], r["payment_date"])
        if len(ev) == 1:
            want = next(iter(ev))
            if want != by_loc:
                # The bank disagrees with the location. Trust the bank for the
                # cash side and record it as intercompany -- do NOT "fix" it.
                intercompany.append((r, by_loc, want))
        elif not ev:
            want = by_loc
            no_evidence.append(r)
        else:
            # Both statements show this amount; cannot attribute. Leave alone.
            intercompany.append((r, by_loc, None))
            continue

        if r["bank_account_id"] == want:
            already_ok.append(r)
            continue

        # Which closed period does it sit in TODAY (under its current account)?
        cur_acct = r["bank_account_id"]
        if cur_acct is None:
            # NULL rows render in the catch-all (5975) register
            cur_acct = acct_by_last4["5975"]["id"]
        rid = closed_rec(cur_acct, r["payment_date"])
        if rid:
            disturbs.append((r, want, rid))
            if not args.include_closed:
                continue
        to_move.append((r, want))

    def money(rs):
        return sum(float(x["payment_total"] or 0) for x in rs)

    print(f"\n  rows total                          : {len(rows)}")
    print(f"  already on the right account        : {len(already_ok)}")
    print(f"  location NULL, cannot derive        : {len(skipped_null_loc)} "
          f"({money(skipped_null_loc):,.2f})")
    print(f"  bank evidence contradicts location  : {len(intercompany)} "
          f"(INTERCOMPANY — bank wins, reported not forced)")
    print(f"  no bank evidence, location used     : {len(no_evidence)} "
          f"({money(no_evidence):,.2f})")
    print(f"  will move                           : {len(to_move)} "
          f"({money([r for r, _ in to_move]):,.2f})")
    print(f"  of which sit in a CLOSED period     : {len(disturbs)} "
          f"({money([r for r, _, _ in disturbs]):,.2f})"
          f"{'  <-- INCLUDED' if args.include_closed else '  <-- skipped'}")

    if intercompany:
        print("\n  --- INTERCOMPANY / unattributable (bank disagrees with location) ---")
        for r, by_loc, ev_acct in intercompany:
            where = f"acct {ev_acct}" if ev_acct else "BOTH statements (ambiguous)"
            print(f"    #{r['id']:<5d} {r['payment_date']} loc={r['location']:8s} "
                  f"{float(r['payment_total'] or 0):>10,.2f} "
                  f"location implies acct {by_loc}, bank says {where}  "
                  f"{(r['vendor'] or '')[:20]}")
        print("    The expense belongs to `location`; the CASH left the account the")
        print("    bank shows. Both facts are correct — this needs an intercompany")
        print("    due-to/due-from entry, not a reassignment.")

    if disturbs:
        print("\n  --- rows inside a signed-off period ---")
        for r, want, rid in disturbs:
            print(f"    #{r['id']:<5d} {r['payment_date']} loc={r['location']:8s} "
                  f"{float(r['payment_total'] or 0):>10,.2f} "
                  f"acct {r['bank_account_id']} -> {want}  "
                  f"(rec#{rid}) {(r['vendor'] or '')[:20]}")
        print("    Moving these changes that period's book balance; it will need")
        print("    re-closing on /bank-reconcile afterwards.")

    by_move = defaultdict(int)
    for r, want in to_move:
        by_move[(r["bank_account_id"], want)] += 1
    if by_move:
        print("\n  --- move summary (from -> to : rows) ---")
        for (frm, to), n in sorted(by_move.items(), key=lambda kv: str(kv[0])):
            print(f"    {str(frm):>6s} -> {to} : {n}")

    if skipped_null_loc:
        print(f"\n  --- location NULL, NOT touched ({len(skipped_null_loc)}) ---")
        print("  These need a location before an account can be derived. Many are")
        print("  older rows; check the invoice they came from.")
        for r in skipped_null_loc[:10]:
            print(f"    #{r['id']:<5d} {r['payment_date']} "
                  f"{float(r['payment_total'] or 0):>10,.2f} "
                  f"acct={r['bank_account_id']} {(r['vendor'] or '')[:24]}")
        if len(skipped_null_loc) > 10:
            print(f"    ... and {len(skipped_null_loc) - 10} more")

    if not args.apply:
        print(f"\nDRY RUN — nothing written. Re-run with --apply to move "
              f"{len(to_move)} rows.")
        conn.close()
        return

    if not to_move:
        print("\nNothing to move.")
        conn.close()
        return

    written = 0
    touched_recs = set()
    for r, want in to_move:
        cur = conn.execute(
            "UPDATE vendor_payments SET bank_account_id = ? WHERE id = ?",
            (want, r["id"]))
        written += cur.rowcount or 0
        if r["reconciliation_id"]:
            touched_recs.add(r["reconciliation_id"])

    # A moved row must not keep an R stamp from a period it no longer belongs to.
    for rid in touched_recs:
        conn.execute(
            "UPDATE vendor_payments SET reconciliation_id = NULL "
            "WHERE reconciliation_id = ? AND bank_account_id NOT IN ("
            "  SELECT bank_account_id FROM bank_reconciliations WHERE id = ?)",
            (rid, rid))
        conn.execute(
            "UPDATE bank_reconciliations SET status = 'stale', "
            "notes = TRIM(COALESCE(notes || char(10), '') || ?) WHERE id = ?",
            ("[backfill_vendor_payment_bank_account] rows moved to another "
             "account; this period no longer ties and must be re-closed.", rid))
    conn.commit()
    print(f"\nMoved {written} rows.")
    if touched_recs:
        print(f"Marked reconciliations {sorted(touched_recs)} stale — re-close them.")

    print("\n  --- state after ---")
    for r in conn.execute("""
        SELECT COALESCE(location,'(null)') loc,
               COALESCE(CAST(bank_account_id AS TEXT),'(NULL)') acct, COUNT(*) n
          FROM vendor_payments GROUP BY loc, acct ORDER BY loc, acct"""):
        print(f"    loc={r['loc']:10s} acct={r['acct']:6s} {r['n']:5d} rows")
    conn.close()


if __name__ == "__main__":
    main()
