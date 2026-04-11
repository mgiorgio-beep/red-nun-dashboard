"""
Food Cost Calculator Routes
===========================
Blueprint: food_cost_bp
Prefix: none (endpoints under /api/inventory/...)

Endpoints
---------
GET /api/inventory/completed-counts            List completed count sessions
GET /api/inventory/completed-counts/<id>       Item detail for one count session
GET /api/inventory/food-cost                   Food/bev/all cost calculation
GET /api/inventory/food-cost/daily-sales       Daily sales drill-down
"""

from flask import Blueprint, jsonify, request
from data_store import get_connection
from auth_routes import login_required

food_cost_bp = Blueprint("food_cost_bp", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 1 — List completed count sessions
# ─────────────────────────────────────────────────────────────────────────────

@food_cost_bp.route("/api/inventory/completed-counts")
@login_required
def list_completed_counts():
    """
    Returns all count_sessions with status='completed', enriched with
    item count, total value, and linked AI session info.

    Query params:
        location=chatham|dennis  (optional filter)
    """
    location = request.args.get("location")
    conn = get_connection()
    try:
        params = []
        location_sql = ""
        if location:
            location_sql = "AND cs.location = ?"
            params.append(location)

        rows = conn.execute(
            f"""
            SELECT
                cs.id,
                cs.location,
                cs.started_at,
                cs.completed_at,
                COUNT(ci.id)                                     AS item_count,
                COALESCE(SUM(ci.count_qty * COALESCE(p.current_price, 0)), 0) AS total_value,
                ai.id                                            AS ai_session_id
            FROM count_sessions cs
            LEFT JOIN count_items ci ON ci.session_id = cs.id
            LEFT JOIN products p     ON p.id = ci.product_id
            LEFT JOIN ai_inventory_sessions ai ON ai.count_session_id = cs.id
            WHERE cs.status = 'completed'
              {location_sql}
            GROUP BY cs.id
            ORDER BY cs.completed_at DESC
            """,
            params
        ).fetchall()

        result = []
        for r in rows:
            result.append({
                "id":           r["id"],
                "location":     r["location"],
                "started_at":   r["started_at"],
                "completed_at": r["completed_at"],
                "item_count":   r["item_count"],
                "total_value":  round(r["total_value"] or 0, 2),
                "ai_session_id": r["ai_session_id"],
            })

        return jsonify(result)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 2 — Item detail for one count session
# ─────────────────────────────────────────────────────────────────────────────

@food_cost_bp.route("/api/inventory/completed-counts/<int:session_id>")
@login_required
def get_count_detail(session_id):
    """
    Returns all count_items for a specific count_session, grouped by
    storage location, with product details and line values.
    """
    conn = get_connection()
    try:
        sess = conn.execute(
            "SELECT id, location, started_at, completed_at FROM count_sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if not sess:
            return jsonify({"error": "Session not found"}), 404

        items = conn.execute(
            """
            SELECT
                ci.id,
                ci.count_qty                                          AS qty,
                ci.expected_unit                                      AS unit,
                p.name                                                AS product,
                p.category,
                COALESCE(p.current_price, 0)                         AS unit_price,
                ROUND(ci.count_qty * COALESCE(p.current_price, 0), 2) AS line_value,
                COALESCE(sl.name, 'Unknown')                         AS storage_location
            FROM count_items ci
            LEFT JOIN products p          ON p.id = ci.product_id
            LEFT JOIN storage_locations sl ON sl.id = ci.storage_location_id
            WHERE ci.session_id = ?
            ORDER BY sl.name, p.name
            """,
            (session_id,)
        ).fetchall()

        total_value = 0.0
        by_location = {}
        for row in items:
            loc_name = row["storage_location"]
            lv = row["line_value"] or 0
            total_value += lv
            if loc_name not in by_location:
                by_location[loc_name] = []
            by_location[loc_name].append({
                "product":    row["product"],
                "category":   row["category"],
                "qty":        row["qty"],
                "unit":       row["unit"],
                "unit_price": round(row["unit_price"], 4),
                "line_value": round(lv, 2),
            })

        return jsonify({
            "session": {
                "id":           sess["id"],
                "location":     sess["location"],
                "started_at":   sess["started_at"],
                "completed_at": sess["completed_at"],
            },
            "total_value": round(total_value, 2),
            "by_location": by_location,
        })
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 3 — Food cost calculation
# ─────────────────────────────────────────────────────────────────────────────

@food_cost_bp.route("/api/inventory/food-cost")
@login_required
def calculate_food_cost():
    """
    Calculates food/bev/all cost between two completed inventory counts.

    Query params:
        begin=<count_session_id>   (required)
        end=<count_session_id>     (required)
        location=chatham|dennis    (optional, inferred from begin session)
        type=food|beverage|all     (default: food)
    """
    begin_id  = request.args.get("begin",    type=int)
    end_id    = request.args.get("end",      type=int)
    cost_type = request.args.get("type",     "food").lower()
    location  = request.args.get("location")

    if not begin_id or not end_id:
        return jsonify({"error": "begin and end params required"}), 400
    if begin_id == end_id:
        return jsonify({"error": "begin and end must be different sessions"}), 400
    if cost_type not in ("food", "beverage", "all"):
        cost_type = "food"

    conn = get_connection()
    try:
        # ── Fetch session metadata ─────────────────────────────────────────
        begin_sess = conn.execute(
            "SELECT id, location, completed_at FROM count_sessions WHERE id = ?", (begin_id,)
        ).fetchone()
        end_sess = conn.execute(
            "SELECT id, location, completed_at FROM count_sessions WHERE id = ?", (end_id,)
        ).fetchone()

        if not begin_sess:
            return jsonify({"error": f"Beginning session {begin_id} not found"}), 404
        if not end_sess:
            return jsonify({"error": f"Ending session {end_id} not found"}), 404

        # Use location from session if not provided
        if not location:
            location = begin_sess["location"]

        begin_date_iso = (begin_sess["completed_at"] or "")[:10]   # "YYYY-MM-DD"
        end_date_iso   = (end_sess["completed_at"]   or "")[:10]

        # ── Inventory values ───────────────────────────────────────────────
        def get_inventory_value(sid):
            row = conn.execute(
                """
                SELECT COALESCE(SUM(ci.count_qty * COALESCE(p.current_price, 0)), 0) AS value
                FROM count_items ci
                LEFT JOIN products p ON p.id = ci.product_id
                WHERE ci.session_id = ?
                """,
                (sid,)
            ).fetchone()
            return round(row["value"] or 0, 2) if row else 0.0

        begin_value = get_inventory_value(begin_id)
        end_value   = get_inventory_value(end_id)

        # ── Purchases (confirmed invoices in the period) ───────────────────
        # Category filter using COALESCE(item category, invoice category)
        cat_sql = ""
        if cost_type == "food":
            cat_sql = "AND COALESCE(sii.category_type, si.category) = 'FOOD'"
        elif cost_type == "beverage":
            cat_sql = "AND COALESCE(sii.category_type, si.category) IN ('BEER','LIQUOR','WINE')"
        # else 'all' → no filter

        purchases_row = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(sii.total_price), 0) AS total,
                COUNT(DISTINCT si.id)             AS invoice_count
            FROM scanned_invoice_items sii
            JOIN scanned_invoices si ON sii.invoice_id = si.id
            WHERE si.status = 'confirmed'
              AND si.invoice_date >= ?
              AND si.invoice_date <= ?
              AND si.location = ?
              {cat_sql}
            """,
            (begin_date_iso, end_date_iso, location)
        ).fetchone()

        purchases_total    = round(purchases_row["total"] or 0, 2)
        purchases_invoices = purchases_row["invoice_count"] or 0

        # Individual invoice list for drill-down
        invoice_rows = conn.execute(
            f"""
            SELECT DISTINCT
                si.id,
                si.vendor_name   AS vendor,
                si.invoice_number AS invoice_number,
                si.invoice_date  AS date,
                COALESCE(SUM(sii2.total_price), si.total) AS total
            FROM scanned_invoices si
            JOIN scanned_invoice_items sii2 ON sii2.invoice_id = si.id {cat_sql.replace('sii.', 'sii2.')}
            WHERE si.status = 'confirmed'
              AND si.invoice_date >= ?
              AND si.invoice_date <= ?
              AND si.location = ?
            GROUP BY si.id
            ORDER BY si.invoice_date
            """,
            (begin_date_iso, end_date_iso, location)
        ).fetchall()

        invoices_list = [
            {
                "id":             r["id"],
                "vendor":         r["vendor"],
                "invoice_number": r["invoice_number"],
                "date":           r["date"],
                "total":          round(r["total"] or 0, 2),
            }
            for r in invoice_rows
        ]

        # ── Net sales from Toast ───────────────────────────────────────────
        # Convert ISO dates to YYYYMMDD for orders.business_date
        begin_biz = begin_date_iso.replace("-", "")  # "20260219"
        end_biz   = end_date_iso.replace("-", "")    # "20260226"

        # order_items has category column — use for food/bev split if available
        sales_note = None
        if cost_type == "food":
            # Check if we can get food-only sales from order_items.category
            # Use menu_group/category heuristic — FOOD items vs BAR items
            oi_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM order_items
                WHERE business_date >= ? AND business_date <= ?
                  AND location = ?
                  AND UPPER(category) LIKE '%FOOD%'
                  AND voided = 0
                """,
                (begin_biz, end_biz, location)
            ).fetchone()
            if oi_row and oi_row["cnt"] > 0:
                net_sales_row = conn.execute(
                    """
                    SELECT COALESCE(SUM((oi.price - oi.discount) * oi.quantity), 0) AS net_sales,
                           COUNT(DISTINCT oi.business_date) AS num_days
                    FROM order_items oi
                    WHERE oi.business_date >= ? AND oi.business_date <= ?
                      AND oi.location = ?
                      AND UPPER(oi.category) LIKE '%FOOD%'
                      AND oi.voided = 0
                    """,
                    (begin_biz, end_biz, location)
                ).fetchone()
                net_sales = round(net_sales_row["net_sales"] or 0, 2)
                num_days  = net_sales_row["num_days"] or 0
                sales_note = "Using food category sales from order items"
            else:
                # Fall back to total net_amount
                net_sales_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(net_amount), 0) AS net_sales,
                           COUNT(DISTINCT business_date) AS num_days
                    FROM orders
                    WHERE business_date >= ? AND business_date <= ?
                      AND location = ?
                      AND json_extract(raw_json, '$.deleted') != 1
                      AND json_extract(raw_json, '$.voided')  != 1
                    """,
                    (begin_biz, end_biz, location)
                ).fetchone()
                net_sales = round(net_sales_row["net_sales"] or 0, 2)
                num_days  = net_sales_row["num_days"] or 0
                sales_note = "Using total net sales (food/bev split not available in order data)"

        elif cost_type == "beverage":
            oi_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM order_items
                WHERE business_date >= ? AND business_date <= ?
                  AND location = ?
                  AND (UPPER(category) LIKE '%BAR%' OR UPPER(category) LIKE '%BEV%' OR UPPER(category) LIKE '%DRINK%' OR UPPER(category) LIKE '%LIQUOR%' OR UPPER(category) LIKE '%BEER%' OR UPPER(category) LIKE '%WINE%')
                  AND voided = 0
                """,
                (begin_biz, end_biz, location)
            ).fetchone()
            if oi_row and oi_row["cnt"] > 0:
                net_sales_row = conn.execute(
                    """
                    SELECT COALESCE(SUM((oi.price - oi.discount) * oi.quantity), 0) AS net_sales,
                           COUNT(DISTINCT oi.business_date) AS num_days
                    FROM order_items oi
                    WHERE oi.business_date >= ? AND oi.business_date <= ?
                      AND oi.location = ?
                      AND (UPPER(oi.category) LIKE '%BAR%' OR UPPER(oi.category) LIKE '%BEV%' OR UPPER(oi.category) LIKE '%DRINK%' OR UPPER(oi.category) LIKE '%LIQUOR%' OR UPPER(oi.category) LIKE '%BEER%' OR UPPER(oi.category) LIKE '%WINE%')
                      AND oi.voided = 0
                    """,
                    (begin_biz, end_biz, location)
                ).fetchone()
                net_sales = round(net_sales_row["net_sales"] or 0, 2)
                num_days  = net_sales_row["num_days"] or 0
                sales_note = "Using bar/beverage category sales from order items"
            else:
                net_sales_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(net_amount), 0) AS net_sales,
                           COUNT(DISTINCT business_date) AS num_days
                    FROM orders
                    WHERE business_date >= ? AND business_date <= ?
                      AND location = ?
                      AND json_extract(raw_json, '$.deleted') != 1
                      AND json_extract(raw_json, '$.voided')  != 1
                    """,
                    (begin_biz, end_biz, location)
                ).fetchone()
                net_sales = round(net_sales_row["net_sales"] or 0, 2)
                num_days  = net_sales_row["num_days"] or 0
                sales_note = "Using total net sales (food/bev split not available in order data)"

        else:
            # All — use total net sales
            net_sales_row = conn.execute(
                """
                SELECT COALESCE(SUM(net_amount), 0) AS net_sales,
                       COUNT(DISTINCT business_date) AS num_days
                FROM orders
                WHERE business_date >= ? AND business_date <= ?
                  AND location = ?
                  AND json_extract(raw_json, '$.deleted') != 1
                  AND json_extract(raw_json, '$.voided')  != 1
                """,
                (begin_biz, end_biz, location)
            ).fetchone()
            net_sales = round(net_sales_row["net_sales"] or 0, 2)
            num_days  = net_sales_row["num_days"] or 0

        # ── Calculations ───────────────────────────────────────────────────
        food_cost     = round(begin_value + purchases_total - end_value, 2)
        food_cost_pct = round((food_cost / net_sales * 100), 1) if net_sales > 0 else 0.0

        return jsonify({
            "beginning_inventory": {
                "session_id": begin_id,
                "date":       begin_date_iso,
                "value":      begin_value,
            },
            "ending_inventory": {
                "session_id": end_id,
                "date":       end_date_iso,
                "value":      end_value,
            },
            "purchases": {
                "total":         purchases_total,
                "invoice_count": purchases_invoices,
                "invoices":      invoices_list,
            },
            "net_sales":      net_sales,
            "num_days":       num_days,
            "food_cost":      food_cost,
            "food_cost_pct":  food_cost_pct,
            "period_start":   begin_date_iso,
            "period_end":     end_date_iso,
            "location":       location,
            "cost_type":      cost_type,
            "sales_note":     sales_note,
        })

    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 4 — Daily sales drill-down
# ─────────────────────────────────────────────────────────────────────────────

@food_cost_bp.route("/api/inventory/food-cost/daily-sales")
@login_required
def daily_sales():
    """
    Daily net sales breakdown for the period.

    Query params:
        start=YYYYMMDD    (required)
        end=YYYYMMDD      (required)
        location=chatham|dennis  (required)
    """
    start    = request.args.get("start")
    end      = request.args.get("end")
    location = request.args.get("location")

    if not start or not end or not location:
        return jsonify({"error": "start, end, and location required"}), 400

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                business_date,
                COALESCE(SUM(net_amount), 0) AS net_sales,
                COUNT(*) AS order_count
            FROM orders
            WHERE business_date >= ?
              AND business_date <= ?
              AND location = ?
              AND json_extract(raw_json, '$.deleted') != 1
              AND json_extract(raw_json, '$.voided')  != 1
            GROUP BY business_date
            ORDER BY business_date
            """,
            (start, end, location)
        ).fetchall()

        return jsonify([
            {
                "date":        r["business_date"],
                "net_sales":   round(r["net_sales"] or 0, 2),
                "order_count": r["order_count"],
            }
            for r in rows
        ])
    finally:
        conn.close()
