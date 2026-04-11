"""
Product Catalog API Routes — Red Nun Analytics
Provides searchable product and vendor data from both
MarginEdge imports and scanned invoices.
"""

import logging
from flask import Blueprint, request, jsonify, send_from_directory
from data_store import get_connection

logger = logging.getLogger(__name__)

catalog_bp = Blueprint("catalog", __name__)


@catalog_bp.route("/catalog")
def catalog_page():
    return send_from_directory("static", "catalog.html")


@catalog_bp.route("/api/catalog/products")
def api_catalog_products():
    """
    Get all products from ME products + scanned invoice items.
    Merged and deduplicated.
    """
    location = request.args.get("location")
    category = request.args.get("category")
    search = request.args.get("q", "").strip().lower()

    conn = get_connection()

    # 1. MarginEdge products
    where_me = ["1=1"]
    params_me = []
    if location:
        where_me.append("location = ?")
        params_me.append(location)
    if category:
        where_me.append("category_type = ?")
        params_me.append(category)

    me_products = conn.execute(f"""
        SELECT product_name, category_type, latest_price,
               report_by_unit, location, 'marginedge' as source
        FROM me_products
        WHERE {' AND '.join(where_me)}
          AND category_type IN ('LIQUOR','BEER','WINE','FOOD','NA_BEVERAGES')
        ORDER BY category_type, product_name
    """, params_me).fetchall()

    # 2. Scanned invoice items (unique products with latest price)
    where_sc = ["si.status = 'confirmed'"]
    params_sc = []
    if location:
        where_sc.append("si.location = ?")
        params_sc.append(location)
    if category:
        where_sc.append("sii.category_type = ?")
        params_sc.append(category)

    scanned_products = conn.execute(f"""
        SELECT sii.product_name, sii.category_type,
               sii.unit_price as latest_price,
               sii.unit as report_by_unit,
               si.location, 'scanned' as source,
               si.vendor_name,
               MAX(si.invoice_date) as last_invoice
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE {' AND '.join(where_sc)}
          AND sii.product_name IS NOT NULL
          AND sii.unit_price > 0
        GROUP BY sii.product_name, si.vendor_name
        ORDER BY sii.product_name
    """, params_sc).fetchall()

    conn.close()

    # Merge results
    products = []
    seen = set()

    for p in me_products:
        d = dict(p)
        key = (d["product_name"] or "").lower()
        if key not in seen:
            seen.add(key)
            products.append(d)

    for p in scanned_products:
        d = dict(p)
        key = (d["product_name"] or "").lower()
        if key not in seen:
            seen.add(key)
            products.append(d)

    # Apply search filter
    if search:
        products = [p for p in products
                    if search in (p.get("product_name") or "").lower()
                    or search in (p.get("vendor_name") or "").lower()
                    or search in (p.get("category_type") or "").lower()]

    # Sort: category, then name
    products.sort(key=lambda p: ((p.get("category_type") or "Z"), (p.get("product_name") or "").lower()))

    return jsonify(products)


@catalog_bp.route("/api/catalog/vendors")
def api_catalog_vendors():
    """Get all vendors with spending totals."""
    location = request.args.get("location")
    search = request.args.get("q", "").strip().lower()
    conn = get_connection()
    from invoice_processor import categorize_vendor

    vendor_map = {}

    # 1. ME vendors table (has all vendor names)
    me_v = conn.execute("SELECT vendor_name, location FROM me_vendors").fetchall()
    for v in me_v:
        name = v["vendor_name"] or "Unknown"
        key = name.lower().strip()
        if key not in vendor_map:
            vendor_map[key] = {
                "vendor_name": name, "location": v["location"],
                "category": categorize_vendor(name),
                "invoice_count": 0, "total_spent": 0,
                "last_invoice": None, "products": []
            }

    # 2. ME invoices (spending data)
    me_inv = conn.execute("""
        SELECT vendor_name, location, COUNT(*) as cnt,
               ROUND(SUM(order_total),2) as total, MAX(invoice_date) as last_inv
        FROM me_invoices GROUP BY vendor_name, location
    """).fetchall()
    for v in me_inv:
        name = v["vendor_name"] or "Unknown"
        key = name.lower().strip()
        if key not in vendor_map:
            vendor_map[key] = {
                "vendor_name": name, "location": v["location"],
                "category": categorize_vendor(name),
                "invoice_count": 0, "total_spent": 0,
                "last_invoice": None, "products": []
            }
        vendor_map[key]["invoice_count"] += v["cnt"]
        vendor_map[key]["total_spent"] += v["total"] or 0
        if v["last_inv"]:
            if not vendor_map[key]["last_invoice"] or v["last_inv"] > vendor_map[key]["last_invoice"]:
                vendor_map[key]["last_invoice"] = v["last_inv"]

    # 3. Scanned invoices
    sc_inv = conn.execute("""
        SELECT vendor_name, location, COUNT(*) as cnt,
               ROUND(SUM(total),2) as total, MAX(invoice_date) as last_inv
        FROM scanned_invoices WHERE status='confirmed'
        GROUP BY vendor_name
    """).fetchall()
    for v in sc_inv:
        name = v["vendor_name"] or "Unknown"
        key = name.lower().strip()
        if key not in vendor_map:
            vendor_map[key] = {
                "vendor_name": name, "location": v["location"],
                "category": categorize_vendor(name),
                "invoice_count": 0, "total_spent": 0,
                "last_invoice": None, "products": []
            }
        vendor_map[key]["invoice_count"] += v["cnt"]
        vendor_map[key]["total_spent"] += v["total"] or 0
        if v["last_inv"]:
            if not vendor_map[key]["last_invoice"] or v["last_inv"] > vendor_map[key]["last_invoice"]:
                vendor_map[key]["last_invoice"] = v["last_inv"]

    # 4. Add scanned products to vendors
    for key, vd in vendor_map.items():
        prods = conn.execute("""
            SELECT DISTINCT sii.product_name, sii.unit_price as latest_price, sii.unit
            FROM scanned_invoice_items sii
            JOIN scanned_invoices si ON sii.invoice_id = si.id
            WHERE LOWER(TRIM(si.vendor_name)) = ?
              AND si.status = 'confirmed' AND sii.product_name IS NOT NULL AND sii.unit_price > 0
            ORDER BY sii.product_name LIMIT 20
        """, (key,)).fetchall()
        vd["products"] = [dict(p) for p in prods]

    conn.close()

    vendors = sorted(vendor_map.values(), key=lambda v: -(v.get("total_spent") or 0))

    if location:
        vendors = [v for v in vendors if v.get("location") == location]
    if search:
        vendors = [v for v in vendors if search in (v.get("vendor_name") or "").lower()]

    return jsonify(vendors)


@catalog_bp.route("/api/catalog/prices")
def api_catalog_prices():
    """
    Get recent product prices from scanned invoices.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT pp.product_name, pp.vendor_name, pp.unit_price,
               pp.unit, pp.invoice_date, pp.location
        FROM product_prices pp
        ORDER BY pp.created_at DESC
        LIMIT 100
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])
