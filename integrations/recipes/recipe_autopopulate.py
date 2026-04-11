#!/usr/bin/env python3
"""
recipe_autopopulate.py — Parse menu descriptions into recipe ingredients
using Claude API. Matches against canonical products.

MANUAL RUN ONLY: python3 recipe_autopopulate.py [--dry-run] [--limit N]

--dry-run:    Print what would be inserted without calling API or writing DB
--limit N:    Only process N recipes (for testing, saves API cost)
--recipe-id N: Process only this specific recipe ID
--food-only:  Only process FOOD category recipes (skip beverages)
"""

import json
import sys
import time
import os
import argparse
import requests
from dotenv import load_dotenv
from data_store import get_connection

load_dotenv()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


def get_api_key():
    """Get Anthropic API key from .env (same as invoice_processor.py)."""
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env file")
        sys.exit(1)
    return key


def get_canonical_products():
    """Load canonical product list from DB."""
    conn = get_connection()
    products = conn.execute("""
        SELECT id, name, category, recipe_unit
        FROM products
        WHERE active = 1
        ORDER BY name
    """).fetchall()
    result = [
        {"id": r["id"], "name": r["name"],
         "category": r["category"], "recipe_unit": r["recipe_unit"]}
        for r in products
    ]
    conn.close()
    return result


def get_recipes_with_descriptions(food_only=False, wipe=False):
    """Get recipes that have menu descriptions to parse from."""
    conn = get_connection()
    where_extra = "AND r.category = 'FOOD'" if food_only else ""
    recipes = conn.execute(f"""
        SELECT r.id, r.name, r.category, r.menu_price,
               mi.description as menu_description
        FROM recipes r
        JOIN menu_items mi ON r.menu_item_guid = mi.guid
        LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
        WHERE r.active = 1
          AND mi.description IS NOT NULL AND mi.description != ''
          {where_extra}
        GROUP BY r.id
        {"" if wipe else "HAVING COUNT(ri.id) = 0"}
        ORDER BY r.name
    """).fetchall()
    result = [
        {"id": r["id"], "name": r["name"],
         "description": r["menu_description"] or "",
         "category": r["category"], "menu_price": r["menu_price"]}
        for r in recipes
    ]
    conn.close()
    return result


def parse_description(recipe, canonical_products, api_key):
    """Send recipe to Claude API for ingredient parsing."""

    # Build a condensed product list (only food-relevant categories)
    food_categories = {'FOOD', 'AI Inventory', 'DAIRY', 'PRODUCE', 'MEAT',
                       'SEAFOOD', 'BAKERY', 'DRY_GOODS', 'FROZEN'}
    bev_categories = {'BEER', 'WINE', 'LIQUOR', 'NA_BEVERAGES', 'SODA',
                      'JUICE', 'COFFEE'}

    if recipe['category'] in ('BEER', 'WINE', 'LIQUOR', 'NA_BEVERAGES'):
        relevant_cats = bev_categories | food_categories
    else:
        relevant_cats = food_categories

    relevant_products = [
        p for p in canonical_products
        if p['category'] in relevant_cats or p['category'] is None
    ]

    # If too many products, just send names (no categories) to save tokens
    if len(relevant_products) > 200:
        canonical_list = "\n".join(f"- {p['name']}" for p in relevant_products)
    else:
        canonical_list = "\n".join(
            f"- {p['name']} (unit: {p['recipe_unit'] or 'ea'})"
            for p in relevant_products
        )

    desc_part = f"\nDESCRIPTION: {recipe['description']}" if recipe['description'] else ""

    prompt = f"""You are a restaurant recipe parser. Extract ONLY the ingredients explicitly mentioned in the menu description below. Do NOT guess or add ingredients that are not stated.

MENU ITEM: {recipe['name']}{desc_part}
CATEGORY: {recipe['category']}

CANONICAL PRODUCT LIST (match to these when possible — use EXACT name):
{canonical_list}

STRICT RULES:
1. ONLY include ingredients that are explicitly named in the DESCRIPTION text.
2. Do NOT add assumed sides (fries, coleslaw) unless the description says them.
3. Do NOT add condiments, sauces, oils, or garnishes unless the description lists them.
4. Do NOT add bread/bun unless the description mentions it. Burgers get an english muffin only if the description says "english muffin", otherwise assume a standard burger bun/roll.
5. Match each ingredient to a product from the canonical list. Use EXACT name (case-sensitive).
6. If no product matches, set canonical_product_name to null and provide a clean suggested_name.
7. Suggest a unit: oz, lb, each, fl_oz, cup, tbsp, tsp, slice, portion.
8. For single-pour beverages (beer, wine, soda), return [].
9. For cocktails, list the spirits and mixers mentioned.
10. If the description is vague (e.g. "served with fries") only include the main protein + fries.
11. If the description says nothing useful (like "whatever you like"), just include the obvious base item from the menu item name (e.g. a burger = burger patty + english muffin).

Return ONLY valid JSON array. No markdown, no backticks:
[{{"canonical_product_name": "Exact Name or null", "suggested_name": "Clean Name or null", "suggested_unit": "oz", "notes": ""}}]

Empty result: []"""

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }

    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        text = text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        ingredients = json.loads(text)
        return ingredients

    except json.JSONDecodeError as e:
        print(f"  JSON parse error for {recipe['name']}: {e}")
        print(f"  Raw text: {text[:200]}")
        return []
    except Exception as e:
        print(f"  API error for {recipe['name']}: {e}")
        return []


def save_ingredients(recipe_id, ingredients, canonical_products):
    """Insert parsed ingredients into recipe_ingredients."""
    if not ingredients:
        return 0, 0, 0

    # Build canonical name → id lookup (case-insensitive)
    canonical_lookup = {}
    for p in canonical_products:
        canonical_lookup[p["name"].lower().strip()] = p["id"]

    conn = get_connection()
    c = conn.cursor()
    saved = 0
    matched = 0
    new = 0

    for ing in ingredients:
        canon_name = ing.get("canonical_product_name")
        suggested = ing.get("suggested_name")
        unit = ing.get("suggested_unit", "ea")
        notes = ing.get("notes", "")

        product_name = canon_name or suggested or "Unknown"
        product_id = 0

        if canon_name:
            # Try exact match first, then case-insensitive
            pid = canonical_lookup.get(canon_name.lower().strip())
            if pid:
                product_id = pid
                matched += 1
            else:
                new += 1
        else:
            new += 1

        try:
            c.execute("""
                INSERT INTO recipe_ingredients
                (recipe_id, product_id, product_name, quantity, unit, yield_pct, notes)
                VALUES (?, ?, ?, 0, ?, 100, ?)
            """, (recipe_id, product_id, product_name, unit, notes))
            saved += 1
        except Exception as e:
            print(f"    ERROR inserting {product_name}: {e}")

    conn.commit()
    conn.close()
    return saved, matched, new


def main():
    parser = argparse.ArgumentParser(description="Auto-populate recipe ingredients from menu descriptions")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be done without calling API or writing DB")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only N recipes (0 = all)")
    parser.add_argument("--recipe-id", type=int, default=0,
                        help="Process only this recipe ID")
    parser.add_argument("--food-only", action="store_true",
                        help="Only process FOOD category recipes")
    parser.add_argument("--wipe", action="store_true",
                        help="Wipe existing ingredients first and re-populate from descriptions")
    args = parser.parse_args()

    print("Recipe Auto-Populate (Description-Only Mode)")
    print("=" * 60)

    canonical_products = get_canonical_products()
    print(f"Canonical products loaded: {len(canonical_products)}")

    recipes = get_recipes_with_descriptions(food_only=args.food_only, wipe=args.wipe)
    print(f"Recipes with menu descriptions: {len(recipes)}")

    if args.recipe_id:
        recipes = [r for r in recipes if r["id"] == args.recipe_id]
        if not recipes:
            print(f"Recipe {args.recipe_id} not found or has no menu description")
            return

    if args.limit:
        recipes = recipes[:args.limit]
        print(f"Limited to {args.limit} recipes")

    if not recipes:
        print("No recipes to process!")
        return

    if args.dry_run:
        print("\n--- DRY RUN MODE ---")
        for i, r in enumerate(recipes, 1):
            desc_preview = (r['description'][:70] + '...') if len(r['description']) > 70 else r['description']
            print(f"[{i}/{len(recipes)}] {r['name']} [{r['category']}]")
            print(f"         \"{desc_preview}\"")
        print(f"\nWould process {len(recipes)} recipes via Claude API")
        est_cost = len(recipes) * 0.005
        print(f"Estimated API cost: ~${est_cost:.2f}")
        if args.wipe:
            print("WARNING: --wipe flag set — would delete existing ingredients first")
        return

    api_key = get_api_key()

    # Estimate cost and confirm
    est_cost = len(recipes) * 0.005
    print(f"\nEstimated API cost: ~${est_cost:.2f}")
    if args.wipe:
        print("WIPE MODE: Will delete existing ingredients before re-populating")
    print(f"Processing {len(recipes)} recipes...")
    print("-" * 60)

    conn = get_connection()
    total_ingredients = 0
    total_matched = 0
    total_new = 0
    total_skipped = 0
    total_api_calls = 0
    total_wiped = 0

    for i, recipe in enumerate(recipes, 1):
        desc_preview = (recipe['description'][:60] + '...') if len(recipe['description']) > 60 else recipe['description']
        print(f"\n[{i}/{len(recipes)}] {recipe['name']} [{recipe['category']}]")
        print(f"  \"{desc_preview}\"")

        ingredients = parse_description(recipe, canonical_products, api_key)
        total_api_calls += 1

        # Rate limit
        if i < len(recipes):
            time.sleep(0.5)

        if not ingredients:
            print(f"  No ingredients parsed")
            total_skipped += 1
            continue

        # Wipe existing ingredients if --wipe
        if args.wipe:
            deleted = conn.execute(
                "DELETE FROM recipe_ingredients WHERE recipe_id = ?",
                (recipe["id"],)
            ).rowcount
            if deleted:
                total_wiped += deleted
                print(f"  Wiped {deleted} old ingredients")
            conn.commit()

        saved, matched, new = save_ingredients(
            recipe["id"], ingredients, canonical_products
        )
        total_ingredients += saved
        total_matched += matched
        total_new += new

        print(f"  -> {saved} ingredients ({matched} matched, {new} new)")

    conn.close()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  Recipes processed:        {len(recipes)}")
    print(f"  API calls made:           {total_api_calls}")
    print(f"  Skipped (no ingredients): {total_skipped}")
    if args.wipe:
        print(f"  Old ingredients wiped:    {total_wiped}")
    print(f"  Total ingredients added:  {total_ingredients}")
    print(f"  Matched to products:      {total_matched}")
    print(f"  New (unmatched):          {total_new}")
    print(f"  Est. API cost:            ~${total_api_calls * 0.005:.2f}")


if __name__ == "__main__":
    main()
