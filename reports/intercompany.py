"""
Who owes whom for stock moved between the houses (brief 8C, 8H).

The houses are separate companies: Red Buoy Inc. (Chatham) and Red Nun Public
House Inc. (Dennis). Every transfer row carries its cost snapshot (taken at log
time from the sender's last invoice), so the month's balance never moves when
prices change later.

  month_net(conn, 'YYYYMM')        -> the one-line running balance (emails)
  build_statement(conn, 'YYYY-MM') -> by category, netted by item, line detail

Netting by item: an item is the cross-house pair (same product id at both houses,
or a confirmed product_links pair). If it went both ways in the month, only the
net shows. Positive net = Chatham sent more = Dennis owes Chatham.
Returns in kind (is_settlement=1) close an earlier month's line; they are not
new activity and are listed separately.
"""

from collections import defaultdict

ENTITY = {'chatham': 'Red Buoy', 'dennis': 'Red Nun Public House'}
ENTITY_FULL = {'chatham': 'Red Buoy Inc.', 'dennis': 'Red Nun Public House Inc.'}
HOUSE = {'chatham': 'Chatham', 'dennis': 'Dennis'}
CAT_LABEL = {'LIQUOR': 'Liquor', 'BEER': 'Beer', 'WINE': 'Wine', 'FOOD': 'Food', 'NA_BEVERAGES': 'NA Bev'}


def _months(ym):
    """'2026-10' or '202610' -> ('20261001', '20261101', '2026-10')."""
    ym = ym.replace('-', '')
    y, m = int(ym[:4]), int(ym[4:6])
    ny, nm = (y + (m == 12), m % 12 + 1)
    return f'{y:04d}{m:02d}01', f'{ny:04d}{nm:02d}01', f'{y:04d}-{m:02d}'


def item_key(r):
    """Chatham-side and Dennis-side product of a transfer row = the item."""
    if r['from_location'] == 'chatham':
        return (r['from_product_id'], r['to_product_id'] or -r['from_product_id'])
    return (r['to_product_id'] or -r['from_product_id'], r['from_product_id'])


def owes_sentence(net):
    if abs(net) < 0.005:
        return 'even'
    debtor, creditor = ('dennis', 'chatham') if net > 0 else ('chatham', 'dennis')
    return f"{ENTITY[debtor]} owes {ENTITY[creditor]} ${abs(net):,.2f}"


def _rows(conn, start, end, include_voided=False):
    from reports.moves import ensure_tables
    ensure_tables(conn)
    return conn.execute(f"""
        SELECT t.*, COALESCE(cn.card_name, p.display_name, p.name) AS item_name
        FROM inventory_transfers t
        JOIN products p ON p.id = t.from_product_id
        LEFT JOIN product_card_names cn ON cn.product_id = t.from_product_id AND cn.status = 'approved'
        WHERE t.business_date >= ? AND t.business_date < ? {'' if include_voided else "AND t.status = 'logged'"}
        ORDER BY t.business_date, t.id
    """, (start, end)).fetchall()


def month_net(conn, ym):
    start, end, label = _months(ym)
    net = 0.0
    for r in _rows(conn, start, end):
        if r['is_settlement'] or r['total_cost'] is None:
            continue
        net += r['total_cost'] if r['from_location'] == 'chatham' else -r['total_cost']
    import calendar
    mon = calendar.month_abbr[int(label[5:])]
    return {'net': round(net, 2), 'label': mon, 'sentence': owes_sentence(net)}


def build_statement(conn, ym, include_voided=False):
    start, end, label = _months(ym)
    rows = _rows(conn, start, end, include_voided=True)
    live = [r for r in rows if r['status'] == 'logged']
    activity = [r for r in live if not r['is_settlement']]
    returns = [r for r in live if r['is_settlement']]
    by_cat = defaultdict(lambda: {'chatham_to_dennis': 0.0, 'dennis_to_chatham': 0.0})
    items = {}
    unpriced = []
    for r in activity:
        cat = CAT_LABEL.get(r['category_type'], (r['category_type'] or 'Other').title())
        d = 'chatham_to_dennis' if r['from_location'] == 'chatham' else 'dennis_to_chatham'
        if r['total_cost'] is None:
            unpriced.append(r)
        else:
            by_cat[cat][d] += r['total_cost']
        k = item_key(r)
        it = items.setdefault(k, {'key': list(k), 'name': r['item_name'], 'category': cat, 'base_unit': r['base_unit'],
                                  'unit': r['unit_entered'], 'c2d_qty': 0.0, 'd2c_qty': 0.0, 'c2d_qty_entered': 0.0,
                                  'd2c_qty_entered': 0.0, 'c2d_cost': 0.0, 'd2c_cost': 0.0, 'needs_link': False,
                                  'priced': True, 'transfer_ids': []})
        s = 'c2d' if r['from_location'] == 'chatham' else 'd2c'
        it[f'{s}_qty'] += r['qty_base'] or 0
        it[f'{s}_qty_entered'] += r['qty_entered'] or 0
        it[f'{s}_cost'] += r['total_cost'] or 0
        it['needs_link'] |= bool(r['needs_link'])
        it['priced'] &= r['total_cost'] is not None
        it['transfer_ids'].append(r['id'])
    lines = []
    for it in items.values():
        it['net_qty'] = round(it['c2d_qty'] - it['d2c_qty'], 4)          # + = Dennis has Chatham's stock
        it['net_cost'] = round(it['c2d_cost'] - it['d2c_cost'], 2)
        it['net_qty_entered'] = round(it['c2d_qty_entered'] - it['d2c_qty_entered'], 4)
        it['direction'] = 'chatham_to_dennis' if it['net_cost'] > 0 or (it['net_cost'] == 0 and it['net_qty'] > 0) \
            else 'dennis_to_chatham' if (it['net_cost'] < 0 or it['net_qty'] < 0) else 'even'
        lines.append(it)
    lines.sort(key=lambda l: (-abs(l['net_cost']), l['name']))
    net = round(sum(l['net_cost'] for l in lines), 2)
    cats = [{'category': c, 'chatham_to_dennis': round(v['chatham_to_dennis'], 2),
             'dennis_to_chatham': round(v['dennis_to_chatham'], 2),
             'net': round(v['chatham_to_dennis'] - v['dennis_to_chatham'], 2)} for c, v in sorted(by_cat.items())]
    owes = None
    if abs(net) >= 0.005:
        debtor, creditor = ('dennis', 'chatham') if net > 0 else ('chatham', 'dennis')
        owes = {'debtor': debtor, 'creditor': creditor, 'amount': abs(net),
                'debtor_entity': ENTITY_FULL[debtor], 'creditor_entity': ENTITY_FULL[creditor]}
    detail = [dict(id=r['id'], date=r['business_date'], from_location=r['from_location'], to_location=r['to_location'],
                   item=r['item_name'], qty=r['qty_entered'], unit=r['unit_entered'], qty_base=r['qty_base'],
                   base_unit=r['base_unit'], cost=r['total_cost'], cost_source=r['cost_source'], by=r['entered_by'],
                   said=r['raw_text'], status=r['status'], is_settlement=r['is_settlement'], needs_link=r['needs_link'],
                   void_reason=r['void_reason'])
              for r in rows if include_voided or r['status'] == 'logged']
    return {'month': label, 'net': net, 'sentence': owes_sentence(net), 'owes': owes, 'categories': cats,
            'lines': lines, 'detail': detail, 'returns': [dict(id=r['id'], item=r['item_name'], qty=r['qty_entered'],
                                                               unit=r['unit_entered'], cost=r['total_cost'],
                                                               date=r['business_date']) for r in returns],
            'unpriced': [dict(id=r['id'], item=r['item_name']) for r in unpriced],
            'needs_link': sum(1 for r in activity if r['needs_link']),
            'voided': sum(1 for r in rows if r['status'] == 'voided')}


# ---------------------------------------------------------------------------
# Returns in kind (8H4) — filled in with the settlement tables.
# ---------------------------------------------------------------------------

def open_return_for(conn, src, dst, from_pid, to_pid):
    """An open 'return due' this transfer settles: src owes dst this item."""
    try:
        r = conn.execute("""
            SELECT sl.*, s.month FROM settlement_lines sl JOIN intercompany_settlements s ON s.id = sl.settlement_id
            WHERE sl.method = 'return' AND sl.status = 'open' AND sl.return_from = ? AND sl.return_to = ?
              AND (sl.chatham_product_id IN (?, ?) OR sl.dennis_product_id IN (?, ?))
            ORDER BY sl.id LIMIT 1""", (src, dst, from_pid, to_pid or -1, from_pid, to_pid or -1)).fetchone()
    except Exception:
        return None
    return dict(r) if r else None


def close_return(conn, line_id, transfer_id, qty_base):
    """Caller holds the transaction. Partial returns leave the line open."""
    line = conn.execute("SELECT * FROM settlement_lines WHERE id = ?", (line_id,)).fetchone()
    got = (line['returned_qty'] or 0) + (qty_base or 0)
    done = got >= abs(line['net_qty']) - 1e-6
    conn.execute("""UPDATE settlement_lines SET returned_qty = ?, status = ?, closed_by_transfer_id = ?,
                    closed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE closed_at END WHERE id = ?""",
                 (got, 'returned' if done else 'open', transfer_id, 1 if done else 0, line_id))
