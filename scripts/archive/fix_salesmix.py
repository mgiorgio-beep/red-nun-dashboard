s = open('analytics.py', 'r', encoding='utf-8').read()

old = '''def get_sales_mix(location=None, start_date=None, end_date=None):
    """
    Get sales mix by category (food, liquor, beer, wine, etc.).
    Joins order_items with menu_items and category_map.
    Returns:
        List of dicts with category, revenue, item_count, pct_of_total
    """
    conn = get_connection()
    where_clauses = ["oi.voided = 0"]
    params = []
    if location:
        where_clauses.append("oi.location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("oi.business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("oi.business_date <= ?")
        params.append(end_date)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    query = f"""
        SELECT
            COALESCE(cm.pour_category, 'uncategorized') as category,
            SUM(oi.price * oi.quantity) as revenue,
            SUM(oi.quantity) as item_count
        FROM order_items oi
        LEFT JOIN menu_items mi ON oi.item_guid = mi.guid
        LEFT JOIN category_map cm ON mi.menu_group_name = cm.menu_group_name
        {where_sql}
        GROUP BY cm.pour_category
        ORDER BY revenue DESC
    """
    rows = conn.execute(query, params).fetchall()
    results = [dict(row) for row in rows]
    # Calculate percentages
    total_revenue = sum(r["revenue"] for r in results) if results else 1
    for r in results:
        r["pct_of_total"] = round((r["revenue"] / total_revenue) * 100, 1)
    conn.close()
    return results'''

new = '''def get_sales_mix(location=None, start_date=None, end_date=None):
    """
    Get sales mix by category using item name pattern matching.
    Returns:
        List of dicts with category, revenue, item_count, pct_of_total
    """
    conn = get_connection()
    where_clauses = ["voided = 0", "price > 0"]
    params = []
    if location:
        where_clauses.append("location = ?")
        params.append(location)
    if start_date:
        where_clauses.append("business_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("business_date <= ?")
        params.append(end_date)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    query = f"""
        SELECT item_name, SUM(price * quantity) as revenue, SUM(quantity) as qty
        FROM order_items {where_sql}
        GROUP BY item_name
        ORDER BY revenue DESC
    """
    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Classify items by name
    BEER_WORDS = ['lager', 'ipa', 'ale', 'stout', 'pilsner', 'kolsch', 'hef',
                  'guinness', 'blue moon', 'sam adams', 'bud light', 'coors',
                  'corona', 'stella', 'heineken', 'devils purse', 'harpoon',
                  'cisco', 'night shift', 'cape cod beer', 'draft', 'can beer',
                  'bottle beer', 'seltzer', 'truly', 'white claw', 'bucket']
    WINE_WORDS = ['wine', 'cab', 'merlot', 'pinot', 'chardonnay', 'chard',
                  'sauvignon', 'riesling', 'prosecco', 'rose', 'rosé',
                  'kim crawford', 'meiomi', 'josh', 'kendall', 'barefoot',
                  'decoy', 'la crema', 'glass of', 'bottle of']
    LIQUOR_WORDS = ['margarita', 'martini', 'cocktail', 'mojito', 'old fashioned',
                    'manhattan', 'negroni', 'daiquiri', 'cosmopolitan', 'gimlet',
                    'sour', 'mule', 'spritz', 'bloody mary', 'espresso martini',
                    'rum', 'vodka', 'whiskey', 'bourbon', 'tequila', 'gin',
                    'shot', 'on the rocks', 'neat', 'mixed drink']
    NA_WORDS = ['soda', 'coffee', 'tea', 'juice', 'water', 'lemonade',
                'sprite', 'coke', 'pepsi', 'ginger ale', 'tonic', 'red bull',
                'arnold palmer', 'shirley temple', 'mocktail', 'n/a', 'non-alc']

    categories = {}
    for row in rows:
        name = (row["item_name"] or "").lower()
        rev = row["revenue"] or 0
        qty = row["qty"] or 0

        cat = "Food"
        for w in BEER_WORDS:
            if w in name:
                cat = "Beer"; break
        if cat == "Food":
            for w in WINE_WORDS:
                if w in name:
                    cat = "Wine"; break
        if cat == "Food":
            for w in LIQUOR_WORDS:
                if w in name:
                    cat = "Liquor"; break
        if cat == "Food":
            for w in NA_WORDS:
                if w in name:
                    cat = "NA Bev"; break

        if cat not in categories:
            categories[cat] = {"category": cat, "revenue": 0, "item_count": 0}
        categories[cat]["revenue"] += rev
        categories[cat]["item_count"] += qty

    results = sorted(categories.values(), key=lambda x: x["revenue"], reverse=True)
    total_revenue = sum(r["revenue"] for r in results) if results else 1
    for r in results:
        r["pct_of_total"] = round((r["revenue"] / total_revenue) * 100, 1)
    return results'''

if old in s:
    s = s.replace(old, new)
    open('analytics.py', 'w', encoding='utf-8').write(s)
    print('FIXED sales mix - now uses item name classification')
else:
    print('ERROR - pattern not found')
