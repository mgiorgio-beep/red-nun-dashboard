"""
Data Storage Layer
Stores Toast API data in a local SQLite database for fast querying and
historical analysis without repeatedly hitting the API.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "toast_data.db")


def get_connection():
    """Get a SQLite connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.executescript("""
        -- Raw order data
        CREATE TABLE IF NOT EXISTS orders (
            guid TEXT PRIMARY KEY,
            location TEXT NOT NULL,
            business_date TEXT NOT NULL,
            opened_at TEXT,
            closed_at TEXT,
            dining_option TEXT,
            server_guid TEXT,
            server_name TEXT,
            check_count INTEGER DEFAULT 0,
            total_amount REAL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            tip_amount REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            void_amount REAL DEFAULT 0,
            raw_json TEXT,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_orders_location_date
            ON orders(location, business_date);
        CREATE INDEX IF NOT EXISTS idx_orders_server
            ON orders(server_guid);

        -- Individual check-level selections (line items)
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_guid TEXT NOT NULL,
            check_guid TEXT,
            location TEXT NOT NULL,
            business_date TEXT NOT NULL,
            item_guid TEXT,
            item_name TEXT,
            menu_group TEXT,
            category TEXT,
            quantity REAL DEFAULT 1,
            price REAL DEFAULT 0,
            discount REAL DEFAULT 0,
            voided INTEGER DEFAULT 0,
            tax REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_items_location_date
            ON order_items(location, business_date);
        CREATE INDEX IF NOT EXISTS idx_items_category
            ON order_items(category);
        CREATE INDEX IF NOT EXISTS idx_items_order
            ON order_items(order_guid);

        -- Payment records
        CREATE TABLE IF NOT EXISTS payments (
            guid TEXT PRIMARY KEY,
            order_guid TEXT NOT NULL,
            location TEXT NOT NULL,
            business_date TEXT NOT NULL,
            payment_type TEXT,
            card_type TEXT,
            amount REAL DEFAULT 0,
            tip_amount REAL DEFAULT 0,
            refund_amount REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_payments_location_date
            ON payments(location, business_date);
        CREATE INDEX IF NOT EXISTS idx_payments_order
            ON payments(order_guid);

        -- Labor / time entries
        CREATE TABLE IF NOT EXISTS time_entries (
            guid TEXT PRIMARY KEY,
            location TEXT NOT NULL,
            employee_guid TEXT NOT NULL,
            employee_name TEXT,
            job_guid TEXT,
            job_title TEXT,
            business_date TEXT,
            clock_in TEXT,
            clock_out TEXT,
            regular_hours REAL DEFAULT 0,
            overtime_hours REAL DEFAULT 0,
            hourly_wage REAL DEFAULT 0,
            total_pay REAL DEFAULT 0,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_labor_location_date
            ON time_entries(location, business_date);
        CREATE INDEX IF NOT EXISTS idx_labor_employee
            ON time_entries(employee_guid);

        -- Employee lookup
        CREATE TABLE IF NOT EXISTS employees (
            guid TEXT PRIMARY KEY,
            location TEXT NOT NULL,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            job_title TEXT,
            wage REAL,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Menu item lookup (for category mapping)
        CREATE TABLE IF NOT EXISTS menu_items (
            guid TEXT PRIMARY KEY,
            location TEXT NOT NULL,
            name TEXT,
            menu_name TEXT,
            menu_group_name TEXT,
            category TEXT,
            description TEXT DEFAULT '',
            price REAL DEFAULT 0,
            cost REAL DEFAULT 0,
            synced_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Category mapping for pour cost analysis
        -- (user-configurable: maps menu groups to liquor/beer/wine/food)
        CREATE TABLE IF NOT EXISTS category_map (
            menu_group_name TEXT PRIMARY KEY,
            pour_category TEXT NOT NULL
                CHECK (pour_category IN (
                    'food', 'well_liquor', 'premium_liquor',
                    'draft_beer', 'bottled_beer', 'wine', 'cocktails',
                    'non_alcoholic', 'other'
                ))
        );

        -- Sync log to track what's been fetched
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            data_type TEXT NOT NULL,
            business_date TEXT,
            started_at TEXT,
            completed_at TEXT,
            record_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending'
        );

        CREATE INDEX IF NOT EXISTS idx_sync_log
            ON sync_log(location, data_type, business_date);
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialized")


# ------------------------------------------------------------------
# Order Storage
# ------------------------------------------------------------------

def store_orders(location, business_date, orders):
    """Parse and store orders from the Toast API response."""
    conn = get_connection()
    cursor = conn.cursor()
    count = 0

    for order in orders:
        try:
            guid = order.get("guid", "")
            if not guid:
                continue

            # Extract server info
            server = order.get("server", {}) or {}
            server_guid = server.get("guid", "")

            # Calculate order totals from checks
            checks = order.get("checks", []) or []
            total_amount = 0
            tax_amount = 0
            tip_amount = 0
            discount_amount = 0

            # Collect line items and payments to insert AFTER the order
            items_to_insert = []
            payments_to_insert = []

            for check in checks:
                total_amount += check.get("totalAmount", 0) or 0
                tax_amount += check.get("taxAmount", 0) or 0

                # Collect payments
                for payment in check.get("payments", []) or []:
                    tip_amount += payment.get("tipAmount", 0) or 0

                    pay_guid = payment.get("guid", "")
                    if pay_guid:
                        payments_to_insert.append((
                            pay_guid, guid, location, business_date,
                            payment.get("type", ""),
                            payment.get("cardType", ""),
                            payment.get("amount", 0) or 0,
                            payment.get("tipAmount", 0) or 0,
                            payment.get("refundAmount", 0) or 0,
                        ))

                # Extract applied discounts
                for discount in check.get("appliedDiscounts", []) or []:
                    discount_amount += discount.get("discountAmount", 0) or 0

                # Collect line items (selections)
                for selection in check.get("selections", []) or []:
                    item_ref = selection.get("item", {}) or {}
                    item_guid = item_ref.get("guid", "") if isinstance(item_ref, dict) else ""

                    items_to_insert.append((
                        guid,
                        check.get("guid", ""),
                        location,
                        business_date,
                        item_guid,
                        selection.get("displayName", ""),
                        selection.get("quantity", 1) or 1,
                        selection.get("price", 0) or 0,
                        selection.get("appliedDiscountAmount", 0) or 0,
                        1 if selection.get("voided") else 0,
                        selection.get("taxAmount", 0) or 0,
                    ))

            # Calculate net_amount (matches Toast "Net Sales" exactly)
            # = sum of preDiscountPrice for all items, minus all discounts (item + check level)
            net_amount_val = 0
            if not order.get("deleted") and not order.get("voided"):
                for chk in checks:
                    if chk.get("voided"): continue
                    # Sum item pre-discount prices
                    for sel in (chk.get("selections") or []):
                        if sel.get("voided"): continue
                        net_amount_val += (sel.get("preDiscountPrice", 0) or 0)
                        # Subtract item-level discounts
                        for disc in (sel.get("appliedDiscounts") or []):
                            net_amount_val -= abs(disc.get("discountAmount", 0) or 0)
                    # Subtract check-level discounts (comps, manager discounts)
                    for disc in (chk.get("appliedDiscounts") or []):
                        net_amount_val -= abs(disc.get("discountAmount", 0) or 0)
            net_amount_val = round(net_amount_val, 2)

            # Dining option
            dining_opt = order.get("diningOption", {}) or {}
            dining_option = dining_opt.get("guid", "") if isinstance(dining_opt, dict) else ""

            # INSERT ORDER FIRST
            # Fix late-night orders: if opened before 5AM ET (10AM UTC), assign to previous day
            # If order opened before 4AM ET on this business_date, it belongs to previous day
            order_business_date = business_date
            opened_str = order.get("openedDate", "")
            if opened_str and business_date:
                try:
                    from zoneinfo import ZoneInfo
                    from datetime import datetime as dt2, timedelta as td2
                    eastern = ZoneInfo("America/New_York")
                    opened_utc = dt2.strptime(opened_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
                    opened_et = opened_utc.astimezone(eastern)
                    biz_date = dt2.strptime(business_date, "%Y%m%d")
                    biz_day_start_et = dt2(biz_date.year, biz_date.month, biz_date.day, 4, 0, 0, tzinfo=eastern)
                    if opened_et < biz_day_start_et:
                        prev = biz_date - td2(days=1)
                        order_business_date = prev.strftime("%Y%m%d")
                except Exception:
                    pass
            cursor.execute("""
                INSERT OR REPLACE INTO orders
                (guid, location, business_date, opened_at, closed_at,
                 dining_option, server_guid, check_count,
                 total_amount, tax_amount, tip_amount, discount_amount,
                 net_amount, raw_json, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                guid, location, order_business_date,
                order.get("openedDate", ""),
                order.get("closedDate", ""),
                dining_option,
                server_guid,
                len(checks),
                total_amount, tax_amount, tip_amount, discount_amount,
                net_amount_val, json.dumps(order),
                datetime.now().isoformat(),
            ))

            # Deduplicate payments: Toast sometimes returns the same charge twice with
            # different GUIDs. Drop extras where sum of payments would exceed order total.
            deduped = []
            running_total = 0.0
            for pay_data in payments_to_insert:
                pay_amt = float(pay_data[6] or 0)
                if total_amount > 0 and running_total + pay_amt > total_amount + 0.01:
                    logger.warning("Skipping duplicate payment guid=%s amt=%s (order total=%.2f, running=%.2f)",
                                   pay_data[0], pay_amt, total_amount, running_total)
                    continue
                running_total += pay_amt
                deduped.append(pay_data)
            payments_to_insert = deduped

            # THEN insert payments (use the adjusted business date)
            for pay_data in payments_to_insert:
                # Update the business_date in payment data to match the order's adjusted date
                pay_data_fixed = (pay_data[0], pay_data[1], pay_data[2], order_business_date, pay_data[4], pay_data[5], pay_data[6], pay_data[7], pay_data[8])
                cursor.execute("""
                    INSERT OR REPLACE INTO payments
                    (guid, order_guid, location, business_date,
                     payment_type, card_type, amount, tip_amount, refund_amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, pay_data_fixed)

            # THEN insert line items - delete old ones first to avoid duplicates
            cursor.execute("DELETE FROM order_items WHERE order_guid = ?", (guid,))
            for item_data in items_to_insert:
                # Update the business_date in item data to match the order's adjusted date
                item_data_fixed = (item_data[0], item_data[1], item_data[2], order_business_date, item_data[4], item_data[5], item_data[6], item_data[7], item_data[8], item_data[9], item_data[10])
                cursor.execute("""
                    INSERT INTO order_items
                    (order_guid, check_guid, location, business_date,
                     item_guid, item_name, quantity, price, discount, voided, tax)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, item_data_fixed)

            count += 1

        except Exception as e:
            logger.error(f"Error storing order {order.get('guid', '?')}: {e}")
            continue

    conn.commit()
    conn.close()
    logger.info(f"Stored {count} orders for {location} on {business_date}")
    return count


# ------------------------------------------------------------------
# Labor Storage
# ------------------------------------------------------------------

def store_time_entries(location, time_entries, employees_map=None):
    """Store time entries from the labor API."""
    conn = get_connection()
    cursor = conn.cursor()
    count = 0
    employees_map = employees_map or {}

    for entry in time_entries:
        try:
            guid = entry.get("guid", "")
            if not guid:
                continue

            emp_ref = entry.get("employeeReference", {}) or {}
            emp_guid = emp_ref.get("guid", "") if isinstance(emp_ref, dict) else ""
            emp_info = employees_map.get(emp_guid, {})
            emp_name = f"{emp_info.get('first_name', '')} {emp_info.get('last_name', '')}".strip()

            job_ref = entry.get("jobReference", {}) or {}
            job_guid = job_ref.get("guid", "") if isinstance(job_ref, dict) else ""

            clock_in = entry.get("inDate", "")
            clock_out = entry.get("outDate", "")

            regular_hours = entry.get("regularHours", 0) or 0
            overtime_hours = entry.get("overtimeHours", 0) or 0
            hourly_wage = entry.get("hourlyWage", 0) or 0

            total_pay = (regular_hours + overtime_hours * 1.5) * hourly_wage

            cursor.execute("""
                INSERT OR REPLACE INTO time_entries
                (guid, location, employee_guid, employee_name, job_guid,
                 business_date, clock_in, clock_out,
                 regular_hours, overtime_hours, hourly_wage, total_pay, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                guid, location, emp_guid, emp_name, job_guid,
                entry.get("businessDate", ""),
                clock_in, clock_out,
                regular_hours, overtime_hours, hourly_wage, total_pay,
                datetime.now().isoformat(),
            ))
            count += 1

        except Exception as e:
            logger.error(f"Error storing time entry {entry.get('guid', '?')}: {e}")
            continue

    conn.commit()
    conn.close()
    logger.info(f"Stored {count} time entries for {location}")
    return count


# ------------------------------------------------------------------
# Employee Storage
# ------------------------------------------------------------------

def store_employees(location, employees):
    """Store employee records."""
    conn = get_connection()
    cursor = conn.cursor()
    count = 0

    for emp in employees:
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO employees
                (guid, location, first_name, last_name, email, wage, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                emp.get("guid", ""),
                location,
                emp.get("firstName", ""),
                emp.get("lastName", ""),
                emp.get("email", ""),
                emp.get("wageOverrides", [{}])[0].get("wage", 0)
                    if emp.get("wageOverrides") else 0,
                datetime.now().isoformat(),
            ))
            count += 1
        except Exception as e:
            logger.error(f"Error storing employee: {e}")
            continue

    conn.commit()
    conn.close()
    logger.info(f"Stored {count} employees for {location}")
    return count


# ------------------------------------------------------------------
# Menu Storage
# ------------------------------------------------------------------

def store_menus(location, menus_data):
    """Parse and store menu items with category info for pour cost mapping.
    Accepts either the raw Toast API response (dict with 'menus' key)
    or a plain list of menu dicts."""
    conn = get_connection()
    cursor = conn.cursor()
    count = 0

    # Handle both raw API response (dict) and pre-extracted list
    if isinstance(menus_data, dict):
        menus_list = menus_data.get("menus", [])
    else:
        menus_list = menus_data

    for menu in menus_list:
        menu_name = menu.get("name", "")

        # Toast V2 API uses "menuGroups", fall back to "groups" for compat
        groups = menu.get("menuGroups") or menu.get("groups") or []
        for group in groups:
            group_name = group.get("name", "")

            # Toast V2 API uses "menuItems", fall back to "items" for compat
            items = group.get("menuItems") or group.get("items") or []
            for item in items:
                try:
                    cursor.execute("""
                        INSERT OR REPLACE INTO menu_items
                        (guid, location, name, menu_name, menu_group_name,
                         description, price, synced_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        item.get("guid", ""),
                        location,
                        item.get("name", ""),
                        menu_name,
                        group_name,
                        item.get("description", "") or "",
                        item.get("price", 0) or 0,
                        datetime.now().isoformat(),
                    ))
                    count += 1
                except Exception as e:
                    logger.error(f"Error storing menu item: {e}")
                    continue

    conn.commit()
    conn.close()
    logger.info(f"Stored {count} menu items for {location}")
    return count
