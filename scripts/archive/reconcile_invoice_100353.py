#!/usr/bin/env python3
"""
Reconcile invoice 100353 (Dennis PFG #744805, $2,401.24) — record the actual
historical payment that happened in Billfire on 5/13/2026 but never made it
into our DB.

Background:
  - Invoice was created at PFG on 4/9/2026 (data feed → scanned_invoices).
  - Mike scheduled payment via Billfire Statement on 5/12/2026.
  - Billfire marked it Closed on 5/13/2026 (Confirmation 260513-af76865e,
    $4,814.51 batch total).
  - Our PFG scraper never picked up the paid status. The receipt poller's
    bogus auto-apply this morning was reversed (ap_payment #87 deleted).
  - Now we need to insert the CORRECT historical payment record.

Wrinkle: the actual Billfire payment used Checking 5975 (Chatham bank), not
Dennis 2757. That makes this another Dennis-owes-Chatham intercompany entry
($2,401.24 to add to the running balance — see project memory).

Pass --apply to write. Without it, runs as a preview.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/reconcile_invoice_100353.py            # preview
        && python scripts/archive/reconcile_invoice_100353.py --apply    # commit
"""
import sys

sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection

INVOICE_ID = 100353
PAYMENT_AMOUNT = 2401.24
PAYMENT_DATE = "2026-05-13"
PAYMENT_REF = "260513-af76865e"
PAYMENT_METHOD = "ach_via_billfire_statement"
MEMO = (
    "Reconciled from Billfire — paid 2026-05-13 as part of confirmation "
    "260513-af76865e ($4,814.51 batch). "
    "WRONG BANK: paid from Chatham 5975 instead of Dennis 2757. "
    "Add $2,401.24 to Dennis-owes-Chatham intercompany balance."
)


def main(apply: bool):
    conn = get_connection()
    cur = conn.cursor()

    print("=" * 72)
    print(f"Reconciling invoice #{INVOICE_ID}  (mode: {'APPLY' if apply else 'PREVIEW'})")
    print("=" * 72)

    inv = cur.execute(
        "SELECT id, vendor_name, invoice_number, invoice_date, total, "
        "COALESCE(balance, total) AS balance, payment_status, location, due_date "
        "FROM scanned_invoices WHERE id = ?",
        (INVOICE_ID,),
    ).fetchone()
    if not inv:
        print(f"Invoice {INVOICE_ID} not found.")
        conn.close()
        return
    inv = dict(inv)

    print("\nCurrent invoice state:")
    for k in ("id", "vendor_name", "invoice_number", "invoice_date",
              "total", "balance", "payment_status", "location"):
        print(f"  {k:18s} = {inv.get(k)!r}")

    if inv["payment_status"] == "paid":
        print("\nAlready paid — refusing to double-apply. Reverse first if needed.")
        conn.close()
        return

    print(f"\nWill insert ap_payment:")
    print(f"  vendor              = {inv['vendor_name']!r}")
    print(f"  payment_date        = {PAYMENT_DATE!r}")
    print(f"  amount              = {PAYMENT_AMOUNT}")
    print(f"  payment_method      = {PAYMENT_METHOD!r}")
    print(f"  reference_number    = {PAYMENT_REF!r}")
    print(f"  memo                = {MEMO!r}")
    print(f"  status              = 'cleared'")

    print(f"\nWill mark invoice #{INVOICE_ID} paid:")
    print(f"  payment_status      = 'paid'")
    print(f"  balance             = 0")
    print(f"  amount_paid         = {inv['total']}")
    print(f"  paid_date           = {PAYMENT_DATE}")
    print(f"  payment_reference   = {PAYMENT_REF}")

    if not apply:
        print()
        print("PREVIEW MODE — no changes written. Re-run with --apply to commit.")
        conn.close()
        return

    # ── Write ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("APPLYING")
    print("=" * 72)

    cur.execute(
        """INSERT INTO ap_payments
           (vendor_name, payment_date, amount, payment_method,
            reference_number, memo, status)
           VALUES (?, ?, ?, ?, ?, ?, 'cleared')""",
        (inv["vendor_name"], PAYMENT_DATE, PAYMENT_AMOUNT, PAYMENT_METHOD,
         PAYMENT_REF, MEMO),
    )
    payment_id = cur.lastrowid
    print(f"  ap_payments id={payment_id}")

    cur.execute(
        """INSERT INTO ap_payment_invoices (payment_id, invoice_id, amount_applied)
           VALUES (?, ?, ?)""",
        (payment_id, INVOICE_ID, PAYMENT_AMOUNT),
    )
    print(f"  ap_payment_invoices linked (rowcount={cur.rowcount})")

    cur.execute(
        """UPDATE scanned_invoices
              SET amount_paid = COALESCE(total, 0),
                  balance = 0,
                  payment_status = 'paid',
                  paid_date = ?,
                  payment_reference = ?,
                  notes = COALESCE(notes, '') || ' | reconciled-from-billfire 2026-05-28: ' || ?
            WHERE id = ?""",
        (PAYMENT_DATE, PAYMENT_REF, MEMO, INVOICE_ID),
    )
    print(f"  invoice #{INVOICE_ID} marked paid (rowcount={cur.rowcount})")

    # Mirror to vendor_payments for the Payments page
    cur.execute(
        """INSERT INTO vendor_payments
           (vendor, location, payment_date, payment_ref, payment_method,
            payment_total, memo, status, source, ap_payment_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'cleared', 'manual_reconcile', ?)""",
        (inv["vendor_name"], inv["location"], PAYMENT_DATE, PAYMENT_REF,
         PAYMENT_METHOD, PAYMENT_AMOUNT, MEMO, payment_id),
    )
    vp_id = cur.lastrowid
    print(f"  vendor_payments mirror id={vp_id}")

    cur.execute(
        """INSERT INTO vendor_payment_invoices
           (payment_id, invoice_number, invoice_date, due_date, amount_paid)
           VALUES (?, ?, ?, ?, ?)""",
        (vp_id, inv["invoice_number"], inv["invoice_date"],
         inv.get("due_date"), PAYMENT_AMOUNT),
    )
    print(f"  vendor_payment_invoices linked (rowcount={cur.rowcount})")

    conn.commit()
    conn.close()
    print("\nReconciliation complete.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
