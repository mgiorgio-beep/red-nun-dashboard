#!/usr/bin/env python3
"""
Import Toast menu items CSV as recipe shells.
Run from /opt/rednun: python3 import_menu_recipes.py
"""
import sys
import os
sys.path.insert(0, '/opt/rednun')

from data_store import get_connection

# Full CSV data embedded
MENU_ITEMS = [
    ("Add Salmon", "Salad Adds", 12.00),
    ("Beef- B&B Burger", "Burgers", 19.00),
    ("Beef- BYO Burger", "Burgers", 17.00),
    ("Beef- Fire Ice Burger", "Burgers", 19.00),
    ("Beef- Nun Burger", "Burgers", 19.00),
    ("Beef Skewers", "Catering Menu", 65.00),
    ("Beef Stir Fry", "Steak", 25.99),
    ("Beef - Thai Burger", "Burgers", 16.99),
    ("Beef- Western Burger", "Burgers", 19.00),
    ("Boneless Wings (1)", "Apps", 13.00),
    ("Boneless Wings (1.5)", "Apps", 18.00),
    ("Boneless Wings (.5)", "Apps", 6.50),
    ("Buddha Burger 1", "Burgers", 16.00),
    ("Buffalo Chic SW", "Sandwiches", 17.00),
    ("Buff Chicken Fingers", "Apps", 13.00),
    ("Butter Toffee Cake", "Dessert", 10.00),
    ("Chicken Finger Platter", "Catering Menu", 32.00),
    ("Chicken Fingers", "Apps", 12.00),
    ("Chicken Potstickers- Platter", "Catering Menu", 20.00),
    ("Chicken Skewers", "Catering Menu", 50.00),
    ("Chocolate Bomb", "Dessert", 9.00),
    ("Chowder- Bowl", "Apps", 7.99),
    ("Chowder- Cup", "Apps", 6.99),
    ("Clam Strip Plate", "Seafood", 18.00),
    ("Crispy Brussels", "Apps", 14.00),
    ("Fish & Chips", "Seafood", 18.00),
    ("Fish SW", "Sandwiches", 16.00),
    ("Fish Tacos", "Seafood", 18.00),
    ("Fried Chicken Sandwich", "Sandwiches", 17.00),
    ("Fried Chic SW", "Sandwiches", 17.00),
    ("Fried Clams", "Seafood", 32.99),
    ("Grilled Chicken Add", "Salad Adds", 9.00),
    ("Grilled Chic SW", "Sandwiches", 17.00),
    ("Jalepeno Poppers- Platter", "Catering Menu", 20.00),
    ("Jerk Rice Bowl", "Salads", 15.00),
    ("Jerk Wrap", "Sandwiches", 18.00),
    ("Meatloaf SP", "Specials", 19.00),
    ("Mini Beef Wellington", "Catering Menu", 50.00),
    ("Mozzarella Sticks- Platter", "Catering Menu", 20.00),
    ("Pretzel", "Apps", 15.00),
    ("Pulled Pork", "Sandwiches", 17.00),
    ("Rice Bowl- Chicken", "Salads", 20.00),
    ("Rice Bowl- No Protein", "Salads", 15.00),
    ("Rice Bowl-Salmon", "Salads", 21.00),
    ("Rice Bowl- StkTips", "Salads", 27.00),
    ("Steak & Cheese", "Sandwiches", 19.99),
    ("Steak & Cheese Egg Roll", "Catering Menu", 60.00),
    ("Steak Frites", "Steak", 32.00),
    ("Steak Tip Add", "Salad Adds", 15.00),
    ("Steak Tips", "Steak", 30.00),
    ("Stuff Clam", "Apps", 9.00),
    ("Sub Rings", "Sub", 1.75),
    ("Sub Sweets", "Sub", 1.75),
    ("Sub Truffle", "Sub", 1.75),
    ("Thai Wrap", "Sandwiches", 18.00),
    ("Turkey-B&B Burger", "Burgers", 18.00),
    ("Turkey-BYO Burger", "Burgers", 16.00),
    ("Turkey Club", "Sandwiches", 14.99),
    ("Turkey- Fire Ice Burger", "Burgers", 18.00),
    ("Turkey-Nun Burger", "Burgers", 19.00),
    ("Turkey- Thai Burger", "Burgers", 17.00),
    ("Turkey - Western Burger", "Burgers", 18.00),
    ("Veggie -B&B Burger", "Burgers", 18.00),
    ("Veggie- BYO Burger", "Burgers", 16.00),
    ("Veggie Egg Roll", "Catering Menu", 30.00),
    ("Veggie-Fire Ice Burger", "Burgers", 18.00),
    ("Veggie Nun Burger", "Burgers", 18.00),
    ("Veggie- Thai Burger", "Burgers", 17.00),
    ("Veggie- Western Burger", "Burgers", 18.00),
    ("Wedge Salad", "Salads", 13.00),
    ("Wings (10)", "Apps", 26.00),
    ("Wings (6)", "Apps", 17.00),
    ("Wings- Platter", "Catering Menu", 32.00),
]

def get_recipes_schema(conn):
    c = conn.cursor()
    c.execute("PRAGMA table_info(recipes)")
    return [row[1] for row in c.fetchall()]

def import_recipes():
    conn = get_connection()
    c = conn.cursor()

    # Check schema
    cols = get_recipes_schema(conn)
    print(f"Recipes table columns: {cols}")

    # Get existing recipe names to skip duplicates
    c.execute("SELECT name FROM recipes")
    existing = {row[0].lower() for row in c.fetchall()}
    print(f"Existing recipes: {len(existing)}")

    inserted = 0
    skipped = 0

    for name, category, price in MENU_ITEMS:
        if name.lower() in existing:
            print(f"  SKIP (exists): {name}")
            skipped += 1
            continue

        # Build insert based on available columns
        fields = ['name', 'category', 'location']
        values = [name, category, 'both']

        if 'menu_price' in cols:
            fields.append('menu_price')
            values.append(price)
        if 'price' in cols and 'menu_price' not in cols:
            fields.append('price')
            values.append(price)
        if 'servings' in cols:
            fields.append('servings')
            values.append(1)

        sql = f"INSERT INTO recipes ({', '.join(fields)}) VALUES ({', '.join(['?']*len(fields))})"
        try:
            c.execute(sql, values)
            print(f"  ✅ {name} ({category}) ${price}")
            inserted += 1
        except Exception as e:
            print(f"  ❌ {name}: {e}")

    conn.commit()
    conn.close()
    print(f"\nDone: {inserted} inserted, {skipped} skipped")

if __name__ == '__main__':
    import_recipes()
