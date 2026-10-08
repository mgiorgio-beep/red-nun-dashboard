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


def short_names(conn, rows, house_of=lambda r: r['from_location'], pid_of=lambda r: r['from_product_id']):
    """{row id: card name} — the same short names as the confirm card."""
    from reports.item_recognition import card_name
    cache, out = {}, {}
    for r in rows:
        k = (pid_of(r), house_of(r))
        if k not in cache:
            p = conn.execute("SELECT * FROM products WHERE id = ?", (k[0],)).fetchone()
            cache[k] = card_name(conn, p, k[1]) if p else r['item_name']
        out[r['id']] = cache[k]
    return out


def _rows(conn, start, end, include_voided=False):
    from reports.moves import ensure_tables
    ensure_tables(conn)
    rows = conn.execute(f"""
        SELECT t.*, COALESCE(cn.card_name, p.display_name, p.name) AS item_name
        FROM inventory_transfers t
        JOIN products p ON p.id = t.from_product_id
        LEFT JOIN product_card_names cn ON cn.product_id = t.from_product_id AND cn.status = 'approved'
        WHERE t.business_date >= ? AND t.business_date < ? {'' if include_voided else "AND t.status = 'logged'"}
        ORDER BY t.business_date, t.id
    """, (start, end)).fetchall()
    names = short_names(conn, rows)
    return [dict(r, item_name=names[r['id']]) for r in rows]


def month_net(conn, ym):
    """Running line for the emails: what is open between the houses right now
    (every transfer, less returns and checks)."""
    import calendar
    _, _, label = _months(ym)
    net = round(sum(p['cost'] for p in positions(conn).values()), 2)
    return {'net': net, 'label': calendar.month_abbr[int(label[5:])], 'sentence': owes_sentence(net) + ' open'}


def build_statement(conn, ym, include_voided=False):
    start, end, label = _months(ym)
    rows = _rows(conn, start, end, include_voided=True)
    live = [r for r in rows if r['status'] == 'logged']
    # Returns count as activity in the month they happen (the month-close entries
    # book them too), so a borrow and its return in the same month net to even.
    activity = live
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
# Settlement (8H, as Mike set it 2026-10-08)
#
# Everyday stock (fries, burgers, oil) is returned in kind: someone says
# "bringing 2 cases of fries back to Dennis" and the item's open line shrinks.
# A return is valued at what the open cases cost when they were borrowed, so the
# item nets to exactly $0 on the intercompany accounts (a price change in between
# lands in the returning house's food cost, not in intercompany).
# When Mike presses Reconcile, whatever is still open (minus lines he keeps open)
# is paid with ONE check from the house that owes, on its own bank and stock.
#
# Books (QBO, via the existing JE store + push; Mike pushes):
#   month close, per house: Dr/Cr <category> COGS against Intercompany (8H1)
#   settlement:  payer  Dr Intercompany / Cr Cash (its bank)
#                payee  Dr Cash (its bank) / Cr Intercompany
# Accounts are resolved by id from move_settings + qb_line_mapping (never by name).
# ---------------------------------------------------------------------------

CAT_JOURNAL = {'FOOD': 'Food', 'BEER': 'Beer', 'LIQUOR': 'Liquor', 'WINE': 'Wine', 'NA_BEVERAGES': 'NA Beverage'}
IC_JOURNAL = {'chatham': 'Intercompany: Red Nun Public House', 'dennis': 'Intercompany: Red Buoy'}
CASH_JOURNAL = {'chatham': 'Intercompany cash: Cape Cod Five (5975)', 'dennis': 'Intercompany cash: Cape Cod Five (2757)'}
BANK_ACCOUNT = {'chatham': 1, 'dennis': 2}          # bank_accounts.id, checked against location on use
PAY_BY_DEFAULT = {'LIQUOR', 'WINE'}                 # power buys are paid; the rest usually comes back


def ensure_settlement_tables(conn):
    from reports.moves import ensure_tables
    ensure_tables(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intercompany_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payer TEXT NOT NULL CHECK (payer IN ('chatham', 'dennis')),
            payee TEXT NOT NULL CHECK (payee IN ('chatham', 'dennis')),
            amount REAL NOT NULL CHECK (amount > 0),
            status TEXT NOT NULL DEFAULT 'approved',      -- approved | printed | cleared | voided
            approved_by TEXT,
            approved_at TEXT DEFAULT CURRENT_TIMESTAMP,
            manual_check_id INTEGER,
            check_number TEXT,
            payer_register_id INTEGER,                    -- manual_bank_entries: outstanding check
            payee_register_id INTEGER,                    -- manual_bank_entries: deposit in transit
            payer_je_id INTEGER,
            payee_je_id INTEGER,
            memo TEXT,
            CHECK (payer <> payee)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settlement_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            settlement_id INTEGER NOT NULL REFERENCES intercompany_settlements(id),
            chatham_product_id INTEGER,
            dennis_product_id INTEGER,
            item_name TEXT,
            category_type TEXT,
            owed_by TEXT NOT NULL,                        -- the house that had the other's stock
            qty_base REAL,
            base_unit TEXT,
            cost REAL NOT NULL,
            method TEXT NOT NULL DEFAULT 'pay',
            status TEXT NOT NULL DEFAULT 'paid'
        )
    """)


def _all_transfers(conn, through=None):
    ensure_settlement_tables(conn)
    q = """SELECT t.*, COALESCE(cn.card_name, p.display_name, p.name) AS item_name
           FROM inventory_transfers t JOIN products p ON p.id = t.from_product_id
           LEFT JOIN product_card_names cn ON cn.product_id = t.from_product_id AND cn.status = 'approved'
           WHERE t.status = 'logged'"""
    args = ()
    if through:
        q += " AND t.business_date < ?"
        args = (through,)
    rows = conn.execute(q + " ORDER BY t.business_date, t.id", args).fetchall()
    names = short_names(conn, rows)
    return [dict(r, item_name=names[r['id']]) for r in rows]


def positions(conn):
    """Every item's open position: + = Dennis has Chatham's stock (Dennis owes)."""
    pos = {}
    for r in _all_transfers(conn):
        k = item_key(r)
        sign = 1 if r['from_location'] == 'chatham' else -1
        p = pos.setdefault(k, {'key': k, 'name': r['item_name'], 'category_type': r['category_type'],
                               'base_unit': r['base_unit'], 'qty': 0.0, 'cost': 0.0, 'unpriced': 0,
                               'needs_link': False, 'last_date': r['business_date'], 'transfer_ids': []})
        p['qty'] += sign * (r['qty_base'] or 0)
        if r['total_cost'] is None:
            p['unpriced'] += 1
        else:
            p['cost'] += sign * r['total_cost']
        p['needs_link'] |= bool(r['needs_link'])
        p['last_date'] = max(p['last_date'], r['business_date'])
        p['transfer_ids'].append(r['id'])
    for l in conn.execute("SELECT * FROM settlement_lines WHERE status = 'paid'").fetchall():
        k = (l['chatham_product_id'], l['dennis_product_id'])
        if k in pos:
            sign = 1 if l['owed_by'] == 'dennis' else -1
            pos[k]['qty'] -= sign * (l['qty_base'] or 0)
            pos[k]['cost'] -= sign * l['cost']
    for p in pos.values():
        p['qty'], p['cost'] = round(p['qty'], 4), round(p['cost'], 2)
    return pos


def open_items(conn):
    """The Reconcile page: items still owed, biggest first, with the suggested method."""
    out = []
    for p in positions(conn).values():
        if abs(p['qty']) < 1e-6 and abs(p['cost']) < 0.005:
            continue
        owed_by = 'dennis' if (p['cost'] > 0 or (p['cost'] == 0 and p['qty'] > 0)) else 'chatham'
        out.append(dict(p, key=list(p['key']), owed_by=owed_by, owed_to=OTHER[owed_by],
                        abs_qty=abs(p['qty']), abs_cost=abs(p['cost']),
                        suggested='pay' if (p['category_type'] or '') in PAY_BY_DEFAULT else 'return'))
    out.sort(key=lambda x: (-x['abs_cost'], x['name']))
    return out


OTHER = {'chatham': 'dennis', 'dennis': 'chatham'}


def open_return_for(conn, src, dst, from_pid, to_pid):
    """A transfer src -> dst of an item src owes dst is a return. Returns the open
    quantity and the average cost it was borrowed at, or None."""
    row = {'from_location': src, 'from_product_id': from_pid, 'to_product_id': to_pid}
    k = item_key(row)
    p = positions(conn).get(k)
    if not p or abs(p['qty']) < 1e-6:
        return None
    src_owes = (p['qty'] > 0) == (src == 'dennis')      # + = Dennis has Chatham's stock
    if not src_owes or p['unpriced']:
        return None
    return {'id': None, 'open_qty': abs(p['qty']), 'unit_cost_base': abs(p['cost']) / abs(p['qty'])}


def value_return(ret, qty_base, current_unit_cost):
    """Up to the open quantity at the borrowed cost; anything beyond is a new transfer."""
    q = qty_base or 0
    inside = min(q, ret['open_qty'])
    beyond = max(0.0, q - ret['open_qty'])
    if beyond and current_unit_cost is None:
        return None
    return round(inside * ret['unit_cost_base'] + beyond * (current_unit_cost or 0), 2)


# --- monthly transfer entries (8H1) -----------------------------------------

def _month_end(ym):
    import calendar
    y, m = int(ym[:4]), int(ym[5:7])
    return f'{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}'


def _mapping(conn, location):
    """journal_name -> current QBO id, through gl_accounts (the spine)."""
    return {r['journal_name']: r['qbo_id'] for r in conn.execute(
        """SELECT m.journal_name, g.qbo_id FROM qb_line_mapping m
           JOIN gl_accounts g ON g.id = m.gl_account_id AND g.active = 1 AND g.location = m.location
           WHERE m.location = ?""", (location,))}


def _entry(conn, location, entry_type, entry_date, je_name, lines, note_unpriced=0):
    mp = _mapping(conn, location)
    items, dr, cr = [], 0.0, 0.0
    for i, (jn, d, c) in enumerate(lines, 1):
        d, c = round(d or 0, 2), round(c or 0, 2)
        if not d and not c:
            continue
        items.append({'journal_name': jn, 'qbo_account': mp.get(jn), 'debit': d or None, 'credit': c or None,
                      'mapped': bool(mp.get(jn)), 'sort_order': i})
        dr, cr = dr + d, cr + c
    balanced = abs(round((dr - cr) * 100)) == 0
    status = 'ready' if balanced and items and all(li['mapped'] for li in items) and not note_unpriced else 'needs_attention'
    return {'entry_type': entry_type, 'location': location, 'entry_date': entry_date, 'je_name': je_name,
            'total_debits': round(dr, 2), 'total_credits': round(cr, 2), 'balanced': balanced, 'status': status,
            'line_items': items}


def build_month_entries(conn, ym, persist=True):
    """One summary entry per house for the month's transfers (returns included,
    at their cost). Status 'ready' only when every line maps and nothing is unpriced."""
    start, end, label = _months(ym)
    rows = conn.execute("""SELECT * FROM inventory_transfers WHERE status = 'logged'
                           AND business_date >= ? AND business_date < ?""", (start, end)).fetchall()
    unpriced = sum(1 for r in rows if r['total_cost'] is None)
    out = {}
    for loc in ('chatham', 'dennis'):
        net_cat = {}
        for r in rows:
            if r['total_cost'] is None:
                continue
            cat = r['category_type'] if r['category_type'] in CAT_JOURNAL else 'FOOD'
            if r['to_location'] == loc:
                net_cat[cat] = net_cat.get(cat, 0) + r['total_cost']     # came in: it's our cost
            elif r['from_location'] == loc:
                net_cat[cat] = net_cat.get(cat, 0) - r['total_cost']     # went out: not our cost
        lines = []
        for cat, v in sorted(net_cat.items()):
            jn = f'Intercompany transfers: {CAT_JOURNAL[cat]}'
            lines.append((jn, v if v > 0 else 0, -v if v < 0 else 0))
        ic = -sum(net_cat.values())             # sent more than received -> the other house owes us (debit)
        lines.append((IC_JOURNAL[loc], ic if ic > 0 else 0, -ic if ic < 0 else 0))
        e = _entry(conn, loc, 'intercompany_transfers', _month_end(label),
                   f"IC-TRF-{label.replace('-', '')}", lines, unpriced)
        if not e['line_items']:
            continue
        if persist:
            from reports.sales_journal import persist_journal_entry
            e['id'] = persist_journal_entry(e)
        out[loc] = e
    return out


# --- Reconcile: pay what's still open with one check (8H5) ------------------

def preview_reconcile(conn, keep_keys=()):
    """What pressing Reconcile would do. keep_keys: items Mike is still returning."""
    keep = {tuple(k) for k in keep_keys}
    pay = [i for i in open_items(conn) if tuple(i['key']) not in keep and not i['unpriced']]
    dennis_owes = round(sum(i['cost'] for i in pay), 2)        # + = Dennis owes Chatham
    if abs(dennis_owes) < 0.005:
        return {'lines': pay, 'amount': 0.0, 'payer': None, 'payee': None}
    payer = 'dennis' if dennis_owes > 0 else 'chatham'
    return {'lines': pay, 'amount': abs(dennis_owes), 'payer': payer, 'payee': OTHER[payer],
            'payer_entity': ENTITY_FULL[payer], 'payee_entity': ENTITY_FULL[OTHER[payer]],
            'kept': [i for i in open_items(conn) if tuple(i['key']) in keep]}


def _setting_int(conn, key):
    from reports.item_recognition import get_setting
    v = get_setting(conn, key)
    return int(v) if v not in (None, '') else None


def reconcile(conn, keep_keys, who, expected_amount):
    """Mike's one confirm: settlement + lines, the check (existing manual-check
    table, paying house's own stock), both register rows and both entries.
    All in one transaction. Refuses on any missing / mismatched house or bank."""
    from datetime import date
    pv = preview_reconcile(conn, keep_keys)
    if not pv['payer']:
        raise ValueError('Nothing to pay: what is open nets to $0.')
    if abs(pv['amount'] - float(expected_amount)) > 0.005:
        raise ValueError(f"The open balance changed (now ${pv['amount']:,.2f}). Reload and check again.")
    payer, payee, amt = pv['payer'], pv['payee'], pv['amount']
    for loc in (payer, payee):                     # the check and each register row on that house's OWN bank
        b = conn.execute("SELECT id, location FROM bank_accounts WHERE id = ?", (BANK_ACCOUNT[loc],)).fetchone()
        if not b or (b['location'] or '').lower() != loc:
            raise ValueError(f'Refusing: no {loc} bank account on file (bank_accounts #{BANK_ACCOUNT[loc]}).')
        if not _setting_int(conn, f'intercompany_gl_{loc}') or not _setting_int(conn, f'intercompany_bank_gl_{loc}'):
            raise ValueError(f'Refusing: intercompany or bank account not mapped for {loc}.')
    today = date.today().isoformat()
    memo = f"Intercompany settlement {today} — transfers"
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    try:
        sid = conn.execute("""INSERT INTO intercompany_settlements (payer, payee, amount, approved_by, memo)
                              VALUES (?, ?, ?, ?, ?)""", (payer, payee, amt, who, memo)).lastrowid
        for i in pv['lines']:
            conn.execute("""INSERT INTO settlement_lines (settlement_id, chatham_product_id, dennis_product_id, item_name,
                                category_type, owed_by, qty_base, base_unit, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (sid, i['key'][0], i['key'][1], i['name'], i['category_type'], i['owed_by'],
                          i['abs_qty'], i['base_unit'], i['abs_cost']))
        # the check: same row the Bill Pay manual-check endpoint writes, on the paying house
        chk = conn.execute("""INSERT INTO manual_checks (payee_name, amount, memo, location, check_type, created_at, updated_at)
                              VALUES (?, ?, ?, ?, 'intercompany', datetime('now'), datetime('now'))""",
                           (ENTITY_FULL[payee], amt, f'{memo} (settlement #{sid})', payer)).lastrowid
        # register rows: same columns as POST /api/register/<account>/manual, coded to Intercompany
        out_id = conn.execute("""INSERT INTO manual_bank_entries (bank_account_id, entry_date, entry_type, payee, memo, ref_number,
                                     amount, cleared, created_by, gl_account_id, gl_source, gl_status)
                                 VALUES (?, ?, 'other', ?, ?, NULL, ?, 0, ?, ?, 'intercompany', 'confirmed')""",
                              (BANK_ACCOUNT[payer], today, ENTITY_FULL[payee], f'{memo} (settlement #{sid}, check)', -amt, who,
                               _setting_int(conn, f'intercompany_gl_{payer}'))).lastrowid
        in_id = conn.execute("""INSERT INTO manual_bank_entries (bank_account_id, entry_date, entry_type, payee, memo, ref_number,
                                    amount, cleared, created_by, gl_account_id, gl_source, gl_status)
                                VALUES (?, ?, 'other', ?, ?, NULL, ?, 0, ?, ?, 'intercompany', 'confirmed')""",
                             (BANK_ACCOUNT[payee], today, ENTITY_FULL[payer], f'{memo} (settlement #{sid}, deposit)', amt, who,
                              _setting_int(conn, f'intercompany_gl_{payee}'))).lastrowid
        conn.execute("""UPDATE intercompany_settlements SET manual_check_id = ?, payer_register_id = ?, payee_register_id = ?
                        WHERE id = ?""", (chk, out_id, in_id, sid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # the two entries, through the existing JE store (Mike pushes them; nothing posts by itself)
    from reports.sales_journal import persist_journal_entry
    pe = _entry(conn, payer, 'intercompany_settlement', today, f'IC-SET-{sid}',
                [(IC_JOURNAL[payer], amt, 0), (CASH_JOURNAL[payer], 0, amt)])
    re_ = _entry(conn, payee, 'intercompany_settlement', today, f'IC-SET-{sid}',
                 [(CASH_JOURNAL[payee], amt, 0), (IC_JOURNAL[payee], 0, amt)])
    pid, rid = persist_journal_entry(pe), persist_journal_entry(re_)
    conn.execute("UPDATE intercompany_settlements SET payer_je_id = ?, payee_je_id = ? WHERE id = ?", (pid, rid, sid))
    conn.commit()
    return settlement(conn, sid)


def sync_check_numbers(conn):
    """When Bill Pay prints the check, carry its number to both register rows so
    bank rec matches the check and the deposit by number."""
    ensure_settlement_tables(conn)
    n = 0
    for s in conn.execute("""SELECT s.id, s.payer_register_id, s.payee_register_id, mc.check_number, mc.voided
                             FROM intercompany_settlements s JOIN manual_checks mc ON mc.id = s.manual_check_id
                             WHERE s.status = 'approved' AND mc.check_number IS NOT NULL""").fetchall():
        conn.execute("UPDATE manual_bank_entries SET ref_number = ? WHERE id IN (?, ?) AND ref_number IS NULL",
                     (s['check_number'], s['payer_register_id'], s['payee_register_id']))
        conn.execute("UPDATE intercompany_settlements SET check_number = ?, status = 'printed' WHERE id = ?",
                     (s['check_number'], s['id']))
        n += 1
    conn.commit()
    return n


def settlement(conn, sid):
    s = conn.execute("SELECT * FROM intercompany_settlements WHERE id = ?", (sid,)).fetchone()
    if not s:
        return None
    out = dict(s)
    out['lines'] = [dict(r) for r in conn.execute("SELECT * FROM settlement_lines WHERE settlement_id = ?", (sid,))]
    out['check'] = dict(conn.execute("SELECT id, check_number, printed_at, voided FROM manual_checks WHERE id = ?",
                                     (s['manual_check_id'],)).fetchone() or {})
    regs = {r['id']: dict(r) for r in conn.execute("SELECT id, cleared, cleared_date, ref_number FROM manual_bank_entries WHERE id IN (?, ?)",
                                                   (s['payer_register_id'], s['payee_register_id']))}
    out['check_cleared'] = bool(regs.get(s['payer_register_id'], {}).get('cleared'))
    out['deposit_cleared'] = bool(regs.get(s['payee_register_id'], {}).get('cleared'))
    out['entries'] = [dict(r) for r in conn.execute("SELECT id, location, je_name, status, qbo_txn_id, qbo_error FROM qb_journal_entries WHERE id IN (?, ?)",
                                                    (s['payer_je_id'], s['payee_je_id']))]
    return out


# --- Tie-out (8H6): it must be able to fail ---------------------------------

def _ic_balance(conn, location, posted_only=False):
    """Balance of a house's Intercompany account in our entries (debit +)."""
    q = """SELECT ROUND(COALESCE(SUM(COALESCE(li.debit, 0) - COALESCE(li.credit, 0)), 0), 2)
           FROM qb_journal_line_items li JOIN qb_journal_entries e ON e.id = li.entry_id
           WHERE e.location = ? AND li.journal_name = ?
             AND e.entry_type IN ('intercompany_transfers', 'intercompany_settlement')"""
    if posted_only:
        q += " AND e.status = 'posted'"
    return conn.execute(q, (location, IC_JOURNAL[location])).fetchone()[0] or 0.0


def expected_balance(conn, through):
    """What Chatham's Intercompany should hold from the records: every transfer
    before `through` (YYYYMMDD) at its cost, less what checks paid. + = Dennis owes."""
    net = 0.0
    for r in _all_transfers(conn, through=through):
        if r['total_cost'] is not None:
            net += r['total_cost'] if r['from_location'] == 'chatham' else -r['total_cost']
    for s in conn.execute("SELECT payer, amount FROM intercompany_settlements WHERE status <> 'voided'"):
        net -= s['amount'] if s['payer'] == 'dennis' else -s['amount']     # a check from Dennis pays down what Dennis owes
    return round(net, 2)


def tie_out(conn, qbo_balances=None):
    """Chatham's Intercompany must equal minus Dennis's, and both must equal what the
    transfers and settlements say through the last closed month. Lists what's missing.
    qbo_balances: optional {'chatham': x, 'dennis': y} read from QBO (posted truth)."""
    ensure_settlement_tables(conn)
    from reports.moves import now_et
    first = now_et().strftime('%Y%m01')
    c, d = _ic_balance(conn, 'chatham'), _ic_balance(conn, 'dennis')
    exp = expected_balance(conn, first)
    problems = []
    if abs(c + d) >= 0.01:
        problems.append(f"Chatham's Intercompany is {c:+,.2f} but Dennis's is {d:+,.2f}; they should be equal and opposite "
                        f"(off by ${abs(c + d):,.2f}).")
    if abs(c - exp) >= 0.01:
        problems.append(f"Chatham's Intercompany entries total {c:+,.2f}; the transfers and checks say {exp:+,.2f} "
                        f"(off by ${abs(c - exp):,.2f}). A month's entries may be missing or out of date.")
    # one-sided entries: built / posted on one house only
    for t in ('intercompany_transfers', 'intercompany_settlement'):
        rows = conn.execute("""SELECT je_name, entry_date, GROUP_CONCAT(location || ':' || status) AS sides
                               FROM qb_journal_entries WHERE entry_type = ? GROUP BY je_name, entry_date""", (t,)).fetchall()
        for r in rows:
            sides = dict(x.split(':') for x in r['sides'].split(','))
            if set(sides) != {'chatham', 'dennis'}:
                problems.append(f"{r['je_name']} ({r['entry_date']}) exists only at {', '.join(sides)}.")
            elif (sides['chatham'] == 'posted') != (sides['dennis'] == 'posted'):
                problems.append(f"{r['je_name']} ({r['entry_date']}) is posted at one house only: {r['sides']}.")
    if qbo_balances:
        qc, qd = qbo_balances.get('chatham'), qbo_balances.get('dennis')
        if qc is not None and qd is not None and abs(qc + qd) >= 0.01:
            problems.append(f"In QuickBooks, Red Buoy's Intercompany is {qc:,.2f} and Red Nun Public House's is {qd:,.2f}; "
                            f"they should cancel (off by ${abs(qc + qd):,.2f}).")
    gap = round(c + d, 2)
    return {'ok': not problems, 'chatham': c, 'dennis': d, 'expected': exp, 'problems': problems,
            'headline': None if not problems else f"INTERCOMPANY OUT OF BALANCE: ${max(abs(gap), abs(c - exp)):,.2f}"}


# --- Open problems: banner, Friday open items, the monthly email ------------

def open_problems(conn):
    """[{level: 'red'|'amber', text, link}] — what needs Mike."""
    from datetime import date, timedelta
    from reports import move_notify
    ensure_settlement_tables(conn)
    out = []
    t = tie_out(conn)
    if not t['ok']:
        out.append({'level': 'red', 'text': t['headline'] + ' — ' + ' '.join(t['problems']), 'link': '/transfer/settle'})
    fails = move_notify.failures(conn)
    if fails:
        out.append({'level': 'red', 'text': f"{len(fails)} transfer/waste email(s) failed to send (entries are saved)",
                    'link': '/transfer/admin#problems'})
    n = conn.execute("SELECT COUNT(*) FROM inventory_transfers WHERE needs_link = 1 AND status = 'logged'").fetchone()[0]
    if n:
        out.append({'level': 'amber', 'text': f'{n} transfer(s) need a product link', 'link': '/transfer/admin#links'})
    today = date.today()
    first = today.strftime('%Y%m01')
    old14 = (today - timedelta(days=14)).strftime('%Y%m%d')
    items = open_items(conn)
    prior = [i for i in items if i['last_date'] < first]
    if prior:
        tot = sum(i['cost'] for i in prior)
        out.append({'level': 'amber', 'text': f"{len(prior)} item(s) from before this month still open between the houses "
                                              f"({owes_sentence(round(tot, 2))})", 'link': '/transfer/settle'})
    stale = [i for i in items if i['suggested'] == 'return' and i['last_date'] < old14]
    if stale:
        out.append({'level': 'amber', 'text': f"{len(stale)} return(s) open more than 14 days: "
                                              + ', '.join(f"{i['name']} ({HOUSE[i['owed_by']]} owes)" for i in stale[:4])
                                              + ' — bring back or switch to Pay', 'link': '/transfer/settle'})
    ten = (today - timedelta(days=10)).isoformat()
    for s in conn.execute("""SELECT s.id, s.payee, s.amount, s.approved_at FROM intercompany_settlements s
                             JOIN manual_bank_entries m ON m.id = s.payee_register_id
                             WHERE s.status <> 'voided' AND COALESCE(m.cleared, 0) = 0 AND s.approved_at < ?""", (ten,)):
        out.append({'level': 'amber', 'text': f"Settlement #{s['id']}: ${s['amount']:,.2f} not deposited at {HOUSE[s['payee']]} after 10 days",
                    'link': '/transfer/settle'})
    have = {r[0][:7] for r in conn.execute("SELECT entry_date FROM qb_journal_entries WHERE entry_type = 'intercompany_transfers'")}
    for (ym,) in conn.execute("""SELECT DISTINCT substr(business_date, 1, 6) FROM inventory_transfers
                                 WHERE status = 'logged' AND business_date < ?""", (first,)):
        if f'{ym[:4]}-{ym[4:]}' not in have:
            out.append({'level': 'amber', 'text': f"{ym[:4]}-{ym[4:]} transfers not booked to QuickBooks yet", 'link': '/transfer/settle'})
    w = conn.execute("SELECT COUNT(*) FROM waste_log WHERE flagged = 1 AND reviewed_at IS NULL AND status = 'logged'").fetchone()[0]
    if w:
        out.append({'level': 'amber', 'text': f'{w} waste entr{"y" if w == 1 else "ies"} over the review threshold', 'link': '/transfer/admin#waste'})
    return out
