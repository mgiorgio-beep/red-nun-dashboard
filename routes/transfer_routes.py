"""
Stock moved between the houses (Chatham <-> Dennis).

  GET  /transfer                         the phone page
  GET  /api/transfers/products?q=        product search for the page
  GET  /api/transfers?days=30            recent transfers, both directions
  POST /api/transfers                    {transfer_id, from_location, to_location, items:[{product_id, quantity, unit}]}
  POST /api/transfers/<id>/void          undo one line entered by mistake

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
