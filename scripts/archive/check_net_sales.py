#!/usr/bin/env python3
"""Quick script to check net sales calculation"""
import sqlite3

conn = sqlite3.connect('toast_data.db')
cursor = conn.cursor()

query = """
SELECT
    COUNT(*) as orders,
    SUM(total_amount) as total,
    SUM(tax_amount) as tax,
    SUM(tip_amount) as tip,
    SUM(discount_amount) as discount,
    SUM(total_amount - tax_amount - tip_amount) as net_current,
    SUM(total_amount - tax_amount - tip_amount - discount_amount) as net_minus_disc
FROM orders
WHERE location = 'chatham'
  AND business_date = '20260213'
  AND json_extract(raw_json, '$.deleted') != 1
  AND json_extract(raw_json, '$.voided') != 1
"""

result = cursor.execute(query).fetchone()
print("Chatham Feb 13 (85 valid orders):")
print(f"Total: ${result[1]:.2f}")
print(f"Tax: ${result[2]:.2f}")
print(f"Tips: ${result[3]:.2f}")
print(f"Discounts: ${result[4]:.2f}")
print(f"Net (current formula: total - tax - tip): ${result[5]:.2f}")
print(f"Net (with discount: total - tax - tip - discount): ${result[6]:.2f}")
print(f"\nToast says: $4,061")
print(f"Difference (current): ${result[5] - 4061:.2f}")
print(f"Difference (minus discount): ${result[6] - 4061:.2f}")

conn.close()
