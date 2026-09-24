#!/usr/bin/env python3
"""Two more Dennis PFG payments Chatham's bank made (Mike, 2026-09-24).

Chatham's statement carries PFG debits against Dennis's customer #09848:
  4/22  4,577.53  = Bill Pay #216 (invoices 721961, 729650, 731144 = 4,595.45)
                    less 17.92 (no credit memo on file). As for the US Foods
                    drafts: the payment takes the amount the bank drew and the
                    difference is noted. Dennis funded it with its same-day
                    4,577.53 transfer to Chatham.
  5/13  4,814.51  = Billfire batch 260513-af76865e (confirmation email: "Payment
                    method Checking 5975"): #359 (744805, 2,401.24) + invoice
                    752301 (4/16, 2,488.26) - credit 781485 (5/12, -74.99)
                    = 2,413.27, the only combination of Dennis's uncovered PFG
                    invoices that makes it. A payment is recorded for those two.
Each is intercompany: location stays dennis, bank moves to Chatham, cleared by
its Chatham line (register_merge_audit; the batch line settles two payments),
coded Loan to Red Nun Dennisport. The Chatham lines were Food Costs -F&B.

Not in this script (awaiting Mike): 3/24 2,607.47 = invoice 699220; 4/07
2,264.76 = invoice 714072 — the same pattern, found by customer #09848.

    python scripts/pfg_dennis_paid_by_chatham_2.py            # dry run
    python scripts/pfg_dennis_paid_by_chatham_2.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-24"
LOAN_TO_DENNIS = 527
NOTE = ("intercompany — Chatham 5975 paid this Dennis PFG bill (customer #09848); Chatham books Loan to Red Nun "
        "Dennisport, Dennis books Dr AP / Cr Loan to Red Buoy Inc.")


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["outstanding_net"])


def clear(conn, vid, me, now, extra=""):
    vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
    assert vp and not vp["cleared"] and vp["location"] == "dennis"
    conn.execute("""UPDATE vendor_payments SET bank_account_id=1, cleared=1, cleared_date=?, gl_account_id=?,
                        gl_source='human', gl_status='confirmed', updated_at=?,
                        memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?""",
                 (me["entry_date"], LOAN_TO_DENNIS, now, f"{WHO}: {NOTE}{extra}", vid))
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,"
                 "detail) VALUES ('row_remap','vendor_payments',?,?,?,'intercompany settlement',?)",
                 (vid, vp["gl_account_id"], LOAN_TO_DENNIS, f"{WHO}: paired with Chatham line {me['id']}"))
    conn.execute(
        """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
               target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
               match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (WHO, 1, "vendor_payment", vid, f"PFG (Dennis) {vp['payment_ref']}", me["entry_date"], me["id"], me["entry_date"],
         me["amount"], json.dumps(dict(me)), None, None, None,
         f"manual ({WHO}): intercompany — Dennis PFG (cust #09848) paid on Chatham's statement"))
    return vp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2)")]
        before = {u: state(conn, u) for u in uploads}

        # 4/22: #216
        me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=2171").fetchone()
        assert me and me["bank_account_id"] == 1 and abs(me["amount"] + 4577.53) < 0.005 and "09848" in me["memo"]
        v = conn.execute("SELECT payment_total FROM vendor_payments WHERE id=216").fetchone()
        assert abs(v[0] - 4595.45) < 0.005
        conn.execute("UPDATE vendor_payments SET payment_total=4577.53 WHERE id=216")
        clear(conn, 216, me, now, "; bank drew 4,577.53 against invoices totalling 4,595.45 — 17.92 short, no credit memo on file")
        conn.execute("DELETE FROM manual_bank_entries WHERE id=2171")
        print("4/22  4,577.53 -> #216 (total 4,595.45 -> 4,577.53; 17.92 noted), Chatham line 2171 merged")

        # 5/13: Billfire batch = #359 + new payment for 752301 / 781485
        me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=2316").fetchone()
        assert me and me["bank_account_id"] == 1 and abs(me["amount"] + 4814.51) < 0.005 and "09848" in me["memo"]
        invs = {r["invoice_number"]: r for r in conn.execute(
            "SELECT * FROM scanned_invoices WHERE location='dennis' AND invoice_number IN ('752301','781485') "
            "AND UPPER(vendor_name) LIKE '%PERFORMANCE%' AND status='confirmed'")}
        assert set(invs) == {"752301", "781485"} and abs(sum(i["total"] for i in invs.values()) - 2413.27) < 0.005
        assert not conn.execute("SELECT 1 FROM vendor_payment_invoices WHERE invoice_number IN ('752301','781485')").fetchone()
        new = conn.execute(
            """INSERT INTO vendor_payments (vendor, location, payment_date, payment_ref, payment_method, payment_total, memo,
                   status, source, bank_account_id, cleared, updated_at)
               VALUES ('Performance Foodservice','dennis','2026-05-13','260513-af76865e-2','ach_via_billfire_statement',
                       2413.27, ?, 'cleared', 'bank', 1, 0, ?)""",
            (f"{WHO}: second half of Billfire batch 260513-af76865e (4,814.51 from Checking 5975) with #359", now)).lastrowid
        for n, i in invs.items():
            conn.execute("INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, amount_paid) "
                         "VALUES (?,?,?,?)", (new, n, i["invoice_date"], i["total"]))
            conn.execute("UPDATE scanned_invoices SET payment_status='paid', paid_date='2026-05-13', amount_paid=total, "
                         "balance=0, payment_reference='Billfire 260513-af76865e' WHERE id=?", (i["id"],))
        v = conn.execute("SELECT payment_total FROM vendor_payments WHERE id=359").fetchone()
        assert abs(v[0] + 2413.27 - 4814.51) < 0.005
        clear(conn, 359, me, now, "; Billfire batch 260513-af76865e with payment for 752301 / 781485")
        clear(conn, new, me, now, "; Billfire batch 260513-af76865e with #359")
        conn.execute("DELETE FROM manual_bank_entries WHERE id=2316")
        print(f"5/13  4,814.51 -> #359 2,401.24 + new #{new} 2,413.27 (752301 2,488.26 - credit 781485 74.99), Chatham line 2316 merged")

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
