#!/usr/bin/env python3
"""The last two Dennis PFG bills Chatham's bank paid (Mike approved 2026-09-24).

Found by Dennis's PFG customer number (#09848) on Chatham's statement, both
coded Food Costs -F&B there, neither with a Dennis payment on file:
  3/24  2,607.47 = invoice 699220 (2/27)
  4/07  2,264.76 = invoice 714072 (3/12; marked 'paid' with no payment behind it)
A payment is recorded for each (location dennis, Chatham's bank), cleared by
its Chatham line (register_merge_audit) and coded Loan to Red Nun Dennisport.
With these the 2026 intercompany net through 8/31 is 21,516.47 (Dennis owes
Chatham), which Mike settles by transfer.

    python scripts/pfg_dennis_paid_by_chatham_3.py            # dry run
    python scripts/pfg_dennis_paid_by_chatham_3.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-24"
LOAN_TO_DENNIS = 527
PAIRS = [(1972, "699220", 2607.47), (2062, "714072", 2264.76)]


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
        for line, inv_no, amt in PAIRS:
            me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (line,)).fetchone()
            assert me and me["bank_account_id"] == 1 and abs(me["amount"] + amt) < 0.005 and "09848" in (me["memo"] or "")
            inv = conn.execute("SELECT * FROM scanned_invoices WHERE location='dennis' AND invoice_number=? AND status='confirmed' "
                               "AND UPPER(vendor_name) LIKE '%PERFORMANCE%'", (inv_no,)).fetchone()
            assert inv and abs(inv["total"] - amt) < 0.005
            assert not conn.execute("SELECT 1 FROM vendor_payment_invoices WHERE invoice_number=?", (inv_no,)).fetchone()
            pid = conn.execute(
                """INSERT INTO vendor_payments (vendor, location, payment_date, payment_ref, payment_method, payment_total,
                       memo, status, source, bank_account_id, cleared, cleared_date, gl_account_id, gl_source, gl_status,
                       reconciliation_id, updated_at)
                   VALUES ('Performance Foodservice','dennis',?,?, 'ach', ?, ?, 'cleared', 'bank', 1, 1, ?, ?, 'human',
                           'confirmed', ?, ?)""",
                (me["entry_date"], f"PFG-09848-{me['entry_date'].replace('-', '')}", amt,
                 f"{WHO}: intercompany — Chatham 5975 paid Dennis PFG invoice {inv_no} (customer #09848); Chatham books "
                 f"Loan to Red Nun Dennisport, Dennis books Dr AP / Cr Loan to Red Buoy Inc.",
                 me["entry_date"], LOAN_TO_DENNIS, me["reconciliation_id"], now)).lastrowid
            conn.execute("INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, amount_paid) "
                         "VALUES (?,?,?,?)", (pid, inv_no, inv["invoice_date"], inv["total"]))
            conn.execute("UPDATE scanned_invoices SET payment_status='paid', paid_date=?, amount_paid=total, balance=0, "
                         "payment_reference=? WHERE id=?", (me["entry_date"], f"PFG ACH from Chatham 5975, payment {pid}", inv["id"]))
            conn.execute(
                """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
                       target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
                       match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (WHO, 1, "vendor_payment", pid, f"PFG (Dennis) {inv_no}", me["entry_date"], line, me["entry_date"],
                 me["amount"], json.dumps(dict(me)), amt, 0, 0,
                 f"manual ({WHO}): intercompany — Dennis PFG (cust #09848) paid on Chatham's statement; invoice {inv_no} exact"))
            conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (line,))
            conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,"
                         "detail) VALUES ('row_remap','vendor_payments',?,NULL,?,'intercompany settlement',?)",
                         (pid, LOAN_TO_DENNIS, f"{WHO}: Chatham line {line} (was {me['gl_account_id']} Food Costs -F&B) merged"))
            print(f"{me['entry_date']} {amt:>9,.2f} -> new payment #{pid} for Dennis invoice {inv_no}; Chatham line {line} merged")
        for u in uploads:
            b, a_ = before[u], state(conn, u)
            assert b[:2] == a_[:2], f"upload {u} bank side moved"
            if b[2] != a_[2]:
                print(f"   upload {u}: outstanding {b[2]:,.2f} -> {a_[2]:,.2f}")
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
