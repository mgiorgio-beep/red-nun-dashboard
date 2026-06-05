#!/usr/bin/env python3
"""
top_liquors_test.py  --  one-off helper for the inventory measurement test.

Prints the top-selling LIQUOR/COCKTAIL menu items by quantity sold, so Mike can
pick ~15 spirits to count for the free-pour variance test.

It introspects the order_items schema first (column names vary), so it won't
crash if a column is named differently than expected.

Run on the Beelink (Chatham):
    cd /opt/red-nun-dashboard
    venv/bin/python3 top_liquors_test.py                 # last 90 days, both locations
    venv/bin/python3 top_liquors_test.py 180 chatham     # 180 days, Chatham only
"""
import sys
from integrations.toast.data_store import get_connection

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
LOCATION = sys.argv[2].strip().lower() if len(sys.argv) > 2 else None

# words that flag a beverage/liquor sales category or menu group
LIQUOR_HINTS = ["liquor", "cocktail", "spirit", "vodka", "whiskey", "whisky",
                "tequila", "rum", "gin", "bourbon", "scotch", "martini",
                "margarita", "well", "call", "premium", "bar"]

conn = get_connection()
cur = conn.cursor()

# ---- 1. what columns does order_items actually have? ----
cols = [r[1] for r in cur.execute("PRAGMA table_info(order_items)").fetchall()]
print("order_items columns:\n  " + ", ".join(cols) + "\n")

def pick(*candidates):
    for c in candidates:
        if c in cols:
            return c
    return None

name_col = pick("name", "item_name", "menu_item_name", "display_name")
qty_col  = pick("quantity", "qty", "count")
cat_col  = pick("sales_category", "sales_category_name", "category", "menu_group",
                "menu_group_name", "group_name")
loc_col  = pick("location", "location_name")
date_col = pick("business_date", "businessDate", "order_date")

print(f"using -> name={name_col}  qty={qty_col}  category={cat_col}  "
      f"location={loc_col}  date={date_col}\n")

# ---- 2. show the beverage-ish sales categories so we can sanity-check the filter ----
if cat_col:
    print(f"--- distinct '{cat_col}' values (top 40 by line count) ---")
    rows = cur.execute(
        f"SELECT {cat_col}, COUNT(*) c FROM order_items "
        f"GROUP BY {cat_col} ORDER BY c DESC LIMIT 40"
    ).fetchall()
    for r in rows:
        print(f"  {str(r[0])[:40]:42} {r[1]}")
    print()

# ---- 3. top liquor items by quantity sold ----
where = []
params = []
if date_col:
    where.append(f"{date_col} >= strftime('%Y%m%d','now', ?)")
    params.append(f"-{DAYS} days")
if LOCATION and loc_col:
    where.append(f"LOWER({loc_col}) = ?")
    params.append(LOCATION)
if cat_col:
    like = " OR ".join([f"LOWER({cat_col}) LIKE ?" for _ in LIQUOR_HINTS])
    where.append("(" + like + ")")
    params += [f"%{h}%" for h in LIQUOR_HINTS]

where_sql = ("WHERE " + " AND ".join(where)) if where else ""
qexpr = f"SUM({qty_col})" if qty_col else "COUNT(*)"

sql = (f"SELECT {name_col} AS item, {qexpr} AS sold "
       f"FROM order_items {where_sql} "
       f"GROUP BY {name_col} ORDER BY sold DESC LIMIT 40")

print(f"--- TOP LIQUOR/COCKTAIL ITEMS  (last {DAYS} days"
      f"{', '+LOCATION if LOCATION else ''}) ---")
for i, r in enumerate(cur.execute(sql, params).fetchall(), 1):
    print(f"  {i:>2}. {str(r[0])[:45]:47} {r[1]}")

conn.close()
print("\nThese are DRINKS, not bottles. Send me this output and I'll map the "
      "top drinks back to the ~15 spirit bottles you should physically count.")
