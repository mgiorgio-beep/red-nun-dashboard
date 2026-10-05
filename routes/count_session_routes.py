"""
Count sessions for the count page: save area by area, then Complete.

  GET  /api/count-sessions/open?location=&notes=   the open count for this house + count type
  POST /api/count-sessions/save                    {location, notes, items, cleared} upsert counts
  POST /api/count-sessions/complete                {location, notes} close it, update on-hand

A count stays in_progress across saves, so a counter can do Kitchen Well, Save,
then the next area, Save, and so on; the first save used to mark the whole count
completed (inventory_routes.save_batch_count, which is frozen) and the page reset.

The open count is found by house + notes (the count type, e.g. 'Full count',
'Weekly key-item count (booze)'), never by id, so queued offline saves and two
phones counting different areas all land on the same count. Each save only
touches the products it sends: it never wipes what another phone saved.
On-hand (`inventory`) and movements are written once, at Complete.

A count line is one product on one SHELF (2026-10-05): Tito's in the Kitchen Well and
Tito's in the Server Well are two lines, each entered on its own, added up at Complete.
Lines are keyed "product:area:shelf" (0 = none), the same key the count page uses.
A line saved without a shelf (an older phone's queued save) lands on the product's
first shelf at that house.
"""

from flask import Blueprint, jsonify, request, session
from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required

count_session_bp = Blueprint('count_session', __name__)

LOCATIONS = ('chatham', 'dennis')
OPEN_DAYS = 7   # an in_progress count older than this is abandoned, not resumed


def _open_count(conn, location, notes):
    return conn.execute("""
        SELECT * FROM inventory_counts
        WHERE location = ? AND notes = ? AND status = 'in_progress'
          AND created_at >= datetime('now', ?)
        ORDER BY id DESC LIMIT 1
    """, (location, notes, '-%d days' % OPEN_DAYS)).fetchone()


def spot_key(pid, loc_id, sec_id):
    return f"{int(pid)}:{int(loc_id or 0)}:{int(sec_id or 0)}"


def _parse_key(key):
    """'619:22:23' -> (619, 22, 23); zeros are None. A bare '619' -> (619, None, None)."""
    parts = [int(x) for x in str(key).split(':')] + [0, 0]
    return parts[0], (parts[1] or None), (parts[2] or None)


def _first_shelf(conn, pid, location):
    r = conn.execute("""
        SELECT psl.storage_location_id, psl.section_id FROM product_storage_locations psl
        JOIN storage_locations sl ON sl.id = psl.storage_location_id
        WHERE psl.product_id = ? AND sl.location = ? ORDER BY psl.sort_order, psl.id LIMIT 1
    """, (pid, location)).fetchone()
    return (r['storage_location_id'], r['section_id']) if r else (None, None)


_SAME_SPOT = "count_id = ? AND product_id = ? AND IFNULL(storage_location_id, 0) = ? AND IFNULL(section_id, 0) = ?"


def _items(conn, count_id):
    rows = conn.execute("""
        SELECT product_id, storage_location_id, section_id, counted_quantity FROM inventory_count_items
        WHERE count_id = ? AND counted_quantity IS NOT NULL
    """, (count_id,)).fetchall()
    return {spot_key(r['product_id'], r['storage_location_id'], r['section_id']): r['counted_quantity'] for r in rows}


def _summary(conn, c):
    if not c:
        return {'count': None, 'items': {}}
    return {'count': {'id': c['id'], 'notes': c['notes'], 'created_at': c['created_at'],
                      'created_by': c['created_by']},
            'items': _items(conn, c['id'])}


def _args(data):
    location = (data.get('location') or '').strip().lower()
    notes = (data.get('notes') or '').strip()
    if location not in LOCATIONS or not notes:
        return None, None
    return location, notes


@count_session_bp.route('/api/count-sessions/open', methods=['GET'])
@login_required
def open_count():
    location, notes = _args(request.args)
    if not location:
        return jsonify({'error': 'location and notes required'}), 400
    conn = get_connection()
    try:
        return jsonify(_summary(conn, _open_count(conn, location, notes)))
    finally:
        conn.close()


@count_session_bp.route('/api/count-sessions/save', methods=['POST'])
@login_required
def save_counts():
    data = request.json or {}
    location, notes = _args(data)
    if not location:
        return jsonify({'error': 'location and notes required'}), 400
    items = data.get('items') or []
    cleared = data.get('cleared') or []

    conn = get_connection()
    try:
        c = _open_count(conn, location, notes)
        if c:
            count_id = c['id']
        else:
            count_id = conn.execute("""
                INSERT INTO inventory_counts (location, count_date, status, notes, created_by)
                VALUES (?, date('now', 'localtime'), 'in_progress', ?, ?)
            """, (location, notes, session.get('username') or 'count-page')).lastrowid

        for it in items:
            if it.get('key'):
                pid, loc_id, sec_id = _parse_key(it['key'])
            else:
                pid, loc_id, sec_id = int(it['product_id']), it.get('storage_location_id'), it.get('section_id')
            if not loc_id:
                loc_id, sec_id = _first_shelf(conn, pid, location)
            qty = float(it['quantity'])
            conn.execute(f"DELETE FROM inventory_count_items WHERE {_SAME_SPOT}", (count_id, pid, loc_id or 0, sec_id or 0))
            conn.execute("""
                INSERT INTO inventory_count_items (count_id, product_id, storage_location_id, section_id, counted_quantity, unit)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (count_id, pid, loc_id, sec_id, qty, it.get('unit') or 'ea'))
        for key in cleared:
            pid, loc_id, sec_id = _parse_key(key)
            if not loc_id and ':' not in str(key):
                loc_id, sec_id = _first_shelf(conn, pid, location)
            conn.execute(f"DELETE FROM inventory_count_items WHERE {_SAME_SPOT}", (count_id, pid, loc_id or 0, sec_id or 0))
        conn.commit()
        return jsonify(_summary(conn, conn.execute("SELECT * FROM inventory_counts WHERE id = ?", (count_id,)).fetchone()))
    finally:
        conn.close()


@count_session_bp.route('/api/count-sessions/complete', methods=['POST'])
@login_required
def complete_count():
    data = request.json or {}
    location, notes = _args(data)
    if not location:
        return jsonify({'error': 'location and notes required'}), 400
    conn = get_connection()
    try:
        c = _open_count(conn, location, notes)
        if not c:
            # Already completed (an offline replay) or never saved: nothing to close.
            return jsonify({'success': True, 'already': True})
        # Every shelf a product sits on is its own line; on-hand is their sum.
        rows = conn.execute("""
            SELECT product_id, SUM(counted_quantity) AS counted_quantity, MAX(unit) AS unit, COUNT(*) AS shelves
            FROM inventory_count_items
            WHERE count_id = ? AND counted_quantity IS NOT NULL
            GROUP BY product_id
        """, (c['id'],)).fetchall()
        for r in rows:
            prev = conn.execute("SELECT quantity FROM inventory WHERE product_id = ? AND location = ?",
                                (r['product_id'], location)).fetchone()
            prev_qty = prev['quantity'] if prev else None
            variance = (r['counted_quantity'] - prev_qty) if prev_qty is not None else None
            conn.execute("""
                INSERT INTO inventory (product_id, location, quantity, unit, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(product_id, location) DO UPDATE SET
                    quantity = excluded.quantity, unit = excluded.unit, updated_at = CURRENT_TIMESTAMP
            """, (r['product_id'], location, r['counted_quantity'], r['unit']))
            conn.execute("""
                INSERT INTO inventory_movements (product_id, location, movement_type, quantity, unit, notes, created_at)
                VALUES (?, ?, 'COUNT', ?, ?, ?, CURRENT_TIMESTAMP)
            """, (r['product_id'], location, r['counted_quantity'], r['unit'],
                  'Variance: ' + str(round(variance, 2)) if variance is not None else 'Initial count'))
        conn.execute("UPDATE inventory_counts SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (c['id'],))
        conn.commit()
        return jsonify({'success': True, 'count_id': c['id'], 'items': len(rows)})
    finally:
        conn.close()
