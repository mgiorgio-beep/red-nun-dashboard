#!/usr/bin/env python3
"""Backfill missing Dennis orders from Toast API for dates that have payments but no orders."""

import sys, os, time, logging
sys.path.insert(0, "/opt/red-nun-dashboard")
os.chdir("/opt/red-nun-dashboard")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger("backfill")
logger.setLevel(logging.INFO)

from datetime import datetime
from integrations.toast.toast_client import ToastAPIClient
from integrations.toast.sync import DataSync
from integrations.toast.data_store import get_connection

# Find Dennis dates that need order backfill
conn = get_connection()
pay_days = conn.execute("""
    SELECT business_date, COUNT(*) as cnt, SUM(amount + COALESCE(tip_amount,0)) as total
    FROM payments WHERE location='dennis'
    GROUP BY business_date
""").fetchall()

ord_days = conn.execute("""
    SELECT business_date, COUNT(*) as cnt
    FROM orders WHERE location='dennis'
    GROUP BY business_date
""").fetchall()
conn.close()

ord_map = {r["business_date"]: r["cnt"] for r in ord_days}

missing = []
for p in pay_days:
    bd = p["business_date"]
    ord_cnt = ord_map.get(bd, 0)
    if p["total"] > 100 and ord_cnt < 5:
        missing.append(bd)
missing.sort()

logger.info(f"{len(missing)} Dennis dates need order backfill")

syncer = DataSync()
errors = []
done = 0

for bd in missing:
    done += 1
    try:
        dt = datetime.strptime(bd, "%Y%m%d").date()
        count = syncer.sync_orders_for_date("dennis", dt)
        sys.stdout.write(f"\r[{done}/{len(missing)}] {bd} -> {count} orders")
        sys.stdout.flush()
        time.sleep(0.5)
    except Exception as e:
        errors.append(f"{bd}: {e}")
        sys.stdout.write(f"\r[{done}/{len(missing)}] {bd} ERROR: {e}\n")
        sys.stdout.flush()

print()
logger.info(f"Done. {done} dates processed, {len(errors)} errors.")
if errors:
    print("ERRORS:")
    for e in errors:
        print(f"  {e}")
