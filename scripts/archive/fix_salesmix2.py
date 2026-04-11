s = open('analytics.py', 'r', encoding='utf-8').read()

# Find the function start and end
start = s.find('def get_sales_mix(')
if start < 0:
    print('ERROR: get_sales_mix not found')
    exit()

# Find the next function definition after get_sales_mix
next_def = s.find('\ndef ', start + 1)
if next_def < 0:
    next_def = len(s)

old_func = s[start:next_def]
print(f'Found get_sales_mix: {len(old_func)} chars')

new_func = '''def get_sales_mix(location=None, start_date=None, end_date=None):
    """
    Get sales mix by category using item name pattern matching.
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

    BEER = ['lager','ipa','ale','stout','pilsner','kolsch','hef',
            'guinness','blue moon','sam adams','bud light','coors',
            'corona','stella','heineken','devils purse','harpoon',
            'cisco','night shift','cape cod beer','draft','seltzer',
            'truly','white claw','bucket']
    WINE = ['wine','cab','merlot','pinot','chardonnay','chard',
            'sauvignon','riesling','prosecco','rose','ros\\xe9',
            'kim crawford','meiomi','josh','kendall','barefoot',
            'decoy','la crema','glass of','bottle of']
    LIQUOR = ['margarita','martini','cocktail','mojito','old fashioned',
              'manhattan','negroni','daiquiri','cosmopolitan','gimlet',
              'sour','mule','spritz','bloody mary','espresso martini',
              'rum','vodka','whiskey','bourbon','tequila','gin',
              'shot','on the rocks','neat','mixed drink']
    NA = ['soda','coffee','tea','juice','water','lemonade',
          'sprite','coke','pepsi','ginger ale','tonic','red bull',
          'arnold palmer','shirley temple','mocktail','n/a','non-alc']

    categories = {}
    for row in rows:
        name = (row["item_name"] or "").lower()
        rev = row["revenue"] or 0
        qty = row["qty"] or 0
        cat = "Food"
        for w in BEER:
            if w in name:
                cat = "Beer"; break
        if cat == "Food":
            for w in WINE:
                if w in name:
                    cat = "Wine"; break
        if cat == "Food":
            for w in LIQUOR:
                if w in name:
                    cat = "Liquor"; break
        if cat == "Food":
            for w in NA:
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
    return results
'''

s = s[:start] + new_func + s[next_def:]
open('analytics.py', 'w', encoding='utf-8').write(s)
print('DONE - get_sales_mix replaced with item name classification')
