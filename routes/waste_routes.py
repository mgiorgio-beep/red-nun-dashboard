"""
Waste log (brief 4B, 8F).

  POST /api/waste/voice          Siri Shortcut "Waste" (Bearer token or login)
  GET  /waste                    phone page (login): say/type it, or pick; week/month by house
  GET  /api/waste?days=7&location=
  GET  /api/waste/summary?location=&days=   $ by reason, top items, by person
  POST /api/waste                web picker {client_id, location, product_id, qty, unit, reason, notes}
  POST /api/waste/<id>/void      {reason}

Waste is a management number: purchases are already expensed to COGS when the
invoice is booked, so there is NO journal entry for waste (that would count it
twice). Staff meal is tracked on its own line, not inside loss.
"""

from flask import Blueprint, current_app, g, jsonify, request, session

from integrations.toast.data_store import get_connection
from reports import moves as M
from routes.auth_routes import login_required
from routes.transfer_routes import handle_voice, is_owner, voice_auth

waste_bp = Blueprint('waste', __name__)
LOCATIONS = ('chatham', 'dennis')


@waste_bp.route('/api/waste/voice', methods=['POST'])
@voice_auth
def waste_voice():
    return handle_voice('waste')


@waste_bp.route('/waste')
def waste_page():                    # static shell; calls carry a login or the phone's token
    return current_app.send_static_file('waste.html')


def _owner():
    return session.get('role') == 'admin'


@waste_bp.route('/api/waste', methods=['GET'])
@voice_auth
def list_waste():
    days = max(1, min(int(request.args.get('days', 7)), 366))
    loc = (request.args.get('location') or '').lower()
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        rows = conn.execute(f"""
            SELECT w.id, w.location, w.product_id, w.qty_entered, w.unit_entered, w.qty_base, w.base_unit, w.total_cost,
                   w.cost_source, w.reason_code, w.reason_words, w.raw_text, w.entered_by, w.entered_via, w.logged_at,
                   w.business_date, w.status, w.voided_by, w.void_reason, w.flagged, w.notes,
                   COALESCE(cn.card_name, p.display_name, p.name) AS item_name, p.category
            FROM waste_log w JOIN products p ON p.id = w.product_id
            LEFT JOIN product_card_names cn ON cn.product_id = w.product_id AND cn.status = 'approved'
            WHERE w.business_date >= strftime('%Y%m%d', 'now', 'localtime', ?)
              {'AND w.location = ?' if loc in LOCATIONS else ''}
            ORDER BY w.logged_at DESC, w.id DESC
        """, (f'-{days} day', *([loc] if loc in LOCATIONS else []))).fetchall()
        out = [dict(r) for r in rows]
        if not is_owner():
            for r in out:
                r.pop('total_cost', None)
                r.pop('cost_source', None)
        return jsonify(out)
    finally:
        conn.close()


def summarize(conn, location, start, end):
    """$ by reason, top items, by person for one house and a business-date range.
    Staff meal is kept apart from loss."""
    M.ensure_tables(conn)
    rows = conn.execute("""
        SELECT w.*, COALESCE(cn.card_name, p.display_name, p.name) AS item_name
        FROM waste_log w JOIN products p ON p.id = w.product_id
        LEFT JOIN product_card_names cn ON cn.product_id = w.product_id AND cn.status = 'approved'
        WHERE w.location = ? AND w.status = 'logged' AND w.business_date >= ? AND w.business_date < ?
    """, (location, start, end)).fetchall()
    by_reason, by_item, by_person = {}, {}, {}
    loss = staff = unpriced = 0.0
    n_unpriced = 0
    for r in rows:
        c = r['total_cost']
        if c is None:
            n_unpriced += 1
            continue
        key = r['reason_code'] or 'none'
        by_reason[key] = by_reason.get(key, 0) + c
        if key == 'staff_meal':
            staff += c
            continue
        loss += c
        by_item[r['item_name']] = by_item.get(r['item_name'], 0) + c
        by_person[r['entered_by'] or '?'] = by_person.get(r['entered_by'] or '?', 0) + c
    top = sorted(by_item.items(), key=lambda x: -x[1])
    return {'location': location, 'start': start, 'end': end, 'entries': len(rows), 'loss': round(loss, 2),
            'staff_meal': round(staff, 2), 'unpriced_entries': n_unpriced,
            'by_reason': {k: round(v, 2) for k, v in sorted(by_reason.items(), key=lambda x: -x[1])},
            'top_items': [{'item': k, 'cost': round(v, 2)} for k, v in top[:10]],
            'by_person': [{'person': k, 'cost': round(v, 2)} for k, v in sorted(by_person.items(), key=lambda x: -x[1])]}


@waste_bp.route('/api/waste/summary', methods=['GET'])
@login_required
def waste_summary():
    if not _owner():
        return jsonify({'error': 'owner only'}), 403
    from datetime import timedelta
    days = max(1, min(int(request.args.get('days', 7)), 366))
    loc = (request.args.get('location') or '').lower()
    end_dt = M.now_et() + timedelta(days=1)
    start = (M.now_et() - timedelta(days=days - 1)).strftime('%Y%m%d')
    conn = get_connection()
    try:
        locs = [loc] if loc in LOCATIONS else list(LOCATIONS)
        return jsonify([summarize(conn, l, start, end_dt.strftime('%Y%m%d')) for l in locs])
    finally:
        conn.close()


@waste_bp.route('/api/waste', methods=['POST'])
@voice_auth
def create_waste():
    d = request.json or {}
    loc = (d.get('location') or '').lower()
    if loc not in LOCATIONS or not d.get('product_id') or not d.get('client_id') or float(d.get('qty') or 0) <= 0:
        return jsonify({'error': 'client_id, location, product_id and qty required'}), 400
    conn = get_connection()
    try:
        res = M.log_direct(conn, 'waste', d['client_id'], {'location': loc, 'product_id': d['product_id'], 'qty': d['qty'],
                                                            'unit': d.get('unit'), 'reason': d.get('reason') or None,
                                                            'notes': d.get('notes'), 'raw_text': d.get('raw_text')},
                           dict(g.actor, via='web'))
        if res.get('status') != 'logged':
            return jsonify({'error': res.get('say'), 'result': res}), 400
        return jsonify({'success': True, 'id': res['id']})
    finally:
        conn.close()


@waste_bp.route('/api/waste/<int:row_id>/void', methods=['POST'])
@voice_auth
def void_waste(row_id):
    reason = ((request.get_json(silent=True) or {}).get('reason') or '').strip()[:200] or None
    conn = get_connection()
    try:
        return jsonify({'success': bool(M.void(conn, 'waste', row_id, g.actor.get('person'), reason))})
    finally:
        conn.close()
