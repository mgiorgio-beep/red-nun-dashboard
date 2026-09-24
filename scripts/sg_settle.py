#!/usr/bin/env python3
"""Settle open Southern Glazer's bank drafts from the FinTech payment statements.

Mike dropped 19 screenshots of SG portal payment statements (Drive folder
"SOuther Glazers INvoices", 2026-09-23 ~18:20). The folder names are not the
entity: each statement is keyed here on its customer id — 73403 = Red Buoy
(Chatham, ACH 5975), 400097 = Red Nun Public House (Dennis, ACH 2757). Each
statement names the invoices one FinTech reference paid.

A cloud session (confirmed_by 'claude-recon') backfilled the Dec–Mar invoices
from the same screenshots at 2026-09-23 22:34 but made no payments. Here:

  * Payments: the 2026-03-26 import rows 103–114 carry the real FinTech
    reference numbers with made-up amounts/dates/10-digit "invoices"; each is
    corrected in place from its statement and cleared by its draft. Payments
    200/276/325 (Dennis Apr–May) list the right invoices at wrong totals: the
    "missing credit memos" of 2026-09-23 were not credits — the invoices on
    file already sum to the drafts. Otherwise a payment is created.
  * Invoices: the two Nov 2025 statements behind the Dennis 1/06 and 1/13
    drafts name invoices not on file (601691, 602842 + its -60.00 credit,
    606567, credits 70135 / 70398). Created header-only, source
    'sg_statement', invoice-dated Nov 2025 — before the books, no P&L effect.
    623802 (Dennis, 1/13/2026, 513.24) was not backfilled either: it is the
    one new invoice inside the books, +513.24 Dennis January liquor cost.
  * Settlements coded Accounts Payable (the invoice is the cost).

Not settled, no statement for them: Chatham 2/10 573.99 (ref 415313953),
3/04 1,208.10 (418169082), 3/11 545.84 (419251664), 7/23 2,278.59 (#514,
invoice 685460 = 2,521.59; 243.00 short).

    python scripts/sg_settle.py            # dry run
    python scripts/sg_settle.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-24"
AP = {"dennis": 2, "chatham": 258}
VENDOR = "Southern Glazer's"

# statement line -> (payment to correct or None, FinTech ref, payment date,
#                    [(invoice number, amount, invoice date)])
PLAN = [
    # Chatham, customer 73403
    (264, 103, "410428243", "2026-01-05", [("608372", 515.70, "2025-12-02"), ("610887", 100.78, "2025-12-09"),
                                           ("70385", -16.20, "2025-11-30")]),
    (323, 104, "411606468", "2026-01-13", [("610888", 357.14, "2025-12-09"), ("613863", 69.40, "2025-12-16")]),
    (373, 105, "412334942", "2026-01-20", [("613416", 408.69, "2025-12-16")]),
    # Dennis, customer 400097
    (468, None, "410428893", "2026-01-05", [("601691", 475.99, "2025-11-13")]),
    (514, None, "411606573", "2026-01-13", [("602842", 368.82, "2025-11-18"), ("602842", -60.00, "2025-11-18"),
                                           ("606567", 357.39, "2025-11-24"), ("70135", -5.07, "2025-11-30"),
                                           ("70398", -8.10, "2025-11-30")]),
    (556, 109, "412334963", "2026-01-20", [("623802", 513.24, "2026-01-13")]),
    (607, 110, "413573927", "2026-01-29", [("608404", 64.39, "2025-12-02"), ("608405", 285.00, "2025-12-02")]),
    (820, 111, "414186851", "2026-02-02", [("611215", 708.95, "2025-12-09")]),
    (864, 112, "415314843", "2026-02-09", [("613446", 25.20, "2025-12-16"), ("613447", 544.00, "2025-12-16"),
                                           ("613957", 69.40, "2025-12-16")]),
    (901, 113, "416184521", "2026-02-17", [("617231", 376.06, "2025-12-22")]),
    (993, 114, "418169032", "2026-03-03", [("619239", 517.70, "2025-12-29"), ("621792", 484.08, "2026-01-06"),
                                           ("74065", -8.10, "2025-12-31"), ("625837", 512.43, "2026-01-20")]),
    (1037, None, "419251686", "2026-03-10", [("627964", 329.57, "2026-01-27")]),
    (1268, 200, "424139668", "2026-04-15", [("637058", 25.19, "2026-02-25"), ("637059", 465.92, "2026-02-25")]),
    (1465, 276, "427545314", "2026-05-11", [("645368", 723.96, "2026-03-17")]),
    (1556, 325, "429066864", "2026-05-26", [("650698", 566.15, "2026-03-31"), ("653270", 554.50, "2026-04-07"),
                                           ("656730", 673.65, "2026-04-16")]),
]


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["identity_holds"])


def find_or_create_invoice(conn, now, loc, num, amt, d, ref, created):
    rows = conn.execute("SELECT * FROM scanned_invoices WHERE location=? AND invoice_number=? AND status='confirmed' "
                        "AND UPPER(vendor_name) LIKE '%GLAZER%' AND ABS(total-?)<0.005", (loc, num, amt)).fetchall()
    assert len(rows) <= 1, f"invoice {num} {amt} ({loc}): {len(rows)} rows"
    if rows:
        return rows[0]
    note = (f"{'Credit' if amt < 0 else 'Invoice'} — header only, from SG FinTech payment statement {ref}. "
            f"No line items on file.")
    iid = conn.execute(
        """INSERT INTO scanned_invoices (location, vendor_name, invoice_number, invoice_date, subtotal, tax, total, category,
               status, notes, source, confirmed_by, confirmed_at, payment_status, auto_confirmed, amount_paid, balance,
               confidence_score, is_low_confidence)
           VALUES (?,?,?,?,?,0,?,'LIQUOR_WINE_BEER','confirmed',?,'sg_statement',?,?,'unpaid',0,0,?,100,0)""",
        (loc, "Southern Glazer's Beverage Company", num, d, amt, amt, note, WHO, now, amt)).lastrowid
    conn.execute(
        """INSERT INTO scanned_invoice_items (invoice_id, product_name, description, quantity, unit, unit_price, total_price,
               category_type) VALUES (?, 'LIQUOR_WINE_BEER', 'Category-level entry (header-only import)', 1.0, 'category', ?, ?,
               'LIQUOR_WINE_BEER')""", (iid, amt, amt))
    created.append((loc, num, d, amt))
    return conn.execute("SELECT * FROM scanned_invoices WHERE id=?", (iid,)).fetchone()


def settle(conn, now, line, pid, ref, paid, spec, created):
    me = conn.execute(
        "SELECT m.*, b.location loc FROM manual_bank_entries m JOIN bank_accounts b ON b.id=m.bank_account_id WHERE m.id=?",
        (line,)).fetchone()
    assert me, f"line {line} not found (already merged?)"
    loc, amt, day = me["loc"], round(abs(me["amount"]), 2), me["entry_date"]
    assert abs(sum(a for _, a, _ in spec) - amt) < 0.005, f"line {line}: statement {sum(a for _, a, _ in spec)} != draft {amt}"
    invs = [find_or_create_invoice(conn, now, loc, n, a, d, ref, created) for n, a, d in spec]
    changed, note = [], f"{WHO}: FinTech payment statement {ref}; settled by the bank draft of {day}"
    if pid:
        vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (pid,)).fetchone()
        assert vp and not vp["cleared"] and vp["location"] == loc and "GLAZER" in vp["vendor"].upper()
        old = sorted(r[0] for r in conn.execute("SELECT invoice_number FROM vendor_payment_invoices WHERE payment_id=?", (pid,)))
        if abs((vp["payment_total"] or 0) - amt) >= 0.005:
            changed.append(f"total {vp['payment_total']} -> {amt}")
        if vp["payment_date"] != paid:
            changed.append(f"date {vp['payment_date']} -> {paid}")
        if vp["status"] == "void":
            changed.append("un-voided")
        if old != sorted(i["invoice_number"] for i in invs):
            changed.append(f"invoices {','.join(old)} -> real")
            note += f"; invoice links corrected (were {','.join(old)})"
        conn.execute("DELETE FROM vendor_payment_invoices WHERE payment_id=?", (pid,))
        conn.execute(
            """UPDATE vendor_payments SET status='cleared', payment_total=?, payment_date=?, payment_ref=?, bank_account_id=?,
                   cleared=1, cleared_date=?, gl_account_id=?, gl_source='human', gl_status='confirmed',
                   reconciliation_id=COALESCE(reconciliation_id, ?), updated_at=?,
                   memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?""",
            (amt, paid, ref, me["bank_account_id"], day, AP[loc], me["reconciliation_id"], now, note, pid))
    else:
        pid = conn.execute(
            """INSERT INTO vendor_payments (vendor, location, payment_date, payment_ref, payment_method, payment_total, memo,
                   status, source, bank_account_id, cleared, cleared_date, gl_account_id, gl_source, gl_status,
                   reconciliation_id, updated_at)
               VALUES (?,?,?,?,'ACH',?,?,'cleared','bank',?,1,?,?,'human','confirmed',?,?)""",
            (VENDOR, loc, paid, ref, amt, note, me["bank_account_id"], day, AP[loc], me["reconciliation_id"], now)).lastrowid
        changed.append("new payment")
    for i in invs:
        conn.execute("INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, amount_paid) VALUES (?,?,?,?)",
                     (pid, i["invoice_number"], i["invoice_date"], i["total"]))
        conn.execute("UPDATE scanned_invoices SET payment_status='paid', paid_date=COALESCE(paid_date, ?), amount_paid=total, "
                     "balance=0, payment_reference=COALESCE(payment_reference, ?) WHERE id=?", (day, ref, i["id"]))
    conn.execute(
        """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label, target_cleared_date,
               deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json, match_amount, match_date_diff_days,
               match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (WHO, me["bank_account_id"], "vendor_payment", pid, f"{VENDOR} {ref}", day, me["id"], day, me["amount"],
         json.dumps({k: me[k] for k in me.keys() if k != "loc"}), amt, 0, 0,
         f"manual ({WHO}): SG FinTech payment statement {ref} names the invoices"))
    conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (me["id"],))
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,detail) "
                 "VALUES ('row_remap','vendor_payments',?,NULL,?,'settlement',?)",
                 (pid, AP[loc], f"{WHO}: SG draft {day} {amt:.2f} -> Accounts Payable"))
    return f"line {line:>4} {loc[:3]} {day} {amt:>8.2f} -> vp#{pid:<4} {'; '.join(changed) or 'as recorded'}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2) ORDER BY id")]
        before = {u: state(conn, u) for u in uploads}
        created = []
        for line, pid, ref, paid, spec in PLAN:
            print(settle(conn, now, line, pid, ref, paid, spec, created))
        for loc, num, d, amt in created:
            print(f"invoice created: {loc} {num} {d} {amt:.2f}")
        moved = False
        for u in uploads:
            after = state(conn, u)
            if before[u] != after:
                print(f"upload {u}: bank {before[u][0]} -> {after[0]}, delta {before[u][1]} -> {after[1]}, "
                      f"identity {before[u][2]} -> {after[2]}")
            if before[u][:2] != after[:2] or (before[u][2] and not after[2]):
                moved = True
        assert not moved, "tie-out moved"
        print(f"tie-out unchanged on all {len(uploads)} periods")
        left = conn.execute("SELECT COUNT(*), ROUND(SUM(amount),2) FROM manual_bank_entries WHERE UPPER(payee) LIKE '%GLAZER%' "
                            "OR UPPER(payee) LIKE '%SOUTHERN%'").fetchone()
        print("SG statement lines left:", tuple(left))
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
