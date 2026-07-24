"""Create the four drafted prepared-item sub-recipes + companion products.
Idempotent by recipe name. Quantities are ai_draft — Mike refines in the Fixer."""
from integrations.toast.data_store import get_connection
from integrations.recipes.recipe_costing import cost_recipe

# recipe name -> (category, yield_qty, yield_unit, [(product_id, qty, unit), ...])
PREPS = {
    'Cole Slaw (prep)': ('dressing', 64, 'oz', [
        (553, 40, 'oz'),    # green cabbage
        (1110, 6, 'oz'),    # carrot
        (197, 12, 'oz'),    # coleslaw dressing (Ken's)
        (557, 4, 'oz'),     # mayonnaise
        (745, 2, 'tbsp'),   # sugar
        (548, 2, 'tbsp'),   # cider vinegar
        (1142, 1, 'tsp'),   # salt
        (1104, 1, 'tsp'),   # black pepper
    ]),
    'Jerk Relish (prep)': ('sauce', 32, 'oz', [
        (565, 8, 'oz'),     # red onion
        (1489, 8, 'oz'),    # red bell pepper
        (988, 2, 'oz'),     # jalapeno
        (549, 2, 'tbsp'),   # jamaican jerk seasoning
        (938, 2, 'each'),   # lime (juice)
        (745, 1, 'tbsp'),   # sugar
        (1142, 1, 'tsp'),   # salt
    ]),
    'Lime Crema (prep)': ('sauce', 24, 'oz', [
        (564, 16, 'fl_oz'), # sour cream
        (557, 4, 'oz'),     # mayonnaise
        (938, 3, 'each'),   # lime (juice + zest)
        (1142, 0.5, 'tsp'), # salt
    ]),
    'Sriracha Aioli (prep)': ('sauce', 24, 'oz', [
        (557, 18, 'oz'),    # mayonnaise
        (1375, 4, 'oz'),    # sriracha
        (102, 3, 'each'),   # garlic clove
        (938, 1, 'each'),   # lime (juice)
        (1142, 0.5, 'tsp'), # salt
    ]),
}


def main():
    conn = get_connection()
    for name, (cat, yq, yu, ings) in PREPS.items():
        existing = conn.execute("SELECT id FROM recipes WHERE name=?", (name,)).fetchone()
        if existing:
            rid = existing['id']
            print(f"exists: {name} (#{rid}) — refreshing ingredients")
            conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (rid,))
        else:
            cur = conn.execute("""
                INSERT INTO recipes (name, category, serving_size, serving_unit, menu_price,
                    active, location, yield_qty, yield_unit, is_inventoried, notes)
                VALUES (?,?,?,?,?,1,'chatham',?,?,1,'AI-drafted prep item — refine quantities')
            """, (name, cat, 1, 'batch', 0, yq, yu))
            rid = cur.lastrowid
            print(f"created: {name} (#{rid})")
        for pid, qty, unit in ings:
            pn = conn.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
            conn.execute("""
                INSERT INTO recipe_ingredients (recipe_id, product_id, product_name, quantity, unit, notes)
                VALUES (?,?,?,?,?, 'ai_draft')
            """, (rid, pid, pn['name'] if pn else None, qty, unit))
        conn.commit()
        # Companion sub-recipe product (cost rolls up via source_recipe_id)
        prod = conn.execute("SELECT id FROM products WHERE source_recipe_id=?", (rid,)).fetchone()
        pname = name.replace(' (prep)', '')
        if not prod:
            pc = conn.execute("""
                INSERT INTO products (name, display_name, category, unit, recipe_unit,
                    active, location, source_recipe_id, setup_complete)
                VALUES (?,?,?, 'oz','oz', 1,'chatham', ?, 1)
            """, (pname, pname, cat, rid))
            prod_id = pc.lastrowid
            print(f"   companion product #{prod_id} -> recipe #{rid}")
        else:
            prod_id = prod['id']
        conn.commit()
        res = cost_recipe(rid, conn)
        per_oz = res['total_cost'] / yq if yq else 0
        print(f"   batch cost ${res['total_cost']:.2f} / {yq} {yu} = ${per_oz:.4f}/{yu}   (product #{prod_id})")


if __name__ == '__main__':
    main()
