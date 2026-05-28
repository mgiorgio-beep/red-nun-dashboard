#!/usr/bin/env python3
"""
Two targeted cleanups from 2026-05-28 sync:

1. Shaun Kalinowski (invoice id 100465, $864) has TWO ap_payment_invoices links
   summing to $1,728 — a duplicate. Mike confirmed: should be 1 payment for $864
   from Dennis. Keep the older link, delete the newer (duplicate) one, then mark
   the invoice paid via the surviving payment.

2. Colonial #553919 (invoice id 100438, total $411.40) has $7 phantom balance
   because the portal scraper applied $404.40 (net of the -$7 credit on #206074)
   instead of the gross $411.40. Colonial's portal shows #553919 as fully paid.
   Simplest fix: zero the $7 balance with a memo noting it was netted via the
   #206074 credit. Don't add a new ap_payment row; the existing link is fine.

Pass --apply to commit. Without it, runs preview.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/fix_shaun_and_colonial_553919.py           # preview
        && python scripts/archive/fix_shaun_and_colonial_553919.py --apply   # commit
"""
import sys

sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection

SHAUN_INVOICE_ID = 100465
COLONIAL_INVOICE_ID = 100438


def dump_invoice(cur, label, inv_id):
    print(f"\n— {label} (invoice id {inv_id}) —")
    inv = cur.execute(
        "SELECT id, vendor_name, invoice_number, invoice_date, total, "
        "COALESCE(balance, total) AS balance, payment_status, location, paid_date, notes "
        "FROM scanned_invoices WHERE id = ?",
        (inv_id,),
    ).fetchone()
    if not inv:
        print(f"  (no invoice with id {inv_id})")
        return None
    inv = dict(inv)
    for k in ("id", "vendor_name", "invoice_number", "total", "balance",
              "payment_status", "location", "paid_date"):
        print(f"  {k:18s} = {inv.get(k)!r}")

    print(f"\n  Linked ap_payment_invoices:")
    links = cur.execute(
        """SELECT api.payment_id, api.amount_applied, ap.payment_date,
                  ap.amount AS payment_total, ap.payment_method, ap.reference_number, ap.memo
             FROM ap_payment_invoices api
             JOIN ap_payments ap ON ap.id = api.payment_id
            WHERE api.invoice_id = ?
            ORDER BY ap.payment_date, api.payment_id""",
        (inv_id,),
    ).fetchall()
    for l in links:
        l = dict(l)
        print(f"    payment_id={l['payment_id']}  applied=${l['amount_applied']}  "
              f"date={l['payment_date']}  pay_total=${l['payment_total']}  "
              f"method={l['payment_method']}  ref={l['reference_number']!r}")
        if l.get("memo"):
            print(f"      memo: {l['memo']}")
    return inv, [dict(l) for l in links]


def main(apply: bool):
    conn = get_connection()
    cur = conn.cursor()

    print("=" * 72)
    print(f"Fix Shaun Kalinowski + Colonial #553919  (mode: {'APPLY' if apply else 'PREVIEW'})")
    print("=" * 72)

    shaun = dump_invoice(cur, "Shaun Kalinowski", SHAUN_INVOICE_ID)
    colonial = dump_invoice(cur, "Colonial #553919", COLONIAL_INVOICE_ID)

    if not shaun or not colonial:
        print("\nOne of the target invoices is missing — aborting.")
        conn.close()
        return

    shaun_inv, shaun_links = shaun
    colonial_inv, colonial_links = colonial

    # ── Shaun: decide which link to drop ──────────────────────────────────────
    print("\n" + "-" * 72)
    print("SHAUN PLAN")
    print("-" * 72)
    if len(shaun_links) < 2:
        print(f"  Only {len(shaun_links)} ap_payment_invoices link(s) found — "
              f"not the expected duplicate. Skipping Shaun cleanup.")
        shaun_to_delete = None
    else:
        # Keep the earliest link, delete the later one.
        shaun_to_delete = shaun_links[-1]  # latest-payment link
        shaun_keep = shaun_links[0]
        print(f"  Keep:   payment_id={shaun_keep['payment_id']}  "
              f"amount=${shaun_keep['amount_applied']}  date={shaun_keep['payment_date']}")
        print(f"  Delete: payment_id={shaun_to_delete['payment_id']}  "
              f"amount=${shaun_to_delete['amount_applied']}  date={shaun_to_delete['payment_date']}")
        print(f"  After: invoice #{SHAUN_INVOICE_ID} marked paid via payment "
              f"#{shaun_keep['payment_id']}, paid_date={shaun_keep['payment_date']}, "
              f"balance=0")

    # ── Colonial: zero the $7 balance ─────────────────────────────────────────
    print("\n" + "-" * 72)
    print("COLONIAL #553919 PLAN")
    print("-" * 72)
    colonial_balance = float(colonial_inv["balance"] or 0)
    print(f"  Current balance: ${colonial_balance}")
    print(f"  Action: mark paid with memo noting the $7 was netted via the -$7 "
          f"credit on invoice #206074. No new ap_payment row created — the "
          f"existing $404.40 link stays.")
    print(f"  paid_date = (max payment_date from links) = "
          f"{colonial_links[-1]['payment_date'] if colonial_links else '?'}")

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

    # Shaun
    if shaun_to_delete is not None:
        shaun_keep_payment = shaun_links[0]
        cur.execute(
            "DELETE FROM ap_payment_invoices WHERE payment_id = ? AND invoice_id = ?",
            (shaun_to_delete["payment_id"], SHAUN_INVOICE_ID),
        )
        print(f"  Deleted duplicate ap_payment_invoices link "
              f"(payment_id={shaun_to_delete['payment_id']}, "
              f"rowcount={cur.rowcount})")

        # Mark invoice paid via the surviving link
        cur.execute(
            """UPDATE scanned_invoices
                  SET amount_paid = COALESCE(total, 0),
                      balance = 0,
                      payment_status = 'paid',
                      paid_date = ?,
                      payment_reference = COALESCE(?, payment_reference),
                      notes = COALESCE(notes, '') || ' | fix-shaun-dupe 2026-05-28: '
                              'deleted duplicate ap_payment_invoices link, '
                              'paid via payment ' || ?
                WHERE id = ?""",
            (shaun_keep_payment["payment_date"],
             shaun_keep_payment["reference_number"],
             str(shaun_keep_payment["payment_id"]),
             SHAUN_INVOICE_ID),
        )
        print(f"  Marked Shaun invoice #{SHAUN_INVOICE_ID} paid (rowcount={cur.rowcount})")

    # Colonial
    paid_date = (colonial_links[-1]["payment_date"]
                 if colonial_links else "2026-05-26")
    cur.execute(
        """UPDATE scanned_invoices
              SET amount_paid = COALESCE(total, 0),
                  balance = 0,
                  payment_status = 'paid',
                  paid_date = ?,
                  notes = COALESCE(notes, '') || ' | fix-colonial-553919 2026-05-28: '
                          'phantom $7 cleared — portal scraper applied $404.40 (net of -$7 '
                          'credit on invoice #206074) instead of gross $411.40. '
                          'Colonial portal confirms #553919 is fully paid.'
            WHERE id = ?""",
        (paid_date, COLONIAL_INVOICE_ID),
    )
    print(f"  Marked Colonial #553919 (invoice id {COLONIAL_INVOICE_ID}) paid "
          f"(rowcount={cur.rowcount})")

    conn.commit()
    conn.close()
    print("\nCleanup complete.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
