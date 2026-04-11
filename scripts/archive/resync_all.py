#!/usr/bin/env python3
"""Full historical re-sync with timezone fix. Run in background with nohup."""
import sys, os, logging
sys.path.insert(0, '/opt/rednun')
os.chdir('/opt/rednun')

from sync import DataSync
from data_store import get_connection
from datetime import date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/rednun/resync.log')
    ]
)
logger = logging.getLogger(__name__)

def main():
    conn = get_connection()
    
    # Clear ALL order sync logs so everything re-syncs
    conn.execute("DELETE FROM sync_log WHERE data_type = 'orders'")
    conn.execute("DELETE FROM order_items")
    conn.execute("DELETE FROM payments")
    conn.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    logger.info("Cleared all order data for re-sync")

    s = DataSync()
    start = date(2025, 1, 31)
    end = date.today()
    current = start
    total_days = (end - start).days + 1
    day_num = 0

    while current <= end:
        day_num += 1
        for loc in ['dennis', 'chatham']:
            try:
                s.sync_orders_for_date(loc, current)
            except Exception as e:
                logger.error(f"Failed {loc} {current}: {e}")
        if day_num % 10 == 0:
            logger.info(f"Progress: {day_num}/{total_days} days done")
        current += timedelta(days=1)

    logger.info(f"RE-SYNC COMPLETE: {total_days} days processed")

if __name__ == '__main__':
    main()
