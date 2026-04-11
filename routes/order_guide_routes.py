"""
Order Guide API — Red Nun Analytics

POST /api/order-guide/search
  Body: {"items": ["chicken breast", "mushrooms", ...]}
  Returns purchase history grouped by item → vendor, showing all product
  descriptions, prices, and last-purchase dates. All options shown so
  the operator can make informed comparisons (not just cheapest).
"""
import logging
from flask import Blueprint, request, jsonify
from data_store import get_connection

logger = logging.getLogger(__name__)

order_guide_bp = Blueprint("order_guide", __name__)


@order_guide_bp.route("/api/order-guide/search", methods=["POST"])
def api_order_guide_search():
    """
    Search invoice purchase history for a list of item names.
    Returns all vendor options with full product descriptions and prices.
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    if not items:
        return jsonify([])

    conn = get_connection()
    results = []

    for search_term in items:
        term = (search_term or "").strip()
        if not term:
            continue

        # Split into words; each word must appear in product_name
        words = [w for w in term.lower().split() if w]
        if not words:
            continue

        like_clauses = " AND ".join(["LOWER(product_name) LIKE ?" for _ in words])
        like_params = [f"%{w}%" for w in words]

        # UNION both invoice sources; keep most recent price per product+vendor combo
        sql = f"""
            WITH combined AS (
                SELECT si.product_name, i.vendor_name,
                       si.unit_price, si.unit, i.invoice_date
                FROM scanned_invoice_items si
                JOIN scanned_invoices i ON si.invoice_id = i.id
                WHERE i.status = 'confirmed'
                  AND si.unit_price > 0
                  AND {like_clauses}
                UNION ALL
                SELECT mi.product_name, inv.vendor_name,
                       mi.unit_price, mi.unit, inv.invoice_date
                FROM me_invoice_items mi
                JOIN me_invoices inv ON mi.order_id = inv.order_id
                WHERE mi.unit_price > 0
                  AND {like_clauses}
            ),
            ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY LOWER(product_name), LOWER(COALESCE(vendor_name,''))
                        ORDER BY invoice_date DESC
                    ) AS rn
                FROM combined
            )
            SELECT product_name, vendor_name, unit_price, unit, invoice_date
            FROM ranked
            WHERE rn = 1
            ORDER BY vendor_name, unit_price
        """

        try:
            rows = conn.execute(sql, like_params * 2).fetchall()
        except Exception as e:
            logger.error(f"Order guide query error for '{term}': {e}")
            rows = []

        # Group by vendor
        vendors = {}
        for row in rows:
            vendor = row["vendor_name"] or "Unknown"
            if vendor not in vendors:
                vendors[vendor] = []
            vendors[vendor].append({
                "product_name": row["product_name"],
                "unit_price": round(row["unit_price"], 2) if row["unit_price"] else None,
                "unit": row["unit"] or "",
                "last_purchased": row["invoice_date"],
            })

        # Find minimum price across all options for highlighting
        all_prices = [
            p["unit_price"]
            for prods in vendors.values()
            for p in prods
            if p["unit_price"]
        ]
        min_price = min(all_prices) if all_prices else None

        results.append({
            "search_term": term,
            "min_price": min_price,
            "match_count": len(rows),
            "matches": [
                {"vendor": vendor, "products": prods}
                for vendor, prods in sorted(vendors.items())
            ],
        })

    conn.close()
    return jsonify(results)
