"""
Storage Assignment System
- Creates product_storage_locations table with sort_order
- Adds API endpoints for assigning products to storage locations
- Adds API endpoints for reordering products within locations

Run: python3 setup_storage.py && systemctl restart rednun
"""
import sqlite3
import os
from dotenv import load_dotenv
load_dotenv()
DB_PATH = os.getenv("DB_PATH", "toast_data.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def create_table():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_storage_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            storage_location_id INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (storage_location_id) REFERENCES storage_locations(id) ON DELETE CASCADE,
            UNIQUE(product_id, storage_location_id)
        )
    """)
    conn.commit()
    conn.close()
    print("✅ product_storage_locations table created")

def patch_storage_routes():
    """Add storage assignment endpoints to storage_routes.py"""
    
    NEW_CODE = '''
# ============================================
# STORAGE ASSIGNMENTS
# ============================================

@storage_bp.route('/locations/<int:loc_id>/products', methods=['GET'])
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


@storage_bp.route('/locations/<int:loc_id>/products', methods=['POST'])
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


@storage_bp.route('/locations/<int:loc_id>/products/<int:product_id>', methods=['DELETE'])
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


@storage_bp.route('/locations/<int:loc_id>/reorder', methods=['POST'])
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


@storage_bp.route('/locations/<int:loc_id>/products/batch', methods=['POST'])
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


@storage_bp.route('/unassigned', methods=['GET'])
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


@storage_bp.route('/count-sheet', methods=['GET'])
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
'''
    
    with open('storage_routes.py', 'r') as f:
        content = f.read()
    
    if '/count-sheet' in content:
        print("storage_routes.py already patched, skipping")
        return
    
    # Make sure imports are there
    if 'from flask import' not in content:
        content = "from flask import Blueprint, jsonify, request\nfrom data_store import get_connection\n\n" + content
    
    content += NEW_CODE
    
    with open('storage_routes.py', 'w') as f:
        f.write(content)
    print("✅ storage_routes.py patched with assignment endpoints")


if __name__ == '__main__':
    print("\n=== Storage Assignment Setup ===\n")
    create_table()
    patch_storage_routes()
    
    # Summary
    conn = get_connection()
    prods = conn.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
    assigned = conn.execute("SELECT COUNT(DISTINCT product_id) FROM product_storage_locations").fetchone()[0]
    print(f"\n📊 {assigned}/{prods} products assigned to storage locations")
    print(f"   {prods - assigned} products unassigned")
    print("\n🎉 Done! Restart: systemctl restart rednun")
