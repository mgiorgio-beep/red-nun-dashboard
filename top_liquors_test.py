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
loc_col  = pick("location", "location_name")
date_col = pick("business_date", "businessDate", "order_date")
void_col = pick("voided", "is_voided", "void")

# choose the category-like column that actually has data (category was all NULL)
def nonnull(c):
    if not c:
        return 0
    return cur.execute(
        f"SELECT COUNT(*) FROM order_items WHERE {c} IS NOT NULL AND {c} != ''"
    ).fetchone()[0]

cat_candidates = [c for c in ("menu_group", "menu_group_name", "category",
                              "sales_category", "group_name") if c in cols]
cat_col = max(cat_candidates, key=nonnull) if cat_candidates else None

print(f"using -> name={name_col}  qty={qty_col}  category={cat_col}  "
      f"location={loc_col}  date={date_col}  voided={void_col}\n")

# ---- 2. show the beverage-ish categories so we can sanity-check the filter ----
if cat_col:
    print(f"--- distinct '{cat_col}' values (top 50 by line count) ---")
    rows = cur.execute(
        f"SELECT {cat_col}, COUNT(*) c FROM order_items "
        f"GROUP BY {cat_col} ORDER BY c DESC LIMIT 50"
    ).fetchall()
    for r in rows:
        print(f"  {str(r[0])[:40]:42} {r[1]}")
    print()

# ---- 3. top items by quantity sold (category cols are empty, so filter by NAME) ----
base = []
params = []
if date_col:
    base.append(f"{date_col} >= strftime('%Y%m%d','now', ?)")
    params.append(f"-{DAYS} days")
if LOCATION and loc_col:
    base.append(f"LOWER({loc_col}) = ?")
    params.append(LOCATION)
if void_col:
    base.append(f"({void_col} IS NULL OR {void_col} IN (0,'0','false','False'))")

base_sql = ("WHERE " + " AND ".join(base)) if base else ""
qexpr = f"SUM({qty_col})" if qty_col else "COUNT(*)"
hdr = f"(last {DAYS} days{', '+LOCATION if LOCATION else ''})"

def run(title, extra_sql="", extra_params=()):
    sql = (f"SELECT {name_col} AS item, {qexpr} AS sold FROM order_items "
           f"{base_sql} {extra_sql} GROUP BY {name_col} "
           f"ORDER BY sold DESC LIMIT 80")
    print(f"--- {title} {hdr} ---")
    for i, r in enumerate(cur.execute(sql, list(params)+list(extra_params)).fetchall(), 1):
        print(f"  {i:>2}. {str(r[0])[:50]:52} {r[1]}")
    print()

# (a) everything, so we can see the whole drink/food mix
run("TOP 80 ITEMS OVERALL")

# (b) items whose NAME looks like a liquor/cocktail
name_like = " OR ".join(["LOWER(%s) LIKE ?" % name_col for _ in LIQUOR_HINTS])
extra = ("AND (" + name_like + ")") if base_sql else ("WHERE (" + name_like + ")")
run("TOP ITEMS WITH LIQUOR/COCKTAIL-ISH NAMES", extra, [f"%{h}%" for h in LIQUOR_HINTS])

conn.close()
print("These are DRINKS, not bottles. Paste me BOTH lists and I'll map the top "
      "drinks back to the ~15 spirit bottles you should physically count.")
