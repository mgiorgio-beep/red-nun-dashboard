"""
Pour Cost Calculator — Red Nun Analytics
Classifies Toast menu items as BEER, LIQUOR, WINE, or FOOD
and calculates pour cost against MarginEdge beverage COGS.
"""

# ---- BEVERAGE CLASSIFICATION ----
# Items are matched case-insensitive. Add new items as menu changes.

BEER_ITEMS = {
    # Drafts
    "guinness", "miller high life", "fiddlehead ipa", "coors lt", "bud light draft",
    "devils purse kolsch", "devil's purse kolsch", "kona big wave", "kona- big wave",
    "cape cod blonde", "mich ultra", "two roads lager", "sam adams", "sam seasonal",
    "sam adams cold snap", "sam adams - boston lager", "blue moon", "miller lite draft",
    "busch light", "wormtown", "stellwagen", "vermont beer scarlet red",
    "hog island nauset haze", "m s - cloud candy", "gunners daughter stout",
    "pbr", "cisco cold wave", "lawson's sip of subshine",
    # Bottles/Cans
    "bud light bottle", "bud light btl", "corona", "corona btl", "mich ultra  btl",
    "mich ultra btl", "budweiser", "budweiser btl", "miller lite btl",
    "bud light btl", "athletic na",
    # Seltzers/Ciders
    "high noon", "highnoon", "whiteclaw", "surfside", "carlson cider",
    "sun cruisers", "downeast cider", "in the weeds seltzer",
    "schilling alexander pills", "blue comet",
    # Buckets
    "bucket",
    # Maine beer
    "maine- lunch 16oz", "maine - lunch", "maine- lunch",
    # Craft
    "wachusett- blueberry", "cruise control",
}

LIQUOR_ITEMS = {
    # Vodka
    "tito's", "ketel one", "kettle one citron", "grey goose", "well vodka",
    "absolut citron", "stoli raz", "stoli orange", "deep eddy lemon", "pearl cucumber",
    # Whiskey/Bourbon
    "jack daniels", "jack fire", "jack fire shot", "jameson", "jameson shot",
    "well bourbon", "jim beam", "bulleit bourbon", "buffalo trace", "basil hayden",
    "blantons", "woodford", "tullamore dew shot", "jw black",
    # Rum
    "capt morgan", "well rum",
    # Tequila
    "hornitos shot", "cuervo shot", "casamigos blanco", "casamigos resposado",
    "don julio resposado", "well tequila", "patron blanco",
    # Other spirits
    "sambuca", "jager", "jager shot", "disaronna", "hendricks",
    "dr mcgillicuddy's", "fernet shot", "green tea shot",
    # Cocktails
    "margarita", "painkiller", "cosmo", "old fashioned", "old fashion",
    "manhattan", "martini", "bloody mary", "dark & stormy", "irish coffee",
    "long island", "moscow mule", "the mule", "mimosa", "aperol spritz", "spritz",
    "pineapple jalapeno marg.", "spiced blood orange marg", "blackberry lemonade",
    "maple bourbon old fasioned", "cranberry mule", "the buzz", "chatham cooler",
    "blue coconut mojito", "sea ice (well)", "lucky one", "chocolate bomb",
    "prosecco", "maschio - prosecco",
}

WINE_ITEMS = {
    "josh- cab", "josh chard", "kim crawford sb", "kim crawford",
    "glen ellen chard", "glen ellen house chard", "mezza pg", "mezzacorona pg",
    "13 celsius sauv blanc", "simi sb", "trapiche malbec", "meiomi pinot noir",
    "pinot noir", "paris rose", "san angelo pg", "seven deadly sins",
    "bottle pinot grigio", "silver gate house cab", "firesteed- pinot gris",
    "acrobat pinot noir",
}

# Items to always exclude from food (non-revenue)
EXCLUDE_ITEMS = {
    "gift card", "e-gift card",
}

# Non-alcoholic beverages (count as food/NA, not pour cost)
NA_BEVERAGE_ITEMS = {
    "diet coke", "coke", "club soda", "ginger ale", "iced tea",
    "shirley temple", "fresca", "ibc root beer", "coffee",
}


def classify_item(item_name):
    """Classify a menu item as BEER, LIQUOR, WINE, FOOD, NA_BEV, or EXCLUDE."""
    lower = (item_name or "").strip().lower()

    if lower in EXCLUDE_ITEMS or any(lower.startswith(x) for x in EXCLUDE_ITEMS):
        return "EXCLUDE"

    # Check exact match first
    if lower in BEER_ITEMS:
        return "BEER"
    if lower in LIQUOR_ITEMS:
        return "LIQUOR"
    if lower in WINE_ITEMS:
        return "WINE"
    if lower in NA_BEVERAGE_ITEMS:
        return "NA_BEV"

    # Fuzzy match for slight variations
    for b in BEER_ITEMS:
        if lower.startswith(b) or b.startswith(lower):
            return "BEER"
    for l in LIQUOR_ITEMS:
        if lower.startswith(l) or l.startswith(lower):
            return "LIQUOR"
    for w in WINE_ITEMS:
        if lower.startswith(w) or w.startswith(lower):
            return "WINE"

    return "FOOD"


def get_beverage_revenue(location, start_date, end_date):
    """
    Get beverage revenue from Toast order items, classified by type.
    Returns dict with beer_rev, liquor_rev, wine_rev, total_bev_rev, food_rev.
    """
    from data_store import get_connection
    conn = get_connection()

    rows = conn.execute("""
        SELECT item_name, SUM(price * quantity) as revenue, SUM(quantity) as qty
        FROM order_items
        WHERE location = ? AND business_date BETWEEN ? AND ? AND voided = 0
        GROUP BY item_name
        ORDER BY revenue DESC
    """, (location, start_date, end_date)).fetchall()
    conn.close()

    totals = {"BEER": 0, "LIQUOR": 0, "WINE": 0, "FOOD": 0, "NA_BEV": 0, "EXCLUDE": 0}
    items_by_cat = {"BEER": [], "LIQUOR": [], "WINE": [], "FOOD": [], "UNCLASSIFIED": []}

    for r in rows:
        name = r["item_name"]
        rev = r["revenue"] or 0
        qty = r["qty"] or 0
        cat = classify_item(name)
        totals[cat] = totals.get(cat, 0) + rev
        if cat in ("BEER", "LIQUOR", "WINE"):
            items_by_cat[cat].append({"name": name, "revenue": rev, "qty": qty})

    total_bev = totals["BEER"] + totals["LIQUOR"] + totals["WINE"]

    return {
        "beer_rev": round(totals["BEER"], 2),
        "liquor_rev": round(totals["LIQUOR"], 2),
        "wine_rev": round(totals["WINE"], 2),
        "total_bev_rev": round(total_bev, 2),
        "food_rev": round(totals["FOOD"] + totals["NA_BEV"], 2),
        "items": items_by_cat,
    }


def get_pour_cost(location, start_date, end_date, manual_bwl=None):
    """
    Calculate pour cost: beverage COGS / beverage revenue.

    Uses MarginEdge invoice line items joined with product categories
    for accurate category-level COGS (BEER, LIQUOR, WINE).
    Falls back to 30-day rolling me_cogs_summary if no line items.
    """
    from data_store import get_connection

    bev = get_beverage_revenue(location, start_date, end_date)
    conn = get_connection()

    sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

    if manual_bwl is not None:
        total_bev_cogs = manual_bwl
        beer_cogs = 0
        liquor_cogs = 0
        wine_cogs = 0
        period_label = f"{start_date} to {end_date}"
    else:
        # Try line-item level COGS first
        try:
            rows = conn.execute("""
                SELECT ii.category_type, SUM(ii.total_price) as cost
                FROM me_invoice_items ii
                JOIN me_invoices i ON ii.order_id = i.order_id AND ii.location = i.location
                WHERE ii.location = ? AND i.invoice_date >= ? AND i.invoice_date <= ?
                  AND ii.category_type IN ('BEER', 'LIQUOR', 'WINE')
                GROUP BY ii.category_type
            """, (location, sd, ed)).fetchall()

            if rows:
                beer_cogs = sum(r["cost"] for r in rows if r["category_type"] == "BEER")
                liquor_cogs = sum(r["cost"] for r in rows if r["category_type"] == "LIQUOR")
                wine_cogs = sum(r["cost"] for r in rows if r["category_type"] == "WINE")
                total_bev_cogs = beer_cogs + liquor_cogs + wine_cogs
                period_label = f"{start_date} to {end_date} (line-item)"
            else:
                raise ValueError("No line items")

        except Exception:
            # Fall back to 30-day rolling from me_cogs_summary
            try:
                row = conn.execute("""
                    SELECT SUM(total_cost) as bwl_cost
                    FROM me_cogs_summary
                    WHERE location = ? AND category_type IN ('LIQUOR', 'BEER')
                """, (location,)).fetchone()
                total_bev_cogs = row["bwl_cost"] or 0
            except Exception:
                total_bev_cogs = 0
            beer_cogs = 0
            liquor_cogs = 0
            wine_cogs = 0
            period_label = "30-day rolling (vendor-level)"

    total_bev_rev = bev["total_bev_rev"]
    pour_cost_pct = (total_bev_cogs / total_bev_rev * 100) if total_bev_rev > 0 else 0

    conn.close()
    return {
        "beer_rev": bev["beer_rev"],
        "liquor_rev": bev["liquor_rev"],
        "wine_rev": bev["wine_rev"],
        "total_bev_rev": total_bev_rev,
        "food_rev": bev["food_rev"],
        "beer_cogs": round(beer_cogs, 2),
        "liquor_cogs": round(liquor_cogs, 2),
        "wine_cogs": round(wine_cogs, 2),
        "total_bev_cogs": round(total_bev_cogs, 2),
        "pour_cost_pct": round(pour_cost_pct, 1),
        "beer_pour_pct": round(beer_cogs / bev["beer_rev"] * 100, 1) if bev["beer_rev"] > 0 else 0,
        "liquor_pour_pct": round(liquor_cogs / bev["liquor_rev"] * 100, 1) if bev["liquor_rev"] > 0 else 0,
        "wine_pour_pct": round(wine_cogs / bev["wine_rev"] * 100, 1) if bev["wine_rev"] > 0 else 0,
        "period_label": period_label,
    }


if __name__ == "__main__":
    import sys
    from datetime import datetime, timedelta

    if len(sys.argv) == 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        today = datetime.now().date()
        last_monday = today - timedelta(days=today.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6)
        start = last_monday.strftime("%Y%m%d")
        end = last_sunday.strftime("%Y%m%d")

    print(f"Pour Cost Report: {start} to {end}")
    print("=" * 60)

    for loc, name in [("dennis", "Dennis Port"), ("chatham", "Chatham")]:
        print(f"\n--- {name} ---")

        pc = get_pour_cost(loc, start, end)
        print(f"  Source: {pc['period_label']}")
        print(f"  Beverage Revenue:  ${pc['total_bev_rev']:,.2f}")
        print(f"    Beer:            ${pc['beer_rev']:,.2f}")
        print(f"    Liquor:          ${pc['liquor_rev']:,.2f}")
        print(f"    Wine:            ${pc['wine_rev']:,.2f}")
        print(f"  Beverage COGS:     ${pc['total_bev_cogs']:,.2f}")
        print(f"    Beer COGS:       ${pc['beer_cogs']:,.2f}")
        print(f"    Liquor COGS:     ${pc['liquor_cogs']:,.2f}")
        print(f"    Wine COGS:       ${pc['wine_cogs']:,.2f}")
        print(f"  Pour Cost:         {pc['pour_cost_pct']:.1f}%")
        print(f"    Beer Pour:       {pc['beer_pour_pct']:.1f}%")
        print(f"    Liquor Pour:     {pc['liquor_pour_pct']:.1f}%")
        print(f"    Wine Pour:       {pc['wine_pour_pct']:.1f}%")
        print(f"  Food Revenue:      ${pc['food_rev']:,.2f}")
        total = pc['total_bev_rev'] + pc['food_rev']
        if total > 0:
            print(f"  Bev/Food Mix:      {pc['total_bev_rev']/total*100:.0f}% / {pc['food_rev']/total*100:.0f}%")
