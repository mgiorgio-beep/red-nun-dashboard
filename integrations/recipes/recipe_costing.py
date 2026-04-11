"""
Recipe Costing Engine — Red Nun Analytics

Calculates food cost for every recipe using active vendor item prices.
Uses product_unit_conversions table for unit resolution — never guesses.

Costing rule: always use the active vendor item price.
No averaging, no history. Simple and predictable.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Weight units — base unit: oz
WEIGHT_TO_OZ = {
    'oz': 1.0, 'ounce': 1.0, 'ounces': 1.0,
    'lb': 16.0, 'lbs': 16.0, 'pound': 16.0, 'pounds': 16.0,
    'g': 0.035274, 'gram': 0.035274, 'grams': 0.035274,
    'kg': 35.274, 'kilogram': 35.274,
}

# Volume units — base unit: fl oz
VOLUME_TO_FLOZ = {
    'fl oz': 1.0, 'floz': 1.0, 'fluid oz': 1.0, 'fluid ounce': 1.0,
    'ml': 0.033814, 'milliliter': 0.033814, 'millilitre': 0.033814,
    'l': 33.814, 'liter': 33.814, 'litre': 33.814,
    'tsp': 0.16667, 'teaspoon': 0.16667,
    'tbsp': 0.5, 'tablespoon': 0.5,
    'cup': 8.0, 'cups': 8.0,
    'pt': 16.0, 'pint': 16.0,
    'qt': 32.0, 'quart': 32.0,
    'gal': 128.0, 'gallon': 128.0, 'gallons': 128.0,
    'gl': 128.0,
}

WEIGHT_UNITS = set(WEIGHT_TO_OZ.keys())
VOLUME_UNITS = set(VOLUME_TO_FLOZ.keys())


def cost_ingredient(ri, conn):
    """
    Cost a single recipe ingredient using its active vendor item price.

    Args:
        ri: dict with keys from recipe_ingredients JOIN products
        conn: SQLite connection

    Returns:
        {'cost': float, 'unit_price': float, 'source': str}
        source: 'vendor_item' | 'standard_conversion' | 'no_conversion' | 'no_price'
    """
    try:
        product_id = ri.get('product_id')
        quantity = ri.get('quantity') or 0
        recipe_unit = (ri.get('unit') or '').strip().lower()

        if not product_id:
            return {'cost': 0.0, 'unit_price': 0.0, 'source': 'no_price'}

        # Get product with active vendor item
        product = conn.execute("""
            SELECT p.id, p.name, p.unit as purchase_unit, p.pack_size, p.pack_unit,
                   p.active_vendor_item_id, p.yield_pct as product_yield_pct,
                   vi.purchase_price, vi.price_per_unit, vi.pack_contains, vi.contains_unit,
                   vi.pack_size as vi_pack_size, vi.pack_unit as vi_pack_unit
            FROM products p
            LEFT JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
            WHERE p.id = ?
        """, (product_id,)).fetchone()

        if not product:
            return {'cost': 0.0, 'unit_price': 0.0, 'source': 'no_price'}

        product = dict(product)
        price = product.get('purchase_price') or 0
        if not price or price <= 0:
            return {'cost': 0.0, 'unit_price': 0.0, 'source': 'no_price'}

        prod_unit = (product.get('purchase_unit') or '').strip().lower()
        pack_unit = (product.get('pack_unit') or prod_unit).strip().lower()
        pack_size = product.get('pack_size') or 1

        # Use vendor item pack_contains if available (e.g. 20/8oz case = 160oz total)
        # This gives us the real cost per base unit
        vi_pack_contains = product.get('pack_contains')
        vi_contains_unit = (product.get('contains_unit') or '').strip().lower()
        if vi_pack_contains and vi_pack_contains > 0 and vi_contains_unit:
            pack_size = vi_pack_contains
            pack_unit = vi_contains_unit

        # Use price_per_unit from vendor item if available (pre-calculated)
        vi_price_per_unit = product.get('price_per_unit')
        if vi_price_per_unit and vi_price_per_unit > 0:
            # price_per_unit is cost per contains_unit (e.g. per oz)
            # Normalize units for comparison (fl oz, fl_oz, floz all the same)
            def norm_unit(u):
                u = (u or '').strip().lower().replace('_', ' ').replace('.', '')
                if u in ('fl oz', 'floz', 'fluid oz', 'fluid ounce', 'fl_oz'): return 'fl oz'
                if u in ('oz', 'ounce', 'ounces'): return 'oz'
                if u in ('lb', 'lbs', 'pound', 'pounds'): return 'lb'
                if u in ('ml', 'milliliter', 'millilitre'): return 'ml'
                if u in ('l', 'liter', 'litre', 'lt'): return 'l'
                if u in ('ea', 'each', 'cnt', 'ct', 'count'): return 'ea'
                return u
            ppu_unit = norm_unit(vi_contains_unit or pack_unit)
            recipe_unit_norm = norm_unit(recipe_unit)
            if ppu_unit == recipe_unit_norm:
                cost = quantity * vi_price_per_unit
                return {'cost': round(cost, 4), 'unit_price': round(vi_price_per_unit, 4), 'source': 'vendor_item'}

        # PATH 1: same unit or no recipe unit -> direct multiply
        if not recipe_unit or recipe_unit == prod_unit or recipe_unit == pack_unit:
            cost = quantity * price
            return {'cost': round(cost, 4), 'unit_price': round(price, 4), 'source': 'vendor_item'}

        # PATH 2a: both weight units -> standard conversion
        ru_wt = WEIGHT_TO_OZ.get(recipe_unit)
        pu_wt = WEIGHT_TO_OZ.get(pack_unit) or WEIGHT_TO_OZ.get(prod_unit)
        if ru_wt and pu_wt and pack_size > 0:
            cost_per_oz = price / (pack_size * pu_wt)
            cost_per_ru = cost_per_oz * ru_wt
            line_cost = quantity * cost_per_ru
            return {'cost': round(line_cost, 4), 'unit_price': round(cost_per_ru, 4),
                    'source': 'standard_conversion'}

        # PATH 2b: both volume units -> standard conversion
        ru_vol = VOLUME_TO_FLOZ.get(recipe_unit)
        pu_vol = VOLUME_TO_FLOZ.get(pack_unit) or VOLUME_TO_FLOZ.get(prod_unit)
        if ru_vol and pu_vol and pack_size > 0:
            cost_per_floz = price / (pack_size * pu_vol)
            cost_per_ru = cost_per_floz * ru_vol
            line_cost = quantity * cost_per_ru
            return {'cost': round(line_cost, 4), 'unit_price': round(cost_per_ru, 4),
                    'source': 'standard_conversion'}

        # PATH 3: check product_unit_conversions table
        conversions = conn.execute("""
            SELECT from_qty, from_unit, to_qty, to_unit
            FROM product_unit_conversions
            WHERE product_id = ?
        """, (product_id,)).fetchall()

        for conv in conversions:
            conv = dict(conv)
            from_unit = (conv['from_unit'] or '').strip().lower()
            to_unit = (conv['to_unit'] or '').strip().lower()
            from_qty = conv['from_qty'] or 1
            to_qty = conv['to_qty'] or 0

            # Match: recipe_unit -> conversion from_unit
            if from_unit == recipe_unit:
                # "1 each = 1.1 oz" with weight purchase
                if to_unit in WEIGHT_UNITS and pu_wt and pack_size > 0:
                    oz_per_ru = (to_qty / from_qty) * WEIGHT_TO_OZ[to_unit]
                    cost_per_oz = price / (pack_size * pu_wt)
                    cost_per_ru = cost_per_oz * oz_per_ru
                    line_cost = quantity * cost_per_ru
                    return {'cost': round(line_cost, 4), 'unit_price': round(cost_per_ru, 4),
                            'source': 'vendor_item'}
                # "1 shot = 1.5 fl oz" with volume purchase
                if to_unit in VOLUME_UNITS and pu_vol and pack_size > 0:
                    floz_per_ru = (to_qty / from_qty) * VOLUME_TO_FLOZ[to_unit]
                    cost_per_floz = price / (pack_size * pu_vol)
                    cost_per_ru = cost_per_floz * floz_per_ru
                    line_cost = quantity * cost_per_ru
                    return {'cost': round(line_cost, 4), 'unit_price': round(cost_per_ru, 4),
                            'source': 'vendor_item'}

            # Match: pack_unit -> conversion from_unit (e.g. "1 gallon = 136 oz")
            if from_unit == pack_unit:
                if to_unit in WEIGHT_UNITS and recipe_unit in WEIGHT_UNITS and pack_size > 0:
                    weight_per_pu = (to_qty / from_qty) * WEIGHT_TO_OZ[to_unit]
                    cost_per_oz = price / (pack_size * weight_per_pu)
                    cost_per_ru = cost_per_oz * WEIGHT_TO_OZ[recipe_unit]
                    line_cost = quantity * cost_per_ru
                    return {'cost': round(line_cost, 4), 'unit_price': round(cost_per_ru, 4),
                            'source': 'vendor_item'}

        # PATH 4: no resolution — flag it, don't guess
        return {'cost': 0.0, 'unit_price': price, 'source': 'no_conversion'}

    except Exception as e:
        logger.error(f"cost_ingredient error for product {ri.get('product_id')}: {e}")
        return {'cost': 0.0, 'unit_price': 0.0, 'source': 'no_price'}


def cost_recipe(recipe_id, conn):
    """
    Calculate the full cost breakdown for a recipe.

    Returns dict with total_cost, cost_per_serving, food_cost_pct, ingredients.
    Updates the recipes table with calculated values.
    """
    recipe = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not recipe:
        return None

    recipe = dict(recipe)
    servings = recipe.get('serving_size') or 1
    menu_price = recipe.get('menu_price') or 0

    ingredients = conn.execute("""
        SELECT ri.id as ingredient_id, ri.product_id, ri.quantity, ri.unit,
               ri.yield_pct, ri.notes,
               p.name as product_name, p.yield_pct as product_yield_pct
        FROM recipe_ingredients ri
        LEFT JOIN products p ON ri.product_id = p.id
        WHERE ri.recipe_id = ?
    """, (recipe_id,)).fetchall()

    total_cost = 0.0
    ingredient_details = []

    for ing in ingredients:
        ing_dict = dict(ing)
        result = cost_ingredient(ing_dict, conn)

        ingredient_cost = result['cost']

        # Apply yield: use ingredient-level yield if set, else product-level yield
        yield_pct = ing_dict.get('yield_pct') or 100
        product_yield = ing_dict.get('product_yield_pct') or 1.0
        if yield_pct > 0 and yield_pct != 100:
            # Ingredient-level override (stored as percentage, e.g., 85 = 85%)
            ingredient_cost = ingredient_cost * (100 / yield_pct)
        elif product_yield > 0 and product_yield < 1.0:
            # Product-level yield (stored as decimal, e.g., 0.85 = 85%)
            ingredient_cost = ingredient_cost / product_yield

        total_cost += ingredient_cost

        ingredient_details.append({
            'ingredient_id': ing_dict['ingredient_id'],
            'product_id': ing_dict['product_id'],
            'product_name': ing_dict.get('product_name') or 'Unknown',
            'quantity': ing_dict['quantity'],
            'unit': ing_dict['unit'],
            'unit_price': result['unit_price'],
            'cost': round(ingredient_cost, 4),
            'source': result['source'],
        })

    total_cost = round(total_cost, 2)
    cost_per_serving = round(total_cost / servings, 2) if servings > 0 else total_cost
    food_cost_pct = round(cost_per_serving / menu_price * 100, 1) if menu_price > 0 else 0.0

    # Update recipes table
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE recipes
        SET total_cost = ?, cost_per_serving = ?, food_cost_pct = ?, last_costed_at = ?
        WHERE id = ?
    """, (total_cost, cost_per_serving, food_cost_pct, now, recipe_id))
    conn.commit()

    return {
        'recipe_id': recipe_id,
        'recipe_name': recipe.get('name', ''),
        'total_cost': total_cost,
        'cost_per_serving': cost_per_serving,
        'food_cost_pct': food_cost_pct,
        'servings': servings,
        'menu_price': menu_price,
        'last_costed_at': now,
        'ingredients': ingredient_details,
    }


def cost_all_recipes(conn):
    """
    Recalculate costs for every recipe.
    Returns: {'updated': int, 'skipped': int, 'errors': int}
    """
    recipes = conn.execute("SELECT id FROM recipes WHERE active = 1").fetchall()

    updated = 0
    skipped = 0
    errors = 0

    for r in recipes:
        try:
            result = cost_recipe(r['id'], conn)
            if result:
                updated += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error(f"cost_all_recipes error on recipe {r['id']}: {e}")
            errors += 1

    logger.info(f"cost_all_recipes: updated={updated}, skipped={skipped}, errors={errors}")
    return {'updated': updated, 'skipped': skipped, 'errors': errors}
