#!/usr/bin/env python3
"""
Finalize Cintas reconciliation after the 6 gap-week invoices were backfilled
by the (now fixed) scraper.

reconcile_cintas_autopay.py recorded 7 payments at the full bank amounts, with
residuals on the May 30 (Chatham + Dennis) and Jun 20 (Chatham) debits because
those invoices weren't imported yet. The scraper has since imported them:
  4266153194 4/15 Chatham $60.42      4266150655 4/15 Dennis $218.99
  4266903669 4/22 Chatham $60.42      4266898890 4/22 Dennis $218.99
  4272871844 6/17 Chatham $60.42      4272862385 6/17 Dennis $218.99

This script:
  1. Links the 5 invoices that fall under existing payments to those payments
     (zeroing their residuals) and marks them paid.
  2. Records the 8th bank debit - Jun 20 Dennis $218.99 (6/17 invoice, paid 6/19),
     which had no autopay email - and marks that invoice paid.

VERIFY the Jun 20 Dennis $218.99 ACH debit against your bank statement before
committing; everything else ties to payments already recorded.

Usage (on the Beelink):
    venv/bin/python3 scripts/archive/reconcile_cintas_backfill.py            # dry run
    venv/bin/python3 scripts/archive/reconcile_cintas_backfill.py --commit   # write
"""
import sys
sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection

PAYMENT_METHOD = "ach_autopay"

BACKFILL = [
    {"inv": "4266153194", "loc": "chatham", "date": "2026-05-30",
     "ref": "CINTAS-AUTOPAY-2026-05-30-chatham", "existing": True},
    {"inv": "4266903669", "loc": "chatham", "date": "2026-05-30",
     "ref": "CINTAS-AUTOPAY-2026-05-30-chatham", "existing": True},
    {"inv": "4266150655", "loc": "dennis", "date": "2026-05-30",
     "ref": "CINTAS-AUTOPAY-2026-05-30-dennis", "existing": True},
    {"inv": "4266898890", "loc": "dennis", "date": "2026-05-30",
     "ref": "CINTAS-AUTOPAY-2026-05-30-dennis", "existing": True},
    {"inv": "4272871844", "loc": "chatham", "date": "2026-06-20",
     "ref": "CINTAS-AUTOPAY-2026-06-20-chatham", "existing": True},
    {"inv": "4272862385", "loc": "dennis", "date": "2026-06-20",
     "ref": "CINTAS-AUTOPAY-2026-06-20-dennis", "existing": False},
]


def main(commit: bool):
    conn = get_connection()
    cur = conn.cursor()

    for b in BACKFILL:
        inv = cur.execute(
            "SELECT id, invoice_number, invoice_date, total, due_date, payment_status "
            "FROM scanned_invoices WHERE vendor_name='Cintas' AND invoice_number=?",
            (b["inv"],),
        ).fetchone()
        if not inv:
            print(f"  [MISS] {b['inv']} not found"); continue
        if inv["payment_status"] == "paid":
            print(f"  [SKIP] {b['inv']} already paid"); continue

        amt = float(inv["total"])

        if b["existing"]:
            ap = cur.execute("SELECT id FROM ap_payments WHERE reference_number=?", (b["ref"],)).fetchone()
            vp = cur.execute("SELECT id FROM vendor_payments WHERE payment_ref=?", (b["ref"],)).fetchone()
            if not ap or not vp:
                print(f"  [MISS] payment {b['ref']} not found - run reconcile_cintas_autopay.py --commit first")
                continue
            ap_id, vp_id = ap["id"], vp["id"]
            action = f"attach to {b['ref']}"
        else:
            ap_id = vp_id = None
            action = f"NEW payment {b['ref']} ${amt:.2f}"

        print(f"  {'APPLY' if commit else '[DRY]'}  {b['inv']}  {b['loc']:<7} ${amt:>7.2f}  -> {action}")
        if not commit:
            continue

        memo = f"Cintas weekly ACH autopay {b['date']} ({b['loc']}) - backfilled invoice"
        if not b["existing"]:
            cur.execute(
                "INSERT INTO ap_payments (vendor_name, payment_date, amount, payment_method, "
                "reference_number, memo, status) VALUES (?,?,?,?,?,?, 'cleared')",
                ("Cintas", b["date"], amt, PAYMENT_METHOD, b["ref"], memo),
            )
            ap_id = cur.lastrowid
            cur.execute(
                "INSERT INTO vendor_payments (vendor, location, payment_date, payment_ref, "
                "payment_method, payment_total, memo, status, source, ap_payment_id) "
                "VALUES (?,?,?,?,?,?,?, 'cleared', 'cintas_autopay_reconcile', ?)",
                ("Cintas", b["loc"], b["date"], b["ref"], PAYMENT_METHOD, amt, memo, ap_id),
            )
            vp_id = cur.lastrowid

        cur.execute(
            "INSERT INTO ap_payment_invoices (payment_id, invoice_id, amount_applied) VALUES (?,?,?)",
            (ap_id, inv["id"], amt),
        )
        cur.execute(
            "INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, "
            "due_date, amount_paid) VALUES (?,?,?,?,?)",
            (vp_id, inv["invoice_number"], inv["invoice_date"], inv["due_date"], amt),
        )
        cur.execute(
            "UPDATE scanned_invoices SET amount_paid=total, balance=0, payment_status='paid', "
            "paid_date=?, payment_reference=COALESCE(payment_reference, ?), "
            "notes=COALESCE(notes,'')||' | '||? WHERE id=?",
            (b["date"], b["ref"], f"Cintas autopay {b['date']} ({b['ref']})", inv["id"]),
        )
        conn.commit()

    conn.close()
    if not commit:
        print("\nDry run only - re-run with --commit to write.")


if __name__ == "__main__":
    main("--commit" in sys.argv)
