#!/usr/bin/env python3
"""
Reproduce the PFG/Performance Foodservice match bug in isolation.

Constructs a synthetic PFG receipt with total $2,381.42, runs the matcher,
prints the SQL it executes, and shows what comes back.

Run:
    cd /opt/red-nun-dashboard && source venv/bin/activate \
        && python scripts/archive/debug_matcher.py
"""
import sys
sys.path.insert(0, "/opt/red-nun-dashboard")

from integrations.toast.data_store import get_connection
from integrations.invoices.receipt_classifier import (
    ClassifiedReceipt,
    ReceiptLineItem,
    find_matching_invoice,
    _fuzzy_match,
)

# Mimic the PFG receipt that triggered the suspicious id=100353 match.
fake = ClassifiedReceipt(
    message_id="DEBUG",
    signature_key="pfg_billfire",
    vendor_canonical="Performance Foodservice",
    total_amount=2381.42,
    line_items=[],
    payment_date="2026-05-28",
    payment_method="ach_via_billfire",
    tier="auto_apply",
    raw_subject="Click2Pay confirmation",
    raw_from="no-reply@valet.billfire.com",
)

print(f"Receipt: {fake.vendor_canonical} total=${fake.total_amount} "
      f"line_items={fake.line_items}")
print()

# Run the actual matcher
conn = get_connection()
matches = find_matching_invoice(fake, conn)
conn.close()

print(f"Matcher returned {len(matches)} result(s):")
for m in matches:
    print(f"  decision={m.decision}")
    print(f"  matched_invoice_id={m.matched_invoice_id}")
    print(f"  candidate_count={m.candidate_count}")
    print(f"  reason={m.reason}")
    print(f"  candidates (first 5):")
    for c in (m.candidates or [])[:5]:
        print(f"    id={c.get('id')} #{c.get('invoice_number')} "
              f"${c.get('balance')} status={c.get('payment_status')!r} "
              f"date={c.get('invoice_date')}")
print()

# Now run the EXACT raw SQL the matcher uses, for sanity
print("=" * 60)
print("Direct SQL re-run (same query as _fuzzy_match):")
print("=" * 60)

conn = get_connection()
cur = conn.cursor()
amount_lo = fake.total_amount * 0.99
amount_hi = fake.total_amount * 1.01
sql = """SELECT id, vendor_name, invoice_number, invoice_date, total,
                COALESCE(balance, total) AS balance, payment_status, location
           FROM scanned_invoices
          WHERE LOWER(vendor_name) LIKE ?
            AND COALESCE(balance, total) BETWEEN ? AND ?
            AND (payment_status != 'paid' OR payment_status IS NULL)
            AND invoice_date >= '2026-03-29'
          ORDER BY invoice_date DESC"""
params = [f"%{fake.vendor_canonical.lower()}%", amount_lo, amount_hi]
print(f"SQL: {sql}")
print(f"Params: {params}")
rows = cur.execute(sql, params).fetchall()
print(f"Direct rows: {len(rows)}")
for r in rows[:5]:
    print(f"  id={r['id']} #{r['invoice_number']} ${r['balance']} "
          f"status={r['payment_status']!r}")
conn.close()
