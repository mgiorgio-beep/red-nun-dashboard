#!/usr/bin/env python3
"""
Daily cron: sync yesterday's Toast orders and generate sales journal entry.
Runs at 9am — after close (4am cutoff) and Toast data is settled.
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, '/opt/red-nun-dashboard')
from integrations.toast.sync import DataSync
from reports.sales_journal import build_journal_entry, persist_journal_entry
from integrations.toast.data_store import get_connection
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

yesterday = date.today() - timedelta(days=1)
ds = yesterday.strftime('%Y-%m-%d')
td = yesterday.strftime('%Y%m%d')

logger.info('Daily journal run for %s', ds)

# Sync orders
try:
    sync = DataSync()
    count = sync.sync_orders_for_date('chatham', yesterday)
    logger.info('Synced %d orders for chatham/%s', count, ds)
except Exception as e:
    logger.error('Sync failed: %s', e)
    sys.exit(1)

# Generate journal entry (skip if already posted to QBO)
conn = get_connection()
existing = conn.execute(
    "SELECT id, status FROM qb_journal_entries WHERE location='chatham' AND entry_date=?", (ds,)
).fetchone()
conn.close()

if existing and existing['status'] == 'posted':
    logger.info('Entry for %s already posted to QBO, skipping.', ds)
    sys.exit(0)

if existing:
    # Regenerate (overwrite needs_attention/ready)
    conn2 = get_connection()
    conn2.execute("DELETE FROM qb_journal_line_items WHERE entry_id=?", (existing['id'],))
    conn2.execute("DELETE FROM qb_journal_entries WHERE id=?", (existing['id'],))
    conn2.commit()
    conn2.close()

try:
    entry = build_journal_entry('chatham', ds)
    if entry['total_debits'] == 0:
        logger.info('No sales data for %s (closed?), skipping.', ds)
        sys.exit(0)
    persist_journal_entry(entry)
    logger.info('Journal entry created: %s balanced=%s debits=%.2f unmapped=%d',
                ds, entry['balanced'], entry['total_debits'],
                sum(1 for li in entry['line_items'] if not li['mapped']))
except Exception as e:
    logger.error('Journal build failed: %s', e)
    sys.exit(1)
