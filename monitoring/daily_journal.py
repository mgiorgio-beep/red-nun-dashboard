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

logger.info('Daily journal run for %s', ds)

for location in ('chatham', 'dennis'):
    # Sync orders
    try:
        sync = DataSync()
        count = sync.sync_orders_for_date(location, yesterday)
        logger.info('Synced %d orders for %s/%s', count, location, ds)
    except Exception as e:
        logger.error('Sync failed for %s: %s', location, e)
        continue

    # Generate journal entry (skip if already posted to QBO)
    conn = get_connection()
    existing = conn.execute(
        "SELECT id, status FROM qb_journal_entries WHERE location=? AND entry_date=?",
        (location, ds)
    ).fetchone()
    conn.close()

    if existing and existing['status'] == 'posted':
        logger.info('%s entry for %s already posted to QBO, skipping.', location, ds)
        continue

    if existing:
        # Regenerate (overwrite needs_attention/ready)
        conn2 = get_connection()
        conn2.execute("DELETE FROM qb_journal_line_items WHERE entry_id=?", (existing['id'],))
        conn2.execute("DELETE FROM qb_journal_entries WHERE id=?", (existing['id'],))
        conn2.commit()
        conn2.close()

    try:
        entry = build_journal_entry(location, ds)
        if entry['total_debits'] == 0:
            logger.info('No sales data for %s/%s (closed?), skipping.', location, ds)
            continue
        persist_journal_entry(entry)
        logger.info('%s journal entry created: %s balanced=%s debits=%.2f unmapped=%d',
                    location, ds, entry['balanced'], entry['total_debits'],
                    sum(1 for li in entry['line_items'] if not li['mapped']))
    except Exception as e:
        logger.error('Journal build failed for %s: %s', location, e)
