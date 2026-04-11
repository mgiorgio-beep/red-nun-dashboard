"""
Canonical Product Routes — Red Nun Analytics

Manages the products table (canonical products) and their linked vendor items.
Replaces the old product_costing_routes.py for Product Setup UI.
"""

from flask import Blueprint, jsonify, request
from data_store import get_connection
from auth_routes import login_required

canonical_product_bp = Blueprint('canonical_products', __name__)


@canonical_product_bp.route('/api/canonical-products', methods=['GET'])
@login_required
def get_all():
    """Get all canonical products with vendor info and setup status."""
    conn = get_connection()
    search = request.args.get('search', '').strip()
    category = request.args.get('category', '').strip()
    needs_setup = request.args.get('needs_setup', '').strip()

    rows = conn.execute("""
        SELECT p.id, p.name, p.display_name, p.category, p.recipe_unit, p.yield_pct,
               p.setup_complete, p.current_price, p.active_vendor_item_id,
               p.created_at, p.updated_at,
               vi.purchase_price as active_price,
               vi.price_per_unit as active_price_per_unit,
               vi.vendor_name as active_vendor_name,
               vi.vendor_description as active_vendor_desc,
               vi.pack_size as active_pack_size,
               vi.pack_contains as active_pack_contains,
               vi.contains_unit as active_contains_unit,
               vi.last_seen_date as active_last_invoice
        FROM products p
        LEFT JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
        WHERE p.active = 1
        ORDER BY p.name ASC
    """).fetchall()

    results = []
    for r in rows:
        r = dict(r)

        # Filter: search (match name or display_name)
        if search:
            s = search.lower()
            if s not in (r['name'] or '').lower() and s not in (r.get('display_name') or '').lower():
                continue
        # Filter: category
        if category and (r['category'] or '').upper() != category.upper():
            continue

        # Get vendor count
        vc = conn.execute(
            "SELECT COUNT(*) c FROM vendor_items WHERE product_id = ?",
            (r['id'],)
        ).fetchone()
        r['vendor_count'] = vc['c']

        # Determine setup status
        has_vendor = r['active_vendor_item_id'] is not None
        has_price = (r['active_price'] or 0) > 0
        has_pack = (r['active_pack_contains'] or 0) > 0
        has_unit = r['recipe_unit'] is not None

        r['needs_setup'] = not (has_vendor and has_price and has_pack)
        r['needs_recipe_unit'] = not has_unit
        r['needs_vendor_link'] = not has_vendor
        r['needs_pack_size'] = has_vendor and not has_pack
        r['dismissed'] = bool(r.get('setup_complete'))

        # Filter: needs_setup variants
        if needs_setup == 'dismissed':
            if not r['dismissed']:
                continue
        elif needs_setup:
            # All setup filters exclude dismissed products
            if r['dismissed']:
                continue
            if needs_setup in ('true', 'needs_setup') and not r['needs_setup'] and not r['needs_recipe_unit']:
                continue
            if needs_setup == 'needs_vendor' and not r['needs_vendor_link']:
                continue
            if needs_setup == 'needs_pack' and not r['needs_pack_size']:
                continue
            if needs_setup == 'needs_unit' and not r['needs_recipe_unit']:
                continue
            if needs_setup == 'fully_setup' and (r['needs_setup'] or r['needs_recipe_unit']):
                continue
            if needs_setup == 'all' and r['dismissed']:
                continue

        results.append(r)

    conn.close()
    return jsonify(results)


@canonical_product_bp.route('/api/canonical-products/setup-summary', methods=['GET'])
@login_required
def setup_summary():
    """Summary stats for Product Setup dashboard."""
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) c FROM products WHERE active = 1").fetchone()['c']

    dismissed = conn.execute(
        "SELECT COUNT(*) c FROM products WHERE active = 1 AND setup_complete = 1"
    ).fetchone()['c']

    needs_recipe_unit = conn.execute(
        "SELECT COUNT(*) c FROM products WHERE active = 1 AND recipe_unit IS NULL AND (setup_complete IS NULL OR setup_complete = 0)"
    ).fetchone()['c']

    needs_vendor = conn.execute(
        "SELECT COUNT(*) c FROM products WHERE active = 1 AND active_vendor_item_id IS NULL AND (setup_complete IS NULL OR setup_complete = 0)"
    ).fetchone()['c']

    needs_pack = conn.execute("""
        SELECT COUNT(*) c FROM products p
        JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
        WHERE p.active = 1 AND (vi.pack_contains IS NULL OR vi.pack_contains = 0)
          AND (p.setup_complete IS NULL OR p.setup_complete = 0)
    """).fetchone()['c']

    fully_setup = conn.execute("""
        SELECT COUNT(*) c FROM products p
        JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
        WHERE p.active = 1
          AND p.recipe_unit IS NOT NULL
          AND vi.pack_contains IS NOT NULL AND vi.pack_contains > 0
          AND vi.purchase_price IS NOT NULL AND vi.purchase_price > 0
    """).fetchone()['c']

    conn.close()
    return jsonify({
        "total": total,
        "fully_setup": fully_setup,
        "needs_recipe_unit": needs_recipe_unit,
        "needs_vendor_link": needs_vendor,
        "needs_pack_size": needs_pack,
        "dismissed": dismissed,
    })


@canonical_product_bp.route('/api/canonical-products/<int:product_id>', methods=['GET'])
@login_required
def get_one(product_id):
    """Full detail for one product including all linked vendor items."""
    conn = get_connection()

    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    result = dict(product)

    # All linked vendor items
    vis = conn.execute("""
        SELECT id, vendor_name, vendor_description, vendor_item_code,
               purchase_price, price_per_unit, pack_size, pack_contains,
               contains_unit, is_active, last_seen_date
        FROM vendor_items WHERE product_id = ?
        ORDER BY is_active DESC, last_seen_date DESC
    """, (product_id,)).fetchall()
    result['vendor_items'] = [dict(v) for v in vis]

    conn.close()
    return jsonify(result)


@canonical_product_bp.route('/api/canonical-products', methods=['POST'])
@login_required
def create():
    """Create a new canonical product."""
    data = request.json
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Name required"}), 400

    conn = get_connection()

    # Check for duplicate
    existing = conn.execute(
        "SELECT id FROM products WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) LIMIT 1",
        (name,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Product already exists", "id": existing['id']}), 409

    conn.execute("""
        INSERT INTO products (name, category, recipe_unit, yield_pct, active, setup_complete,
                              created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, 0, datetime('now'), datetime('now'))
    """, (
        name,
        (data.get('category') or 'FOOD').upper(),
        data.get('recipe_unit'),
        data.get('yield_pct', 1.0),
    ))
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    product = conn.execute("SELECT * FROM products WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    return jsonify(dict(product)), 201


@canonical_product_bp.route('/api/canonical-products/<int:product_id>', methods=['PUT'])
@login_required
def update(product_id):
    """Update canonical product fields."""
    data = request.json
    conn = get_connection()

    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    # Build dynamic update
    fields = []
    params = []
    for field, col in [('recipe_unit', 'recipe_unit'), ('yield_pct', 'yield_pct'),
                       ('category', 'category'), ('name', 'name'),
                       ('display_name', 'display_name'),
                       ('setup_complete', 'setup_complete')]:
        if field in data:
            val = data[field]
            if field == 'category' and val:
                val = val.upper()
            fields.append(f"{col} = ?")
            params.append(val)

    if not fields:
        conn.close()
        return jsonify({"error": "No fields to update"}), 400

    fields.append("updated_at = datetime('now')")
    params.append(product_id)

    conn.execute(f"UPDATE products SET {', '.join(fields)} WHERE id = ?", params)

    # If name changed, update recipe_ingredients.product_name references
    if 'name' in data and data['name'] != product['name']:
        conn.execute("""
            UPDATE recipe_ingredients SET product_name = ?
            WHERE product_id = ?
        """, (data['name'], product_id))

    conn.commit()

    updated = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return jsonify(dict(updated))


@canonical_product_bp.route('/api/canonical-products/<int:product_id>/dismiss', methods=['POST'])
@login_required
def dismiss(product_id):
    """Toggle setup_complete flag — dismiss/restore product from setup queue."""
    conn = get_connection()
    product = conn.execute("SELECT id, setup_complete FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    new_val = 0 if product['setup_complete'] else 1
    conn.execute("UPDATE products SET setup_complete = ?, updated_at = datetime('now') WHERE id = ?",
                 (new_val, product_id))
    conn.commit()
    conn.close()
    return jsonify({"setup_complete": new_val})


@canonical_product_bp.route('/api/canonical-products/<int:product_id>', methods=['DELETE'])
@login_required
def delete(product_id):
    """Delete a product (only if no recipe ingredients reference it)."""
    conn = get_connection()

    refs = conn.execute(
        "SELECT COUNT(*) c FROM recipe_ingredients WHERE product_id = ?",
        (product_id,)
    ).fetchone()
    if refs['c'] > 0:
        conn.close()
        return jsonify({"error": f"Cannot delete: {refs['c']} recipe ingredients reference this product"}), 409

    # Unlink vendor items (don't delete them)
    conn.execute("UPDATE vendor_items SET product_id = NULL WHERE product_id = ?", (product_id,))
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@canonical_product_bp.route('/api/canonical-products/<int:product_id>/vendor-items', methods=['GET'])
@login_required
def get_vendor_items(product_id):
    """Get all vendor items linked to a product."""
    conn = get_connection()
    vis = conn.execute("""
        SELECT vi.*, v.name as vendor_display_name
        FROM vendor_items vi
        LEFT JOIN vendors v ON vi.vendor_id = v.id
        WHERE vi.product_id = ?
        ORDER BY vi.is_active DESC, vi.last_seen_date DESC
    """, (product_id,)).fetchall()
    conn.close()
    return jsonify([dict(v) for v in vis])


@canonical_product_bp.route('/api/canonical-products/<int:product_id>/merge', methods=['POST'])
@login_required
def merge(product_id):
    """Merge another product into this one. Moves vendor items + recipe ingredients."""
    data = request.json
    merge_id = data.get('merge_product_id')
    if not merge_id or merge_id == product_id:
        return jsonify({'error': 'Invalid merge product'}), 400

    conn = get_connection()

    keep = conn.execute("SELECT id, name FROM products WHERE id = ?", (product_id,)).fetchone()
    merge_p = conn.execute("SELECT id, name FROM products WHERE id = ?", (merge_id,)).fetchone()
    if not keep or not merge_p:
        conn.close()
        return jsonify({'error': 'Product not found'}), 404

    vi_moved = conn.execute(
        "UPDATE vendor_items SET product_id = ? WHERE product_id = ?",
        (product_id, merge_id)
    ).rowcount

    ing_moved = conn.execute(
        "UPDATE recipe_ingredients SET product_id = ? WHERE product_id = ?",
        (product_id, merge_id)
    ).rowcount

    conn.execute("UPDATE products SET active = 0 WHERE id = ?", (merge_id,))

    # Set active vendor item if missing
    keep_vi = conn.execute("SELECT active_vendor_item_id FROM products WHERE id = ?",
                           (product_id,)).fetchone()
    if not keep_vi['active_vendor_item_id']:
        best_vi = conn.execute("""
            SELECT id FROM vendor_items WHERE product_id = ? AND purchase_price > 0
            ORDER BY purchase_price DESC LIMIT 1
        """, (product_id,)).fetchone()
        if best_vi:
            conn.execute("UPDATE products SET active_vendor_item_id = ? WHERE id = ?",
                         (best_vi['id'], product_id))

    conn.commit()
    conn.close()

    return jsonify({
        'message': f'Merged "{merge_p["name"]}" into product {product_id}',
        'vendor_items_moved': vi_moved,
        'ingredients_moved': ing_moved
    })
