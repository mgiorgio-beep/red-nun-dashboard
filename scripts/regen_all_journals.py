#!/usr/bin/env python3
"""One-time script: regenerate all sales journal entries from existing order data."""

import sys, os, time, logging
sys.path.insert(0, "/opt/red-nun-dashboard")
os.chdir("/opt/red-nun-dashboard")

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger("regen")
logger.setLevel(logging.INFO)

from datetime import datetime, timedelta
from integrations.toast.data_store import get_connection
from reports.sales_journal import build_journal_entry, persist_journal_entry, init_sales_journal_tables

init_sales_journal_tables()

conn = get_connection()

# Get all distinct business dates per location
results = {}
for loc in ("chatham", "dennis"):
    rows = conn.execute("""
        SELECT DISTINCT business_date FROM orders
        WHERE location=? ORDER BY business_date
    """, (loc,)).fetchall()
    dates = []
    for r in rows:
        bd = r["business_date"]
        try:
            dt = datetime.strptime(bd, "%Y%m%d")
            dates.append(dt.strftime("%Y-%m-%d"))
        except ValueError:
            continue
    results[loc] = dates
    logger.info(f"{loc}: {len(dates)} dates to process")

conn.close()

total = sum(len(v) for v in results.values())
done = 0
errors = []

for loc in ("chatham", "dennis"):
    for entry_date in results[loc]:
        done += 1
        try:
            entry = build_journal_entry(loc, entry_date)
            persist_journal_entry(entry)
            bal = "BAL" if entry["balanced"] else "UNBAL"
            status_char = "R" if entry["status"] == "ready" else "A"
            sys.stdout.write(f"\r[{done}/{total}] {loc} {entry_date} ${entry['total_credits']:>10,.2f} {bal} {status_char}")
            sys.stdout.flush()
        except Exception as e:
            errors.append(f"{loc} {entry_date}: {e}")
            sys.stdout.write(f"\r[{done}/{total}] {loc} {entry_date} ERROR: {e}\n")
            sys.stdout.flush()
        time.sleep(0.2)  # rate limit courtesy

print()
logger.info(f"Done. {done} entries processed, {len(errors)} errors.")
if errors:
    print("ERRORS:")
    for e in errors:
        print(f"  {e}")
