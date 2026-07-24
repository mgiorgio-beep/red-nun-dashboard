"""One-off recipe inspection tool for the 2026-07 costing cleanup pass."""
import sys
from integrations.toast.data_store import get_connection
from integrations.recipes.recipe_costing import cost_ingredient, cost_recipe


def inspect(recipe_id, conn):
    r = dict(conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone())
    print(f"\n=== Recipe {recipe_id}: {r['name']} ===")
    print(f"  menu_price={r['menu_price']}  serving_size={r['serving_size']}  "
          f"total_cost={r['total_cost']}  fc%={r['food_cost_pct']}")
    rows = conn.execute("""
        SELECT ri.id ri_id, ri.product_id, ri.quantity, ri.unit, ri.yield_pct, ri.notes,
               p.name pname, p.unit p_unit, p.pack_size p_pack, p.pack_unit p_packunit,
               p.inventory_unit, p.yield_pct p_yield, p.current_price, p.active_vendor_item_id avi,
               vi.id vi_id, vi.vendor_description vdesc, vi.pack_size vi_pack, vi.pack_unit vi_packunit,
               vi.pack_contains, vi.contains_unit, vi.purchase_price, vi.price_per_unit, vi.is_active
        FROM recipe_ingredients ri
        LEFT JOIN products p ON ri.product_id=p.id
        LEFT JOIN vendor_items vi ON p.active_vendor_item_id=vi.id
        WHERE ri.recipe_id=?
    """, (recipe_id,)).fetchall()
    for row in rows:
        d = dict(row)
        res = cost_ingredient({'product_id': d['product_id'], 'quantity': d['quantity'],
                               'unit': d['unit']}, conn)
        print(f"\n  ri#{d['ri_id']} qty={d['quantity']} {d['unit']!r}  -> ${res['cost']:.4f} [{res['source']}]  {d['notes'] or ''}")
        print(f"     product #{d['product_id']}: {d['pname']!r}")
        print(f"        p.unit={d['p_unit']!r} pack_size={d['p_pack']} pack_unit={d['p_packunit']!r} "
              f"inv_unit={d['inventory_unit']!r} yield={d['p_yield']} cur_price={d['current_price']}")
        if d['avi']:
            print(f"        vi#{d['vi_id']}: {d['vdesc']!r} pack_size={d['vi_pack']!r} "
                  f"pack_unit={d['vi_packunit']!r} pack_contains={d['pack_contains']} "
                  f"contains_unit={d['contains_unit']!r} price={d['purchase_price']} ppu={d['price_per_unit']} active={d['is_active']}")
        else:
            print(f"        NO ACTIVE VENDOR ITEM (avi_id NULL)")
        convs = conn.execute("SELECT from_qty,from_unit,to_qty,to_unit,source FROM product_unit_conversions WHERE product_id=?", (d['product_id'],)).fetchall()
        for c in convs:
            c = dict(c)
            print(f"        conv: {c['from_qty']} {c['from_unit']} = {c['to_qty']} {c['to_unit']}  ({c['source']})")


if __name__ == '__main__':
    conn = get_connection()
    for rid in sys.argv[1:]:
        inspect(int(rid), conn)
