#!/usr/bin/env python3
"""
Backfill missing ap_payments rows from vendor_payments.

Background (2026-05-28):
  - The now-disabled portal payment scraper (see project_rednun_payment_scraper_disabled)
    recorded payments in vendor_payments and marked scanned_invoices.payment_status=paid,
    but never inserted matching ap_payments + ap_payment_invoices rows.
  - Result: vendor_payments has rows with ap_payment_id IS NULL. AP-side accounting
    reports (which read from ap_payments) are missing every portal-paid payment.
  - Confirmed via Cintas Dennis: 10+ portal payments missing from ap_payments.

For each vendor_payments row where ap_payment_id IS NULL:
  1. Insert a new ap_payments row mirroring the vendor_payment fields.
  2. For each linked vendor_payment_invoices row, find the matching scanned_invoices
     by invoice_number + invoice_date (preferred) and insert an ap_payment_invoices link.
  3. Update the vendor_payments row with the new ap_payment_id.

Does NOT touch scanned_invoices balances — those were already set by the scraper.

Pass --apply to commit. Without it, runs as preview.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/backfill_ap_payments_from_vendor_payments.py           # preview
        && python scripts/archive/backfill_ap_payments_from_vendor_payments.py --apply   # commit
"""
import sys
from collections import defaultdict

sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection


def main(apply: bool):
    conn = get_connection()
    cur = conn.cursor()

    print("=" * 72)
    print(f"Backfill ap_payments from vendor_payments  (mode: {'APPLY' if apply else 'PREVIEW'})")
    print("=" * 72)

    orphans = cur.execute("""
        SELECT id, vendor, location, payment_date, payment_ref, payment_method,
               payment_total, check_number, memo, status, source
          FROM vendor_payments
         WHERE ap_payment_id IS NULL
         ORDER BY payment_date DESC, id DESC
    """).fetchall()
    orphans = [dict(r) for r in orphans]

    if not orphans:
        print("\nNo orphan vendor_payments rows. Nothing to backfill.")
        conn.close()
        return

    # Group by vendor for the summary
    by_vendor = defaultdict(lambda: {"count": 0, "total": 0.0})
    for vp in orphans:
        v = vp["vendor"] or "(unknown)"
        by_vendor[v]["count"] += 1
        by_vendor[v]["total"] += float(vp.get("payment_total") or 0)

    print(f"\nOrphan vendor_payments rows (no ap_payment_id): {len(orphans)}")
    print(f"{'Vendor':<35}  {'#':>5}  {'Total':>12}")
    print("-" * 60)
    for vendor in sorted(by_vendor, key=lambda v: -by_vendor[v]["total"]):
        s = by_vendor[vendor]
        print(f"{vendor[:35]:<35}  {s['count']:>5}  ${s['total']:>11,.2f}")
    grand_total = sum(s["total"] for s in by_vendor.values())
    print("-" * 60)
    print(f"{'TOTAL':<35}  {len(orphans):>5}  ${grand_total:>11,.2f}")

    # Per-row detail
    print()
    print(f"First 20 rows that will get ap_payments created:")
    print(f"{'vp_id':>6}  {'date':<10}  {'vendor':<25}  {'method':<8}  {'total':>10}  {'invs':>4}")
    print("-" * 78)
    for vp in orphans[:20]:
        n_invs = cur.execute(
            "SELECT COUNT(*) AS n FROM vendor_payment_invoices WHERE payment_id = ?",
            (vp["id"],),
        ).fetchone()["n"]
        print(f"{vp['id']:>6}  {vp['payment_date'] or '?':<10}  "
              f"{(vp['vendor'] or '?')[:25]:<25}  "
              f"{(vp['payment_method'] or '?'):<8}  ${float(vp.get('payment_total') or 0):>9,.2f}  {n_invs:>4}")
    if len(orphans) > 20:
        print(f"... and {len(orphans) - 20} more")

    if not apply:
        print()
        print("PREVIEW MODE — no changes written. Re-run with --apply to commit.")
        conn.close()
        return

    # ── Apply ─────────────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("APPLYING")
    print("=" * 72)

    created = 0
    linked_invoices = 0
    unmatched_invoices = 0

    for vp in orphans:
        vp_id = vp["id"]

        # Insert ap_payments row mirroring the vendor_payment
        cur.execute(
            """INSERT INTO ap_payments
                 (vendor_name, payment_date, amount, payment_method,
                  check_number, reference_number, memo, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (vp["vendor"], vp["payment_date"], vp["payment_total"],
             vp.get("payment_method") or "ach",
             vp.get("check_number"),
             vp.get("payment_ref"),
             (vp.get("memo") or "") + f" | backfilled-from-vp{vp_id}",
             vp.get("status") or "cleared"),
        )
        ap_id = cur.lastrowid
        created += 1

        # Update vendor_payments to point at the new ap_payments row
        cur.execute(
            "UPDATE vendor_payments SET ap_payment_id = ? WHERE id = ?",
            (ap_id, vp_id),
        )

        # Walk the vendor_payment_invoices links — for each, find the scanned_invoices
        # row by invoice_number (preferred) + invoice_date, and create the
        # ap_payment_invoices link.
        vpi_rows = cur.execute(
            "SELECT invoice_number, invoice_date, amount_paid "
            "FROM vendor_payment_invoices WHERE payment_id = ?",
            (vp_id,),
        ).fetchall()
        for vpi in vpi_rows:
            vpi = dict(vpi)
            inv_no = vpi.get("invoice_number")
            inv_date = vpi.get("invoice_date")
            amt = float(vpi.get("amount_paid") or 0)
            if not inv_no:
                unmatched_invoices += 1
                continue

            # Try exact (invoice_number + invoice_date) first; fall back to
            # invoice_number alone if that fails.
            match = cur.execute(
                "SELECT id FROM scanned_invoices "
                "WHERE invoice_number = ? AND invoice_date = ? AND LOWER(vendor_name) = LOWER(?) "
                "LIMIT 1",
                (inv_no, inv_date, vp["vendor"]),
            ).fetchone()
            if not match:
                match = cur.execute(
                    "SELECT id FROM scanned_invoices "
                    "WHERE invoice_number = ? AND LOWER(vendor_name) = LOWER(?) "
                    "LIMIT 1",
                    (inv_no, vp["vendor"]),
                ).fetchone()
            if not match:
                unmatched_invoices += 1
                continue

            cur.execute(
                "INSERT INTO ap_payment_invoices (payment_id, invoice_id, amount_applied) "
                "VALUES (?, ?, ?)",
                (ap_id, match["id"], amt),
            )
            linked_invoices += 1

    conn.commit()
    conn.close()

    print(f"  ap_payments rows created:        {created}")
    print(f"  ap_payment_invoices links added: {linked_invoices}")
    print(f"  invoices that didn't match:      {unmatched_invoices}")
    print()
    print("Backfill complete.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
