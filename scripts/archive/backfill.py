"""Backfill 12 months of Toast order + labor data."""
from datetime import date, timedelta
from sync import DataSync
import time

ds = DataSync()
start = date(2025, 2, 1)
end = date(2025, 11, 18)  # day before our existing data starts

current = start
total_orders = 0
total_labor = 0
day_count = 0

while current <= end:
    for location in ["dennis", "chatham"]:
        try:
            o = ds.sync_orders_for_date(location, current)
            total_orders += o
        except Exception as e:
            print(f"  Order error {location} {current}: {e}")
        try:
            l = ds.sync_labor_for_date(location, current)
            total_labor += l
        except Exception as e:
            print(f"  Labor error {location} {current}: {e}")
    
    day_count += 1
    if day_count % 7 == 0:
        print(f"  Week {day_count//7}: through {current}, {total_orders} orders, {total_labor} labor entries so far")
    
    current += timedelta(days=1)
    time.sleep(0.1)  # gentle on the API

print(f"\n✅ Backfill complete: {day_count} days, {total_orders} orders, {total_labor} labor entries")
