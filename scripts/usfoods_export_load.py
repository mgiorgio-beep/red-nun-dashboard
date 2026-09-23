#!/usr/bin/env python3
"""Load US Foods invoices from the portal's "All Invoices" export and settle
the Dennis bank drafts that had nothing behind them.

Mike, 2026-09-23. The four Dennis "VENDOR PAY US FOODSERVICE" statement
lines (1/06, 1/30, 2/03, 3/31) matched no recorded payment. The export names
the invoices:

    1/06  2,739.96 = invoice 423061 (12/22/2025)
    1/30  2,693.86 = 1096161 (2,661.36) + 1142038 (32.50): payments 11 + 3,
                     both voided, 11 filed under chatham by mistake
    2/03  3,431.14 = invoice 1357437 (1/19)
    3/31  2,880.95 = invoice 225348 (3/09); its payment (#135) is gone but
                     the invoice link to 135 survived

Invoices the dashboard does not have are created HEADER ONLY (one
category-level FOOD item, like the manual credit memos already on file),
source 'usfoods_export', confirmed on Mike's word. Credits become negative
invoices under the invoice's own number, the convention the payment links
already use. A credit whose invoice is on file with the credit netted in
(Chatham 1453370) is skipped.

Settlements are coded to Accounts Payable: accrual books, the invoice is the
cost. Every merge writes register_merge_audit with the statement line
captured in full. Tie-out on the touched periods is asserted unchanged.

    python scripts/usfoods_export_load.py --dir <folder with the .xlsx>            # dry run
    python scripts/usfoods_export_load.py --dir <folder with the .xlsx> --apply
"""
import argparse, glob, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openpyxl  # noqa: E402
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-23"
AP = {"dennis": 2, "chatham": 258}
DENNIS_BANK = 2
# invoice number -> bank draft date, for the four lines this script settles
BANK_DATE = {"423061": "2026-01-06", "1357437": "2026-02-03", "225348": "2026-03-31",
             "1096161": "2026-01-30", "1142038": "2026-01-30"}


def iso(d):
    return datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["identity_holds"])


def load_invoices(conn, folder, now):
    created = []
    paid_by = {r["invoice_number"]: r["payment_id"]
               for r in conn.execute("SELECT invoice_number, payment_id FROM vendor_payment_invoices")}
    pay = {r["id"]: r for r in conn.execute("SELECT id, payment_date, cleared_date, payment_ref FROM vendor_payments")}
    for f in sorted(glob.glob(os.path.join(folder, "All Invoices*.xlsx"))):
        loc = "dennis" if "dennis" in os.path.basename(f).lower() else "chatham"
        ws = openpyxl.load_workbook(f, read_only=True, data_only=True).worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        hdr = rows[0]
        for r in [dict(zip(hdr, x)) for x in rows[1:] if x[0]]:
            num = str(r["Primary Transaction Number"]).lstrip("0")
            amt = round(float(r["Amount"]), 2)
            typ = r["Primary Transaction Type"]
            d = iso(r["Date Issued"])
            due = iso(r["Due Date"]) if r["Due Date"] else None
            existing = conn.execute(
                "SELECT id, total, source FROM scanned_invoices WHERE location=? AND invoice_number=? "
                "AND UPPER(vendor_name) LIKE '%US FOOD%'", (loc, num)).fetchall()
            # US Foods files a credit under its invoice's number, and the
            # export lists newest first, so the credit can arrive before the
            # invoice. Skip a row only when the same amount is on file, or when
            # a copy from another source is on file (it may carry the credit
            # netted in: Chatham 1453370 is 2,483.62 = 2,513.07 - 29.45).
            if any(abs(e["total"] - amt) < 0.005 for e in existing):
                continue
            if any(e["source"] != "usfoods_export" for e in existing):
                continue
            pid = paid_by.get(num)
            p = pay.get(pid) if pid else None
            pdate = BANK_DATE.get(num) or ((p["cleared_date"] or p["payment_date"]) if p else None)
            order = r.get("Order Number")
            note = (f"{typ} — header only, from the US Foods account export 'All Invoices' "
                    f"12/01/2025–03/31/2026{' (order ' + str(order) + ')' if order else ''}. No line items on file.")
            cur = conn.execute(
                """INSERT INTO scanned_invoices (location, vendor_name, invoice_number, invoice_date, subtotal, tax,
                       total, category, status, notes, source, confirmed_by, confirmed_at, payment_status, paid_date,
                       payment_method, payment_reference, auto_confirmed, amount_paid, balance, due_date,
                       confidence_score, is_low_confidence)
                   VALUES (?,?,?,?,?,0,?,'FOOD','confirmed',?,'usfoods_export',?,?,?,?,?,?,0,?,0,?,100,0)""",
                (loc, "US Foods", num, d, amt, amt, note, WHO, now, "paid" if pdate else "unpaid", pdate,
                 "ACH" if pdate else None, p["payment_ref"] if p else None, amt if pdate else 0, due))
            conn.execute(
                """INSERT INTO scanned_invoice_items (invoice_id, product_name, description, quantity, unit,
                       unit_price, total_price, category_type)
                   VALUES (?, 'FOOD', 'Category-level entry (header-only import)', 1.0, 'category', ?, ?, 'FOOD')""",
                (cur.lastrowid, amt, amt))
            created.append((loc, num, d, amt, typ))
    return created


def new_payment(conn, now, pid, date, total, invs, ref):
    cols = ("vendor, location, payment_date, payment_ref, payment_method, payment_total, memo, status, source, "
            "bank_account_id, cleared, cleared_date, gl_account_id, gl_source, gl_status, updated_at")
    vals = ("US Foods", "dennis", date, ref, "ACH", total,
            f"{WHO}: recorded from the bank draft; invoices from the US Foods account export",
            "cleared", "bank", DENNIS_BANK, 1, date, AP["dennis"], "human", "confirmed", now)
    if pid:
        conn.execute(f"INSERT INTO vendor_payments (id, {cols}) VALUES (?,{','.join('?' * 16)})", (pid, *vals))
        new_id = pid
    else:
        new_id = conn.execute(f"INSERT INTO vendor_payments ({cols}) VALUES ({','.join('?' * 16)})", vals).lastrowid
    for num, d, a in invs:
        if not conn.execute("SELECT 1 FROM vendor_payment_invoices WHERE payment_id=? AND invoice_number=?",
                            (new_id, num)).fetchone():
            conn.execute("INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, amount_paid) "
                         "VALUES (?,?,?,?)", (new_id, num, d, a))
    return new_id


def merge(conn, me_id, targets):
    me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (me_id,)).fetchone()
    assert me, f"statement line {me_id} already gone"
    tot = sum(conn.execute("SELECT payment_total FROM vendor_payments WHERE id=?", (t,)).fetchone()[0] for t in targets)
    assert abs(tot - abs(me["amount"])) < 0.005, (me_id, tot, me["amount"])
    for t in targets:
        vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (t,)).fetchone()
        conn.execute("UPDATE vendor_payments SET reconciliation_id=COALESCE(reconciliation_id, ?) WHERE id=?",
                     (me["reconciliation_id"], t))
        conn.execute(
            """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
                   target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
                   match_amount, match_date_diff_days, match_tolerance_days, match_rule)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (WHO, me["bank_account_id"], "vendor_payment", t, f"US Foods {vp['payment_ref']}", me["entry_date"],
             me_id, me["entry_date"], me["amount"], json.dumps(dict(me)), vp["payment_total"], 0, 0,
             f"manual (Mike 2026-09-23): US Foods account export identified the invoices; "
             f"{'many-to-one ' if len(targets) > 1 else ''}{tot:.2f}"))
    conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (me_id,))
    return me


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--invoices-only", action="store_true",
                    help="load invoices; skip the payments and merges (already applied)")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        before = {u: state(conn, u) for u in (6, 7, 8)}
        created = load_invoices(conn, a.dir, now)
        print(f"invoices to create: {len(created)}")
        by_month = {}
        for loc, num, d, amt, typ in created:
            by_month[(loc, d[:7])] = round(by_month.get((loc, d[:7]), 0) + amt, 2)
        for k in sorted(by_month):
            print(f"   {k[0]:<8} {k[1]}  {by_month[k]:>10.2f}")

        if a.invoices_only:
            if a.apply:
                conn.commit(); print("APPLIED (invoices only)")
            else:
                conn.rollback(); print("DRY RUN — nothing written")
            return
        pA = new_payment(conn, now, None, "2026-01-06", 2739.96, [("423061", "2025-12-22", 2739.96)], "CTX-20260106-2739.96")
        pC = new_payment(conn, now, None, "2026-02-03", 3431.14, [("1357437", "2026-01-19", 3431.14)], "CTX-20260203-3431.14")
        pD = new_payment(conn, now, 135, "2026-03-31", 2880.95, [("225348", "2026-03-09", 2880.95)], "CTX-20260331-2880.95")
        for pid in (3, 11):
            conn.execute(
                """UPDATE vendor_payments SET status='cleared', location='dennis', bank_account_id=?, cleared=1,
                       cleared_date='2026-01-30', gl_account_id=?, gl_source='human', gl_status='confirmed', updated_at=?,
                       memo = TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?""",
                (DENNIS_BANK, AP["dennis"], now,
                 f"{WHO}: un-voided; one bank draft of 2693.86 on 2026-01-30 covered payments 3 + 11 "
                 f"(11 was filed under chatham; invoice 1096161 is Dennis)", pid))
        for me_id, targets in ((471, [pA]), (611, [3, 11]), (822, [pC]), (1177, [pD])):
            me = merge(conn, me_id, targets)
            print(f"merge statement line {me_id} ({me['entry_date']} {me['amount']}) -> payments {targets}")
        for num, d in BANK_DATE.items():
            conn.execute("UPDATE scanned_invoices SET payment_status='paid', paid_date=COALESCE(paid_date, ?), "
                         "amount_paid=total, balance=0 WHERE location='dennis' AND invoice_number=? "
                         "AND UPPER(vendor_name) LIKE '%US FOOD%'", (d, num))
        for t in (pA, pC, pD, 3, 11):
            conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,"
                         "match_rule,detail) VALUES ('row_remap','vendor_payments',?,NULL,?,'US Foods settlement',?)",
                         (t, AP["dennis"], "Mike 2026-09-23: bank draft settles US Foods invoices -> Accounts Payable"))
        after = {u: state(conn, u) for u in (6, 7, 8)}
        for u in (6, 7, 8):
            print(f"upload {u}: bank {before[u][0]} -> {after[u][0]}, delta {before[u][1]} -> {after[u][1]}, identity {after[u][2]}")
            assert before[u][:2] == after[u][:2], "tie-out moved"
        left = conn.execute("SELECT COUNT(*) FROM manual_bank_entries WHERE UPPER(payee) LIKE '%US FOOD%'").fetchone()[0]
        print("US Foods statement lines left:", left)
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
