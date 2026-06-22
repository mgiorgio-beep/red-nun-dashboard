#!/usr/bin/env python3
"""
One-off reconciliation: mark the Cintas invoices already paid by myCintas weekly
ACH autopay as paid in the dashboard.

Background: Cintas autopays every invoice weekly (Saturday). The receipt poller
didn't recognize Cintas until 2026-06-22, and the confirmation emails go to
mike@rednun.com (not dashboard@), so 13 already-paid invoices sat as 'unpaid'.

Reconciled from the "myCintas Autopay Confirmation" emails:
  - invoices dated <= 2026-05-28  -> paid by the 2026-05-30 autopay (backlog catch-up)
  - invoices dated    2026-06-03  -> paid by the 2026-06-06 autopay (amounts matched exactly)
  - invoices dated    2026-06-10  -> paid by the 2026-06-13 autopay (amounts matched exactly)

Each invoice is recorded as a real payment via the poller's apply_payment(), so
ap_payments / ap_payment_invoices / vendor_payments all get written, not just a
status flip.

Usage (on the Beelink):
    venv/bin/python3 scripts/archive/reconcile_cintas_autopay.py            # dry run
    venv/bin/python3 scripts/archive/reconcile_cintas_autopay.py --commit   # apply
"""
import sys
sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection
from integrations.invoices.watchers.email_receipt_poller import apply_payment

# invoice_number -> paid_date (the autopay cycle that covered it)
PAID = {
    # Chatham (payer 0025003893)
    "4268394000": "2026-05-30",
    "4269126332": "2026-05-30",
    "4269887711": "2026-05-30",
    "4270769912": "2026-05-30",
    "4271401902": "2026-06-06",
    "4272158402": "2026-06-13",
    # Dennis (payer 0025041129)
    "4267641815": "2026-05-30",
    "4268386774": "2026-05-30",
    "4269094146": "2026-05-30",
    "4269880249": "2026-05-30",
    "4270568594": "2026-05-30",
    "4271395192": "2026-06-06",
    "4272149833": "2026-06-13",
}


def main(commit: bool):
    conn = get_connection()
    placeholders = ",".join("?" * len(PAID))
    rows = conn.execute(
        "SELECT id, invoice_number, location, total, "
        "COALESCE(balance, total) AS bal, payment_status "
        "FROM scanned_invoices "
        "WHERE vendor_name = 'Cintas' AND invoice_number IN (%s)" % placeholders,
        list(PAID.keys()),
    ).fetchall()
    conn.close()

    found = {r["invoice_number"]: r for r in rows}
    total = 0.0
    applied = 0
    for inv_no, paid_date in PAID.items():
        r = found.get(inv_no)
        if not r:
            print(f"  [MISS]  {inv_no} - not found, skipping")
            continue
        if r["payment_status"] == "paid":
            print(f"  [SKIP]  {inv_no} - already marked paid")
            continue
        amt = float(r["bal"])
        tag = "APPLY " if commit else "[DRY] "
        print(f"  {tag} {inv_no}  {r['location']:<7} ${amt:>8.2f}  paid {paid_date}")
        total += amt
        if commit:
            apply_payment(
                invoice_id=r["id"],
                amount=amt,
                payment_method="ach_autopay",
                payment_date=paid_date,
                reference=f"CINTAS-AUTOPAY-{paid_date}",
                memo="Cintas weekly ACH autopay (reconciled from myCintas email)",
            )
            applied += 1

    verb = "Applied" if commit else "Would apply"
    print(f"\n{verb}: ${total:.2f} across {applied if commit else len(PAID)} invoices")
    if not commit:
        print("Dry run only - re-run with --commit to write the payments.")


if __name__ == "__main__":
    main("--commit" in sys.argv)
