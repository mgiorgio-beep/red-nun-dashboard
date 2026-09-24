#!/usr/bin/env python3
"""The Payroll Liabilities switch (Mike, 2026-09-24). One transaction.

Labor moves to the payroll runs (reports/profit_loss.labor), so every bank
line that PAYS a run must leave the labor accounts for Payroll Liabilities
(116), or it would count twice. In order:

 1. Chatham Payroll Liabilities (gl 433, QBO 116) reactivated. 1/1/2026
    openings: Chatham 6,588.62, Dennis 3,279.17 — the 2025 paychecks cashed
    in 2026 (Dec 8-21 run plus older 2025 checks; Dennis includes Angel
    Germosen 7860 / 9529).
 2. Chatham Q1 2026 tax true-up recorded as an adjustment run: 3/31, period
    1/01-3/31, employer taxes -405.62, cited to Check HQ's 4/02 "Filing
    Period Closed" notice (7shifts exported no journal for it). The $0.02
    "separately refundable" per company stays off the books until it arrives.
 3. Mike's directed recodes: Dennis 9704 Yarmouth Coed Softball 900.00 and
    9654 Cape Cod Dart League 130.00 -> Advertising & Marketing; 9721 Barrows
    Waste 480.00 -> Trash Removal; Chatham TAX 7shifts -0.27 (1/29) -> Payroll
    Taxes.
 4. -> 116: every 7shifts impound (PCR 7shifts), both Q1 TAX refunds, and the
    carry-in checks above. (Maximilian Anderson's three returned deposits
    went to 116 earlier today.)
 5. Every paper paycheck (payroll_checks, not direct deposit) coded 116: the
    register rows that settle the run.
 6. Paper checks cashed weeks late merged into their paychecks through the
    dedupe's payroll mode (register_merge_audit rows), plus the five Chatham
    checks cashed 94-143 days late, each confirmed by its check image. Dennis
    9690 (Maya Jones, 57.46) waits for Mike to see the image.

Asserted: every statement period's bank balance and delta unchanged; the
per-pay-date tie-out (scripts/payroll_tieout.py) ends with zero failures; no
7shifts impound or paired check is left on a labor account.

    python scripts/payroll_switch_2026_09_24.py            # dry run
    python scripts/payroll_switch_2026_09_24.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from payroll_tieout import tieout  # noqa: E402

WHO = "mike-2026-09-24"
PL = {"chatham": 433, "dennis": 177}
BANK = {"chatham": 1, "dennis": 2}
OPENING = {"chatham": 6588.62, "dennis": 3279.17}
CARRY_IN = {
    # Dec 8-21 run (paid 12/26/2025) cashed in 2026
    "chatham": [311, 274, 239, 1662, 419, 1865, 257,
                # older 2025 paychecks cashed in 2026 (check numbers 1697-1832)
                273, 275, 310, 333, 351, 418, 422, 1592, 1665, 1666, 1763, 1819],
    "dennis": [794, 843, 493, 540,
               454, 491, 1045, 1046,
               3376, 3377],                      # Angel Germosen 7860 (7/11/25), 9529 (8/22/25)
}
DIRECTED = [(3217, 7, "Yarmouth Coed Softball League check 9704 — Advertising & Marketing"),
            (1124, 7, "Cape Cod Dart League check 9654 — Advertising & Marketing"),
            (3356, 238, "Barrows Waste Systems check 9721 (Inv I20898) — Trash Removal"),
            (423, 416, "TAX 7shifts -0.27 (1/29) — Payroll Taxes")]
TAX_REFUNDS = [2091, 1241]
LATE = [(1975, 144), (2433, 168), (2650, 69), (2647, 53), (2648, 29)]   # statement line -> paycheck


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"])


def recode(conn, line, gl, why):
    r = conn.execute("SELECT m.*, b.location FROM manual_bank_entries m JOIN bank_accounts b ON b.id=m.bank_account_id "
                     "WHERE m.id=?", (line,)).fetchone()
    assert r, f"line {line} missing"
    g = conn.execute("SELECT location, active FROM gl_accounts WHERE id=?", (gl,)).fetchone()
    assert g and g["location"] == r["location"] and g["active"], (line, gl)
    conn.execute("UPDATE manual_bank_entries SET gl_account_id=?, gl_source='human', gl_status='confirmed' WHERE id=?",
                 (gl, line))
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,"
                 "detail) VALUES ('row_remap','manual_bank_entries',?,?,?,'payroll liabilities switch',?)",
                 (line, r["gl_account_id"], gl, f"{WHO}: {why}"))
    return r


def merge_late(conn, line, pc_id):
    me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (line,)).fetchone()
    pc = conn.execute("SELECT * FROM payroll_checks WHERE id=?", (pc_id,)).fetchone()
    assert me and pc and not pc["cleared"] and abs(pc["net_pay"] + me["amount"]) < 0.005, (line, pc_id)
    conn.execute("UPDATE payroll_checks SET cleared=1, cleared_date=? WHERE id=?", (me["entry_date"], pc_id))
    conn.execute(
        """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
               target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
               match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (WHO, me["bank_account_id"], "payroll_check", pc_id, f"Payroll: {pc['employee_name']}", me["entry_date"],
         line, me["entry_date"], me["amount"], json.dumps(dict(me)), pc["net_pay"], None, None,
         f"manual ({WHO}): check image names the employee, amount and pay period; cashed beyond the 60-day window"))
    conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (line,))
    return me, pc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2)")]
        before = {u: state(conn, u) for u in uploads}

        # 1. account + openings
        conn.execute("UPDATE gl_accounts SET active=1 WHERE id=?", (PL["chatham"],))
        for loc, amt in OPENING.items():
            carried = -sum(conn.execute("SELECT amount FROM manual_bank_entries WHERE id=?", (i,)).fetchone()[0]
                           for i in CARRY_IN[loc])
            assert abs(carried - amt) < 0.005, f"{loc}: carry-in lines {carried:.2f} != opening {amt:.2f}"
            conn.execute("UPDATE gl_accounts SET opening_balance=?, opening_date='2026-01-01' WHERE id=?", (amt, PL[loc]))
            print(f"1. {loc} Payroll Liabilities opening 2026-01-01 = {amt:,.2f} (= {len(CARRY_IN[loc])} carry-in lines)")

        # 2. Chatham Q1 true-up
        assert not conn.execute("SELECT 1 FROM payroll_runs WHERE location='chatham' AND pay_date='2026-03-31'").fetchone()
        rid = conn.execute(
            """INSERT INTO payroll_runs (location, pay_period_start, pay_period_end, pay_date, memo, employee_count,
                   check_count, total_gross, total_net, total_wages, total_paycheck_tips, total_cash_tips,
                   total_ee_taxes, total_er_taxes, status, created_at, updated_at)
               VALUES ('chatham','2026-01-01','2026-03-31','2026-03-31',?,0,0,0,0,0,0,0,0,-405.62,'complete',
                       datetime('now'),datetime('now'))""",
            (f"Employer-tax adjustment 2026-03-31 — {WHO}: Q1 2026 filing-period true-up per Check HQ "
             f"'Filing Period Closed' notice of 2026-04-02 (refund 405.62 credited 4/13; 7shifts exported no "
             f"journal; $0.02 separately refundable left off the books)",)).lastrowid
        print(f"2. Chatham Q1 true-up recorded as run #{rid}: employer taxes -405.62")

        # 3. directed
        for line, gl, why in DIRECTED:
            r = recode(conn, line, gl, why)
            print(f"3. line {line} {r['entry_date']} {r['amount']:>9.2f} -> gl {gl}  ({why})")

        # 4. impounds, refunds, carry-ins -> 116
        n = t = 0
        for loc in PL:
            for r in conn.execute("""SELECT m.id FROM manual_bank_entries m WHERE m.bank_account_id=?
                                     AND m.payee LIKE 'PCR 7shifts%'""", (BANK[loc],)).fetchall():
                x = recode(conn, r["id"], PL[loc], "7shifts payroll impound settles the run")
                n += 1; t += -x["amount"]
        print(f"4. {n} 7shifts impounds -> 116 ({t:,.2f})")
        for line in TAX_REFUNDS:
            loc = "chatham" if line == 2091 else "dennis"
            x = recode(conn, line, PL[loc], "Q1 2026 tax true-up refund settles the adjustment run")
            print(f"4. line {line} {x['entry_date']} +{x['amount']:,.2f} TAX refund -> 116")
        for loc, lines in CARRY_IN.items():
            for line in lines:
                recode(conn, line, PL[loc], "2025 paycheck cashed in 2026 — settles the 1/1 Payroll Liabilities opening")
            print(f"4. {loc}: {len(lines)} carry-in lines -> 116")

        # 5. paper paychecks -> 116
        for loc in PL:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM payroll_checks WHERE location=? AND payment_method<>'Direct Deposit' "
                "AND COALESCE(voided,0)=0 AND ABS(COALESCE(net_pay,0))>0.0049", (loc,))]
            for i in ids:
                old = conn.execute("SELECT gl_account_id FROM payroll_checks WHERE id=?", (i,)).fetchone()[0]
                conn.execute("UPDATE payroll_checks SET gl_account_id=?, gl_source='human', gl_status='confirmed' "
                             "WHERE id=?", (PL[loc], i))
                if old != PL[loc]:
                    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,"
                                 "new_gl_account_id,match_rule,detail) VALUES ('row_remap','payroll_checks',?,?,?,"
                                 "'payroll liabilities switch',?)", (i, old, PL[loc], f"{WHO}: paper paycheck settles the run"))
            print(f"5. {loc}: {len(ids)} paper paychecks coded 116")

        # 6. merges
        for loc in PL:
            bank = dict(conn.execute("SELECT id, account_last4 FROM bank_accounts WHERE id=?", (BANK[loc],)).fetchone())
            r = brr._dedupe_period(conn, bank, "2026-01-01", "2026-08-31", 5, False, True, True, WHO, payroll_mode=True)
            s = r["summary"]
            assert s["ambiguous"] == 0 and s["name_mismatch"] == 0, s
            print(f"6. {loc}: payroll-mode dedupe merged {r['merged_count']} (${s['would_merge_amount']:,.2f})")
        for line, pc in LATE:
            me, p = merge_late(conn, line, pc)
            print(f"6. late: line {line} {me['entry_date']} {-me['amount']:>8.2f} -> paycheck #{pc} {p['employee_name']}")

        # checks
        after = {u: state(conn, u) for u in uploads}
        moved = [u for u in uploads if after[u] != before[u]]
        assert not moved, f"tie-out moved on uploads {moved}"
        print(f"bank balance and delta unchanged on all {len(uploads)} statement periods")
        rows, failures = tieout(conn, "2026-08-31")
        bad = [f for f in failures if f["pay"] >= "2026-01-01"]
        for f in bad:
            print("   TIE-OUT FAIL", f)
        assert not bad, "tie-out has failures"
        print("payroll tie-out: 0 failures")
        left = conn.execute(
            """SELECT b.location, g.name, COUNT(*), ROUND(SUM(-m.amount),2) FROM manual_bank_entries m
               JOIN bank_accounts b ON b.id=m.bank_account_id JOIN gl_accounts g ON g.id=m.gl_account_id
               WHERE g.name IN ('Wages','Payroll Expenses','Payroll Taxes','Tip Wages','Cash Tip Expense','Payroll Fees',
                                'Contract Labor','Wages-ERC') AND m.entry_date BETWEEN '2026-01-01' AND '2026-08-31'
               GROUP BY 1,2""").fetchall()
        print("bank rows still on labor accounts (these stay labor):")
        for r in left:
            print(f"   {r[0]:<8} {r[1]:<18} {r[2]:>3} rows {r[3]:>10,.2f}")
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
