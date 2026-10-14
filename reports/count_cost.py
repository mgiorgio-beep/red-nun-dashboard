"""
Completed counts: what each is worth, and the cost between two of them (2026-10-14).

A count is Food or Booze (inventory_counts.notes, the count page's type). Its
dollar value is each line's quantity x what one counted unit costs
(reports.count_units.unit_cost, with the unit the line RECORDED). A line with no
price is listed as "price needed", never guessed.

Actual cost between two counts of the same house and type:

    beginning count + purchases - ending count = cost of goods

  purchases — confirmed invoice lines on COGS accounts plus transfers between the
              houses (reports.cost_of_sales, the P&L's own numbers), invoice
              dates from the day AFTER the beginning count through the ending
              count's day (a delivery on count day is on the shelf when counted)
  sales     — the sales journal's Gross Sales lines from the beginning count's
              day through the day BEFORE the ending count (counts are taken
              before service)

Food counts cover FOOD; booze counts cover BEER + LIQUOR + WINE. Supplies and NA
beverages a food count walks are valued and listed, but stay out of food cost,
the same as the P&L.

Theoretical cost is the other side: what the recipes say the items sold should
have cost (menu item -> pmix_mapping -> recipe cost per serving). It only covers
items mapped to a costed recipe, so `coverage_pct` says how much of the sales it
represents. Actual minus theoretical is the variance: waste, over-portioning,
comps, theft, or missing recipes.
"""
from datetime import date, timedelta

from reports.count_units import unit_cost
from reports.cost_of_sales import cost_of_sales

TYPE_CATS = {'food': ('FOOD',), 'booze': ('BEER', 'LIQUOR', 'WINE')}
# recipes.category values that are food (a couple of prep recipes are lower-case)
FOOD_RECIPE_CATS = ('FOOD', 'sauce', 'dressing')


def count_type(notes):
    """'Food count' -> 'food'. Mirrors count.html countTypeOf()."""
    n = (notes or '').lower()
    if n.startswith('weekly'):
        return 'weekly'
    if 'booze' in n:
        return 'booze'
    if 'food' in n:
        return 'food'
    return 'full'


def _r2(x):
    return round(x or 0, 2)


def count_lines(conn, count_id):
    """Every counted line of one count, with its value."""
    rows = conn.execute("""
        SELECT i.product_id, i.counted_quantity AS qty, i.unit,
               COALESCE(NULLIF(p.display_name, ''), p.name) AS product, p.category,
               COALESCE(sl.name, 'No area') AS area, ss.name AS shelf
        FROM inventory_count_items i
        JOIN products p ON p.id = i.product_id
        LEFT JOIN storage_locations sl ON sl.id = i.storage_location_id
        LEFT JOIN storage_sections ss ON ss.id = i.section_id
        WHERE i.count_id = ? AND i.counted_quantity IS NOT NULL
        ORDER BY sl.name, ss.sort_order, ss.name, p.name
    """, (count_id,)).fetchall()
    costs = {}
    out = []
    for r in rows:
        key = (r['product_id'], (r['unit'] or '').lower())
        if key not in costs:
            costs[key] = unit_cost(conn, r['product_id'], r['unit'])
        uc = costs[key]
        cost = uc['cost']
        out.append({
            'product_id': r['product_id'], 'product': r['product'], 'category': r['category'],
            'area': r['area'], 'shelf': r['shelf'], 'qty': r['qty'], 'unit': r['unit'],
            'unit_cost': cost, 'basis': uc['basis'],
            'value': _r2(r['qty'] * cost) if cost is not None else None,
        })
    return out


def summarize(lines, ctype):
    """Totals for a count: all lines, and the part that counts toward food / bev cost."""
    cats = TYPE_CATS.get(ctype)
    total = sum(l['value'] or 0 for l in lines)
    cost_value = sum(l['value'] or 0 for l in lines if cats is None or (l['category'] or '').upper() in cats)
    by_cat = {}
    for l in lines:
        c = by_cat.setdefault(l['category'] or 'OTHER', {'category': l['category'] or 'OTHER', 'items': 0, 'value': 0.0})
        c['items'] += 1
        c['value'] += l['value'] or 0
    return {
        'lines': len(lines),
        'products': len({l['product_id'] for l in lines}),
        'total_value': _r2(total),
        'cost_value': _r2(cost_value),           # FOOD for a food count, beer+liquor+wine for booze
        'unpriced': sum(1 for l in lines if l['value'] is None),
        'by_category': sorted(({**c, 'value': _r2(c['value'])} for c in by_cat.values()), key=lambda c: -c['value']),
    }


def completed_counts(conn, location=None):
    sql = "SELECT * FROM inventory_counts WHERE status = 'completed'"
    args = []
    if location:
        sql += " AND location = ?"
        args.append(location)
    out = []
    for c in conn.execute(sql + " ORDER BY count_date DESC, id DESC", args).fetchall():
        ctype = count_type(c['notes'])
        s = summarize(count_lines(conn, c['id']), ctype)
        if not s['lines']:
            continue
        s.pop('by_category')
        out.append({'id': c['id'], 'location': c['location'], 'count_date': c['count_date'],
                    'completed_at': c['completed_at'], 'type': ctype, 'notes': c['notes'],
                    'created_by': c['created_by'], **s})
    return out


def _day(iso, delta):
    return (date.fromisoformat(iso) + timedelta(days=delta)).isoformat()


def theoretical(conn, location, start, end, ctype):
    """Recipe cost of what sold (start..end, YYYY-MM-DD) for food or booze."""
    cats = FOOD_RECIPE_CATS if ctype == 'food' else TYPE_CATS['booze']
    s, e = start.replace('-', ''), end.replace('-', '')
    ph = ','.join('?' * len(cats))
    rows = conn.execute(f"""
        SELECT oi.item_name, SUM(oi.quantity) AS qty, SUM(oi.price) AS revenue,
               r.name AS recipe, r.cost_per_serving AS cps, COALESCE(pm.multiplier, 1) AS mult
        FROM order_items oi
        JOIN pmix_mapping pm ON pm.menu_item_name = oi.item_name
        JOIN recipes r ON r.id = pm.recipe_id
        WHERE oi.voided = 0 AND oi.location = ? AND oi.business_date BETWEEN ? AND ?
          AND r.category IN ({ph}) AND r.cost_per_serving > 0
        GROUP BY oi.item_name
    """, (location, s, e, *cats)).fetchall()
    items = [{'item': r['item_name'], 'recipe': r['recipe'], 'qty': r['qty'] or 0,
              'revenue': _r2(r['revenue']), 'cost': _r2((r['qty'] or 0) * r['cps'] * r['mult'])} for r in rows]
    costed_rev = sum(i['revenue'] for i in items)
    cost = sum(i['cost'] for i in items)
    return {'cost': _r2(cost), 'costed_sales': _r2(costed_rev),
            'pct': round(cost / costed_rev * 100, 1) if costed_rev > 0 else None,
            'items': sorted(items, key=lambda i: -i['cost'])[:25]}


def cost_between(conn, begin_id, end_id):
    """Actual (and theoretical) cost between two completed counts of the same house and type."""
    b = conn.execute("SELECT * FROM inventory_counts WHERE id = ?", (begin_id,)).fetchone()
    e = conn.execute("SELECT * FROM inventory_counts WHERE id = ?", (end_id,)).fetchone()
    if not b or not e:
        return {'error': 'Count not found'}
    if b['location'] != e['location']:
        return {'error': 'Both counts must be from the same house'}
    ctype = count_type(b['notes'])
    if ctype != count_type(e['notes']):
        return {'error': 'Both counts must be the same type (Food with Food, Booze with Booze)'}
    if ctype not in TYPE_CATS:
        return {'error': 'Cost needs Food or Booze counts'}
    if (b['count_date'], b['id']) > (e['count_date'], e['id']):
        b, e = e, b
    if b['count_date'] == e['count_date']:
        return {'error': 'The two counts are on the same day'}
    loc, start, end = b['location'], b['count_date'], e['count_date']
    cats = TYPE_CATS[ctype]

    bs = summarize(count_lines(conn, b['id']), ctype)
    es = summarize(count_lines(conn, e['id']), ctype)
    bs.pop('by_category'), es.pop('by_category')

    buy = cost_of_sales(loc, _day(start, 1), end, conn=conn)
    sold = cost_of_sales(loc, start, _day(end, -1), conn=conn)
    if ctype == 'food':
        purchases, sales, vendors = buy['food']['cost'], sold['food']['sales'], buy['food']['vendors']
    else:
        purchases, sales, vendors = buy['bev']['cost'], sold['bev']['sales'], buy['bev']['vendors']

    actual = _r2(bs['cost_value'] + purchases - es['cost_value'])
    theo = theoretical(conn, loc, start, _day(end, -1), ctype)
    actual_pct = round(actual / sales * 100, 1) if sales > 0 else None
    return {
        'location': loc, 'type': ctype, 'categories': list(cats),
        'begin': {'id': b['id'], 'date': start, **bs},
        'end': {'id': e['id'], 'date': end, **es},
        'purchases': _r2(purchases), 'purchase_dates': [_day(start, 1), end], 'vendors': vendors[:10],
        'sales': _r2(sales), 'sales_dates': [start, _day(end, -1)],
        'sales_days': sold['journal_days'].get(loc, 0),
        'actual_cost': actual, 'actual_pct': actual_pct,
        'theoretical': {**theo, 'coverage_pct': round(theo['costed_sales'] / sales * 100, 1) if sales > 0 else None},
        'variance_pct': round(actual_pct - theo['pct'], 1) if actual_pct is not None and theo['pct'] is not None else None,
    }
