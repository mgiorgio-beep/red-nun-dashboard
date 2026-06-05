#!/usr/bin/env python3
"""
One-off diagnostic: why is pour/food cost % empty, and what do the wine recipes need?

Read-only. Prints to stdout — run on the Beelink and paste the output back.

    cd /opt/red-nun-dashboard && venv/bin/python3 -m scripts.diag_costing_gaps
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from dotenv import load_dotenv
    env_path = os.path.join(_REPO_ROOT, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
except Exception:
    pass

from integrations.toast.data_store import get_connection


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def q1(conn, sql, params=()):
    try:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None
    except Exception as e:
        return f"ERR: {e}"


def main():
    conn = get_connection()

    # ---- 1. category_map ---------------------------------------------------
    section("1. category_map (maps menu_group_name -> pour_category)")
    print("row count:", q1(conn, "SELECT COUNT(*) FROM category_map"))
    try:
        for r in conn.execute(
            "SELECT menu_group_name, pour_category FROM category_map ORDER BY menu_group_name"
        ).fetchall():
            print(f"   {r['menu_group_name']!r:40} -> {r['pour_category']}")
    except Exception as e:
        print("   ERR:", e)

    # ---- 2. menu_items -----------------------------------------------------
    section("2. menu_items (need cost populated + menu_group_name)")
    print("total menu_items:", q1(conn, "SELECT COUNT(*) FROM menu_items"))
    print("with cost > 0:", q1(conn, "SELECT COUNT(*) FROM menu_items WHERE cost > 0"))
    print("with non-null menu_group_name:",
          q1(conn, "SELECT COUNT(*) FROM menu_items WHERE menu_group_name IS NOT NULL AND menu_group_name != ''"))
    print("menu_items whose menu_group_name IS in category_map:",
          q1(conn, """SELECT COUNT(*) FROM menu_items mi
                      WHERE mi.menu_group_name IN (SELECT menu_group_name FROM category_map)"""))
    print("\n   distinct menu_group_name values actually in use (top 30 by item count):")
    try:
        for r in conn.execute(
            """SELECT menu_group_name, COUNT(*) n, SUM(CASE WHEN cost > 0 THEN 1 ELSE 0 END) with_cost
               FROM menu_items GROUP BY menu_group_name ORDER BY n DESC LIMIT 30"""
        ).fetchall():
            print(f"   {str(r['menu_group_name'])[:38]:40} items={r['n']:<5} with_cost={r['with_cost']}")
    except Exception as e:
        print("   ERR:", e)

    # ---- 3. order_items -> menu_items join health --------------------------
    section("3. order_items -> menu_items link (last 30 biz days)")
    try:
        cutoff = q1(conn, "SELECT strftime('%Y%m%d', date('now','-30 days'))")
        print("cutoff business_date:", cutoff)
        print("order_items rows:",
              q1(conn, "SELECT COUNT(*) FROM order_items WHERE business_date >= ? AND voided = 0", (cutoff,)))
        print("with item_guid set:",
              q1(conn, "SELECT COUNT(*) FROM order_items WHERE business_date >= ? AND voided = 0 AND item_guid IS NOT NULL", (cutoff,)))
        print("item_guid matches a menu_items.guid:",
              q1(conn, """SELECT COUNT(*) FROM order_items oi WHERE oi.business_date >= ? AND oi.voided = 0
                          AND oi.item_guid IN (SELECT guid FROM menu_items)""", (cutoff,)))
        print("...of those, the menu_item has cost > 0:",
              q1(conn, """SELECT COUNT(*) FROM order_items oi
                          JOIN menu_items mi ON oi.item_guid = mi.guid
                          WHERE oi.business_date >= ? AND oi.voided = 0 AND mi.cost > 0""", (cutoff,)))
    except Exception as e:
        print("   ERR:", e)

    # ---- 4. wine / by-the-glass recipes + ingredients ----------------------
    section("4. WINE recipes + their ingredients (costed at bottle?)")
    try:
        recs = conn.execute(
            """SELECT id, name, serving_size, menu_price, cost_per_serving, food_cost_pct
               FROM recipes WHERE active = 1 AND category = 'WINE' ORDER BY food_cost_pct DESC"""
        ).fetchall()
        for r in recs:
            print(f"\n   [{r['id']}] {r['name']}  serving_size={r['serving_size']!r}  "
                  f"menu=${r['menu_price']}  cost/serv=${r['cost_per_serving']}  fc%={r['food_cost_pct']}")
            ings = conn.execute(
                """SELECT ri.quantity, ri.unit, p.name AS product, p.pack_size, p.pack_unit
                   FROM recipe_ingredients ri LEFT JOIN products p ON ri.product_id = p.id
                   WHERE ri.recipe_id = ?""", (r["id"],)
            ).fetchall()
            for ig in ings:
                print(f"        - {ig['quantity']} {ig['unit']} of {ig['product']}  "
                      f"(pack: {ig['pack_size']} {ig['pack_unit']})")
    except Exception as e:
        print("   ERR:", e)

    conn.close()
    print("\n[done]")


if __name__ == "__main__":
    main()
