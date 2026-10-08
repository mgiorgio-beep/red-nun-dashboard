"""
Stock moved between the houses (Chatham <-> Dennis), and the shared voice/Fix plumbing.

  POST /api/transfers/voice              Siri Shortcut "Transfer" (Bearer token or login)
  POST /api/waste/voice                  Siri Shortcut "Waste" (same, routes/waste_routes.py page)
  GET  /transfer/fix, /waste/fix         Fix page (signed link from the card, no login)
  GET/POST /api/moves/fix[...]           its API (signed link)
  GET  /transfer                         phone page (login): say/type it, or pick; list with Edit/Void
  GET  /api/transfers?days=30            recent transfers, both directions
  POST /api/transfers                    web picker {transfer_id, from_location, to_location, items:[{product_id, quantity, unit}]}
  POST /api/transfers/<id>/void          {reason}
  GET  /transfer/statement               month statement (?month=YYYY-MM), CSV at /api/transfers/statement.csv

The logic lives in reports/moves.py (state machine), reports/item_recognition.py
(which product), reports/house_moves.py (words, units, cost) and
reports/intercompany.py (who owes whom). Nothing is written before Confirm.

Staff never log in to the dashboard (Mike, 2026-10-08). Their per-person token
(the same one the Shortcut uses) opens: the voice endpoints, the /transfer and
/waste phone pages and their list / pick / void calls. The pages are static and
send the token from the phone (set once by the setup link /transfer/setup#<token>).
The month statement, settlement, $ summaries and admin stay login-only.
"""

from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request, session

from integrations.toast.data_store import get_connection
from reports import moves as M
from routes.auth_routes import login_required

transfer_bp = Blueprint('transfer', __name__)

LOCATIONS = ('chatham', 'dennis')
NOT_SET_UP = "This phone isn't set up. Ask Mike."


def voice_auth(f):
    """Session OR an active per-person transfer token. Nothing else."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            conn = get_connection()
            try:
                t = M.check_token(conn, auth[7:].strip())
            finally:
                conn.close()
            if not t:
                return jsonify({'status': 'error', 'say': NOT_SET_UP}), 401
            g.actor = M.actor_from_token(t)
        elif 'user_id' in session:
            g.actor = M.actor_from_session(session)
        else:
            return jsonify({'status': 'error', 'say': NOT_SET_UP}), 401
        return f(*args, **kwargs)
    return wrapped


def is_owner():
    return (getattr(g, 'actor', None) or {}).get('role') == 'owner'


def _truthy(v):
    return v is True or str(v).strip().lower() in ('1', 'true', 'yes', 'confirm')


def handle_voice(kind):
    """Shared by /api/transfers/voice and /api/waste/voice."""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    conn = get_connection()
    try:
        pid = (data.get('pending_id') or '').strip()
        if pid:
            p = conn.execute("SELECT kind, person, token_id FROM move_pending WHERE pending_id = ?", (pid,)).fetchone()
            if not p or p['kind'] != kind:
                return jsonify(M._err("I lost that one. Say it again."))
            if g.actor.get('token_id') and p['token_id'] and p['token_id'] != g.actor['token_id']:
                return jsonify(M._err("That one belongs to another phone.")), 403
            if _truthy(data.get('cancel')):
                return jsonify(M.cancel(conn, pid))
            if _truthy(data.get('confirm')):
                return jsonify(M.confirm(conn, pid, g.actor))
            if data.get('choice') not in (None, ''):
                return jsonify(M.answer(conn, pid, data['choice'], g.actor))
            p = conn.execute("SELECT * FROM move_pending WHERE pending_id = ?", (pid,)).fetchone()
            return jsonify(M.respond(conn, p, g.actor))
        return jsonify(M.start(conn, kind, data.get('text'), data.get('client_id'), g.actor))
    except Exception as e:
        current_app.logger.exception(f'{kind} voice failed')
        return jsonify(M._err("Something broke on our end. It's not logged. Try the transfer page.", detail=str(e)[:200])), 500
    finally:
        conn.close()


@transfer_bp.route('/api/transfers/voice', methods=['POST'])
@voice_auth
def transfer_voice():
    return handle_voice('transfer')


# ---------------------------------------------------------------------------
# Fix page (signed link; no login so staff can use it from the card)
# ---------------------------------------------------------------------------

@transfer_bp.route('/transfer/fix')
@transfer_bp.route('/waste/fix')
def fix_page():
    return current_app.send_static_file('move_fix.html')


def _signed(conn):
    p, why = M.load_signed(conn, request.args.get('p'), request.args.get('s'))
    if not p:
        return None, None, (jsonify({'status': 'error', 'say': why}), 410)
    actor = {'person': p['person'], 'token_id': p['token_id'], 'role': p['role'], 'home': p['home'], 'via': p['via']}
    return p, actor, None


@transfer_bp.route('/api/moves/fix', methods=['GET'])
def fix_get():
    conn = get_connection()
    try:
        p, actor, bad = _signed(conn)
        if bad:
            return bad
        return jsonify(M.fix_view(conn, p, actor))
    finally:
        conn.close()


@transfer_bp.route('/api/moves/fix/search', methods=['GET'])
def fix_search():
    conn = get_connection()
    try:
        p, actor, bad = _signed(conn)
        if bad:
            return bad
        import json
        st = json.loads(p['state'])
        house = st.get('from') if p['kind'] == 'transfer' else st.get('location')
        return jsonify(M.search_products(conn, house, request.args.get('q')))
    finally:
        conn.close()


@transfer_bp.route('/api/moves/fix', methods=['POST'])
def fix_post():
    conn = get_connection()
    try:
        p, actor, bad = _signed(conn)
        if bad:
            return bad
        res = M.fix(conn, p, request.get_json(silent=True) or {}, actor)
        p = conn.execute("SELECT * FROM move_pending WHERE pending_id = ?", (p['pending_id'],)).fetchone()
        return jsonify({'result': res, 'view': M.fix_view(conn, p, actor)})
    finally:
        conn.close()


@transfer_bp.route('/api/moves/fix/confirm', methods=['POST'])
def fix_confirm():
    conn = get_connection()
    try:
        p, actor, bad = _signed(conn)
        if bad:
            return bad
        return jsonify(M.confirm(conn, p['pending_id'], actor))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Web page (login)
# ---------------------------------------------------------------------------

@transfer_bp.route('/transfer')
def transfer_page():
    # static shell; every call it makes carries a login or the phone's token
    return current_app.send_static_file('transfer.html')


@transfer_bp.route('/transfer/setup')
def setup_page():
    """Opened from the link Mike texts: stores the token on the phone (it rides in
    the #fragment, so it never reaches a server log) and opens /transfer."""
    return current_app.send_static_file('move_setup.html')


@transfer_bp.route('/api/moves/whoami', methods=['GET'])
@voice_auth
def whoami():
    a = g.actor
    return jsonify({'person': a.get('person'), 'home': a.get('home'), 'owner': a.get('role') == 'owner',
                    'via_token': bool(a.get('token_id'))})


@transfer_bp.route('/api/transfers/products', methods=['GET'])
@voice_auth
def search_products():
    house = (request.args.get('house') or '').lower()
    conn = get_connection()
    try:
        return jsonify(M.search_products(conn, house if house in LOCATIONS else None, request.args.get('q')))
    finally:
        conn.close()


@transfer_bp.route('/api/transfers', methods=['GET'])
@voice_auth
def list_transfers():
    days = max(1, min(int(request.args.get('days', 30)), 366))
    owner = is_owner()
    conn = get_connection()
    try:
        M.ensure_tables(conn)
        rows = conn.execute("""
            SELECT t.id, t.from_location, t.to_location, t.from_product_id, t.to_product_id, t.qty_entered, t.unit_entered,
                   t.qty_base, t.base_unit, t.total_cost, t.cost_source, t.business_date, t.transferred_at, t.entered_by,
                   t.entered_via, t.raw_text, t.status, t.voided_by, t.void_reason, t.needs_link, t.is_settlement,
                   COALESCE(cn.card_name, p.display_name, p.name) AS item_name, p.category
            FROM inventory_transfers t JOIN products p ON p.id = t.from_product_id
            LEFT JOIN product_card_names cn ON cn.product_id = t.from_product_id AND cn.status = 'approved'
            WHERE t.business_date >= strftime('%Y%m%d', 'now', 'localtime', ?)
            ORDER BY t.transferred_at DESC, t.id DESC
        """, (f'-{days} day',)).fetchall()
        out = [dict(r) for r in rows]
        if not owner:
            for r in out:
                r.pop('total_cost', None)
                r.pop('cost_source', None)
        return jsonify(out)
    finally:
        conn.close()


@transfer_bp.route('/api/transfers', methods=['POST'])
@voice_auth
def create_transfer():
    """Web pickers. One client_id per line ({transfer_id}:{product_id}) so an
    offline replay lands once."""
    data = request.json or {}
    tid = (data.get('transfer_id') or '').strip()
    src = (data.get('from_location') or '').strip().lower()
    dst = (data.get('to_location') or '').strip().lower()
    items = data.get('items') or []
    if not tid or src not in LOCATIONS or dst not in LOCATIONS or src == dst or not items:
        return jsonify({'error': 'transfer_id, two different houses and items required'}), 400
    actor = dict(g.actor, via='web')
    conn = get_connection()
    try:
        ids = []
        for it in items:
            qty = float(it.get('quantity') or 0)
            if qty <= 0:
                continue
            res = M.log_direct(conn, 'transfer', f"{tid}:{int(it['product_id'])}",
                               {'from': src, 'to': dst, 'product_id': it['product_id'], 'qty': qty,
                                'unit': it.get('unit'), 'raw_text': it.get('raw_text'), 'notes': it.get('notes')}, actor)
            if res.get('status') != 'logged':
                return jsonify({'error': res.get('say'), 'result': res}), 400
            ids.append(res['id'])
        return jsonify({'success': True, 'added': len(ids), 'ids': ids})
    finally:
        conn.close()


@transfer_bp.route('/api/transfers/<int:row_id>/void', methods=['POST'])
@voice_auth
def void_transfer(row_id):
    reason = ((request.get_json(silent=True) or {}).get('reason') or '').strip()[:200] or None
    conn = get_connection()
    try:
        n = M.void(conn, 'transfer', row_id, g.actor.get('person'), reason)
        return jsonify({'success': bool(n)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Month-end statement
# ---------------------------------------------------------------------------

def _month_ok(month):
    import re
    return bool(re.fullmatch(r'\d{4}-\d{2}', month or ''))


@transfer_bp.route('/transfer/statement')
@login_required
def statement_page():
    return current_app.send_static_file('transfer_statement.html')


@transfer_bp.route('/api/transfers/statement', methods=['GET'])
@login_required
def statement():
    from reports.intercompany import build_statement
    month = request.args.get('month') or ''
    if not _month_ok(month):
        return jsonify({'error': 'month=YYYY-MM required'}), 400
    conn = get_connection()
    try:
        return jsonify(build_statement(conn, month, include_voided=request.args.get('voided') == '1'))
    finally:
        conn.close()


@transfer_bp.route('/api/transfers/statement.csv', methods=['GET'])
@login_required
def statement_csv():
    import csv
    import io
    from flask import Response
    from reports.intercompany import build_statement
    month = request.args.get('month') or ''
    if not _month_ok(month):
        return jsonify({'error': 'month=YYYY-MM required'}), 400
    conn = get_connection()
    try:
        st = build_statement(conn, month, include_voided=request.args.get('voided') == '1')
    finally:
        conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f'Red Nun stock transfers {month}'])
    w.writerow([st['sentence'].capitalize() if st['owes'] else 'Even (nets to $0.00)'])
    if st['unpriced']:
        w.writerow(['Not priced', '; '.join(u['item'] for u in st['unpriced'])])
    w.writerow([])
    w.writerow(['Category', 'Chatham -> Dennis $', 'Dennis -> Chatham $', 'Net (+ = Dennis owes Chatham)'])
    for c in st['categories']:
        w.writerow([c['category'], f"{c['chatham_to_dennis']:.2f}", f"{c['dennis_to_chatham']:.2f}", f"{c['net']:.2f}"])
    w.writerow([])
    w.writerow(['Item', 'Category', 'Chatham -> Dennis qty', 'Dennis -> Chatham qty', 'Net qty', 'Count unit',
                'Chatham -> Dennis $', 'Dennis -> Chatham $', 'Net $', 'Needs link'])
    for l in st['lines']:
        w.writerow([l['name'], l['category'], l['c2d_qty'], l['d2c_qty'], l['net_qty'], l['base_unit'],
                    f"{l['c2d_cost']:.2f}", f"{l['d2c_cost']:.2f}", f"{l['net_cost']:.2f}", 'yes' if l['needs_link'] else ''])
    w.writerow([])
    w.writerow(['Date', 'From', 'To', 'Item', 'Qty', 'Unit', 'Count qty', 'Count unit', '$', 'Cost source', 'By', 'Said',
                'Status'])
    for t in st['detail']:
        w.writerow([t['date'], t['from_location'], t['to_location'], t['item'], t['qty'], t['unit'], t['qty_base'],
                    t['base_unit'], '' if t['cost'] is None else f"{t['cost']:.2f}", t['cost_source'], t['by'], t['said'],
                    t['status'] + (' (return)' if t['is_settlement'] else '')])
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=transfers_{month}.csv'})
