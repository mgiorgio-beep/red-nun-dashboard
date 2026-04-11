"""
Patch: Add batch count endpoints to inventory_routes.py
and /count route to server.py
Run on server: python3 patch_count.py && systemctl restart rednun
"""
import re

# ============================================
# 1. Patch inventory_routes.py — add batch count + history endpoints
# ============================================
ENDPOINTS = '''

# ============================================
# INVENTORY COUNTS (batch)
# ============================================

@inventory_bp.route('/counts', methods=['POST'])
def create_count_session():
    """Create a new count session"""
    data = request.json
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO inventory_counts (location, count_date, status, notes, created_by)
        VALUES (?, date('now'), 'in_progress', ?, ?)
    """, (
        data.get('location'),
        data.get('notes', ''),
        data.get('created_by', 'admin')
    ))
    count_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': count_id, 'message': 'Count session created'}), 201


@inventory_bp.route('/counts/batch', methods=['POST'])
def save_batch_count():
    """Save a batch of inventory counts"""
    data = request.json
    count_id = data.get('count_id')
    location = data.get('location')
    items = data.get('items', [])

    if not location or not items:
        return jsonify({'error': 'location and items required'}), 400

    conn = get_connection()

    for item in items:
        pid = item['product_id']
        qty = item['quantity']
        unit = item.get('unit', 'ea')

        # Get previous quantity for variance
        prev = conn.execute(
            "SELECT quantity FROM inventory WHERE product_id = ? AND location = ?",
            (pid, location)
        ).fetchone()
        prev_qty = prev['quantity'] if prev else None
        variance = (qty - prev_qty) if prev_qty is not None else None

        # Save count item if we have a session
        if count_id:
            conn.execute("""
                INSERT INTO inventory_count_items
                (count_id, product_id, expected_quantity, counted_quantity, variance, unit)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (count_id, pid, prev_qty, qty, variance, unit))

        # Update current inventory (replace, not add)
        conn.execute("""
            INSERT INTO inventory (product_id, location, quantity, unit, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(product_id, location) DO UPDATE SET
                quantity = ?,
                unit = ?,
                updated_at = CURRENT_TIMESTAMP
        """, (pid, location, qty, unit, qty, unit))

        # Log movement
        conn.execute("""
            INSERT INTO inventory_movements
            (product_id, location, movement_type, quantity, unit, notes, created_at)
            VALUES (?, ?, 'COUNT', ?, ?, ?, CURRENT_TIMESTAMP)
        """, (pid, location, qty, unit,
              'Variance: ' + str(round(variance, 2)) if variance is not None else 'Initial count'))

    # Mark session complete
    if count_id:
        conn.execute("""
            UPDATE inventory_counts
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (count_id,))

    conn.commit()
    conn.close()

    return jsonify({'message': f'{len(items)} items counted', 'count_id': count_id})


@inventory_bp.route('/counts/history', methods=['GET'])
def get_count_history():
    """Get recent count sessions with item counts"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT c.*,
            (SELECT COUNT(*) FROM inventory_count_items WHERE count_id = c.id) as item_count
        FROM inventory_counts c
        ORDER BY c.created_at DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@inventory_bp.route('/counts/<int:count_id>', methods=['GET'])
def get_count_detail(count_id):
    """Get count session with all items"""
    conn = get_connection()
    count = conn.execute("SELECT * FROM inventory_counts WHERE id = ?", (count_id,)).fetchone()
    if not count:
        conn.close()
        return jsonify({'error': 'Count not found'}), 404

    items = conn.execute("""
        SELECT ci.*, p.name as product_name, p.category, p.inventory_unit
        FROM inventory_count_items ci
        JOIN products p ON ci.product_id = p.id
        WHERE ci.count_id = ?
        ORDER BY p.category, p.name
    """, (count_id,)).fetchall()

    conn.close()
    result = dict(count)
    result['items'] = [dict(i) for i in items]
    return jsonify(result)
'''

# Read current file
with open('inventory_routes.py', 'r') as f:
    content = f.read()

# Check if already patched
if '/counts/batch' in content:
    print("inventory_routes.py already patched, skipping")
else:
    # Append endpoints
    content += ENDPOINTS
    with open('inventory_routes.py', 'w') as f:
        f.write(content)
    print("✅ inventory_routes.py patched with batch count endpoints")


# ============================================
# 2. Patch server.py — add /count route
# ============================================
with open('server.py', 'r') as f:
    server = f.read()

if '/count' in server and 'count.html' in server:
    print("server.py already has /count route, skipping")
else:
    # Add after /manage route
    count_route = '''
@app.route("/count")
def count_page():
    """Serve the inventory count interface."""
    return send_from_directory("static", "count.html")
'''
    server = server.replace(
        '    return send_from_directory("static", "manage.html")',
        '    return send_from_directory("static", "manage.html")\n' + count_route
    )
    with open('server.py', 'w') as f:
        f.write(server)
    print("✅ server.py patched with /count route")


print("\n🎉 Patch complete!")
print("   Now copy count.html to static/ and restart:")
print("   cp count.html static/count.html")
print("   systemctl restart rednun")
print("   Then visit: https://dashboard.rednun.com/count")
