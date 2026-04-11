"""
Product Costing Routes — LEGACY (read-only from archived table)
Kept for backward compatibility. New pipeline uses canonical_product_routes.py.
"""

from flask import Blueprint, jsonify, request
from data_store import get_connection

product_costing_bp = Blueprint('product_costing', __name__, url_prefix='/api/product-costing')

TABLE = '_archived_product_costing'


@product_costing_bp.route('', methods=['GET'])
def get_all():
    """Get all products (read-only from archived table)."""
    conn = get_connection()
    search = request.args.get('search', '').strip()

    query = f"SELECT * FROM {TABLE} WHERE 1=1"
    params = []

    if search:
        query += " AND product_name LIKE ?"
        params.append(f'%{search}%')

    query += " ORDER BY product_name ASC"

    try:
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception:
        conn.close()
        return jsonify([])


@product_costing_bp.route('/count-needs-setup', methods=['GET'])
def count_needs_setup():
    conn = get_connection()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) c FROM {TABLE} WHERE units_per_case IS NULL OR units_per_case = 0"
        ).fetchone()
        conn.close()
        return jsonify({"count": row["c"]})
    except Exception:
        conn.close()
        return jsonify({"count": 0})


@product_costing_bp.route('/<path:product_name>', methods=['PUT'])
def save_product(product_name):
    """No-op: writes to archived table are ignored. Use canonical products API."""
    return jsonify({"message": "Legacy endpoint — use /api/canonical-products instead"}), 200


@product_costing_bp.route('/by-name/<path:product_name>', methods=['GET'])
def get_by_name(product_name):
    """Look up product costing by name (read-only, archived data)."""
    conn = get_connection()
    try:
        row = conn.execute(f"""
            SELECT * FROM {TABLE}
            WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
            LIMIT 1
        """, (product_name,)).fetchone()

        if not row:
            row = conn.execute(f"""
                SELECT * FROM {TABLE}
                WHERE LOWER(TRIM(?)) LIKE LOWER(TRIM(product_name)) || '%'
                   OR LOWER(TRIM(product_name)) LIKE LOWER(TRIM(?)) || '%'
                ORDER BY (cost_per_recipe_unit IS NOT NULL AND cost_per_recipe_unit > 0) DESC,
                         LENGTH(product_name) DESC
                LIMIT 1
            """, (product_name, product_name)).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Not found"}), 404

        result = dict(row)
        result['pack_size_hint'] = None
        conn.close()
        return jsonify(result)
    except Exception:
        conn.close()
        return jsonify({"error": "Not found"}), 404
