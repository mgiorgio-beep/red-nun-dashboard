#!/usr/bin/env python3
"""Backfill payroll_checks.check_number from bank-statement check lines.

WHY
---
`payroll_checks.check_number` is populated on only ~1/3 of rows (230/687 as of
2026-08-27). Of the 330 Manual-method rows -- the ones that actually hit the
bank as a paper check -- 100 carry no check number. Direct Deposit rows
legitimately have none and are ignored here.

`_match_transactions` in bank_reconcile_routes scores a statement line against
the register like this:

    exact   ref equality AND date within 14 days   -> score 100 - day_diff
    likely  date within 4 days                     -> score  50 - day_diff
    likely  date within 7 days                     -> score  30 - day_diff
    none    otherwise

The register exposes a payroll row's ref as the raw check_number (line 937).
With no check number the ref is "", `ref_match` is False, and the row can
never reach `exact` -- it falls back to pure date proximity and anything
clearing more than 7 days after pay date never matches at all. Both the
payroll row and the statement's check line then sit in the register as a
double count. Worked example, Dennis May: payroll PR-2051 Leticia $1,160.20
dated 05-01 and statement `Check 9711` $1,160.20 dated 05-04, three days
apart, both present.

Now that a completed reconciliation stamps rows R, a wrong match is sticky --
so this backfill refuses to guess.

SAFETY
------
Writes ONLY where the pairing is unambiguous in both directions:

  * same bank_account_id
  * statement row is an outflow carrying a numeric ref_number (the check no.)
  * net_pay equals |statement amount| to the penny (integer cents)
  * statement date is 0..MAX_LAG_DAYS on or after the pay date (a check
    cannot clear before it is written)
  * exactly ONE candidate statement line for that payroll row, AND exactly
    ONE candidate payroll row for that statement line
  * the check number is not already taken by a different payroll row

Anything ambiguous is reported and left alone. Dry run by default.

USAGE
-----
    # report only, writes nothing
    venv/bin/python3 scripts/backfill_payroll_check_numbers.py

    # write the unambiguous ones
    venv/bin/python3 scripts/backfill_payroll_check_numbers.py --apply

    # against a scratch copy (NOTE: the env var is DB_PATH, not TOAST_DB_PATH)
    sqlite3 /var/lib/rednun/toast_data.db ".backup /tmp/t.db"
    DB_PATH=/tmp/t.db venv/bin/python3 scripts/backfill_payroll_check_numbers.py

Take a backup before --apply. Plain `cp` of the live DB gives a TORN snapshot
while gunicorn is writing -- use sqlite3 ".backup".
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import get_connection, DB_PATH  # noqa: E402

# A paper check normally clears within a few weeks. 45 days is generous enough
# to catch a slow one while staying well short of "any check that month".
MAX_LAG_DAYS = 45


def cents(x):
    return int(round(float(x or 0) * 100))


def parse_day(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the unambiguous matches (default: dry run)")
    ap.add_argument("--max-lag", type=int, default=MAX_LAG_DAYS,
                    help=f"max days between pay date and clear date (default {MAX_LAG_DAYS})")
    args = ap.parse_args()

    print(f"DB_PATH = {DB_PATH}")
    print(f"mode    = {'APPLY (writing)' if args.apply else 'DRY RUN (no writes)'}")
    print(f"max lag = {args.max_lag} days\n")

    conn = get_connection()

    # ── payroll rows needing a check number ────────────────────────────────
    targets = [dict(r) for r in conn.execute("""
        SELECT pc.id, pc.employee_name, pc.net_pay, pc.bank_account_id,
               pc.cleared, pc.reconciliation_id,
               COALESCE(pr.pay_date, pc.pay_period_end) AS pay_date
          FROM payroll_checks pc
          LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
         WHERE (pc.payment_method IS NULL OR pc.payment_method != 'Direct Deposit')
           AND (pc.voided IS NULL OR pc.voided = 0)
           AND (pc.check_number IS NULL OR TRIM(CAST(pc.check_number AS TEXT)) = '')
           AND pc.bank_account_id IS NOT NULL
           AND pc.net_pay IS NOT NULL AND pc.net_pay > 0
         ORDER BY pay_date, pc.id
    """)]
    print(f"payroll rows missing a check number (Manual, not voided): {len(targets)}")

    # ── check numbers already in use, so we never duplicate one ────────────
    taken = {}
    for r in conn.execute("""
        SELECT id, bank_account_id, TRIM(CAST(check_number AS TEXT)) cn
          FROM payroll_checks
         WHERE check_number IS NOT NULL AND TRIM(CAST(check_number AS TEXT)) <> ''
    """):
        taken.setdefault((r["bank_account_id"], r["cn"].lstrip("0")), []).append(r["id"])

    # ── candidate statement check lines ────────────────────────────────────
    stmt = [dict(r) for r in conn.execute("""
        SELECT id, bank_account_id, entry_date, amount, ref_number, payee
          FROM manual_bank_entries
         WHERE amount < 0
           AND ref_number IS NOT NULL AND TRIM(ref_number) <> ''
           AND bank_account_id IS NOT NULL
    """)]
    # keep only numeric refs -- that is what a check number looks like
    stmt = [s for s in stmt if str(s["ref_number"]).strip().isdigit()]
    print(f"statement lines with a numeric check ref:                 {len(stmt)}")

    by_acct_amt = defaultdict(list)
    for s in stmt:
        by_acct_amt[(s["bank_account_id"], cents(abs(s["amount"])))].append(s)

    # ── pass 1: collect candidate pairs ───────────────────────────────────
    cand_for_payroll = defaultdict(list)   # payroll id -> [stmt rows]
    cand_for_stmt = defaultdict(list)      # stmt id    -> [payroll ids]

    for t in targets:
        pd = parse_day(t["pay_date"])
        if pd is None:
            continue
        for s in by_acct_amt.get((t["bank_account_id"], cents(t["net_pay"])), []):
            sd = parse_day(s["entry_date"])
            if sd is None:
                continue
            lag = (sd - pd).days
            if lag < 0 or lag > args.max_lag:
                continue
            cand_for_payroll[t["id"]].append(s)
            cand_for_stmt[s["id"]].append(t["id"])

    # ── pass 2: keep only mutually unique pairs ───────────────────────────
    by_id = {t["id"]: t for t in targets}
    resolved, ambiguous, unmatched, blocked = [], [], [], []

    for t in targets:
        cands = cand_for_payroll.get(t["id"], [])
        if not cands:
            unmatched.append(t)
            continue
        if len(cands) > 1:
            ambiguous.append((t, cands, "multiple statement lines match this check"))
            continue
        s = cands[0]
        rivals = cand_for_stmt.get(s["id"], [])
        if len(rivals) > 1:
            ambiguous.append(
                (t, cands,
                 f"statement line #{s['id']} also matches payroll "
                 f"{[r for r in rivals if r != t['id']]}"))
            continue
        cn = str(s["ref_number"]).strip()
        owner = taken.get((t["bank_account_id"], cn.lstrip("0")))
        if owner:
            blocked.append((t, s, f"check {cn} already on payroll {owner}"))
            continue
        resolved.append((t, s, cn))

    # ── report ────────────────────────────────────────────────────────────
    print(f"\n  unambiguous, will backfill : {len(resolved)}")
    print(f"  ambiguous, left alone      : {len(ambiguous)}")
    print(f"  check no. already in use    : {len(blocked)}")
    print(f"  no candidate statement line : {len(unmatched)}")

    if resolved:
        print("\n--- WILL SET (payroll -> check number) ---")
        for t, s, cn in resolved:
            lag = (parse_day(s["entry_date"]) - parse_day(t["pay_date"])).days
            print(f"  payroll#{t['id']:<5d} acct{t['bank_account_id']} "
                  f"{t['pay_date']} {t['net_pay']:>9,.2f} "
                  f"{(t['employee_name'] or '')[:22]:22s} -> check {cn:>8s} "
                  f"(stmt#{s['id']}, cleared {s['entry_date']}, +{lag}d)")

    if ambiguous:
        print("\n--- AMBIGUOUS, NOT WRITTEN (resolve by hand) ---")
        for t, cands, why in ambiguous:
            print(f"  payroll#{t['id']:<5d} {t['pay_date']} {t['net_pay']:>9,.2f} "
                  f"{(t['employee_name'] or '')[:22]:22s} :: {why}")
            for s in cands[:6]:
                print(f"        cand stmt#{s['id']} {s['entry_date']} "
                      f"ref={s['ref_number']} {(s['payee'] or '')[:28]}")

    if blocked:
        print("\n--- BLOCKED (check number already assigned) ---")
        for t, s, why in blocked:
            print(f"  payroll#{t['id']:<5d} {t['pay_date']} {t['net_pay']:>9,.2f} :: {why}")

    if unmatched:
        print(f"\n--- NO STATEMENT LINE ({len(unmatched)}) ---")
        print("  Either the check has not cleared, or no statement covers the")
        print("  period it cleared in. Not an error on its own.")
        for t in unmatched[:20]:
            print(f"  payroll#{t['id']:<5d} acct{t['bank_account_id']} {t['pay_date']} "
                  f"{t['net_pay']:>9,.2f} cleared={t['cleared']} "
                  f"{(t['employee_name'] or '')[:24]}")
        if len(unmatched) > 20:
            print(f"  ... and {len(unmatched) - 20} more")

    # ── WRONG-SERIES DETECTION ────────────────────────────────────────────
    # A populated check_number is not automatically a useful one. Each location
    # prints from its own sequence (check_config.check_number_next): Chatham
    # 2000s, Dennis 9700s. Dennis rows carrying a 2000-series number are
    # Chatham's sequence leaking onto Dennis checks, so the ref can never equal
    # what the bank recorded and the row can never reach the `exact` path --
    # the same failure as an empty column, but wearing a confident-looking
    # value. Worked example: payroll#183 records 2051, the bank saw 9711.
    #
    # Detected, never auto-corrected: overwriting would discard the number
    # actually printed on the paper check. Report and let a human decide.
    print("\n" + "=" * 74)
    print("WRONG-SERIES CHECK: populated numbers the bank never saw")
    print("=" * 74)

    stmt_refs = defaultdict(set)
    for s in stmt:
        stmt_refs[s["bank_account_id"]].add(str(s["ref_number"]).strip().lstrip("0"))

    cfg = {}
    try:
        for r in conn.execute(
                "SELECT location, check_number_next FROM check_config"):
            cfg[r["location"]] = r["check_number_next"]
        print(f"  check_config next-numbers: {cfg}")
    except Exception as e:
        print(f"  (check_config unreadable: {e})")

    populated = [dict(r) for r in conn.execute("""
        SELECT pc.id, pc.employee_name, pc.net_pay, pc.check_number,
               pc.bank_account_id, pc.location,
               COALESCE(pr.pay_date, pc.pay_period_end) AS pay_date
          FROM payroll_checks pc
          LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
         WHERE pc.check_number IS NOT NULL
           AND TRIM(CAST(pc.check_number AS TEXT)) <> ''
           AND (pc.voided IS NULL OR pc.voided = 0)
           AND (pc.payment_method IS NULL OR pc.payment_method != 'Direct Deposit')
           AND pc.bank_account_id IS NOT NULL
         ORDER BY pay_date, pc.id
    """)]

    on_stmt, off_stmt = [], []
    for p in populated:
        cn = str(p["check_number"]).strip().lstrip("0")
        (on_stmt if cn in stmt_refs.get(p["bank_account_id"], set())
         else off_stmt).append(p)
    print(f"  confirmed against a statement line : {len(on_stmt)}")
    print(f"  never seen on any statement line   : {len(off_stmt)}")

    # Split the unconfirmed ones: is there even a statement covering the period?
    cov = defaultdict(list)
    for r in conn.execute(
            "SELECT bank_account_id, period_start, period_end "
            "FROM bank_statement_uploads "
            "WHERE period_start IS NOT NULL AND period_end IS NOT NULL"):
        cov[r["bank_account_id"]].append((r["period_start"], r["period_end"]))

    def covered(acct, day):
        return any(a <= day <= b for a, b in cov.get(acct, []))

    no_cov = [p for p in off_stmt if not covered(p["bank_account_id"], p["pay_date"] or "")]
    suspect = [p for p in off_stmt if covered(p["bank_account_id"], p["pay_date"] or "")]
    print(f"    of those, no statement covers the pay date : {len(no_cov)}"
          f"   (coverage gap, NOT a bad number)")
    print(f"    of those, a statement DOES cover it        : {len(suspect)}"
          f"   <-- wrong series / wrong number")

    if suspect:
        print("\n  --- SUSPECT: statement covers the date but this number is absent ---")
        for p in suspect:
            band = (int(str(p["check_number"]).strip()) // 1000) * 1000 \
                if str(p["check_number"]).strip().isdigit() else "?"
            alt = [s for s in by_acct_amt.get(
                (p["bank_account_id"], cents(p["net_pay"])), [])]
            alt_s = ", ".join(
                f"stmt#{a['id']}:{a['ref_number']}@{a['entry_date']}" for a in alt[:4]) or "none"
            print(f"    payroll#{p['id']:<5d} acct{p['bank_account_id']} {p['pay_date']} "
                  f"{p['net_pay']:>9,.2f} recorded={p['check_number']!r} "
                  f"(band {band}) {(p['employee_name'] or '')[:20]:20s} "
                  f"same-amount stmt lines: {alt_s}")
        print("\n  NOT auto-corrected — overwriting would discard the number printed")
        print("  on the paper check. Decide per row, then set it by hand.")

    # ── apply ─────────────────────────────────────────────────────────────
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write "
              f"{len(resolved)} check numbers.")
        conn.close()
        return

    if not resolved:
        print("\nNothing unambiguous to write.")
        conn.close()
        return

    written = 0
    for t, s, cn in resolved:
        cur = conn.execute(
            "UPDATE payroll_checks SET check_number = ? "
            "WHERE id = ? AND (check_number IS NULL "
            "                  OR TRIM(CAST(check_number AS TEXT)) = '')",
            (cn, t["id"]),
        )
        written += cur.rowcount or 0
    conn.commit()
    print(f"\nWrote {written} check numbers.")

    have = conn.execute(
        "SELECT COUNT(*) FROM payroll_checks WHERE check_number IS NOT NULL "
        "AND TRIM(CAST(check_number AS TEXT)) <> ''").fetchone()[0]
    tot = conn.execute("SELECT COUNT(*) FROM payroll_checks").fetchone()[0]
    man = conn.execute(
        "SELECT COUNT(*) FROM payroll_checks WHERE (payment_method IS NULL "
        "OR payment_method != 'Direct Deposit')").fetchone()[0]
    man_have = conn.execute(
        "SELECT COUNT(*) FROM payroll_checks WHERE (payment_method IS NULL "
        "OR payment_method != 'Direct Deposit') AND check_number IS NOT NULL "
        "AND TRIM(CAST(check_number AS TEXT)) <> ''").fetchone()[0]
    print(f"check_number now {have}/{tot} overall "
          f"({man_have}/{man} of Manual-method rows, which are the ones that "
          f"hit the bank).")
    conn.close()


if __name__ == "__main__":
    main()
