"""
Weekly key-item count list (count_templates).

The weekly count is the 30-40 products per house that carry most of the
purchase dollars. `rank_key_items()` ranks products by confirmed-invoice spend
over the last N days; `count_templates` holds the list Mike actually counts.
Ranking never changes the list by itself: the list page shows the ranking as
suggestions and Mike adds or removes.
"""

from collections import defaultdict

BOOZE_CATS = ('BEER', 'LIQUOR', 'WINE')
# Invoice lines that can be inventory. Fees, supplies, services and tax are not counted.
INVENTORY_LINE_CATS = ('FOOD', 'BEER', 'LIQUOR', 'WINE', 'NA_BEVERAGES')
LOCATIONS = ('chatham', 'dennis')


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS count_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            frequency TEXT NOT NULL DEFAULT 'weekly',
            sort_order INTEGER DEFAULT 0,
            source TEXT,
            added_by TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(location, product_id, frequency)
        )
    """)


def group_of(category):
    return 'booze' if (category or '').upper() in BOOZE_CATS else 'food'


def rank_key_items(conn, location, days=90):
    """Products ranked by confirmed purchase $ at one house over `days`.

    Invoice lines resolve to a product through vendor_items: item code first,
    then the vendor's description, preferring the same vendor and active rows.
    Food/booze grouping comes from the product's category (invoice line
    categories are sometimes wrong, e.g. a kale mix filed as BEER).
    Returns [{product_id, name, category, group, dollars, lines}] best first.
    """
    by_code, by_desc = defaultdict(list), defaultdict(list)
    for r in conn.execute("""
        SELECT product_id, vendor_name, vendor_description, vendor_item_code, is_active, id
        FROM vendor_items WHERE product_id IS NOT NULL
    """):
        if r['vendor_item_code']:
            by_code[r['vendor_item_code']].append(r)
        by_desc[r['vendor_description']].append(r)

    ph = ','.join('?' * len(INVENTORY_LINE_CATS))
    lines = conn.execute(f"""
        SELECT sii.product_name, sii.vendor_item_code, sii.total_price, si.vendor_name
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON si.id = sii.invoice_id
        WHERE si.status = 'confirmed' AND si.location = ?
          AND si.invoice_date >= date('now', ?)
          AND sii.category_type IN ({ph})
    """, (location, f'-{int(days)} day', *INVENTORY_LINE_CATS)).fetchall()

    dollars, count = defaultdict(float), defaultdict(int)
    for l in lines:
        cands = (by_code.get(l['vendor_item_code']) if l['vendor_item_code'] else None) \
            or by_desc.get(l['product_name']) or []
        if not cands:
            continue
        best = max(cands, key=lambda r: (r['vendor_name'] == l['vendor_name'], r['is_active'] or 0, r['id']))
        dollars[best['product_id']] += l['total_price'] or 0
        count[best['product_id']] += 1
    if not dollars:
        return []

    ids = list(dollars)
    prods = {r['id']: r for r in conn.execute(
        f"SELECT id, name, display_name, category, active FROM products WHERE id IN ({','.join('?' * len(ids))})", ids)}
    out = []
    for pid, d in sorted(dollars.items(), key=lambda kv: -kv[1]):
        p = prods.get(pid)
        if not p or not p['active']:
            continue
        out.append({'product_id': pid, 'name': p['display_name'] or p['name'], 'category': p['category'],
                    'group': group_of(p['category']), 'dollars': round(d, 2), 'lines': count[pid]})
    return out


def seed_weekly(conn, location, food_n=25, beer_n=10, spirits_n=8, days=90, added_by='seed'):
    """Put the top food, beer, and liquor+wine products on the weekly list.
    Beer and liquor/wine get separate slots: kegs carry the bar's dollars and
    would otherwise crowd out every bottle. Only adds; never removes what Mike
    already has. Returns the number added."""
    ensure_tables(conn)
    ranked = rank_key_items(conn, location, days)
    cat = lambda r: (r['category'] or '').upper()
    pick = [r for r in ranked if r['group'] == 'food'][:food_n] + \
           [r for r in ranked if cat(r) == 'BEER'][:beer_n] + \
           [r for r in ranked if cat(r) in ('LIQUOR', 'WINE')][:spirits_n]
    added = 0
    for i, r in enumerate(pick):
        cur = conn.execute("""
            INSERT OR IGNORE INTO count_templates (location, product_id, frequency, sort_order, source, added_by)
            VALUES (?, ?, 'weekly', ?, ?, ?)
        """, (location, r['product_id'], i, f"top {days}d purchases ${r['dollars']:,.0f}", added_by))
        added += cur.rowcount
    return added


def weekly_product_ids(conn, location):
    ensure_tables(conn)
    return [r[0] for r in conn.execute("""
        SELECT ct.product_id FROM count_templates ct JOIN products p ON p.id = ct.product_id
        WHERE ct.location = ? AND ct.frequency = 'weekly' AND p.active = 1
        ORDER BY ct.sort_order, ct.id
    """, (location,))]


def last_weekly_count(conn, location):
    """Most recent saved weekly count at a house, or None.
    The count page saves to inventory_counts with notes 'Weekly key-item count'."""
    return conn.execute("""
        SELECT c.id, c.created_at, COUNT(ci.id) AS items
        FROM inventory_counts c JOIN inventory_count_items ci ON ci.count_id = c.id
        WHERE c.location = ? AND c.notes LIKE 'Weekly%'
        GROUP BY c.id
        ORDER BY c.created_at DESC LIMIT 1
    """, (location,)).fetchone()
