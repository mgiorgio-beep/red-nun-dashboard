#!/usr/bin/env python3
"""
One-off reconciliation: record the myCintas weekly ACH autopays as payments so
the bank account reconciles, and clear the Cintas invoices they paid.

Cintas autopays weekly (Saturday). The receipt poller didn't know Cintas until
2026-06-22 and the confirmation emails go to mike@rednun.com (not dashboard@),
so these autopays were never recorded. Reconciled from the "myCintas Autopay
Confirmation" emails.

IMPORTANT - structured for bank reconciliation:
Each entry below is ONE autopay = ONE ACH debit on the bank statement. We create
ONE vendor_payment (+ ap_payment) per debit, at the FULL bank amount, so each
bank line matches one dashboard payment. That payment is then applied across the
invoices it cleared.

Where bank_amount > the invoices we can apply it to (May 30 catch-up, Jun 20),
the difference is Cintas invoices from weeks the scraper never imported
(approx 4/15, 4/22, 6/17). The payment still records at the true bank amount; the
residual is noted in the memo. Those missing invoices should be backfilled
separately so AP ties out fully - they are NOT invented here.

Verified against vendor_payments: no Cintas payment exists dated >= 2026-05-30,
so this does not double-count.

Usage (on the Beelink):
    venv/bin/python3 scripts/archive/reconcile_cintas_autopay.py            # dry run
    venv/bin/python3 scripts/archive/reconcile_cintas_autopay.py --commit   # write
"""
import sys
sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection

PAYMENT_METHOD = "ach_autopay"

# One dict per autopay = one bank ACH debit.
PAYMENTS = [
    {"date": "2026-05-30", "location": "chatham", "bank_amount": 484.88,
     "invoices": ["4268394000", "4269126332", "4269887711", "4270769912"]},
    {"date": "2026-05-30", "location": "dennis", "bank_amount": 1672.03,
     "invoices": ["4267641815", "4268386774", "4269094146", "4269880249", "4270568594"]},
    {"date": "2026-06-06", "location": "chatham", "bank_amount": 105.68,
     "invoices": ["4271401902"]},
    {"date": "2026-06-06", "location": "dennis", "bank_amount": 358.09,
     "invoices": ["4271395192"]},
    {"date": "2026-06-13", "location": "chatham", "bank_amount": 60.42,
     "invoices": ["4272158402"]},
    {"date": "2026-06-13", "location": "dennis", "bank_amount": 218.99,
     "invoices": ["4272149833"]},
    {"date": "2026-06-20", "location": "chatham", "bank_amount": 60.42,
     "invoices": []},  # 6/17 Chatham invoice not imported
]


def main(commit: bool):
    conn = get_connection()
    cur = conn.cursor()
    grand_bank = 0.0
    grand_applied = 0.0

    for p in PAYMENTS:
        ref = f"CINTAS-AUTOPAY-{p['date']}-{p['location']}"
        if cur.execute("SELECT 1 FROM vendor_payments WHERE payment_ref = ?", (ref,)).fetchone():
            print(f"[SKIP] {ref} already recorded")
            continue

        invs = []
        for inv_no in p["invoices"]:
            r = cur.execute(
                "SELECT id, invoice_number, invoice_date, total, due_date, payment_status "
                "FROM scanned_invoices WHERE vendor_name='Cintas' AND invoice_number=?",
                (inv_no,),
            ).fetchone()
            if not r:
                print(f"   [MISS] invoice {inv_no} not found")
                continue
            invs.append(r)

        applied = round(sum(float(r["total"]) for r in invs), 2)
        residual = round(p["bank_amount"] - applied, 2)
        memo = f"Cintas weekly ACH autopay {p['date']} ({p['location']}) - reconciled from myCintas email"
        if residual > 0.01:
            memo += f"; ${residual:.2f} covers Cintas invoices not yet imported"

        print(f"{'APPLY' if commit else '[DRY]'}  {ref}  bank ${p['bank_amount']:>8.2f}  "
              f"applied ${applied:>8.2f}  residual ${residual:>7.2f}  ({len(invs)} inv)")
        grand_bank += p["bank_amount"]
        grand_applied += applied
        if not commit:
            continue

        cur.execute(
            "INSERT INTO ap_payments (vendor_name, payment_date, amount, payment_method, "
            "reference_number, memo, status) VALUES (?,?,?,?,?,?, 'cleared')",
            ("Cintas", p["date"], p["bank_amount"], PAYMENT_METHOD, ref, memo),
        )
        ap_id = cur.lastrowid
        cur.execute(
            "INSERT INTO vendor_payments (vendor, location, payment_date, payment_ref, "
            "payment_method, payment_total, memo, status, source, ap_payment_id) "
            "VALUES (?,?,?,?,?,?,?, 'cleared', 'cintas_autopay_reconcile', ?)",
            ("Cintas", p["location"], p["date"], ref, PAYMENT_METHOD, p["bank_amount"], memo, ap_id),
        )
        vp_id = cur.lastrowid
        for r in invs:
            amt = float(r["total"])
            cur.execute(
                "INSERT INTO ap_payment_invoices (payment_id, invoice_id, amount_applied) VALUES (?,?,?)",
                (ap_id, r["id"], amt),
            )
            cur.execute(
                "INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, "
                "due_date, amount_paid) VALUES (?,?,?,?,?)",
                (vp_id, r["invoice_number"], r["invoice_date"], r["due_date"], amt),
            )
            cur.execute(
                "UPDATE scanned_invoices SET amount_paid=total, balance=0, payment_status='paid', "
                "paid_date=?, payment_reference=COALESCE(payment_reference, ?), "
                "notes=COALESCE(notes,'')||' | '||? WHERE id=?",
                (p["date"], ref, f"Cintas autopay {p['date']} ({ref})", r["id"]),
            )
        conn.commit()

    conn.close()
    verb = "Recorded" if commit else "Would record"
    print(f"\n{verb} {len(PAYMENTS)} bank payments totaling ${grand_bank:.2f} "
          f"(${grand_applied:.2f} applied to dashboard invoices, "
          f"${grand_bank - grand_applied:.2f} = invoices not yet imported)")
    if not commit:
        print("Dry run only - re-run with --commit to write.")


if __name__ == "__main__":
    main("--commit" in sys.argv)
