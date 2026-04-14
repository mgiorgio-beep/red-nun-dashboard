import sqlite3
"""
Red Nun Analytics — Dashboard Server
Flask app that serves the dashboard and provides JSON API endpoints
for the frontend to consume.
"""

import os
import logging
from datetime import datetime, timedelta
from integrations.thermostat.thermostat import get_thermostats, set_setpoint
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from integrations.toast.data_store import init_db, get_connection
from integrations.toast.sync import DataSync
from reports.analytics import (
    get_daily_revenue,
    get_revenue_by_daypart,
    get_sales_mix,
    get_labor_summary,
    get_daily_labor,
    get_labor_by_role,
    get_server_performance,
    get_pour_cost_by_category,
    get_bartender_pour_variance,
    get_weekly_summary,
    get_price_movers,
)
from routes.invoice_routes import invoice_bp
from routes.catalog_routes import catalog_bp
from routes.inventory_routes import inventory_bp
from routes.product_mapping_routes import mapping_bp
from ai.inventory_ai_routes import ai_inventory_bp
from routes.auth_routes import auth_bp, login_required
from routes.storage_routes import storage_bp
from routes.order_guide_routes import order_guide_bp
from routes.specials_routes import specials_bp, init_specials_tables
from routes.food_cost_routes import food_cost_bp
from routes.vendor_routes import vendor_bp
from routes.voice_recipe_routes import voice_recipe_bp
from routes.pmix_routes import pmix_bp
from routes.product_costing_routes import product_costing_bp
from routes.menu_routes import menu_bp
from routes.canonical_product_routes import canonical_product_bp
from scraping.sports_guide import sports_bp, scrape_fanzo_guide
from scraping.sports_guide.espn_odds_fetcher import fetch_all_odds
from staff.staff import staff_bp
from staff.tv_power import tv_power_bp
from routes.billpay_routes import billpay_bp
from routes.payment_routes import payment_bp, init_payment_tables
from integrations.invoices.processor import init_invoice_tables
import secrets

from routes.availability_routes import availability_bp
from routes.application_routes import application_bp
from routes.daily_sales_routes import daily_sales_bp
from reports.sales_journal import init_sales_journal_tables, run_daily_journal, send_weekly_unresolved_summary

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")

# Fix for reverse proxy - tells Flask to trust X-Forwarded headers from nginx

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False  # Required for HTTPS
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # Stay signed in for 30 days
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max upload (AI inventory videos)
CORS(app)
app.register_blueprint(auth_bp)
app.register_blueprint(invoice_bp)
app.register_blueprint(catalog_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(storage_bp)
app.register_blueprint(sports_bp)
app.register_blueprint(mapping_bp)
app.register_blueprint(ai_inventory_bp)
app.register_blueprint(order_guide_bp)
app.register_blueprint(specials_bp)
app.register_blueprint(food_cost_bp)
app.register_blueprint(vendor_bp)
app.register_blueprint(voice_recipe_bp)
app.register_blueprint(pmix_bp)
app.register_blueprint(product_costing_bp)
app.register_blueprint(menu_bp)
app.register_blueprint(canonical_product_bp)
app.register_blueprint(staff_bp)
app.register_blueprint(tv_power_bp)
app.register_blueprint(billpay_bp)
app.register_blueprint(payment_bp)
app.register_blueprint(availability_bp)
app.register_blueprint(application_bp)
app.register_blueprint(daily_sales_bp)

# Initialize database
init_db()
# Initialize invoice scanner tables
try:
    init_invoice_tables()
    logger.info("Invoice scanner tables initialized")
except Exception as e:
    logger.warning(f"Invoice table init failed: {e}")

try:
    init_specials_tables()
    logger.info("Daily specials table initialized")
except Exception as e:
    logger.warning(f"Specials table init failed: {e}")

try:
    init_payment_tables()
    try:
        init_sales_journal_tables()
    except Exception as e:
        import logging; logging.getLogger(__name__).warning(f'Sales journal table init failed: {e}')
except Exception as e:
    logger.warning(f"Payment table init failed: {e}")


# ------------------------------------------------------------------
# Helper: Parse common query params
# ------------------------------------------------------------------

def parse_filters():
    """Extract common filter params from the request."""
    location = request.args.get("location")  # None = both
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    # Default to current week if no dates provided
    if not start_date:
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        start_date = monday.strftime("%Y%m%d")
    if not end_date:
        end_date = datetime.now().date().strftime("%Y%m%d")

    return location, start_date, end_date


# ------------------------------------------------------------------
# Dashboard HTML
# ------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    """Serve the dashboard."""
    return send_from_directory("static", "index.html")


@app.route("/manage")
@login_required
def manage():
    """Serve the management interface."""
    from flask import make_response; resp = make_response(send_from_directory("static", "manage.html")); resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"; return resp

@app.route("/count")
def count_page():
    """Serve the inventory count interface."""
    return send_from_directory("static", "count.html")

@app.route("/storage")
def storage_page():
    """Serve the storage layout interface."""
    return send_from_directory("static", "storage.html")

@app.route("/ai-inventory")
@login_required
def ai_inventory_page():
    """Serve the AI inventory count interface."""
    return send_from_directory("static", "ai_inventory.html")

@app.route("/local-upload")
def local_upload_page():
    """Serve local network upload page — no auth (uses token in URL params)."""
    return send_from_directory("static", "local_upload.html")

@app.route("/live-record")
def live_record_page():
    """Serve live audio recording page — no auth (uses token in URL params)."""
    return send_from_directory("static", "live_record.html")

@app.route("/order-guide")
@login_required
def order_guide_page():
    """Serve the order guide page."""
    return send_from_directory("static", "order_guide.html")


@app.route("/vendor-status")
@login_required
def vendor_status_page():
    """Serve the vendor session status page."""
    return send_from_directory("static", "vendor_status.html")


@app.route("/payments")
@login_required
def payments_page():
    """Serve the vendor payments tracking page."""
    return send_from_directory("static", "payments.html")


@app.route("/specials")
def specials_page():
    """Serve the chalkboard specials display (no login — for TV/public)."""
    return send_from_directory("static", "chalkboard_specials_portrait.html")


@app.route("/specials-admin")
@login_required
def specials_admin_page():
    """Serve the specials admin page (manager phone UI)."""
    return send_from_directory("static", "specials_admin.html")


@app.route("/voice-recipe")
@login_required
def voice_recipe_page():
    """Serve the voice recipe builder page."""
    return send_from_directory("static", "voice_recipe.html")


# ------------------------------------------------------------------
# Health Check
# ------------------------------------------------------------------

@app.route("/api/health")
def api_health():
    """Public health check endpoint — no login required. Used by Beelink DDNS monitoring."""
    import shutil
    try:
        db_path = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "toast_data.db"))
        db_size_mb = round(os.path.getsize(db_path) / 1024 / 1024, 1) if os.path.exists(db_path) else 0

        total, used, free = shutil.disk_usage(os.path.dirname(db_path) or "/")
        disk_total_gb = round(total / 1024**3, 1)
        disk_free_gb = round(free / 1024**3, 1)
        disk_pct_used = round((used / total) * 100, 1) if total > 0 else 0

        conn = get_connection()
        pending_row = conn.execute(
            "SELECT COUNT(*) FROM scanned_invoices WHERE status IN ('pending','review')"
        ).fetchone()
        pending_invoices = pending_row[0] if pending_row else 0

        last_row = conn.execute(
            "SELECT MAX(invoice_date) FROM scanned_invoices WHERE status = 'confirmed'"
        ).fetchone()
        last_invoice = last_row[0] if last_row else None
        conn.close()

        return jsonify({
            "status": "ok",
            "db_size_mb": db_size_mb,
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "disk_pct_used": disk_pct_used,
            "pending_invoices": pending_invoices,
            "last_invoice": last_invoice,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ------------------------------------------------------------------
# Revenue Endpoints
# ------------------------------------------------------------------

@app.route("/api/revenue/daily")
def api_daily_revenue():
    location, start, end = parse_filters()
    data = get_daily_revenue(location, start, end)
    return jsonify(data)


@app.route("/api/revenue/daypart")
def api_revenue_daypart():
    location, start, end = parse_filters()
    data = get_revenue_by_daypart(location, start, end)
    return jsonify(data)


@app.route("/api/revenue/salesmix")
def api_sales_mix():
    location, start, end = parse_filters()
    data = get_sales_mix(location, start, end)
    return jsonify(data)


# ------------------------------------------------------------------
# Labor Endpoints
# ------------------------------------------------------------------

@app.route("/api/labor/summary")
def api_labor_summary():
    location, start, end = parse_filters()
    data = get_labor_summary(location, start, end)
    return jsonify(data)


@app.route("/api/labor/daily")
def api_daily_labor():
    location, start, end = parse_filters()
    data = get_daily_labor(location, start, end)
    return jsonify(data)


@app.route("/api/labor/byrole")
def api_labor_by_role():
    location, start, end = parse_filters()
    data = get_labor_by_role(location, start, end)
    return jsonify(data)


# ------------------------------------------------------------------
# Server Performance
# ------------------------------------------------------------------

@app.route("/api/servers")
def api_servers():
    location, start, end = parse_filters()
    limit = int(request.args.get("limit", 10))
    data = get_server_performance(location, start, end, limit)
    return jsonify(data)


# ------------------------------------------------------------------
# Pour Cost Endpoints (Toast-based)
# ------------------------------------------------------------------

@app.route("/api/pourcost/category")
def api_pour_cost_category():
    location, start, end = parse_filters()
    data = get_pour_cost_by_category(location, start, end)
    return jsonify(data)


@app.route("/api/pourcost/bartender")
def api_pour_cost_bartender():
    location, start, end = parse_filters()
    data = get_bartender_pour_variance(location, start, end)
    return jsonify(data)


# ------------------------------------------------------------------
# MarginEdge COGS Endpoints
@app.route("/api/revenue/topsellers")
def api_top_sellers():
    loc = request.args.get("location", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    where = ["voided=0", "item_name != ''"]
    params = []
    if loc:
        where.append("location=?"); params.append(loc)
    if start:
        where.append("business_date>=?"); params.append(start)
    if end:
        where.append("business_date<=?"); params.append(end)
    w = " AND ".join(where)
    conn = get_connection(); rows = conn.execute(f"SELECT item_name, SUM(quantity) as qty, SUM(price) as revenue, COUNT(DISTINCT order_guid) as order_count FROM order_items WHERE " + w + " GROUP BY item_name ORDER BY revenue DESC LIMIT 30", params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/price-movers")
def api_price_movers():
    """Top price increases and decreases from invoice history."""
    location = request.args.get("location")
    limit = int(request.args.get("limit", 5))
    return jsonify(get_price_movers(location, limit))

# ------------------------------------------------------------------

@app.route("/api/cogs/summary")
def api_cogs_summary():
    location = request.args.get("location")
    start = request.args.get("start")
    end = request.args.get("end")
    if start and len(start) == 8:
        start = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    if end and len(end) == 8:
        end = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    if not start or not end:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    else:
        start_date = start
        end_date = end
    conn = get_connection()
    where = "WHERE invoice_date >= ? AND invoice_date <= ?"
    params = [start_date, end_date]
    if location:
        where += " AND location = ?"
        params.append(location)
    rows = conn.execute("SELECT vendor_name, SUM(order_total) as total, COUNT(*) as cnt FROM me_invoices " + where + " GROUP BY vendor_name ORDER BY total DESC", params).fetchall()
    hints = {"southern glazer": "LIQUOR", "l. knife": "LIQUOR", "martignetti": "LIQUOR", "atlantic beverage": "LIQUOR", "horizon beverage": "LIQUOR", "colonial wholesale": "BEER", "craft collective": "BEER", "cape cod beer": "BEER", "us foods": "FOOD", "reinhart": "FOOD", "performance food": "FOOD", "chefs warehouse": "FOOD", "cape fish": "FOOD", "sysco": "FOOD", "cintas": "NON_COGS", "unifirst": "NON_COGS", "cozzini": "NON_COGS", "rooter": "NON_COGS", "dennisport village": "NON_COGS", "caron group": "NON_COGS", "robert b. our": "NON_COGS", "marginedge": "NON_COGS"}
    cats = {}
    for row in rows:
        vname = (row["vendor_name"] or "").lower()
        matched = "OTHER"
        for hint, cat in hints.items():
            if hint in vname:
                matched = cat
                break
        if matched not in cats:
            cats[matched] = {"total": 0, "invoices": 0}
        cats[matched]["total"] += row["total"]
        cats[matched]["invoices"] += row["cnt"]
    conn.close()
    total = sum(c["total"] for c in cats.values())
    result = []
    for cat, data in sorted(cats.items(), key=lambda x: -x[1]["total"]):
        result.append({"category_type": cat, "total_cost": round(data["total"], 2), "invoice_count": data["invoices"], "pct_of_total": round((data["total"] / total * 100), 1) if total > 0 else 0})
    return jsonify({"period_start": start_date, "period_end": end_date, "total_cost": round(total, 2), "categories": result})


@app.route("/api/cogs/products")
def api_cogs_products():
    """
    Get product costs from MarginEdge by category type.
    Useful for seeing all liquor/beer/wine products with their latest prices.
    """
    location = request.args.get("location", "dennis")
    category_type = request.args.get("type")  # LIQUOR, BEER, WINE, FOOD, etc.

    conn = get_connection()

    if category_type:
        rows = conn.execute("""
            SELECT product_name, category_name, category_type,
                   latest_price, report_by_unit
            FROM me_products
            WHERE location = ? AND category_type = ?
            ORDER BY product_name
        """, (location, category_type.upper())).fetchall()
    else:
        rows = conn.execute("""
            SELECT product_name, category_name, category_type,
                   latest_price, report_by_unit
            FROM me_products
            WHERE location = ? AND category_type IN
                  ('LIQUOR','BEER','WINE','NA_BEVERAGES','FOOD')
            ORDER BY category_type, product_name
        """, (location,)).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cogs/vendors")
def api_cogs_vendors():
    location = request.args.get("location", "dennis")
    category = request.args.get("category", "")
    start = request.args.get("start")
    end = request.args.get("end")
    if start and len(start) == 8:
        start = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    if end and len(end) == 8:
        end = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    if not start or not end:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    else:
        start_date = start
        end_date = end
    food_hints = ["us foods", "reinhart", "performance food", "chefs warehouse", "cape fish", "sysco"]
    bev_hints = ["southern glazer", "l. knife", "martignetti", "atlantic beverage", "horizon beverage", "colonial wholesale", "craft collective", "cape cod beer"]
    conn = get_connection()
    rows = conn.execute("""
        SELECT vendor_name, COUNT(*) as invoice_count,
               SUM(order_total) as total_spent,
               MIN(invoice_date) as first_invoice,
               MAX(invoice_date) as last_invoice
        FROM me_invoices
        WHERE location = ? AND invoice_date >= ? AND invoice_date <= ?
        GROUP BY vendor_name
        ORDER BY total_spent DESC
    """, (location, start_date, end_date)).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    if category == "food":
        result = [r for r in result if any(h in r["vendor_name"].lower() for h in food_hints)]
    elif category == "bev":
        result = [r for r in result if any(h in r["vendor_name"].lower() for h in bev_hints)]
    return jsonify(result)

@app.route("/api/cogs/invoices")
def api_cogs_invoices():
    """
    Get recent invoices from MarginEdge.
    """
    location = request.args.get("location", "dennis")
    days = int(request.args.get("days", 30))
    limit = int(request.args.get("limit", 50))

    conn = get_connection()
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT order_id, vendor_name, invoice_number, invoice_date,
               order_total, status
        FROM me_invoices
        WHERE location = ? AND invoice_date >= ? AND invoice_date <= ?
        ORDER BY invoice_date DESC
        LIMIT ?
    """, (location, start_date, end_date, limit)).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cogs/spending")
def api_cogs_spending_trend():
    """
    Get daily/weekly spending trend from MarginEdge invoices.
    Groups invoice totals by week for trend charts.
    """
    location = request.args.get("location")
    days = int(request.args.get("days", 90))

    conn = get_connection()
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    if location:
        rows = conn.execute("""
            SELECT
                strftime('%Y-W%W', invoice_date) as week,
                MIN(invoice_date) as week_start,
                SUM(order_total) as total_spent,
                COUNT(*) as invoice_count
            FROM me_invoices
            WHERE location = ? AND invoice_date >= ? AND invoice_date <= ?
            GROUP BY week
            ORDER BY week
        """, (location, start_date, end_date)).fetchall()
    else:
        rows = conn.execute("""
            SELECT
                strftime('%Y-W%W', invoice_date) as week,
                MIN(invoice_date) as week_start,
                SUM(order_total) as total_spent,
                COUNT(*) as invoice_count
            FROM me_invoices
            WHERE invoice_date >= ? AND invoice_date <= ?
            GROUP BY week
            ORDER BY week
        """, (start_date, end_date)).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cogs/pourcost")
def api_cogs_pour_cost():
    """
    Calculate actual pour cost using MarginEdge COGS + Toast revenue.
    Compares what you spent on beverages (MarginEdge) vs what you sold (Toast).
    """
    location, start, end = parse_filters()

    conn = get_connection()
    # Convert YYYYMMDD → YYYY-MM-DD for me_cogs_summary period comparison
    start_iso = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    end_iso = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

    # Get beverage COGS from MarginEdge — include periods overlapping requested range
    if location:
        cogs_rows = conn.execute("""
            SELECT category_type, total_cost
            FROM me_cogs_summary
            WHERE location = ?
              AND category_type IN ('LIQUOR', 'BEER', 'WINE')
              AND period_end >= ?
              AND period_start <= ?
        """, (location, start_iso, end_iso)).fetchall()
    else:
        cogs_rows = conn.execute("""
            SELECT category_type, SUM(total_cost) as total_cost
            FROM me_cogs_summary
            WHERE category_type IN ('LIQUOR', 'BEER', 'WINE')
              AND period_end >= ?
              AND period_start <= ?
            GROUP BY category_type
        """, (start_iso, end_iso)).fetchall()

    # Get total beverage revenue from Toast (start/end are YYYYMMDD)
    # (This is a simplified approach — ideally we'd match by bev category)
    if location:
        rev_row = conn.execute("""
            SELECT SUM(total_amount - tax_amount) as net_revenue
            FROM orders
            WHERE location = ? AND business_date >= ? AND business_date <= ?
        """, (location, start, end)).fetchone()
    else:
        rev_row = conn.execute("""
            SELECT SUM(total_amount - tax_amount) as net_revenue
            FROM orders
            WHERE business_date >= ? AND business_date <= ?
        """, (start, end)).fetchone()

    conn.close()

    # Calculate pour costs
    bev_cogs = {}
    total_bev_cost = 0
    for r in cogs_rows:
        cost = r["total_cost"] or 0
        bev_cogs[r["category_type"]] = cost
        total_bev_cost += cost

    total_revenue = (rev_row["net_revenue"] or 0) if rev_row else 0

    # Estimate beverage revenue as ~30% of total (industry standard)
    # This will be refined once we have Toast menu category mapping
    est_bev_revenue = total_revenue * 0.30

    overall_pour_pct = (total_bev_cost / est_bev_revenue * 100) if est_bev_revenue > 0 else 0

    return jsonify({
        "period_start": start_iso,
        "period_end": end_iso,
        "total_revenue": round(total_revenue, 2),
        "est_bev_revenue": round(est_bev_revenue, 2),
        "bev_cogs": {k: round(v, 2) for k, v in bev_cogs.items()},
        "total_bev_cost": round(total_bev_cost, 2),
        "pour_cost_pct": round(overall_pour_pct, 1),
        "categories": [
            {
                "type": cat,
                "cost": round(cost, 2),
                "pct_of_bev_rev": round((cost / est_bev_revenue * 100), 1) if est_bev_revenue > 0 else 0,
            }
            for cat, cost in bev_cogs.items()
        ],
        "note": "Beverage revenue estimated at 30% of total. Refine with Toast menu category mapping."
    })


@app.route("/api/invoices/me/<order_id>")
def api_me_invoice_detail(order_id):
    """Get a MarginEdge invoice with its line items."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    inv = conn.execute("SELECT * FROM me_invoices WHERE order_id = ?", (order_id,)).fetchone()
    if not inv:
        return jsonify({"error": "Invoice not found"}), 404
    items = conn.execute("""
        SELECT product_name, quantity, unit, unit_price, total_price, category_type
        FROM me_invoice_items WHERE order_id = ?
        ORDER BY product_name
    """, (order_id,)).fetchall()
    conn.close()
    result = dict(inv)
    result['items'] = [dict(i) for i in items]
    return jsonify(result)


@app.route("/api/invoices/me/<order_id>/approve", methods=["POST"])
def approve_me_invoice(order_id):
    """Mark a MarginEdge invoice as reviewed/approved."""
    conn = get_connection()
    conn.execute("UPDATE me_invoices SET status = 'CLOSED' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/inventory/product-settings/unreviewed-count")
def unreviewed_product_count():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM product_inventory_settings WHERE reviewed = 0").fetchone()[0]
    conn.close()
    return jsonify({"count": count})

@app.route("/api/invoices/pending-count")
def pending_count():
    conn = get_connection()
    c = conn.cursor()
    scanned = c.execute("SELECT COUNT(*) FROM scanned_invoices WHERE status = 'pending' OR status = 'review'").fetchone()[0]
    me = c.execute("SELECT COUNT(*) FROM me_invoices WHERE status != 'CLOSED'").fetchone()[0]
    count = scanned + me
    return jsonify({"count": count})

# ------------------------------------------------------------------
# Forecast Endpoint
# ------------------------------------------------------------------

@app.route("/api/forecast")
def api_forecast():
    """Get revenue forecast for next week."""
    try:
        from reports.forecast import forecast_week
        location = request.args.get("location")
        locations = [location] if location else ["dennis", "chatham"]
        result = {}
        for loc in locations:
            result[loc] = forecast_week(loc)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------
# Weekly Summary
# ------------------------------------------------------------------

@app.route("/api/summary/weekly")
def api_weekly_summary():
    location, start, end = parse_filters()
    data = get_weekly_summary(start, end, location)
    return jsonify(data)


# ------------------------------------------------------------------
# Sync Controls (Toast)
# ------------------------------------------------------------------

@app.route("/api/sync/daily", methods=["POST"])
def api_trigger_sync():
    """Manually trigger a daily sync."""
    try:
        sync = DataSync()
        sync.daily_sync()
        return jsonify({"status": "ok", "message": "Daily sync complete"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/sync/initial", methods=["POST"])
def api_trigger_initial():
    """Trigger initial historical load."""
    weeks = int(request.args.get("weeks", 12))
    try:
        sync = DataSync()
        sync.initial_load(weeks_back=weeks)
        return jsonify({"status": "ok", "message": f"Initial load ({weeks} weeks) complete"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/sync/status")
def api_sync_status():
    """Get sync history."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT location, data_type, business_date, completed_at,
               record_count, status
        FROM sync_log
        ORDER BY completed_at DESC
        LIMIT 50
    """).fetchall()

    # Also include MarginEdge sync history
    me_rows = []
    try:
        me_rows = conn.execute("""
            SELECT location, data_type, started_at, completed_at,
                   record_count, status
            FROM me_sync_log
            ORDER BY completed_at DESC
            LIMIT 20
        """).fetchall()
    except Exception:
        pass  # Table might not exist yet

    conn.close()

    return jsonify({
        "toast": [dict(r) for r in rows],
        "marginedge": [dict(r) for r in me_rows],
    })


# ------------------------------------------------------------------
# Scheduled Sync
# ------------------------------------------------------------------

def setup_scheduler():
    """Set up automatic syncs."""
    scheduler = BackgroundScheduler()

    # Toast sync every 30 min during operating hours (10 AM - 1 AM)
    scheduler.add_job(
        func=lambda: DataSync().daily_sync(),
        trigger="cron",
        hour="10-23,0",
        minute="*/10",
        id="intraday_toast_sync",
    )
    logger.info("Toast intraday sync: every 30 min, 10 AM - 1 AM")

    scheduler.add_job(func=scrape_fanzo_guide, trigger='cron', hour=5, minute=0, timezone='America/New_York', id='fanzo_scrape')
    scheduler.add_job(fetch_all_odds, 'cron', hour='5,7,9,11,13,15,17,19,21,23', id='odds_fetch', replace_existing=True)
    # Daily Sales Journal: generate entries at 5:00 AM ET
    scheduler.add_job(func=run_daily_journal, trigger='cron', hour=5, minute=0, timezone='America/New_York', id='daily_sales_journal', replace_existing=True)
    # Sales Journal: weekly unresolved summary Monday 7:00 AM ET
    scheduler.add_job(func=send_weekly_unresolved_summary, trigger='cron', day_of_week='mon', hour=7, minute=5, timezone='America/New_York', id='weekly_journal_summary', replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started — Toast intraday sync, fanzo scrape, odds fetch")


# ------------------------------------------------------------------
# Entry Point
# ------------------------------------------------------------------



@app.route("/api/thermostats")
def api_thermostats():
    data = get_thermostats()
    return jsonify(data)

@app.route("/api/thermostats/set", methods=["POST"])
def api_thermostat_set():
    body = request.get_json()
    location = body.get("location")
    device_id = body.get("device_id")
    heat_sp = body.get("heat_setpoint")
    cool_sp = body.get("cool_setpoint")
    result = set_setpoint(location, device_id, heat_sp, cool_sp)
    return jsonify(result)

# Start scheduler in exactly one gunicorn worker (or in dev mode).
# filelock ensures only the first process to grab the lock runs jobs;
# the lock auto-releases when that process exits so another worker
# can pick it up on restart.
import filelock as _filelock

_sched_lock = _filelock.FileLock("/tmp/rednun_scheduler.lock", timeout=0)
try:
    _sched_lock.acquire(blocking=False)
    setup_scheduler()
    app._scheduler_lock = _sched_lock  # prevent GC from closing the FD
    logger.info("Scheduler started in this worker (lock acquired)")
except _filelock.Timeout:
    logger.info("Scheduler running in another worker (lock held)")

if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 8080))
    logger.info(f"Starting Red Nun Analytics on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

# ── Product Setup API ──────────────────────────────────────────

@app.route("/api/inventory/product-settings")
def get_product_settings():
    """Get all products for Product Setup view."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    location = request.args.get("location", "dennis")
    
    # Get products with latest price info from ME
    rows = conn.execute("""
        SELECT ps.*, 
               mp.category_name as me_category_name,
               mp.category_type as me_category_type,
               mp.report_by_unit as me_unit,
               mp.latest_price as me_latest_price
        FROM product_inventory_settings ps
        LEFT JOIN me_products mp ON LOWER(TRIM(ps.product_name)) = LOWER(TRIM(mp.product_name))
            AND mp.location = ?
        ORDER BY ps.reviewed ASC, ps.product_name ASC
    """, (location,)).fetchall()
    
    products = []
    for r in rows:
        products.append({
            "id": r["id"],
            "product_name": r["product_name"],
            "vendor_name": r["vendor_name"],
            "ordering_unit": r["ordering_unit"],
            "inventory_unit": r["inventory_unit"],
            "case_pack_size": r["case_pack_size"],
            "category": r["category"],
            "skip_inventory": r["skip_inventory"],
            "reviewed": r["reviewed"],
            "purchase_price": r["purchase_price"],
            "contains_qty": r["contains_qty"],
            "contains_unit": r["contains_unit"],
            "cost_per_unit": r["cost_per_unit"],
            "notes": r["notes"],
            "me_category_name": r["me_category_name"],
            "me_category_type": r["me_category_type"],
            "me_unit": r["me_unit"],
            "me_latest_price": r["me_latest_price"]
        })
    conn.close()
    return jsonify(products)


@app.route("/api/inventory/product-settings/<int:product_id>", methods=["PUT"])
def update_product_setting(product_id):
    """Update a single product's inventory settings."""
    data = request.json
    conn = get_connection()
    
    fields = []
    values = []
    allowed = ["ordering_unit", "inventory_unit", "case_pack_size", "category", "skip_inventory", "reviewed", "purchase_price", "contains_qty", "contains_unit", "cost_per_unit", "notes", "par_level", "order_guide_qty"]
    for key in allowed:
        if key in data:
            fields.append(f"{key} = ?")
            values.append(data[key])
    
    if not fields:
        return jsonify({"error": "No valid fields"}), 400

    # Auto-compute cost_per_unit from purchase_price and contains_qty
    row = conn.execute("SELECT purchase_price, case_pack_size, contains_qty FROM product_inventory_settings WHERE id = ?", (product_id,)).fetchone()
    if row:
        pp = float(data.get("purchase_price", row["purchase_price"]) or 0)
        cps = float(data.get("case_pack_size", row["case_pack_size"]) or 1)
        cq = float(data.get("contains_qty", row["contains_qty"]) or 0)
        if pp and cq:
            cpu = round(pp / (cps * cq), 3)
            fields.append("cost_per_unit = ?")
            values.append(cpu)

    fields.append("updated_at = datetime('now')")
    values.append(product_id)

    conn.execute(f"UPDATE product_inventory_settings SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/inventory/product-settings/bulk", methods=["PUT"])
def bulk_update_product_settings():
    """Bulk update multiple products."""
    data = request.json
    updates = data.get("updates", [])
    conn = get_connection()
    
    for item in updates:
        pid = item.get("id")
        if not pid:
            continue
        fields = []
        values = []
        for key in ["ordering_unit", "inventory_unit", "case_pack_size", "category", "skip_inventory", "reviewed", "purchase_price", "contains_qty", "contains_unit", "cost_per_unit", "notes", "par_level", "order_guide_qty"]:
            if key in item:
                fields.append(f"{key} = ?")
                values.append(item[key])
        if fields:
            # Auto-compute cost_per_unit
            row = conn.execute("SELECT purchase_price, case_pack_size, contains_qty FROM product_inventory_settings WHERE id = ?", (pid,)).fetchone()
            if row:
                pp = item.get("purchase_price", row["purchase_price"])
                cps = item.get("case_pack_size", row["case_pack_size"]) or 1
                cq = item.get("contains_qty", row["contains_qty"])
                if pp and cq:
                    fields.append("cost_per_unit = ?")
                    values.append(round(pp / (cps * cq), 3))
            fields.append("updated_at = datetime('now')")
            values.append(pid)
            conn.execute(f"UPDATE product_inventory_settings SET {', '.join(fields)} WHERE id = ?", values)

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "count": len(updates)})


@app.route("/api/inventory/order-guide")
def get_order_guide():
    """Generate order guide for products below par level."""
    location = request.args.get("location", "dennis")
    conn = get_connection()
    conn.row_factory = sqlite3.Row

    # Get products with par levels set
    products = conn.execute("""
        SELECT id, product_name, vendor_name, category, ordering_unit,
               par_level, order_guide_qty, purchase_price
        FROM product_inventory_settings
        WHERE par_level > 0 AND skip_inventory = 0
        ORDER BY vendor_name, product_name
    """).fetchall()

    # Group by vendor
    by_vendor = {}
    total_items = 0
    total_cost = 0

    for p in products:
        vendor = p["vendor_name"] or "Unknown"
        if vendor not in by_vendor:
            by_vendor[vendor] = {
                "vendor": vendor,
                "items": [],
                "total_cost": 0,
                "item_count": 0
            }

        # Assume current stock is 0 for now (will be enhanced with actual inventory counts later)
        current_stock = 0
        needed = max(0, p["par_level"] - current_stock)
        order_qty = p["order_guide_qty"] if p["order_guide_qty"] else needed

        if needed > 0:
            item_cost = (p["purchase_price"] or 0) * order_qty
            by_vendor[vendor]["items"].append({
                "id": p["id"],
                "product_name": p["product_name"],
                "category": p["category"],
                "unit": p["ordering_unit"],
                "par_level": p["par_level"],
                "current_stock": current_stock,
                "needed": needed,
                "order_qty": order_qty,
                "unit_price": p["purchase_price"],
                "total_cost": round(item_cost, 2)
            })
            by_vendor[vendor]["total_cost"] += item_cost
            by_vendor[vendor]["item_count"] += 1
            total_items += 1
            total_cost += item_cost

    # Convert to list and round costs
    vendors = []
    for v in by_vendor.values():
        if v["item_count"] > 0:
            v["total_cost"] = round(v["total_cost"], 2)
            vendors.append(v)

    conn.close()
    return jsonify({
        "vendors": vendors,
        "total_items": total_items,
        "total_cost": round(total_cost, 2),
        "generated_at": datetime.now().isoformat()
    })


# ── Recipe API (DELETE only — GET/POST/PUT handled by inventory_bp) ──
@app.route("/api/inventory/recipes/<int:recipe_id>", methods=["DELETE"])
def delete_recipe(recipe_id):
    """Soft delete a recipe."""
    conn = get_connection()
    conn.execute("UPDATE recipes SET active = 0 WHERE id = ?", (recipe_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Dashboard Overview API ──────────────────────────────────
@app.route("/api/dashboard/overview")
def api_dashboard_overview():
    location = request.args.get("location", "dennis")
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    today = datetime.now().date()
    weekday = today.weekday()
    week_start = today - timedelta(days=weekday)
    ws = week_start.strftime("%Y%m%d")
    ts = today.strftime("%Y%m%d")
    lws = (week_start - timedelta(days=7)).strftime("%Y%m%d")
    lwe = (week_start - timedelta(days=1)).strftime("%Y%m%d")
    lys = (week_start - timedelta(days=364)).strftime("%Y%m%d")
    lye = (week_start - timedelta(days=358)).strftime("%Y%m%d")
    ms = today.replace(day=1).strftime("%Y%m%d")
    lyms = (today.replace(day=1) - timedelta(days=365)).strftime("%Y%m%d")
    lyme = (today - timedelta(days=365)).strftime("%Y%m%d")
    ys = today.replace(month=1, day=1).strftime("%Y%m%d")
    lyys = (today.replace(month=1, day=1) - timedelta(days=365)).strftime("%Y%m%d")
    lyye = (today - timedelta(days=365)).strftime("%Y%m%d")
    def sr(s, e, loc):
        r = conn.execute("SELECT COALESCE(SUM(net_amount),0), COUNT(DISTINCT business_date), COUNT(*) FROM orders WHERE location=? AND business_date>=? AND business_date<=?", (loc, s, e)).fetchone()
        return {"sales": round(r[0], 2), "days": r[1], "orders": r[2]}
    def ds(s, e, loc):
        rows = conn.execute("SELECT business_date, SUM(net_amount) as net, COUNT(*) as orders FROM orders WHERE location=? AND business_date>=? AND business_date<=? GROUP BY business_date ORDER BY business_date", (loc, s, e)).fetchall()
        result = [None]*7
        for r in rows:
            from datetime import datetime as dtp
            dt = dtp.strptime(str(r["business_date"]), "%Y%m%d")
            dow = dt.weekday()
            result[dow] = {"date": r["business_date"], "sales": round(r["net"], 2), "orders": r["orders"]}
        return result
    def labor(s, e, loc):
        r = conn.execute("SELECT COALESCE(SUM(regular_hours * hourly_wage + overtime_hours * hourly_wage * 1.5), 0) FROM time_entries WHERE location=? AND business_date>=? AND business_date<=?", (loc, s, e)).fetchone()
        return round(r[0], 2)
    result = {
        "this_week": {"daily": ds(ws, ts, location), "total": sr(ws, ts, location)},
        "last_week": {"daily": ds(lws, lwe, location), "total": sr(lws, lwe, location)},
        "last_year_week": {"daily": ds(lys, lye, location), "total": sr(lys, lye, location)},
        "period_to_date": {"this_year": sr(ms, ts, location)["sales"], "last_year": sr(lyms, lyme, location)["sales"]},
        "year_to_date": {"this_year": sr(ys, ts, location)["sales"], "last_year": sr(lyys, lyye, location)["sales"]},
        "labor": {"this_week": labor(ws, ts, location), "last_week": labor(lws, lwe, location)},
        "monthly_trend": [],
    }
    for i in range(12):
        if i == 0:
            m_start = today.replace(day=1)
            m_end = today
        else:
            ref = today.replace(day=1)
            for j in range(i):
                ref = (ref - timedelta(days=1)).replace(day=1)
            m_start = ref
            m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        s = sr(m_start.strftime("%Y%m%d"), m_end.strftime("%Y%m%d"), location)
        result["monthly_trend"].append({"month": m_start.strftime("%b %Y"), "sales": s["sales"], "orders": s["orders"], "days": s["days"]})
    result["monthly_trend"].reverse()
    conn.close()
    return jsonify(result)


@app.route("/api/dashboard/today")
def api_today_snapshot():
    """Get today's key metrics at a glance for mobile"""
    location = request.args.get("location", "dennis")
    conn = get_connection()
    conn.row_factory = sqlite3.Row

    today = datetime.now().date().strftime("%Y%m%d")

    # Today's sales and covers (order count)
    sales_row = conn.execute("""
        SELECT COALESCE(SUM(net_amount), 0) as sales,
               COUNT(*) as covers
        FROM orders
        WHERE location = ?
          AND business_date = ?
          AND json_extract(raw_json, '$.deleted') != 1
          AND json_extract(raw_json, '$.voided') != 1
    """, (location, today)).fetchone()

    sales = round(sales_row['sales'], 2) if sales_row else 0
    covers = sales_row['covers'] if sales_row else 0

    # Today's labor cost
    labor_row = conn.execute("""
        SELECT COALESCE(SUM(regular_hours * hourly_wage + overtime_hours * hourly_wage * 1.5), 0) as labor_cost
        FROM time_entries
        WHERE location = ?
          AND business_date = ?
    """, (location, today)).fetchone()

    labor_cost = round(labor_row['labor_cost'], 2) if labor_row else 0
    labor_pct = round((labor_cost / sales * 100), 1) if sales > 0 else 0

    conn.close()

    return jsonify({
        'sales': sales,
        'covers': covers,
        'labor_cost': labor_cost,
        'labor_pct': labor_pct,
        'date': today
    })

# Invoice thumbnail and image serving routes
@app.route('/invoice_thumbnails/<filename>')
def serve_invoice_thumbnail(filename):
    """Serve invoice thumbnail images"""
    return send_from_directory('/opt/red-nun-dashboard/invoice_thumbnails', filename)

@app.route('/invoice_images/<filename>')
def serve_invoice_image(filename):
    """Serve full-size invoice images"""
    return send_from_directory('/opt/red-nun-dashboard/invoice_images', filename)
