"""
Weekly key-item count list (count_templates).

The weekly count is the 30-40 products per house that SELL most (Mike,
2026-10-05: the weekly full count took too long; count the top sellers weekly,
everything once a month). `rank_by_sales()` ranks products from Toast sales;
`rank_key_items()` (purchase $) still breaks ties in the voice matcher.
`count_templates` holds the list Mike actually counts.
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


# A drink's name names its bottle only on these menus. Tap, bottle and wine names
# are plain product names, so a loose match is enough ("Coors Lt", "Mezzacorona PG");
# shots and cordials need a close one. Items missing from Toast's menu list (straight
# pours like "Tito's", "Well Vodka") need a near-exact one. Cocktails and soft drinks
# never match by name ("Martini" is not Martini & Rossi, "Cosmo" is not Glendalough,
# "Iced Tea" is not Sun Cruiser): their booze comes from their recipe.
_NAME_MATCH_MIN = {'draft beer': 50, 'bottled beer': 50, 'bottle beer': 50, 'wine': 50,
                   'shots': 67, 'cordials': 67, '': 90}
_NOT_INVENTORY_MENUS = ('merch',)


def rank_by_sales(conn, location, days=90):
    """Products ranked by what Toast sales use up at one house over `days`.

    Food: units sold x recipe (pmix_mapping -> recipe_ingredients), costed with
    the recipe engine, so the score is the $ of each product the sales consumed.
    Prepared items (chowder, sauces) pass their dollars to their raw ingredients.
    Booze: the drink's sales $ go to the bottle/keg its name matches (straight
    pours like "Tito's" have no recipe); failing a match, to the booze in its
    recipe, split by cost. Food and booze scores are compared only within their
    own group, never with each other.
    Returns [{product_id, name, category, group, dollars, basis}] best first.
    """
    from reports.item_match import Matcher, norm
    from integrations.recipes.recipe_costing import cost_ingredient

    since = conn.execute("SELECT strftime('%Y%m%d', 'now', ?)", (f'-{int(days)} day',)).fetchone()[0]
    sales = conn.execute("""
        SELECT oi.item_name, SUM(oi.quantity) AS qty, SUM(oi.price) AS rev,
               LOWER(COALESCE(MAX(mi.menu_name), '')) AS menu, LOWER(COALESCE(MAX(mi.menu_group_name), '')) AS grp
        FROM order_items oi LEFT JOIN menu_items mi ON mi.guid = oi.item_guid
        WHERE oi.location = ? AND oi.business_date >= ? AND COALESCE(oi.voided, 0) = 0
          AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.guid = oi.order_guid
                          AND (json_extract(o.raw_json, '$.deleted') = 1 OR json_extract(o.raw_json, '$.voided') = 1))
        GROUP BY oi.item_name
    """, (location, since)).fetchall()

    pmix = {}
    for r in conn.execute("SELECT LOWER(TRIM(menu_item_name)) n, recipe_id, COALESCE(multiplier, 1) m "
                          "FROM pmix_mapping WHERE recipe_id IS NOT NULL"):
        pmix.setdefault(r['n'], (r['recipe_id'], r['m']))
    prods = {r['id']: r for r in conn.execute(
        "SELECT id, name, display_name, category, active, source_recipe_id FROM products")}
    is_booze = lambda pid: pid in prods and (prods[pid]['category'] or '').upper() in BOOZE_CATS

    cache = {}
    def recipe_lines(rid):
        """[(product_id, cost per serving)]. Read-only: cost_recipe() writes the recipe row."""
        if rid not in cache:
            rec = conn.execute("SELECT serving_size FROM recipes WHERE id = ?", (rid,)).fetchone()
            servings = (rec['serving_size'] if rec else 1) or 1
            out = []
            for ing in conn.execute("""
                SELECT ri.product_id, ri.quantity, ri.unit, ri.yield_pct, p.yield_pct AS product_yield_pct
                FROM recipe_ingredients ri LEFT JOIN products p ON p.id = ri.product_id
                WHERE ri.recipe_id = ?""", (rid,)):
                d = dict(ing)
                cost = cost_ingredient(d, conn)['cost']
                y, py = d.get('yield_pct') or 100, d.get('product_yield_pct') or 1.0
                if y > 0 and y != 100:
                    cost *= 100 / y
                elif 0 < py < 1:
                    cost /= py
                if d['product_id'] and cost > 0:
                    out.append((d['product_id'], cost / servings))
            cache[rid] = out
        return cache[rid]

    score = defaultdict(float)
    def credit(pid, dollars, depth=0):
        rid = prods[pid]['source_recipe_id'] if pid in prods else None
        lines = recipe_lines(rid) if rid and depth < 4 else []
        tot = sum(x for _, x in lines)
        if tot <= 0:
            score[pid] += dollars
            return
        for p, x in lines:
            credit(p, dollars * x / tot, depth + 1)

    matcher = Matcher(conn, location, 'booze')
    for s in sales:
        if s['menu'] in _NOT_INVENTORY_MENUS or not norm(s['item_name']):
            continue
        qty, rev = s['qty'] or 0, s['rev'] or 0
        m = pmix.get((s['item_name'] or '').strip().lower())
        lines = recipe_lines(m[0]) if m else []
        # Food in any recipe (burgers, but also the lemons in a drink): $ used.
        for pid, cps in lines:
            if not is_booze(pid):
                credit(pid, qty * m[1] * cps)
        if s['menu'] == 'food' or rev <= 0:
            continue
        # Booze: the drink's sales $ to the bottle it names, else to its recipe's booze.
        need = _NAME_MATCH_MIN.get(s['grp'] if s['menu'] else '')
        best = (matcher.candidates(s['item_name'], 1) or [None])[0] if need else None
        if best and best['score'] >= need:
            score[best['product_id']] += rev
            continue
        booze = [(pid, cps) for pid, cps in lines if is_booze(pid)]
        tot = sum(x for _, x in booze)
        for pid, cps in booze:
            score[pid] += rev * cps / tot

    out = []
    for pid, d in sorted(score.items(), key=lambda kv: -kv[1]):
        p = prods.get(pid)
        if not p or not p['active'] or d <= 0:
            continue
        g = group_of(p['category'])
        out.append({'product_id': pid, 'name': p['display_name'] or p['name'], 'category': p['category'],
                    'group': g, 'dollars': round(d, 2), 'basis': 'sales' if g == 'booze' else 'used'})
    return out


def seed_weekly(conn, location, food_n=25, beer_n=10, spirits_n=8, days=90, added_by='seed'):
    """Put the top-selling food, beer, and liquor+wine products on the weekly list.
    Beer and liquor/wine get separate slots: kegs carry the bar's dollars and
    would otherwise crowd out every bottle. Only adds; never removes what Mike
    already has. Returns the number added."""
    ensure_tables(conn)
    ranked = rank_by_sales(conn, location, days)
    cat = lambda r: (r['category'] or '').upper()
    pick = [r for r in ranked if r['group'] == 'food'][:food_n] + \
           [r for r in ranked if cat(r) == 'BEER'][:beer_n] + \
           [r for r in ranked if cat(r) in ('LIQUOR', 'WINE')][:spirits_n]
    added = 0
    for i, r in enumerate(pick):
        cur = conn.execute("""
            INSERT OR IGNORE INTO count_templates (location, product_id, frequency, sort_order, source, added_by)
            VALUES (?, ?, 'weekly', ?, ?, ?)
        """, (location, r['product_id'], i,
              f"top {days}d {'sales' if r['basis'] == 'sales' else 'used in sales'} ${r['dollars']:,.0f}", added_by))
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
