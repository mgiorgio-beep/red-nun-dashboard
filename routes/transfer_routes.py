"""
Stock moved between the houses (Chatham <-> Dennis).

  GET  /transfer                         the phone page
  GET  /api/transfers/products?q=        product search for the page
  GET  /api/transfers?days=30            recent transfers, both directions
  POST /api/transfers                    {transfer_id, from_location, to_location, items:[{product_id, quantity, unit}]}
  POST /api/transfers/<id>/void          undo one line entered by mistake
  GET  /transfer/statement               month-end statement page (?month=YYYY-MM)
  GET  /api/transfers/statement[.csv]    netted by product, valued, who owes whom

A transfer is not on any invoice, so without it the sending house's usage looks
high and the receiving house's stock appears from nowhere. Weekly cost uses it:
out of a house reduces that house's usage, into a house counts like a delivery.
Quantities are in the count unit (bottles for liquor and wine), the same as counts.
`transfer_id` comes from the phone, so an offline replay is applied once.

Not accounting: the two houses are separate companies (Red Buoy Inc / Red Nun
Public House). Whether a transfer is billed intercompany is a books decision this
log does not make.
"""

from flask import Blueprint, current_app, jsonify, request, session
from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required

transfer_bp = Blueprint('transfer', __name__)

LOCATIONS = ('chatham', 'dennis')


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_id TEXT NOT NULL,
            from_location TEXT NOT NULL,
            to_location TEXT NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity REAL NOT NULL,
            unit TEXT,
            transfer_date TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            voided INTEGER DEFAULT 0,
            voided_by TEXT,
            voided_at TEXT,
            UNIQUE(transfer_id, product_id)
        )
    """)


@transfer_bp.route('/transfer')
@login_required
def transfer_page():
    return current_app.send_static_file('transfer.html')


@transfer_bp.route('/api/transfers/products', methods=['GET'])
@login_required
def search_products():
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify([])
    conn = get_connection()
    try:
        words = q.split()
        where = ' AND '.join(["(p.name LIKE ? OR COALESCE(p.display_name, '') LIKE ?)"] * len(words))
        params = [x for w in words for x in (f'%{w}%', f'%{w}%')]
        rows = conn.execute(f"""
            SELECT p.id, p.name, p.display_name, p.category, p.unit, p.inventory_unit,
                   EXISTS (SELECT 1 FROM product_storage_locations psl WHERE psl.product_id = p.id) AS shelved
            FROM products p
            WHERE p.active = 1 AND {where}
            ORDER BY shelved DESC, LENGTH(COALESCE(p.display_name, p.name))
            LIMIT 25
        """, params).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@transfer_bp.route('/api/transfers', methods=['GET'])
@login_required
def list_transfers():
    days = max(1, min(int(request.args.get('days', 30)), 366))
    conn = get_connection()
    try:
        ensure_table(conn)
        rows = conn.execute("""
            SELECT t.id, t.transfer_id, t.from_location, t.to_location, t.product_id, t.quantity, t.unit,
                   t.transfer_date, t.created_by, t.created_at, t.voided,
                   p.name, p.display_name, p.category
            FROM inventory_transfers t JOIN products p ON p.id = t.product_id
            WHERE t.transfer_date >= date('now', 'localtime', ?)
            ORDER BY t.created_at DESC, t.id DESC
        """, (f'-{days} day',)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@transfer_bp.route('/api/transfers', methods=['POST'])
@login_required
def create_transfer():
    data = request.json or {}
    tid = (data.get('transfer_id') or '').strip()
    src = (data.get('from_location') or '').strip().lower()
    dst = (data.get('to_location') or '').strip().lower()
    items = data.get('items') or []
    if not tid or src not in LOCATIONS or dst not in LOCATIONS or src == dst or not items:
        return jsonify({'error': 'transfer_id, two different houses and items required'}), 400
    conn = get_connection()
    try:
        ensure_table(conn)
        added = 0
        for it in items:
            qty = float(it.get('quantity') or 0)
            pid = int(it['product_id'])
            if qty <= 0:
                continue
            if not conn.execute("SELECT 1 FROM products WHERE id = ?", (pid,)).fetchone():
                return jsonify({'error': f'unknown product {pid}'}), 400
            added += conn.execute("""
                INSERT OR IGNORE INTO inventory_transfers
                    (transfer_id, from_location, to_location, product_id, quantity, unit, transfer_date, created_by)
                VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, date('now', 'localtime')), ?)
            """, (tid, src, dst, pid, qty, it.get('unit') or 'ea', data.get('transfer_date'),
                  session.get('username'))).rowcount
        conn.commit()
        return jsonify({'success': True, 'added': added})
    finally:
        conn.close()


@transfer_bp.route('/api/transfers/<int:row_id>/void', methods=['POST'])
@login_required
def void_transfer(row_id):
    conn = get_connection()
    try:
        ensure_table(conn)
        conn.execute("""
            UPDATE inventory_transfers SET voided = 1, voided_by = ?, voided_at = CURRENT_TIMESTAMP
            WHERE id = ? AND voided = 0
        """, (session.get('username'), row_id))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Month-end statement
# ---------------------------------------------------------------------------
# Borrow-and-return (take 2 Tito's from Dennis, send 2 back) nets to zero.
# A bulk buy at one house shipped to the other and never returned leaves a
# balance: the house that received the stock owes the house that bought it.
# Nobody tags a transfer as a loan or a sale; the netting says which it was.

def _month_bounds(month):
    import re
    if not re.fullmatch(r'\d{4}-\d{2}', month or ''):
        return None, None
    y, m = int(month[:4]), int(month[5:])
    nxt = f'{y + (m == 12):04d}-{(m % 12) + 1:02d}-01'
    return f'{month}-01', nxt


def build_statement(conn, month):
    from reports.count_units import unit_cost
    start, end = _month_bounds(month)
    ensure_table(conn)
    rows = conn.execute("""
        SELECT t.*, p.name, p.display_name, p.category
        FROM inventory_transfers t JOIN products p ON p.id = t.product_id
        WHERE t.voided = 0 AND t.transfer_date >= ? AND t.transfer_date < ?
        ORDER BY t.transfer_date, t.id
    """, (start, end)).fetchall()
    by = {}
    for r in rows:
        line = by.setdefault(r['product_id'], {'product_id': r['product_id'], 'name': r['display_name'] or r['name'],
                                               'category': r['category'], 'unit': r['unit'],
                                               'chatham_to_dennis': 0.0, 'dennis_to_chatham': 0.0})
        line['chatham_to_dennis' if r['from_location'] == 'chatham' else 'dennis_to_chatham'] += r['quantity']
    lines, unpriced, total = [], [], 0.0      # total > 0: Dennis owes Chatham
    for line in by.values():
        net = round(line['chatham_to_dennis'] - line['dennis_to_chatham'], 4)
        uc = unit_cost(conn, line['product_id'], line['unit'])
        line.update(net=net, unit_cost=uc['cost'], basis=uc['basis'],
                    value=round(net * uc['cost'], 2) if uc['cost'] is not None else None)
        if net and uc['cost'] is None:
            unpriced.append(line)
        elif line['value']:
            total += line['value']
        lines.append(line)
    lines.sort(key=lambda l: (-abs(l['value'] or 0), l['name']))
    total = round(total, 2)
    owes = None
    if abs(total) >= 0.01:
        owes = {'debtor': 'dennis' if total > 0 else 'chatham', 'creditor': 'chatham' if total > 0 else 'dennis',
                'amount': abs(total)}
    return {'month': month, 'lines': lines, 'net_value': total, 'owes': owes,
            'unpriced': [l['name'] for l in unpriced],
            'transfers': [dict(product=r['display_name'] or r['name'], date=r['transfer_date'],
                               from_location=r['from_location'], to_location=r['to_location'],
                               quantity=r['quantity'], unit=r['unit'], by=r['created_by']) for r in rows]}


@transfer_bp.route('/transfer/statement')
@login_required
def statement_page():
    return current_app.send_static_file('transfer_statement.html')


@transfer_bp.route('/api/transfers/statement', methods=['GET'])
@login_required
def statement():
    month = request.args.get('month') or ''
    if not _month_bounds(month)[0]:
        return jsonify({'error': 'month=YYYY-MM required'}), 400
    conn = get_connection()
    try:
        return jsonify(build_statement(conn, month))
    finally:
        conn.close()


@transfer_bp.route('/api/transfers/statement.csv', methods=['GET'])
@login_required
def statement_csv():
    import csv, io
    from flask import Response
    month = request.args.get('month') or ''
    if not _month_bounds(month)[0]:
        return jsonify({'error': 'month=YYYY-MM required'}), 400
    conn = get_connection()
    try:
        st = build_statement(conn, month)
    finally:
        conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f'Red Nun stock transfers {month}'])
    if st['owes']:
        w.writerow([f"{st['owes']['debtor'].title()} owes {st['owes']['creditor'].title()}", f"{st['owes']['amount']:.2f}"])
    else:
        w.writerow(['Even (nets to $0.00)'])
    if st['unpriced']:
        w.writerow(['Not priced (bottles per case unknown)', '; '.join(st['unpriced'])])
    w.writerow([])
    w.writerow(['Product', 'Unit', 'Chatham -> Dennis', 'Dennis -> Chatham', 'Net to Dennis', 'Unit cost', 'Value', 'Price basis'])
    for l in st['lines']:
        w.writerow([l['name'], l['unit'], l['chatham_to_dennis'], l['dennis_to_chatham'], l['net'],
                    '' if l['unit_cost'] is None else f"{l['unit_cost']:.2f}",
                    '' if l['value'] is None else f"{l['value']:.2f}", l['basis']])
    w.writerow([])
    w.writerow(['Date', 'From', 'To', 'Product', 'Quantity', 'Unit', 'By'])
    for t in st['transfers']:
        w.writerow([t['date'], t['from_location'], t['to_location'], t['product'], t['quantity'], t['unit'], t['by']])
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=transfers_{month}.csv'})
