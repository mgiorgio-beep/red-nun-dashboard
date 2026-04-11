"""
Storage Location Routes
Handles storage location management and product-location assignments.
"""

from flask import Blueprint, jsonify, request
from data_store import get_connection
from auth_routes import login_required

storage_bp = Blueprint('storage', __name__)


@storage_bp.route('/api/storage/locations', methods=['GET'])
@login_required
def get_locations():
    """Get all storage locations."""
    conn = get_connection()
    locations = conn.execute("""
        SELECT * FROM storage_locations
        ORDER BY name
    """).fetchall()
    conn.close()
    return jsonify([dict(loc) for loc in locations])


@storage_bp.route('/api/storage/locations', methods=['POST'])
@login_required
def add_location():
    """Add a new storage location."""
    data = request.json
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'error': 'Location name is required'}), 400

    restaurant = data.get("location", "dennis")
    conn = get_connection()

    # Check if location already exists
    existing = conn.execute(
        "SELECT id FROM storage_locations WHERE name = ? AND location = ?",
        (name, restaurant)
    ).fetchone()

    if existing:
        conn.close()
        return jsonify({'error': 'Location already exists'}), 400

    # Insert new location
    cursor = conn.execute(
        "INSERT INTO storage_locations (name, location) VALUES (?, ?)",
        (name, restaurant)
    )
    conn.commit()
    location_id = cursor.lastrowid
    conn.close()

    return jsonify({'id': location_id, 'name': name})


@storage_bp.route('/api/storage/locations/<int:loc_id>', methods=['DELETE'])
@login_required
def delete_location(loc_id):
    """Delete a storage location."""
    conn = get_connection()

    # Delete associated product-location mappings first
    conn.execute(
        "DELETE FROM product_storage_locations WHERE location_id = ?",
        (location_id,)
    )

    # Delete the location
    conn.execute(
        "DELETE FROM storage_locations WHERE id = ?",
        (location_id,)
    )

    conn.commit()
    conn.close()

    return jsonify({'success': True})


@storage_bp.route('/api/storage/product/<int:product_id>/locations', methods=['GET'])
@login_required
def get_product_locations(product_id):
    """Get all storage locations for a product."""
    conn = get_connection()
    locations = conn.execute("""
        SELECT sl.*
        FROM storage_locations sl
        JOIN product_storage_locations psl ON sl.id = psl.storage_location_id
        WHERE psl.product_id = ?
        ORDER BY sl.name
    """, (product_id,)).fetchall()
    conn.close()
    return jsonify([dict(loc) for loc in locations])


@storage_bp.route('/api/storage/product/<int:product_id>/locations', methods=['POST'])
@login_required
def set_product_locations(product_id):
    """Set storage locations for a product (replaces existing)."""
    data = request.json
    location_ids = data.get('location_ids', [])

    conn = get_connection()

    # Delete existing mappings
    conn.execute(
        "DELETE FROM product_storage_locations WHERE product_id = ?",
        (product_id,)
    )

    # Insert new mappings
    for location_id in location_ids:
        conn.execute(
            "INSERT INTO product_storage_locations (product_id, storage_location_id) VALUES (?, ?)",
            (product_id, location_id)
        )

    conn.commit()
    conn.close()

    return jsonify({'success': True})

# ============================================
# STORAGE ASSIGNMENTS
# ============================================

@storage_bp.route('/api/storage/locations/<int:loc_id>/products', methods=['GET'])
def get_location_products(loc_id):
    """Get products assigned to a storage location, ordered by sort_order"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT p.id, p.name, p.category, p.unit, p.inventory_unit, p.current_price,
               psl.sort_order, psl.id as assignment_id
        FROM product_storage_locations psl
        JOIN products p ON psl.product_id = p.id
        WHERE psl.storage_location_id = ? AND p.active = 1
        ORDER BY psl.sort_order
    """, (loc_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@storage_bp.route('/api/storage/locations/<int:loc_id>/products', methods=['POST'])
def assign_product(loc_id):
    """Assign a product to a storage location"""
    data = request.json
    product_id = data.get('product_id')
    conn = get_connection()
    
    # Get max sort_order for this location
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM product_storage_locations WHERE storage_location_id = ?",
        (loc_id,)
    ).fetchone()[0]
    
    try:
        conn.execute("""
            INSERT INTO product_storage_locations (product_id, storage_location_id, sort_order)
            VALUES (?, ?, ?)
        """, (product_id, loc_id, max_order + 1))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': 'Already assigned'}), 409
    
    conn.close()
    return jsonify({'message': 'Product assigned', 'sort_order': max_order + 1}), 201


@storage_bp.route('/api/storage/locations/<int:loc_id>/products/<int:product_id>', methods=['DELETE'])
def unassign_product(loc_id, product_id):
    """Remove a product from a storage location"""
    conn = get_connection()
    conn.execute(
        "DELETE FROM product_storage_locations WHERE storage_location_id = ? AND product_id = ?",
        (loc_id, product_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'message': 'Product unassigned'})


@storage_bp.route('/api/storage/locations/<int:loc_id>/reorder', methods=['POST'])
def reorder_products(loc_id):
    """Reorder products within a storage location. Expects {product_ids: [1, 5, 3, ...]}"""
    data = request.json
    product_ids = data.get('product_ids', [])
    conn = get_connection()
    
    for i, pid in enumerate(product_ids):
        conn.execute("""
            UPDATE product_storage_locations
            SET sort_order = ?
            WHERE storage_location_id = ? AND product_id = ?
        """, (i, loc_id, pid))
    
    conn.commit()
    conn.close()
    return jsonify({'message': f'Reordered {len(product_ids)} products'})


@storage_bp.route('/api/storage/locations/<int:loc_id>/products/batch', methods=['POST'])
def batch_assign_products(loc_id):
    """Assign multiple products to a location at once. Expects {product_ids: [1, 2, 3]}"""
    data = request.json
    product_ids = data.get('product_ids', [])
    conn = get_connection()
    
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM product_storage_locations WHERE storage_location_id = ?",
        (loc_id,)
    ).fetchone()[0]
    
    added = 0
    for pid in product_ids:
        try:
            max_order += 1
            conn.execute("""
                INSERT INTO product_storage_locations (product_id, storage_location_id, sort_order)
                VALUES (?, ?, ?)
            """, (pid, loc_id, max_order))
            added += 1
        except:
            pass  # Skip duplicates
    
    conn.commit()
    conn.close()
    return jsonify({'message': f'{added} products assigned'}), 201


@storage_bp.route('/api/storage/unassigned', methods=['GET'])
def get_unassigned_products():
    """Get products not assigned to any storage location"""
    location = request.args.get('location', '')
    conn = get_connection()
    
    query = """
        SELECT p.id, p.name, p.category, p.unit, p.inventory_unit, p.current_price
        FROM products p
        WHERE p.active = 1
          AND p.id NOT IN (
            SELECT DISTINCT psl.product_id
            FROM product_storage_locations psl
            JOIN storage_locations sl ON psl.storage_location_id = sl.id
            WHERE 1=1
    """
    params = []
    if location:
        query += " AND sl.location = ?"
        params.append(location)
    
    query += ") ORDER BY p.category, p.name"
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@storage_bp.route('/api/storage/count-sheet', methods=['GET'])
def get_count_sheet():
    """Get full count sheet organized by storage location for a restaurant location"""
    location = request.args.get('location')
    if not location:
        return jsonify({'error': 'location required'}), 400
    
    conn = get_connection()
    
    # Get storage locations for this restaurant
    locs = conn.execute(
        "SELECT * FROM storage_locations WHERE location = ? ORDER BY id", (location,)
    ).fetchall()
    
    result = []
    for loc in locs:
        products = conn.execute("""
            SELECT p.id, p.name, p.category, p.unit, p.inventory_unit, p.current_price,
                   p.par_level, psl.sort_order,
                   inv.quantity as current_qty
            FROM product_storage_locations psl
            JOIN products p ON psl.product_id = p.id
            LEFT JOIN inventory inv ON inv.product_id = p.id AND inv.location = ?
            WHERE psl.storage_location_id = ? AND p.active = 1
            ORDER BY psl.sort_order
        """, (location, loc['id'])).fetchall()
        
        if products:
            result.append({
                'location_id': loc['id'],
                'location_name': loc['name'],
                'products': [dict(p) for p in products]
            })
    
    conn.close()
    return jsonify(result)
