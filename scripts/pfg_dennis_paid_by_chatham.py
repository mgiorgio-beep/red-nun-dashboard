#!/usr/bin/env python3
"""Dennis PFG payments that Chatham's bank paid (Mike, 2026-09-24).

Dennis March "outstanding" held six PFG Bill Pay payments (vendor_payments
30-34, 139; 14,187.92). None was outstanding: each cleared the same day on
CHATHAM's statement as "AR PAYMENT PERFORMANCEBOS" (manual_bank_entries 1656,
1719, 1777, 1844, 1888, 2014), where the lines sat coded Food Costs -F&B on
top of the Dennis PFG invoices. The dedupe never paired them because it
matches within one bank account. Same intercompany pattern as PFG #27-29
(January) and the Apr-May Martignetti drafts.

Fix, as for Martignetti: each payment keeps location='dennis' (the cost is
Dennis's) and moves to the Chatham bank account (where the cash left),
cleared by its Chatham line (register_merge_audit row), coded on Chatham's
chart to Loan to Red Nun Dennisport. #27-29 already cleared on Chatham but
were never coded: same account. The Dennis side (Dr Accounts Payable /
Cr Loan to Red Buoy Inc.) is the QBO intercompany JE.

    python scripts/pfg_dennis_paid_by_chatham.py            # dry run
    python scripts/pfg_dennis_paid_by_chatham.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-24"
LOAN_TO_DENNIS = 527
PAIRS = [(30, 1656), (31, 1719), (32, 1777), (33, 1844), (34, 1888), (139, 2014)]
JANUARY = [27, 28, 29]


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["outstanding_net"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2)")]
        before = {u: state(conn, u) for u in uploads}
        note = (f"{WHO}: intercompany — Chatham 5975 paid this Dennis PFG payment; Chatham books Loan to Red Nun "
                f"Dennisport, Dennis books Dr Accounts Payable / Cr Loan to Red Buoy Inc. (QBO JE)")
        total = 0.0
        for vid, line in PAIRS:
            vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
            me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (line,)).fetchone()
            assert vp and me and vp["location"] == "dennis" and not vp["cleared"] and me["bank_account_id"] == 1
            assert abs(vp["payment_total"] + me["amount"]) < 0.005 and "PERFORMANCE" in me["payee"].upper()
            conn.execute("""UPDATE vendor_payments SET bank_account_id=1, cleared=1, cleared_date=?, gl_account_id=?,
                                gl_source='human', gl_status='confirmed', updated_at=?,
                                memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?""",
                         (me["entry_date"], LOAN_TO_DENNIS, now, note, vid))
            conn.execute(
                """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
                       target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
                       match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (WHO, 1, "vendor_payment", vid, f"PFG (Dennis) {vp['payment_ref']}", me["entry_date"], line,
                 me["entry_date"], me["amount"], json.dumps(dict(me)), vp["payment_total"], 0, 0,
                 f"manual ({WHO}): intercompany — Dennis PFG payment cleared on Chatham's statement, same amount and date"))
            conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (line,))
            conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,"
                         "match_rule,detail) VALUES ('row_remap','vendor_payments',?,?,?,'intercompany settlement',?)",
                         (vid, vp["gl_account_id"], LOAN_TO_DENNIS, f"{WHO}: Chatham line {line} (was Food Costs -F&B) merged"))
            total += vp["payment_total"]
            print(f"vp#{vid:<4} {vp['payment_date']} {vp['payment_total']:>9,.2f} -> Chatham, cleared {me['entry_date']} by line {line}")
        for vid in JANUARY:
            vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
            assert vp["bank_account_id"] == 1 and vp["cleared"] and vp["location"] == "dennis" and not vp["gl_account_id"]
            conn.execute("UPDATE vendor_payments SET gl_account_id=?, gl_source='human', gl_status='confirmed', updated_at=?, "
                         "memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?", (LOAN_TO_DENNIS, now, note, vid))
            conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,"
                         "match_rule,detail) VALUES ('row_remap','vendor_payments',?,NULL,?,'intercompany settlement',?)",
                         (vid, LOAN_TO_DENNIS, f"{WHO}: January Dennis PFG paid by Chatham, was uncoded"))
            total += vp["payment_total"]
            print(f"vp#{vid:<4} {vp['payment_date']} {vp['payment_total']:>9,.2f} already cleared on Chatham -> Loan to Red Nun Dennisport")
        print(f"PFG intercompany (Dennis owes Chatham): {total:,.2f}")
        for u in uploads:
            b, a_ = before[u], state(conn, u)
            assert b[:2] == a_[:2], f"upload {u} bank side moved"
            if b[2] != a_[2]:
                print(f"upload {u}: outstanding {b[2]:,.2f} -> {a_[2]:,.2f} (bank balance, delta unchanged)")
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
