"""
Transfers and waste in the cost numbers (brief 8A, 8B, 8F).

  transfers(conn, house, start, end, cats)  -> $ in / $ out of a house, by category
  waste(conn, house, start, end, cats)      -> logged waste $ by reason (staff meal apart)
  adjusted_purchases(invoice_total, t)      -> invoices + transfers in - transfers out

Dates are business dates, either 'YYYY-MM-DD' or 'YYYYMMDD', end inclusive.
cats: None = everything, else an iterable of category_type ('FOOD', 'BEER', ...).

Waste is NOT a cost on top of purchases: purchases are already expensed when the
invoice is booked, so waste is already inside COGS. It is shown as "how much of
COGS was thrown out, and why" and never added to a journal entry.
"""

BEV = ('BEER', 'LIQUOR', 'WINE')


def _d(x):
    return (x or '').replace('-', '')[:8]


def _cat_sql(cats, col):
    if not cats:
        return '', ()
    cats = tuple(c.upper() for c in cats)
    return f" AND UPPER(COALESCE({col}, 'FOOD')) IN ({','.join('?' * len(cats))})", cats


def transfers(conn, house, start, end, cats=None):
    from reports.moves import ensure_tables
    ensure_tables(conn)
    cs, cp = _cat_sql(cats, 'category_type')
    rows = conn.execute(f"""
        SELECT category_type, from_location, to_location, COUNT(*) n,
               SUM(COALESCE(total_cost, 0)) cost, SUM(total_cost IS NULL) unpriced
        FROM inventory_transfers
        WHERE status = 'logged' AND business_date BETWEEN ? AND ? AND (from_location = ? OR to_location = ?) {cs}
        GROUP BY category_type, from_location, to_location""", (_d(start), _d(end), house, house, *cp)).fetchall()
    out = {'in': 0.0, 'out': 0.0, 'count': 0, 'unpriced': 0, 'by_category': {}}
    for r in rows:
        side = 'in' if r['to_location'] == house else 'out'
        out[side] += r['cost'] or 0
        out['count'] += r['n']
        out['unpriced'] += r['unpriced'] or 0
        c = out['by_category'].setdefault(r['category_type'] or 'FOOD', {'in': 0.0, 'out': 0.0})
        c[side] = round(c[side] + (r['cost'] or 0), 2)
    out['in'], out['out'] = round(out['in'], 2), round(out['out'], 2)
    out['net'] = round(out['in'] - out['out'], 2)
    return out


def adjusted_purchases(invoice_total, t):
    return round((invoice_total or 0) + t['in'] - t['out'], 2)


def waste(conn, house, start, end, cats=None):
    from reports.moves import ensure_tables
    ensure_tables(conn)
    cs, cp = _cat_sql(cats, 'category_type')
    rows = conn.execute(f"""
        SELECT COALESCE(reason_code, 'none') reason, COUNT(*) n, SUM(COALESCE(total_cost, 0)) cost,
               SUM(total_cost IS NULL) unpriced
        FROM waste_log WHERE status = 'logged' AND location = ? AND business_date BETWEEN ? AND ? {cs}
        GROUP BY 1""", (house, _d(start), _d(end), *cp)).fetchall()
    by = {r['reason']: round(r['cost'] or 0, 2) for r in rows}
    staff = by.pop('staff_meal', 0.0)
    return {'loss': round(sum(by.values()), 2), 'staff_meal': staff, 'by_reason': by,
            'entries': sum(r['n'] for r in rows), 'unpriced': sum(r['unpriced'] or 0 for r in rows)}


def top_waste_items(conn, house, start, end, n=3):
    rows = conn.execute("""
        SELECT COALESCE(cn.card_name, p.display_name, p.name) item, SUM(w.total_cost) cost
        FROM waste_log w JOIN products p ON p.id = w.product_id
        LEFT JOIN product_card_names cn ON cn.product_id = w.product_id AND cn.status = 'approved'
        WHERE w.status = 'logged' AND w.location = ? AND w.business_date BETWEEN ? AND ?
          AND COALESCE(w.reason_code, '') <> 'staff_meal' AND w.total_cost IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT ?""", (house, _d(start), _d(end), n)).fetchall()
    return [{'item': r['item'], 'cost': round(r['cost'], 2)} for r in rows]


def product_moves(conn, house, product_id, start, end):
    """Base-unit quantities for expected on-hand: in, out, waste for one product."""
    from reports.moves import ensure_tables
    ensure_tables(conn)
    r = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN to_location = ? AND COALESCE(to_product_id, from_product_id) = ? THEN qty_base END), 0) tin,
               COALESCE(SUM(CASE WHEN from_location = ? AND from_product_id = ? THEN qty_base END), 0) tout
        FROM inventory_transfers WHERE status = 'logged' AND business_date BETWEEN ? AND ?""",
                     (house, product_id, house, product_id, _d(start), _d(end))).fetchone()
    w = conn.execute("""SELECT COALESCE(SUM(qty_base), 0) FROM waste_log WHERE status = 'logged' AND location = ?
                        AND product_id = ? AND business_date BETWEEN ? AND ?""",
                     (house, product_id, _d(start), _d(end))).fetchone()[0]
    return {'transfers_in': r['tin'], 'transfers_out': r['tout'], 'waste': w}


def power_buys(conn, house, days=60):
    """8G: per product bought here in the last N days: bought, sent out, and what
    should still be here by that math (before usage)."""
    from reports.key_items import rank_key_items
    from reports.moves import ensure_tables
    ensure_tables(conn)
    out = []
    for x in rank_key_items(conn, house, days=days):
        t = conn.execute("""SELECT COALESCE(SUM(qty_base), 0) q, COALESCE(SUM(total_cost), 0) c, MAX(base_unit) u
                            FROM inventory_transfers WHERE status = 'logged' AND from_location = ? AND from_product_id = ?
                              AND business_date >= strftime('%Y%m%d', 'now', 'localtime', ?)""",
                         (house, x['product_id'], f'-{days} day')).fetchone()
        if not t['q']:
            continue
        out.append({'product_id': x['product_id'], 'name': x['name'], 'bought_dollars': round(x['dollars'], 2),
                    'sent_out_qty': t['q'], 'sent_out_dollars': round(t['c'], 2), 'unit': t['u']})
    return sorted(out, key=lambda r: -r['sent_out_dollars'])
