#!/usr/bin/env python3
"""Mike, 2026-09-24, after the payroll switch:

 1. Chatham's 7shifts software charges come off labor (Payroll Expenses) to
    Dues & Subscriptions — the account Dennis's own 7shifts rule already uses.
    The 5/26 charge of 2,050.48 is an annual one: it moves to a new Prepaid
    Expenses account (Other Current Asset; local until created in QBO) and an
    expense_amortization schedule releases 1/12 a month, May 2026 - Apr 2027.
    The two rules that fed these lines ("SHIFTS", "COL 7SHIFTS") follow.
 2. Dennis check 9690 (57.46, 6/18) pairs to Maya Jones's 3/20 paycheck. The
    image reads Maya Jones, 03/20/26, pay period 03/02-03/15/26; the OCR's
    "Leticia Nascimento" was a misread. Cashed 90 days late, so outside the
    dedupe window: merged here with a register_merge_audit row.

Bank balance and delta of every statement period asserted unchanged.

    python scripts/chatham_7shifts_software_and_9690.py            # dry run
    python scripts/chatham_7shifts_software_and_9690.py --apply
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402
from routes.register_routes import init_register_tables  # noqa: E402

WHO = "mike-2026-09-24"
SUBS = 310                     # Chatham Dues & Subscriptions
ANNUAL_LINE = 2399             # 2026-05-26 7shifts 2,050.48
MONTHLY = [283, 1586, 1826, 2009, 2218, 2435, 2621, 2824, 3030]
RULES = [84, 422]              # Chatham "SHIFTS", "COL 7SHIFTS"


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"])


def log(conn, table, tid, old, new, rule, detail, kind="row_remap"):
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,"
                 "detail) VALUES (?,?,?,?,?,?,?)", (kind, table, tid, old, new, rule, f"{WHO}: {detail}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    init_register_tables()
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2)")]
        before = {u: state(conn, u) for u in uploads}

        pre = conn.execute("SELECT id FROM gl_accounts WHERE location='chatham' AND name='Prepaid Expenses'").fetchone()
        prepaid = pre[0] if pre else conn.execute(
            "INSERT INTO gl_accounts (name, account_type, account_subtype, location, active, opening_balance, "
            "opening_date) VALUES ('Prepaid Expenses','Other Current Asset','PrepaidExpenses','chatham',1,0,"
            "'2026-01-01')").lastrowid
        print(f"Prepaid Expenses (chatham) gl {prepaid}{'' if pre else ' — created, no QBO id yet'}")

        ann = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (ANNUAL_LINE,)).fetchone()
        assert ann and ann["bank_account_id"] == 1 and abs(ann["amount"] + 2050.48) < 0.005 and "7SHIFTS" in ann["payee"].upper()
        conn.execute("UPDATE manual_bank_entries SET gl_account_id=?, gl_source='human', gl_status='confirmed' WHERE id=?",
                     (prepaid, ANNUAL_LINE))
        log(conn, "manual_bank_entries", ANNUAL_LINE, ann["gl_account_id"], prepaid, "annual prepaid",
            "7shifts annual charge 5/26 -> Prepaid Expenses, released over 12 months")
        conn.execute("""INSERT INTO expense_amortization (location, source_table, source_id, prepaid_gl_account_id,
                            expense_gl_account_id, amount, start_month, months, memo, created_by)
                        VALUES ('chatham','manual_bank_entries',?,?,?,2050.48,'2026-05',12,
                                '7shifts annual (paid 2026-05-26)',?)""", (ANNUAL_LINE, prepaid, SUBS, WHO))
        print(f"line {ANNUAL_LINE} 2026-05-26 2,050.48 -> Prepaid; 12 x 170.87 (last 170.91) into Dues & Subscriptions, 2026-05..2027-04")

        tot = 0.0
        for line in MONTHLY:
            r = conn.execute("SELECT m.*, g.name FROM manual_bank_entries m LEFT JOIN gl_accounts g ON g.id=m.gl_account_id "
                             "WHERE m.id=?", (line,)).fetchone()
            assert r and r["bank_account_id"] == 1 and "SHIFTS" in r["payee"].upper() and r["name"] == "Payroll Expenses", line
            conn.execute("UPDATE manual_bank_entries SET gl_account_id=?, gl_source='human', gl_status='confirmed' WHERE id=?",
                         (SUBS, line))
            log(conn, "manual_bank_entries", line, r["gl_account_id"], SUBS, "7shifts software",
                "7shifts software/processing fee, not labor")
            tot += -r["amount"]
        print(f"{len(MONTHLY)} monthly 7shifts charges ({tot:,.2f}) Payroll Expenses -> Dues & Subscriptions")
        for rid in RULES:
            r = conn.execute("SELECT * FROM gl_account_rules WHERE id=?", (rid,)).fetchone()
            conn.execute("UPDATE gl_account_rules SET gl_account_id=?, created_by=? WHERE id=?", (SUBS, WHO, rid))
            log(conn, "gl_account_rules", rid, r["gl_account_id"], SUBS, r["pattern"], "7shifts software fees",
                kind="rule_remap")
            print(f"rule {r['pattern']!r} -> Dues & Subscriptions")

        me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=3115").fetchone()
        pc = conn.execute("SELECT * FROM payroll_checks WHERE id=27").fetchone()
        assert me and pc and pc["employee_name"] == "Maya Jones" and not pc["cleared"] and abs(pc["net_pay"] + me["amount"]) < 0.005
        conn.execute("UPDATE payroll_checks SET cleared=1, cleared_date=? WHERE id=27", (me["entry_date"],))
        conn.execute(
            """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
                   target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
                   match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (WHO, me["bank_account_id"], "payroll_check", 27, "Payroll: Maya Jones", me["entry_date"], 3115,
             me["entry_date"], me["amount"], json.dumps(dict(me)), pc["net_pay"], 90, None,
             f"manual ({WHO}): check 9690 image reads Maya Jones 03/20/26, pay period 03/02-03/15/26; OCR 'Leticia' was a misread"))
        conn.execute("DELETE FROM manual_bank_entries WHERE id=3115")
        print("check 9690 (6/18, 57.46) merged into paycheck #27 Maya Jones (3/20)")

        after = {u: state(conn, u) for u in uploads}
        assert all(after[u] == before[u] for u in uploads), "tie-out moved"
        print(f"bank balance and delta unchanged on all {len(uploads)} periods")
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
