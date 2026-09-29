"""
Weekly key-item count list routes.

  GET    /api/count-templates/sheet?location=   count sheet for the weekly count
  GET    /api/count-templates?location=         the list + ranked suggestions
  POST   /api/count-templates                   {location, product_id} add to the list
  DELETE /api/count-templates/<location>/<pid>  take off the list
  GET    /count/list                            the list page
"""

from flask import Blueprint, jsonify, request, session, current_app
from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required
from reports.key_items import (ensure_tables, rank_key_items, weekly_product_ids,
                               last_weekly_count, group_of, LOCATIONS)

count_template_bp = Blueprint('count_template', __name__)

NOT_ON_SHELF = 'Not on a shelf yet'


def _loc():
    loc = (request.args.get('location') or '').strip().lower()
    return loc if loc in LOCATIONS else None


@count_template_bp.route('/count/list')
@login_required
def count_list_page():
    return current_app.send_static_file('count_list.html')


@count_template_bp.route('/api/count-templates/sheet', methods=['GET'])
@login_required
def weekly_sheet():
    """Same shape as /api/storage/count-sheet, but only the weekly key items.

    Every storage area at the house is returned, empty or not, so the counter can
    open any shelf and tap "It's here". Key items with no shelf at this house come
    last in a pseudo-area (location_id null) until the walk puts them somewhere.
    """
    location = _loc()
    if not location:
        return jsonify({'error': 'location required'}), 400
    conn = get_connection()
    try:
        ids = weekly_product_ids(conn, location)
        if not ids:
            return jsonify([])
        ph = ','.join('?' * len(ids))
        result, placed = [], set()
        for loc in conn.execute("SELECT * FROM storage_locations WHERE location = ? ORDER BY id", (location,)).fetchall():
            products = conn.execute(f"""
                SELECT p.id, p.name, p.display_name, p.category, p.unit, p.inventory_unit, p.current_price,
                       p.par_level, psl.sort_order, psl.section_id, inv.quantity AS current_qty
                FROM product_storage_locations psl
                JOIN products p ON psl.product_id = p.id
                LEFT JOIN inventory inv ON inv.product_id = p.id AND inv.location = ?
                WHERE psl.storage_location_id = ? AND p.active = 1 AND p.id IN ({ph})
                ORDER BY psl.section_id NULLS LAST, psl.sort_order
            """, (location, loc['id'], *ids)).fetchall()
            sections = conn.execute("""
                SELECT id, name, sort_order FROM storage_sections
                WHERE storage_location_id = ? ORDER BY sort_order, name
            """, (loc['id'],)).fetchall()
            placed.update(p['id'] for p in products)
            result.append({'location_id': loc['id'], 'location_name': loc['name'],
                           'sections': [dict(s) for s in sections], 'products': [dict(p) for p in products]})

        loose = [i for i in ids if i not in placed]
        if loose:
            rows = {r['id']: r for r in conn.execute(f"""
                SELECT p.id, p.name, p.display_name, p.category, p.unit, p.inventory_unit, p.current_price,
                       p.par_level, NULL AS section_id, inv.quantity AS current_qty
                FROM products p LEFT JOIN inventory inv ON inv.product_id = p.id AND inv.location = ?
                WHERE p.id IN ({','.join('?' * len(loose))})
            """, (location, *loose))}
            result.append({'location_id': None, 'location_name': NOT_ON_SHELF, 'sections': [],
                           'products': [dict(rows[i]) for i in loose if i in rows]})
        return jsonify(result)
    finally:
        conn.close()


@count_template_bp.route('/api/count-templates', methods=['GET'])
@login_required
def get_list():
    location = _loc()
    if not location:
        return jsonify({'error': 'location required'}), 400
    conn = get_connection()
    try:
        ensure_tables(conn)
        items = conn.execute("""
            SELECT ct.product_id, ct.source, ct.added_by, ct.added_at,
                   COALESCE(p.display_name, p.name) AS name, p.category,
                   (SELECT sl.name FROM product_storage_locations psl
                      JOIN storage_locations sl ON sl.id = psl.storage_location_id
                     WHERE psl.product_id = p.id AND sl.location = ct.location LIMIT 1) AS area
            FROM count_templates ct JOIN products p ON p.id = ct.product_id
            WHERE ct.location = ? AND ct.frequency = 'weekly' AND p.active = 1
            ORDER BY ct.sort_order, ct.id
        """, (location,)).fetchall()
        on_list = {r['product_id'] for r in items}
        ranked = rank_key_items(conn, location)
        spend = {r['product_id']: r['dollars'] for r in ranked}
        items_out = [dict(r, group=group_of(r['category']), dollars=spend.get(r['product_id'], 0)) for r in items]
        suggestions = [r for r in ranked if r['product_id'] not in on_list][:30]
        last = last_weekly_count(conn, location)
        return jsonify({'location': location, 'items': items_out, 'suggestions': suggestions,
                        'last_weekly': dict(last) if last else None})
    finally:
        conn.close()


@count_template_bp.route('/api/count-templates', methods=['POST'])
@login_required
def add_item():
    data = request.json or {}
    location = (data.get('location') or '').strip().lower()
    pid = data.get('product_id')
    if location not in LOCATIONS or not pid:
        return jsonify({'error': 'location and product_id required'}), 400
    conn = get_connection()
    try:
        ensure_tables(conn)
        if not conn.execute("SELECT 1 FROM products WHERE id = ? AND active = 1", (pid,)).fetchone():
            return jsonify({'error': 'Unknown product'}), 404
        nxt = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM count_templates WHERE location = ? AND frequency = 'weekly'",
                           (location,)).fetchone()[0]
        conn.execute("""
            INSERT OR IGNORE INTO count_templates (location, product_id, frequency, sort_order, source, added_by)
            VALUES (?, ?, 'weekly', ?, 'added by hand', ?)
        """, (location, pid, nxt, session.get('username')))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@count_template_bp.route('/api/count-templates/<location>/<int:pid>', methods=['DELETE'])
@login_required
def remove_item(location, pid):
    if location not in LOCATIONS:
        return jsonify({'error': 'bad location'}), 400
    conn = get_connection()
    try:
        ensure_tables(conn)
        conn.execute("DELETE FROM count_templates WHERE location = ? AND product_id = ? AND frequency = 'weekly'",
                     (location, pid))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()
