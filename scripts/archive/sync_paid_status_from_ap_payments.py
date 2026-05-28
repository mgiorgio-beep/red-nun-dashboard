#!/usr/bin/env python3
"""
Sync scanned_invoices.payment_status from ap_payment_invoices.

After today's backfill, many scanned_invoices rows have linked ap_payment_invoices
records (i.e. we know a payment was applied to them) but their payment_status
is still 'unpaid' and balance > 0. This is because the portal scraper recorded
the payment but never updated the invoice status.

For each such invoice:
  - sum of amount_applied across all ap_payment_invoices links
  - if sum >= balance (within $1): mark paid, balance=0
  - if sum > 0 but < balance: mark partial, subtract from balance
  - if sum == 0 (no real payments linked): skip
  - paid_date = max(payment_date) of linked payments
  - payment_reference = reference_number of latest payment

Does NOT touch invoices that are already paid or already partial.

Pass --apply to commit. Without it, runs preview.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/sync_paid_status_from_ap_payments.py            # preview
        && python scripts/archive/sync_paid_status_from_ap_payments.py --apply    # commit
"""
import sys
from collections import defaultdict

sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection

AMOUNT_TOLERANCE = 1.00  # dollars — treat as full pay if sum within this of balance


def main(apply: bool):
    conn = get_connection()
    cur = conn.cursor()

    print("=" * 72)
    print(f"Sync paid status from ap_payment_invoices  (mode: {'APPLY' if apply else 'PREVIEW'})")
    print("=" * 72)

    # Find every invoice that is still unpaid but has at least one ap_payment link
    rows = cur.execute("""
        SELECT si.id, si.vendor_name, si.invoice_number, si.invoice_date,
               si.total, COALESCE(si.balance, si.total) AS balance,
               si.payment_status, si.location,
               SUM(api.amount_applied) AS total_applied,
               MAX(ap.payment_date)    AS last_payment_date,
               GROUP_CONCAT(api.payment_id) AS payment_ids
          FROM scanned_invoices si
          JOIN ap_payment_invoices api ON api.invoice_id = si.id
          JOIN ap_payments ap          ON ap.id = api.payment_id
         WHERE COALESCE(si.payment_status, 'unpaid') NOT IN ('paid')
           AND COALESCE(si.balance, si.total) > 0
         GROUP BY si.id
         ORDER BY si.invoice_date DESC
    """).fetchall()

    rows = [dict(r) for r in rows]
    if not rows:
        print("\nNo invoices need syncing.")
        conn.close()
        return

    by_vendor = defaultdict(lambda: {"count": 0, "applied": 0.0, "balance": 0.0})
    updates_full = []
    updates_partial = []
    skipped_no_money = []
    overapplied = []           # applied > balance + tolerance — needs human review

    for r in rows:
        applied = float(r["total_applied"] or 0)
        balance = float(r["balance"] or 0)

        v = r["vendor_name"] or "(unknown)"
        by_vendor[v]["count"] += 1
        by_vendor[v]["applied"] += applied
        by_vendor[v]["balance"] += balance

        if applied <= 0:
            skipped_no_money.append(r)
            continue

        if abs(applied - balance) <= AMOUNT_TOLERANCE:
            # Close-enough match (within $1) — safe to mark fully paid
            updates_full.append(r)
        elif applied < balance - AMOUNT_TOLERANCE:
            # Applied less than balance — true partial payment
            updates_partial.append(r)
        else:
            # Applied > balance by more than tolerance — duplicate link,
            # mismatched data, or already-partially-paid. Don't touch.
            overapplied.append(r)

    # Per-vendor summary
    print(f"\nTotal invoices with linked ap_payments but still unpaid: {len(rows)}")
    print(f"  → will mark FULL paid:        {len(updates_full)}")
    print(f"  → will mark PARTIAL:          {len(updates_partial)}")
    print(f"  → skip (no $$ linked):        {len(skipped_no_money)}")
    print(f"  → skip (overapplied, review): {len(overapplied)}")
    print()
    print(f"{'Vendor':<40}  {'#':>4}  {'Balance':>11}  {'Applied':>11}")
    print("-" * 78)
    for v in sorted(by_vendor, key=lambda x: -by_vendor[x]["balance"]):
        s = by_vendor[v]
        print(f"{v[:40]:<40}  {s['count']:>4}  ${s['balance']:>10,.2f}  ${s['applied']:>10,.2f}")
    print("-" * 78)
    total_balance = sum(s["balance"] for s in by_vendor.values())
    total_applied = sum(s["applied"] for s in by_vendor.values())
    print(f"{'TOTAL':<40}  {len(rows):>4}  ${total_balance:>10,.2f}  ${total_applied:>10,.2f}")

    # Show first 15 full-pay updates so we can sanity check
    print()
    print(f"First 15 FULL-PAY updates that will be applied:")
    print(f"{'inv id':>7}  {'vendor':<30}  {'inv#':<14}  {'bal':>9}  {'paid':>9}  {'paid_date':<12}")
    print("-" * 95)
    for r in updates_full[:15]:
        print(f"{r['id']:>7}  {(r['vendor_name'] or '?')[:30]:<30}  "
              f"{(r['invoice_number'] or '?')[:14]:<14}  ${float(r['balance']):>8,.2f}  "
              f"${float(r['total_applied']):>8,.2f}  {r['last_payment_date'] or '?':<12}")

    if updates_partial:
        print()
        print(f"PARTIAL updates (applied < balance):")
        for r in updates_partial[:15]:
            applied = float(r["total_applied"] or 0)
            balance = float(r["balance"] or 0)
            print(f"  {r['id']:>7}  {(r['vendor_name'] or '?')[:30]:<30}  "
                  f"#{r['invoice_number']}  bal=${balance:,.2f}  applied=${applied:,.2f}  "
                  f"remaining=${balance - applied:,.2f}")

    if overapplied:
        print()
        print(f"OVERAPPLIED — NOT TOUCHED (review manually):")
        for r in overapplied:
            applied = float(r["total_applied"] or 0)
            balance = float(r["balance"] or 0)
            print(f"  inv {r['id']:>7}  {(r['vendor_name'] or '?')[:30]:<30}  "
                  f"#{r['invoice_number'] or '(null)'}  bal=${balance:,.2f}  "
                  f"applied=${applied:,.2f}  excess=${applied - balance:,.2f}")

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

    full_count = 0
    partial_count = 0

    for r in updates_full:
        applied = float(r["total_applied"] or 0)
        # Latest payment's reference
        ref_row = cur.execute(
            "SELECT reference_number FROM ap_payments WHERE id IN ("
            + r["payment_ids"] + ") ORDER BY payment_date DESC, id DESC LIMIT 1"
        ).fetchone()
        ref = (dict(ref_row).get("reference_number") if ref_row else None) or None

        cur.execute(
            """UPDATE scanned_invoices
                  SET amount_paid = COALESCE(total, 0),
                      balance = 0,
                      payment_status = 'paid',
                      paid_date = ?,
                      payment_reference = COALESCE(?, payment_reference),
                      notes = COALESCE(notes, '') || ' | sync-paid-from-ap 2026-05-28'
                WHERE id = ?""",
            (r["last_payment_date"], ref, r["id"]),
        )
        full_count += cur.rowcount

    for r in updates_partial:
        applied = float(r["total_applied"] or 0)
        balance = float(r["balance"] or 0)
        new_balance = max(0, balance - applied)
        new_amount_paid = float(r.get("total") or 0) - new_balance

        cur.execute(
            """UPDATE scanned_invoices
                  SET amount_paid = ?,
                      balance = ?,
                      payment_status = 'partial',
                      notes = COALESCE(notes, '') || ' | sync-partial-from-ap 2026-05-28'
                WHERE id = ?""",
            (new_amount_paid, new_balance, r["id"]),
        )
        partial_count += cur.rowcount

    conn.commit()
    conn.close()

    print(f"  Invoices marked FULL paid: {full_count}")
    print(f"  Invoices marked PARTIAL:   {partial_count}")
    print()
    print("Sync complete.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
