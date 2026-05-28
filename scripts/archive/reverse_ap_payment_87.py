#!/usr/bin/env python3
"""
Reverse the incorrectly-auto-applied ap_payment #87 from 2026-05-28.

Background: the receipt poller fuzzy-matched a $2,381.42 Chatham PFG/Billfire
payment confirmation against Dennis invoice id=100353 (#744805, $2,401.24).
That match was wrong on three counts: (a) wrong location, (b) wrong invoice
number, (c) wrong amount. The Billfire portal shows the actual $2,381.42 was
applied to four Chatham invoices: 766686, 769921, 770588, 775130.

This script:
  1. Prints the current state of ap_payment #87 and invoice 100353
  2. Deletes ap_payment_invoices link, vendor_payment_invoices link,
     vendor_payments mirror, ap_payments row
  3. Restores invoice 100353 to unpaid
  4. Checks whether the four real Chatham invoices exist in scanned_invoices

Pass --apply to actually write. Without it, runs as a preview.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/reverse_ap_payment_87.py            # preview
        && python scripts/archive/reverse_ap_payment_87.py --apply    # commit
"""
import sys

sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection

PAYMENT_ID = 87
INVOICE_ID = 100353
CHATHAM_INVOICE_NUMBERS = ["766686", "769921", "770588", "775130"]
EXPECTED_AMOUNTS = {
    "766686": 857.02,
    "769921": 289.29,
    "770588": 121.99,
    "775130": 1113.12,
}


def fetchone_dict(cur, sql, params=()):
    row = cur.execute(sql, params).fetchone()
    return dict(row) if row else None


def fetchall_dicts(cur, sql, params=()):
    return [dict(r) for r in cur.execute(sql, params).fetchall()]


def main(apply: bool):
    conn = get_connection()
    cur = conn.cursor()

    print("=" * 72)
    print(f"Reversing ap_payment #{PAYMENT_ID}  (mode: {'APPLY' if apply else 'PREVIEW'})")
    print("=" * 72)

    # Current state
    pay = fetchone_dict(cur, "SELECT * FROM ap_payments WHERE id = ?", (PAYMENT_ID,))
    print("\nap_payment row:")
    if pay:
        for k in ("id", "vendor_name", "payment_date", "amount", "payment_method",
                  "reference_number", "memo", "status"):
            print(f"  {k:18s} = {pay.get(k)!r}")
    else:
        print(f"  (no ap_payments row with id={PAYMENT_ID} — nothing to reverse)")
        conn.close()
        return

    links = fetchall_dicts(cur,
        "SELECT * FROM ap_payment_invoices WHERE payment_id = ?", (PAYMENT_ID,))
    print(f"\nap_payment_invoices links: {len(links)}")
    for l in links:
        print(f"  invoice_id={l.get('invoice_id')}  amount_applied={l.get('amount_applied')}")

    vp_rows = fetchall_dicts(cur,
        "SELECT * FROM vendor_payments WHERE ap_payment_id = ?", (PAYMENT_ID,))
    print(f"\nvendor_payments mirror: {len(vp_rows)} row(s)")
    for vp in vp_rows:
        print(f"  vp_id={vp.get('id')} vendor={vp.get('vendor')} "
              f"location={vp.get('location')} total={vp.get('payment_total')}")

    inv = fetchone_dict(cur,
        "SELECT id, vendor_name, invoice_number, total, balance, payment_status, "
        "paid_date, payment_reference, notes FROM scanned_invoices WHERE id = ?",
        (INVOICE_ID,))
    print(f"\ninvoice #{INVOICE_ID} (target of incorrect apply):")
    for k, v in (inv or {}).items():
        print(f"  {k:18s} = {v!r}")

    # The four Chatham invoices the Billfire portal says should have been paid
    print("\n" + "-" * 72)
    print("Sanity check — do the 4 real Chatham invoices exist in scanned_invoices?")
    print("-" * 72)
    found_chatham = {}
    for inv_no in CHATHAM_INVOICE_NUMBERS:
        rows = fetchall_dicts(cur,
            "SELECT id, vendor_name, invoice_number, invoice_date, total, "
            "COALESCE(balance, total) AS balance, payment_status, location "
            "FROM scanned_invoices WHERE invoice_number = ?", (inv_no,))
        if not rows:
            print(f"  #{inv_no} (expected ${EXPECTED_AMOUNTS[inv_no]:.2f}): NOT in scanned_invoices")
        else:
            for r in rows:
                print(f"  #{inv_no}: id={r['id']} {r['vendor_name']!r} "
                      f"date={r['invoice_date']} total=${r['total']} "
                      f"balance=${r['balance']} status={r['payment_status']} "
                      f"loc={r['location']}")
                found_chatham[inv_no] = r

    if not apply:
        print()
        print("PREVIEW MODE — no changes written. Re-run with --apply to commit the reversal.")
        conn.close()
        return

    # ── Apply the reversal ────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("APPLYING REVERSAL")
    print("=" * 72)

    # 1. Delete vendor_payment_invoices linked to the vendor_payments mirror(s)
    for vp in vp_rows:
        vp_id = vp.get("id")
        cur.execute("DELETE FROM vendor_payment_invoices WHERE payment_id = ?", (vp_id,))
        print(f"  Deleted vendor_payment_invoices for vp_id={vp_id} "
              f"(rowcount={cur.rowcount})")

    # 2. Delete vendor_payments mirror
    cur.execute("DELETE FROM vendor_payments WHERE ap_payment_id = ?", (PAYMENT_ID,))
    print(f"  Deleted vendor_payments rows (rowcount={cur.rowcount})")

    # 3. Delete ap_payment_invoices link
    cur.execute("DELETE FROM ap_payment_invoices WHERE payment_id = ?", (PAYMENT_ID,))
    print(f"  Deleted ap_payment_invoices links (rowcount={cur.rowcount})")

    # 4. Delete ap_payments row
    cur.execute("DELETE FROM ap_payments WHERE id = ?", (PAYMENT_ID,))
    print(f"  Deleted ap_payments row (rowcount={cur.rowcount})")

    # 5. Restore invoice 100353 to unpaid
    cur.execute(
        """UPDATE scanned_invoices
              SET payment_status = 'unpaid',
                  balance = total,
                  amount_paid = 0,
                  paid_date = NULL,
                  payment_reference = NULL,
                  notes = REPLACE(COALESCE(notes, ''), ' | auto-receipt 2026-05-28: Auto-applied from pfg_billfire receipt', '')
            WHERE id = ?""",
        (INVOICE_ID,))
    print(f"  Restored invoice #{INVOICE_ID} to unpaid (rowcount={cur.rowcount})")

    conn.commit()
    conn.close()
    print("\nReversal complete.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    main(apply)
