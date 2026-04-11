"""
Analytics Engine
Computes revenue, labor, pour cost, and sales mix metrics from stored Toast data.
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from data_store import get_connection

logger = logging.getLogger(__name__)


# ==================================================================
# REVENUE & SALES ANALYTICS
# ==================================================================

def get_daily_revenue(location=None, start_date=None, end_date=None):
    """
    Get daily revenue broken down by location.
    
    Returns:
        List of dicts: [{date, location, net_revenue, tax, tips, discounts,
                         order_count, avg_check}, ...]
    """
    conn = get_connection()
    where_clauses = ["json_extract(raw_json, '$.deleted') != 1", "json_extract(raw_json, '$.voided') != 1"]
    params = []

    if location:
        where_clauses.append("location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    query = f"""
        SELECT
            business_date,
            location,
            SUM(total_amount - tax_amount - tip_amount) as net_revenue,
            SUM(tax_amount) as tax,
            SUM(tip_amount) as tips,
            SUM(discount_amount) as discounts,
            COUNT(*) as order_count,
            ROUND(AVG(total_amount), 2) as avg_check
        FROM orders
        {where_sql}
        GROUP BY business_date, location
        ORDER BY business_date
    """

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_revenue_by_daypart(location=None, start_date=None, end_date=None):
    """
    Break down revenue by daypart (lunch, happy hour, dinner, late night).
    Uses opened_at timestamp to determine daypart.
    
    Daypart definitions:
        Lunch:      11:00 - 15:00
        Happy Hour: 15:00 - 17:30
        Dinner:     17:30 - 22:00
        Late Night: 22:00 - close
    """
    conn = get_connection()
    where_clauses = ["json_extract(raw_json, '$.deleted') != 1", "json_extract(raw_json, '$.voided') != 1"]
    params = []

    if location:
        where_clauses.append("location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    query = f"""
        SELECT
            CASE
                WHEN CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) >= 22
                     OR CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) < 4
                    THEN 'Late Night'
                WHEN CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) < 16
                    THEN 'Lunch'
                ELSE 'Dinner'
            END as daypart,
            location,
            SUM(total_amount - tax_amount - tip_amount) as revenue,
            COUNT(*) as order_count,
            ROUND(AVG(total_amount - tax_amount - tip_amount), 2) as avg_check
        FROM orders
        {where_sql}
        AND opened_at IS NOT NULL AND opened_at != ''
        GROUP BY daypart, location
        ORDER BY
            CASE daypart
                WHEN 'Lunch' THEN 1
                WHEN 'Happy Hour' THEN 2
                WHEN 'Dinner' THEN 3
                WHEN 'Late Night' THEN 4
            END
    """

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_sales_mix(location=None, start_date=None, end_date=None):
    """
    Get sales mix by category using item name pattern matching.
    """
    conn = get_connection()
    where_clauses = ["voided = 0", "price > 0"]
    params = []
    if location:
        where_clauses.append("location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("business_date <= ?")
        params.append(end_date)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    query = f"""
        SELECT item_name, SUM(price * quantity) as revenue, SUM(quantity) as qty
        FROM order_items {where_sql}
        GROUP BY item_name
        ORDER BY revenue DESC
    """
    rows = conn.execute(query, params).fetchall()
    conn.close()

    BEER = ['lager','ipa','ale','stout','pilsner','kolsch','hef',
            'guinness','blue moon','sam adams','bud light','coors',
            'corona','stella','heineken','devils purse','harpoon',
            'cisco','night shift','cape cod beer','draft','seltzer',
            'truly','white claw','bucket']
    WINE = ['wine','cab','merlot','pinot','chardonnay','chard',
            'sauvignon','riesling','prosecco','rose','ros\xe9',
            'kim crawford','meiomi','josh','kendall','barefoot',
            'decoy','la crema','glass of','bottle of']
    LIQUOR = ['margarita','martini','cocktail','mojito','old fashioned',
              'manhattan','negroni','daiquiri','cosmopolitan','gimlet',
              'sour','mule','spritz','bloody mary','espresso martini',
              'rum','vodka','whiskey','bourbon','tequila','gin',
              'shot','on the rocks','neat','mixed drink']
    NA = ['soda','coffee','tea','juice','water','lemonade',
          'sprite','coke','pepsi','ginger ale','tonic','red bull',
          'arnold palmer','shirley temple','mocktail','n/a','non-alc']

    categories = {}
    for row in rows:
        name = (row["item_name"] or "").lower()
        rev = row["revenue"] or 0
        qty = row["qty"] or 0
        cat = "Food"
        for w in BEER:
            if w in name:
                cat = "Beer"; break
        if cat == "Food":
            for w in WINE:
                if w in name:
                    cat = "Wine"; break
        if cat == "Food":
            for w in LIQUOR:
                if w in name:
                    cat = "Liquor"; break
        if cat == "Food":
            for w in NA:
                if w in name:
                    cat = "NA Bev"; break
        if cat not in categories:
            categories[cat] = {"category": cat, "revenue": 0, "item_count": 0}
        categories[cat]["revenue"] += rev
        categories[cat]["item_count"] += qty

    results = sorted(categories.values(), key=lambda x: x["revenue"], reverse=True)
    total_revenue = sum(r["revenue"] for r in results) if results else 1
    for r in results:
        r["pct_of_total"] = round((r["revenue"] / total_revenue) * 100, 1)
    return results

def get_labor_summary(location=None, start_date=None, end_date=None):
    """
    Get labor cost summary.
    
    Returns:
        Dict with total_hours, total_pay, overtime_hours, overtime_pay,
        labor_pct (if revenue data is available)
    """
    conn = get_connection()
    # time_entries has no raw_json — build a clean WHERE for it
    where_clauses = []
    params = []

    if location:
        where_clauses.append("location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    labor_query = f"""
        SELECT
            SUM(regular_hours) as total_regular_hours,
            SUM(overtime_hours) as total_overtime_hours,
            SUM(regular_hours + overtime_hours) as total_hours,
            SUM(total_pay) as total_labor_cost,
            COUNT(DISTINCT employee_guid) as unique_employees
        FROM time_entries
        {where_sql}
    """

    labor = dict(conn.execute(labor_query, params).fetchone())

    # orders needs the voided/deleted filter prepended
    orders_extra = ["json_extract(raw_json, '$.deleted') != 1", "json_extract(raw_json, '$.voided') != 1"]
    orders_where = "WHERE " + " AND ".join(orders_extra + where_clauses)

    # Get matching revenue for labor % calculation
    rev_query = f"""
        SELECT SUM(total_amount) as total_revenue
        FROM orders
        {orders_where}
    """
    rev = dict(conn.execute(rev_query, params).fetchone())

    total_revenue = rev.get("total_revenue") or 0
    total_labor = labor.get("total_labor_cost") or 0

    # Add salaried employees (not tracked in 7shifts time entries)
    SALARIES = {
        "dennis": 880.0 / 7,   # $880/week = $125.71/day
        "chatham": 1375.0 / 7, # $1375/week = $196.43/day
    }
    day_query = f"""
        SELECT location, COUNT(DISTINCT business_date) as days
        FROM orders
        {orders_where}
        GROUP BY location
    """
    day_rows = conn.execute(day_query, params).fetchall()
    salary_cost = 0
    for row in day_rows:
        loc = row[0]
        days = row[1]
        daily_rate = SALARIES.get(loc, 0)
        salary_cost += daily_rate * days
    total_labor += salary_cost
    labor["total_labor_cost"] = round(total_labor, 2)
    labor["salary_cost"] = round(salary_cost, 2)

    labor["total_revenue"] = total_revenue
    labor["labor_pct"] = round(
        (total_labor / total_revenue * 100) if total_revenue > 0 else 0, 1
    )

    conn.close()
    return labor


def get_daily_labor(location=None, start_date=None, end_date=None):
    """Get labor cost by day, useful for trend charts."""
    conn = get_connection()
    # time_entries has no raw_json — the orders subquery has its own filter
    where_clauses = []
    params = []

    if location:
        where_clauses.append("t.location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("t.business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("t.business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    query = f"""
        SELECT
            t.business_date,
            t.location,
            SUM(t.total_pay) as labor_cost,
            SUM(t.regular_hours + t.overtime_hours) as total_hours,
            COALESCE(o.revenue, 0) as revenue,
            CASE
                WHEN COALESCE(o.revenue, 0) > 0
                THEN ROUND(SUM(t.total_pay) / o.revenue * 100, 1)
                ELSE 0
            END as labor_pct
        FROM time_entries t
        LEFT JOIN (
            SELECT business_date, location, SUM(total_amount) as revenue
            FROM orders
            WHERE json_extract(raw_json, '$.deleted') != 1
              AND json_extract(raw_json, '$.voided') != 1
            GROUP BY business_date, location
        ) o ON t.business_date = o.business_date AND t.location = o.location
        {where_sql}
        GROUP BY t.business_date, t.location
        ORDER BY t.business_date
    """

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_labor_by_role(location=None, start_date=None, end_date=None):
    """
    Break down labor cost by job role (FOH, BOH, Management).
    Uses job_title to categorize.
    """
    conn = get_connection()
    where_clauses = []
    params = []

    if location:
        where_clauses.append("location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    query = f"""
        SELECT
            job_title,
            SUM(total_pay) as labor_cost,
            SUM(regular_hours + overtime_hours) as total_hours,
            COUNT(DISTINCT employee_guid) as employee_count
        FROM time_entries
        {where_sql}
        GROUP BY job_title
        ORDER BY labor_cost DESC
    """

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ==================================================================
# SERVER / BARTENDER PERFORMANCE
# ==================================================================

def get_server_performance(location=None, start_date=None, end_date=None, limit=10):
    """
    Rank servers by sales performance.
    
    Returns:
        List of dicts: [{server_guid, server_name, total_sales, order_count,
                         avg_check, total_tips}, ...]
    """
    conn = get_connection()
    where_clauses = ["json_extract(o.raw_json, '$.deleted') != 1", "json_extract(o.raw_json, '$.voided') != 1"]
    params = []

    if location:
        where_clauses.append("o.location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("o.business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("o.business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    query = f"""
        SELECT
            o.server_guid,
            COALESCE(e.first_name || ' ' || SUBSTR(e.last_name, 1, 1) || '.', 
                     'Unknown') as server_name,
            o.location,
            SUM(o.total_amount) as total_sales,
            COUNT(*) as order_count,
            ROUND(AVG(o.total_amount), 2) as avg_check,
            SUM(o.tip_amount) as total_tips,
            ROUND(AVG(o.tip_amount), 2) as avg_tip
        FROM orders o
        LEFT JOIN employees e ON o.server_guid = e.guid
        {where_sql}
        AND o.server_guid IS NOT NULL AND o.server_guid != '' AND (e.wage IS NULL OR e.wage = 0) AND e.first_name NOT LIKE 'Host%'
        GROUP BY o.server_guid
        ORDER BY total_sales DESC
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ==================================================================
# POUR COST ANALYTICS
# ==================================================================

def get_pour_cost_by_category(location=None, start_date=None, end_date=None):
    """
    Calculate pour cost by beverage category.
    
    This compares the cost of goods (from menu item cost data) against
    the revenue generated. Requires menu items to have cost data populated
    and category_map to be configured.
    
    Returns:
        List of dicts: [{category, revenue, cost, pour_cost_pct, item_count}, ...]
    """
    conn = get_connection()
    where_clauses = ["oi.voided = 0"]
    params = []

    if location:
        where_clauses.append("oi.location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("oi.business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("oi.business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    query = f"""
        SELECT
            COALESCE(cm.pour_category, 'uncategorized') as category,
            SUM(oi.price * oi.quantity) as revenue,
            SUM(COALESCE(mi.cost, 0) * oi.quantity) as cost,
            SUM(oi.quantity) as item_count,
            CASE
                WHEN SUM(oi.price * oi.quantity) > 0
                THEN ROUND(
                    SUM(COALESCE(mi.cost, 0) * oi.quantity) /
                    SUM(oi.price * oi.quantity) * 100, 1
                )
                ELSE 0
            END as pour_cost_pct
        FROM order_items oi
        LEFT JOIN menu_items mi ON oi.item_guid = mi.guid
        LEFT JOIN category_map cm ON mi.menu_group_name = cm.menu_group_name
        {where_sql}
        AND cm.pour_category NOT IN ('food', 'non_alcoholic')
        GROUP BY cm.pour_category
        ORDER BY revenue DESC
    """

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_bartender_pour_variance(location=None, start_date=None, end_date=None):
    """
    Compare pour cost by bartender to identify over-pouring.
    
    Returns:
        List of dicts per bartender with their beverage sales and
        theoretical vs actual cost.
    """
    conn = get_connection()
    where_clauses = ["oi.voided = 0"]
    params = []

    if location:
        where_clauses.append("o.location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("o.business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("o.business_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    query = f"""
        SELECT
            o.server_guid,
            COALESCE(e.first_name || ' ' || SUBSTR(e.last_name, 1, 1) || '.', 
                     'Unknown') as bartender_name,
            SUM(oi.price * oi.quantity) as bev_revenue,
            SUM(COALESCE(mi.cost, 0) * oi.quantity) as bev_cost,
            SUM(oi.quantity) as drink_count,
            CASE
                WHEN SUM(oi.price * oi.quantity) > 0
                THEN ROUND(
                    SUM(COALESCE(mi.cost, 0) * oi.quantity) /
                    SUM(oi.price * oi.quantity) * 100, 1
                )
                ELSE 0
            END as pour_cost_pct
        FROM order_items oi
        JOIN orders o ON oi.order_guid = o.guid
        LEFT JOIN menu_items mi ON oi.item_guid = mi.guid
        LEFT JOIN category_map cm ON mi.menu_group_name = cm.menu_group_name
        LEFT JOIN employees e ON o.server_guid = e.guid
        {where_sql}
        AND cm.pour_category NOT IN ('food', 'non_alcoholic', 'other')
        GROUP BY o.server_guid
        HAVING bev_revenue > 0
        ORDER BY pour_cost_pct DESC
    """

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ==================================================================
# WEEKLY SUMMARY (matches your existing report format)
# ==================================================================

def get_price_movers(location=None, limit=5):
    """
    Get top price increases and decreases from invoice history.
    Compares current price to previous invoice price for each product.

    Returns:
        Dict with 'increases' and 'decreases' lists, each containing:
        [{product_name, current_price, previous_price, change_pct, current_date, previous_date}, ...]
    """
    conn = get_connection()

    query = """
    WITH combined AS (
        -- Scanned invoice items: map to canonical name where available
        SELECT sii.product_name,
               sii.unit_price,
               si.invoice_date,
               COALESCE(pnm.canonical_name, sii.product_name) AS group_name
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON sii.invoice_id = si.id
        LEFT JOIN product_name_map pnm
            ON LOWER(pnm.source_name) = LOWER(sii.product_name)
           AND pnm.source_table = 'scanned_invoice_items'
           AND pnm.canonical_name IS NOT NULL
        WHERE si.status IN ('confirmed', 'pending') AND sii.unit_price > 0
        UNION ALL
        -- ME invoice items: already in canonical form
        SELECT mii.product_name,
               mii.unit_price,
               mi.invoice_date,
               mii.product_name AS group_name
        FROM me_invoice_items mii
        JOIN me_invoices mi ON mii.order_id = mi.order_id
        WHERE mii.unit_price > 0
    ),
    price_history AS (
        SELECT group_name, unit_price, invoice_date,
               ROW_NUMBER() OVER (PARTITION BY group_name ORDER BY invoice_date DESC) as rn
        FROM combined
        WHERE invoice_date >= date('now', '-90 days')
    ),
    current_prices AS (
        SELECT group_name, unit_price as current_price, invoice_date as current_date
        FROM price_history
        WHERE rn = 1 AND invoice_date >= date('now', '-30 days')
    ),
    previous_prices AS (
        SELECT group_name, unit_price as previous_price, invoice_date as previous_date
        FROM price_history WHERE rn = 2
    )
    SELECT
        cp.group_name as product_name,
        cp.current_price,
        pp.previous_price,
        cp.current_date,
        pp.previous_date,
        ROUND((cp.current_price - pp.previous_price) / pp.previous_price * 100, 1) as change_pct,
        cp.current_price - pp.previous_price as change_amount
    FROM current_prices cp
    JOIN previous_prices pp ON cp.group_name = pp.group_name
    WHERE pp.previous_price >= 5.0
    ORDER BY change_pct DESC
    """

    rows = conn.execute(query).fetchall()
    conn.close()

    all_movers = [dict(row) for row in rows]

    # Split into increases and decreases
    increases = [m for m in all_movers if m['change_pct'] > 0][:limit]
    decreases = sorted([m for m in all_movers if m['change_pct'] < 0],
                       key=lambda x: x['change_pct'])[:limit]

    return {
        "increases": increases,
        "decreases": decreases
    }


def get_weekly_summary(start_date, end_date, location=None):
    """
    Generate a comprehensive weekly summary similar to your existing
    Red Nun weekly reports.
    """
    return {
        "period": {"start": start_date, "end": end_date},
        "revenue": {
            "daily": get_daily_revenue(location, start_date, end_date),
            "by_daypart": get_revenue_by_daypart(location, start_date, end_date),
            "sales_mix": get_sales_mix(location, start_date, end_date),
        },
        "labor": {
            "summary": get_labor_summary(location, start_date, end_date),
            "daily": get_daily_labor(location, start_date, end_date),
            "by_role": get_labor_by_role(location, start_date, end_date),
        },
        "pour_cost": {
            "by_category": get_pour_cost_by_category(location, start_date, end_date),
            "by_bartender": get_bartender_pour_variance(location, start_date, end_date),
        },
        "server_performance": get_server_performance(location, start_date, end_date),
    }
