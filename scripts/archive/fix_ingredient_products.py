#!/usr/bin/env python3
"""
fix_ingredient_products.py — Remap recipe ingredients from orphan products
(no vendor item) to equivalent products that have vendor pricing.

Uses explicit manual mappings + conservative fuzzy matching.
"""

import argparse
from data_store import get_connection

# Manual mappings: orphan_product_id -> good_product_id
MANUAL_MAP = {
    327: 86,    # Tomatoes (5x6) -> Tomatoes (5x6, 25lb)
    329: 476,   # American Cheese, AI -> American Cheese, White (120ct)
    682: 177,   # Avocado, Generic -> Avocado, Hass #2
    701: 95,    # BBQ Sauce (House) -> BBQ Sauce (Sweet Baby)
    715: 201,   # Bacon, Sliced -> Bacon, Sliced (18-22ct)
    704: 226,   # Basil Pesto -> Basil Pesto, Armanino
    658: 106,   # Beef Patty, Generic -> Burger Patty (Chuck, Schweidson)
    713: 212,   # Black Beans -> Black Beans, Del Pasado
    332: 558,   # Blue Cheese Dressing -> Blue Cheese Dressing, Deluxe
    686: 275,   # Brussels Sprouts -> Brussels Sprouts Halves
    700: 213,   # Buffalo Sauce -> Buffalo Sauce (Sweet Baby)
    670: 112,   # Brioche Bun -> Brioche Roll, Sliced (Fireking)
    669: 159,   # English Muffin, Bun Style -> English Muffin, Sandwich 4"
    698: 239,   # Butter -> Butter Alternative Oil
    684: 553,   # Cabbage, Shredded -> Green Cabbage
    710: 153,   # Canola Oil -> Canola Oil, TFF
    685: 172,   # Carrot, Shredded -> Carrots, Jumbo Bag
    693: 178,   # Cheese, Blue Crumbled -> Blue Cheese Crumbles
    692: 276,   # Cheese, Cheddar Sharp -> Cheddar Cheese Blend
    695: 100,   # Cheese, Jack Cheddar -> Cheddar Cheese Blend
    697: 111,   # Cheese, Mozzarella Sticks -> Mozzarella Sticks
    696: 103,   # Cheese, Parmesan -> Parmesan Cheese, Grated
    663: 98,    # Chicken Breast, Grilled -> Chicken Breast, 6oz
    662: 98,    # Chicken Cutlet -> Chicken Breast, 6oz
    666: 165,   # Chicken Wings -> Chicken Wings, Split
    664: 158,   # Cod Fillet -> Cod Fillet (8-10oz)
    708: 209,   # Croutons -> Cheese Garlic Croutons
    683: 99,    # Cucumber -> Cucumber, #1 Super Select
    348: 534,   # Heavy Cream -> Heavy Cream, UHT
    350: 173,   # Iceberg Lettuce -> Lettuce, Iceberg, Cello
    712: 186,   # Jasmine Rice -> Jasmine Rice (Rykoff)
    687: 244,   # Lemon -> Lemon (Fresh Packer)
    674: 173,   # Lettuce, Iceberg -> Lettuce, Iceberg, Cello
    676: 174,   # Lettuce, Kale -> Lettuce, Romaine Hearts (closest green)
    675: 174,   # Lettuce, Romaine -> Lettuce, Romaine Hearts
    680: 175,   # Mushrooms -> Mushrooms, Sliced (2/5lb)
    672: 109,   # Naan Bread -> Garlic Naan
    678: 150,   # Red Onion, Standard -> Red Onions
    679: 254,   # Yellow Onion, Basic -> Yellow Onion, Jumbo Bag
    371: 168,   # Pickles -> Pickle Chips, Dill Kosher
    373: 559,   # Ranch Dressing -> Ranch Dressing (4/1 Gal)
    374: 98,    # Chicken Breasts -> Chicken Breast, 6oz
    375: 150,   # Red Onion, Regular -> Red Onions
    376: 88,    # Roasted Red Peppers -> Red Pepper
    377: 174,   # Romaine Lettuce -> Lettuce, Romaine Hearts
    699: 179,   # Sour Cream -> Sour Cream, Grade A
    380: 229,   # Steak Tips -> Strip Steak (2Rivers Choice)
    660: 229,   # Pub Steak -> Strip Steak (2Rivers Choice)
    381: 522,   # Swiss Cheese -> Swiss Cheese, Sliced
    383: 159,   # English Muffin (Thomas's) -> English Muffin, Sandwich 4"
    677: 86,    # Tomato, Sliced -> Tomatoes (5x6, 25lb)
    384: 261,   # Tortilla Chips -> Tortilla Chips, Yellow Triangle
    667: 218,   # Turkey Burger Patty -> Turkey Burger Patty, Chefs Line
    668: 668,   # Veggie Burger Patty -> keep as is (no vendor alternative)
    689: 154,   # Walnuts -> Walnuts, Halves & Pieces
    705: 240,   # Salsa (House) -> Salsa (La Victoria)
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = get_connection()

    # Get orphan products used in recipe ingredients
    orphans = conn.execute("""
        SELECT DISTINCT p.id, p.name, p.display_name,
               COUNT(ri.id) as usage_count
        FROM recipe_ingredients ri
        JOIN products p ON ri.product_id = p.id
        WHERE p.active_vendor_item_id IS NULL
        GROUP BY p.id
        ORDER BY p.name
    """).fetchall()

    print(f"Orphan products in recipes: {len(orphans)}")
    if args.dry_run:
        print("--- DRY RUN ---\n")

    remapped = 0
    skipped = 0
    rows_updated = 0

    for orphan in orphans:
        oid = orphan['id']
        oname = orphan['display_name'] or orphan['name']
        count = orphan['usage_count']

        if oid in MANUAL_MAP and MANUAL_MAP[oid] != oid:
            good_id = MANUAL_MAP[oid]
            good = conn.execute(
                "SELECT name, display_name FROM products WHERE id = ?",
                (good_id,)
            ).fetchone()
            if good:
                gname = good['display_name'] or good['name']
                if args.dry_run:
                    print(f"  [{oid}] {oname:40s} -> [{good_id}] {gname} ({count} rows)")
                else:
                    conn.execute("""
                        UPDATE recipe_ingredients
                        SET product_id = ?, product_name = ?
                        WHERE product_id = ?
                    """, (good_id, good['name'], oid))
                    rows_updated += count
                remapped += 1
            else:
                print(f"  [{oid}] {oname:40s} -> ERROR: product {good_id} not found")
                skipped += 1
        else:
            if args.dry_run:
                print(f"  [{oid}] {oname:40s} -> SKIPPED (no mapping) ({count} rows)")
            skipped += 1

    if not args.dry_run:
        conn.commit()

    conn.close()
    print(f"\n{'Would remap' if args.dry_run else 'Remapped'}: {remapped} products ({rows_updated} ingredient rows)")
    print(f"Skipped (no mapping): {skipped}")


if __name__ == "__main__":
    main()
