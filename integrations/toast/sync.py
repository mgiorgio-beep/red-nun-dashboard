"""
Data Sync Service
Pulls data from the Toast API and stores it in the local database.
Supports initial historical load and daily incremental syncs.
"""

import logging
from datetime import datetime, timedelta, date
from toast_client import ToastAPIClient
from data_store import (
    init_db, store_orders, store_time_entries,
    store_employees, store_menus, get_connection,
)

logger = logging.getLogger(__name__)


class DataSync:
    """Manages data synchronization between Toast API and local database."""

    def __init__(self):
        self.client = ToastAPIClient()
        init_db()

    def _date_str(self, dt):
        """Format date as YYYYMMDD for internal tracking."""
        return dt.strftime("%Y%m%d")

    def _iso_str(self, dt):
        """Format date as ISO for Toast API."""
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    def _already_synced(self, location, data_type, business_date):
        """Check if we've already synced this data."""
        conn = get_connection()
        row = conn.execute("""
            SELECT id FROM sync_log
            WHERE location = ? AND data_type = ? AND business_date = ?
            AND status = 'complete'
        """, (location, data_type, business_date)).fetchone()
        conn.close()
        return row is not None

    def _log_sync(self, location, data_type, business_date, count, status="complete"):
        """Record a sync event."""
        conn = get_connection()
        conn.execute("""
            INSERT INTO sync_log (location, data_type, business_date,
                                  started_at, completed_at, record_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            location, data_type, business_date,
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            count, status,
        ))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Sync Operations
    # ------------------------------------------------------------------

    def sync_employees(self, location):
        """Sync employee data for a location."""
        logger.info(f"Syncing employees for {location}...")
        try:
            employees = self.client.get_employees(location)
            if not isinstance(employees, list):
                logger.error(f"Unexpected employees response type: {type(employees)}")
                return 0
            count = store_employees(location, employees)
            self._log_sync(location, "employees", "all", count)
            return count
        except Exception as e:
            logger.error(f"Failed to sync employees for {location}: {e}")
            self._log_sync(location, "employees", "all", 0, "error")
            return 0

    def sync_menus(self, location):
        """Sync menu data for a location."""
        logger.info(f"Syncing menus for {location}...")
        try:
            menus = self.client.get_menus(location)
            
            # Handle different response formats
            if isinstance(menus, list):
                # Filter to only dict items (skip any string GUIDs)
                menu_dicts = [m for m in menus if isinstance(m, dict)]
                if len(menu_dicts) < len(menus):
                    logger.warning(
                        f"Filtered {len(menus) - len(menu_dicts)} non-dict menu entries"
                    )
                count = store_menus(location, menu_dicts)
            elif isinstance(menus, dict):
                # Single menu object
                count = store_menus(location, [menus])
            else:
                logger.error(f"Unexpected menus response type: {type(menus)}")
                count = 0
            
            self._log_sync(location, "menus", "all", count)
            return count
        except Exception as e:
            logger.error(f"Failed to sync menus for {location}: {e}")
            self._log_sync(location, "menus", "all", 0, "error")
            return 0

    def sync_orders_for_date(self, location, dt):
        """Sync all orders for a specific business date.
        
        Args:
            dt: a datetime.date object
        """
        date_str = self._date_str(dt)

        if self._already_synced(location, "orders", date_str):
            logger.info(f"Orders already synced for {location} on {date_str}")
            return 0

        logger.info(f"Syncing orders for {location} on {date_str}...")
        try:
            orders = self.client.get_all_orders_for_date(location, dt)
            count = store_orders(location, date_str, orders)
            self._log_sync(location, "orders", date_str, count)
            return count
        except Exception as e:
            logger.error(f"Failed to sync orders for {location} on {date_str}: {e}")
            self._log_sync(location, "orders", date_str, 0, "error")
            return 0

    def sync_labor_for_date(self, location, dt):
        """Sync time entries for a specific business date.
        
        Args:
            dt: a datetime.date object
        """
        date_str = self._date_str(dt)

        if self._already_synced(location, "labor", date_str):
            logger.info(f"Labor already synced for {location} on {date_str}")
            return 0

        logger.info(f"Syncing labor for {location} on {date_str}...")
        try:
            # Use 4 AM to 4 AM window to match Toast closeout
            start_dt = datetime(dt.year, dt.month, dt.day, 4, 0, 0)
            end_dt = start_dt + timedelta(hours=24)
            
            start = self._iso_str(start_dt)
            end = self._iso_str(end_dt)
            entries = self.client.get_time_entries(location, start, end)

            if not isinstance(entries, list):
                logger.warning(f"Unexpected labor response type: {type(entries)}")
                entries = []

            # Get employee lookup for names
            conn = get_connection()
            rows = conn.execute(
                "SELECT guid, first_name, last_name FROM employees WHERE location = ?",
                (location,)
            ).fetchall()
            conn.close()
            emp_map = {r["guid"]: dict(r) for r in rows}

            count = store_time_entries(location, entries, emp_map)
            self._log_sync(location, "labor", date_str, count)
            return count
        except Exception as e:
            logger.error(f"Failed to sync labor for {location} on {date_str}: {e}")
            self._log_sync(location, "labor", date_str, 0, "error")
            return 0

    # ------------------------------------------------------------------
    # Bulk / Scheduled Sync
    # ------------------------------------------------------------------

    def initial_load(self, weeks_back=12):
        """
        Perform initial historical data load.
        Toast recommends 12 weeks of historical data.
        """
        logger.info(f"Starting initial load ({weeks_back} weeks of history)...")

        for location in ["dennis", "chatham"]:
            # Sync reference data first
            self.sync_employees(location)
            self.sync_menus(location)

            # Sync historical orders and labor
            today = date.today()
            start_date = today - timedelta(weeks=weeks_back)

            current = start_date
            while current <= today:
                self.sync_orders_for_date(location, current)
                self.sync_labor_for_date(location, current)
                current += timedelta(days=1)

        logger.info("Initial load complete!")

    def daily_sync(self):
        """
        Daily sync job — pulls yesterday's data (to ensure closeout is done)
        and re-syncs today's partial data.
        """
        logger.info("Running daily sync...")
        yesterday = date.today() - timedelta(days=1)
        today = date.today()

        for location in ["dennis", "chatham"]:
            # Refresh employees
            self.sync_employees(location)

            # Sync yesterday (finalized) and today (partial/live)
            self.sync_orders_for_date(location, yesterday)
            self.sync_labor_for_date(location, yesterday)

            # For today, force re-sync (delete existing sync log)
            conn = get_connection()
            conn.execute("""
                DELETE FROM sync_log
                WHERE location = ? AND business_date = ? AND data_type IN ('orders', 'labor')
            """, (location, self._date_str(today)))
            conn.commit()
            conn.close()

            self.sync_orders_for_date(location, today)
            self.sync_labor_for_date(location, today)

        logger.info("Daily sync complete!")


# ------------------------------------------------------------------
# CLI Entry Point
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sync = DataSync()

    if len(sys.argv) > 1 and sys.argv[1] == "initial":
        weeks = int(sys.argv[2]) if len(sys.argv) > 2 else 12
        sync.initial_load(weeks_back=weeks)
    else:
        sync.daily_sync()
