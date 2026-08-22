#!/usr/bin/env python3
"""
Daily cron: sync yesterday's Toast orders and generate sales journal entry.
Runs at 9am — after close (4am cutoff) and Toast data is settled.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, '/opt/red-nun-dashboard')

# Opt-in full re-pull of yesterday's orders (see the FORCE_RESYNC block below).
FORCE_RESYNC = ('--force-resync' in sys.argv) or os.environ.get('FORCE_RESYNC') == '1'
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
        # WHY THIS EXISTS: the live 10-min Toast sync can freeze mid-evening (seen
        # 2026-08-11, froze ~23:50), so Late Night service (runs to 1am) and
        # after-midnight tabs never get captured. Clearing the sync_log row
        # defeats _already_synced and forces a full re-pull; store_orders upserts
        # by GUID, so it is idempotent and safe to repeat.
        #
        # It is GATED because it was originally left running unconditionally,
        # which re-pulled every order for both locations from Toast every single
        # night. Run it deliberately when a day looks short:
        #     daily_journal.py --force-resync      (or FORCE_RESYNC=1)
        if FORCE_RESYNC:
            _c = get_connection()
            _c.execute("DELETE FROM sync_log WHERE location=? AND business_date=? AND data_type='orders'",
                       (location, sync._date_str(yesterday)))
            _c.commit(); _c.close()
            logger.info('FORCE_RESYNC: cleared sync_log for %s/%s — full re-pull', location, ds)
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
