#!/usr/bin/env python3
"""Re-extract payments from stored raw_json using the fixed GUID-based dedup logic.
Fixes payments that were lost by the old running-total dedup code."""

import sys, os, json, logging
sys.path.insert(0, "/opt/red-nun-dashboard")
os.chdir("/opt/red-nun-dashboard")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger("reprocess")
logger.setLevel(logging.INFO)

from integrations.toast.data_store import get_connection

conn = get_connection()

# Get all orders with raw_json
rows = conn.execute("""
    SELECT guid, location, business_date, raw_json
    FROM orders WHERE raw_json IS NOT NULL AND LENGTH(raw_json) > 10
""").fetchall()

logger.info(f"Processing {len(rows)} orders")

added = 0
skipped = 0
total_orders = 0

for row in rows:
    total_orders += 1
    try:
        data = json.loads(row["raw_json"])
    except Exception:
        continue

    checks = data.get("checks", []) or []
    seen_pay_guids = set()

    for check in checks:
        for payment in check.get("payments", []) or []:
            pay_guid = payment.get("guid", "")
            if not pay_guid or pay_guid in seen_pay_guids:
                continue
            pay_status = (payment.get("paymentStatus") or "").upper()
            if pay_status in ("DENIED", "VOIDED"):
                skipped += 1
                continue
            seen_pay_guids.add(pay_guid)

            # Check if already in DB
            existing = conn.execute(
                "SELECT guid FROM payments WHERE guid=?", (pay_guid,)
            ).fetchone()
            if existing:
                continue

            # Insert missing payment
            conn.execute("""
                INSERT OR IGNORE INTO payments
                (guid, order_guid, location, business_date,
                 payment_type, card_type, amount, tip_amount, refund_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pay_guid, row["guid"], row["location"], row["business_date"],
                payment.get("type", ""),
                payment.get("cardType", ""),
                payment.get("amount", 0) or 0,
                payment.get("tipAmount", 0) or 0,
                payment.get("refundAmount", 0) or 0,
            ))
            added += 1

    if total_orders % 5000 == 0:
        conn.commit()
        sys.stdout.write(f"\r{total_orders} orders processed, {added} payments added...")
        sys.stdout.flush()

conn.commit()
conn.close()

print(f"\rDone. {total_orders} orders processed.")
print(f"  {added} missing payments added")
print(f"  {skipped} DENIED/VOIDED payments skipped")
