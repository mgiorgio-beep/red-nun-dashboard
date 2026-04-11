#!/usr/bin/env python3
"""
product_name_cleanup.py — Generate clean display_name for products using Claude API.

MANUAL RUN ONLY: python3 product_name_cleanup.py [--dry-run] [--limit N]

Converts vendor-style names like "BACON, PORK 18-22 CT SLCD LAID" to
human-friendly names like "Bacon, Sliced".
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
BATCH_SIZE = 60


def get_api_key():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env file")
        sys.exit(1)
    return key


def get_products_needing_names():
    conn = get_connection()
    products = conn.execute("""
        SELECT id, name, category
        FROM products
        WHERE active = 1
          AND (display_name IS NULL OR display_name = '')
        ORDER BY name
    """).fetchall()
    result = [{"id": r["id"], "name": r["name"], "category": r["category"]} for r in products]
    conn.close()
    return result


def clean_names_batch(products, api_key):
    """Send a batch of product names to Claude for cleanup."""

    product_lines = "\n".join(
        f'{p["id"]}|{p["name"]}|{p["category"]}'
        for p in products
    )

    prompt = f"""You are cleaning up product names for a restaurant inventory system.
Convert vendor-style, abbreviated, or ALL-CAPS names into short, clean, human-friendly display names.

RULES:
1. Keep it SHORT — 1-3 words ideally, 4 max. Think menu/kitchen language.
2. Drop vendor codes, pack sizes, brand names, SKU numbers, abbreviations like "HRML", "MOLLYS KIT", "FCY CND"
3. Keep the essential identity: "BACON, PORK 18-22 CT SLCD LAID" → "Bacon, Sliced"
4. Use title case: "Burger Patty", "Blue Cheese Crumble", "Sweet Potato Fries"
5. For liquor/beer/wine, keep the brand: "ABSOLUT CITRON VODKA 80" → "Absolut Citron"
6. For supplies/non-food, simplify: "BAG RESEAL SHOP PAPR 65 LB" → "Paper Bags"
7. If the name is already clean (like "Avocado", "BBQ Sauce"), keep it as-is.
8. For duplicates that differ only in brand/vendor, the display name can be the same.

INPUT FORMAT: id|original_name|category
OUTPUT FORMAT: Return ONLY a JSON object mapping id (as string) to clean name. No markdown, no backticks.

INPUT:
{product_lines}

Return JSON object like: {{"123": "Bacon, Sliced", "456": "Burger Patty"}}"""

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }

    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        return json.loads(text)

    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        print(f"  Raw: {text[:300]}")
        return {}
    except Exception as e:
        print(f"  API error: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(description="Clean up product display names")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only N products (0 = all)")
    args = parser.parse_args()

    print("Product Name Cleanup")
    print("=" * 60)

    products = get_products_needing_names()
    print(f"Products needing display names: {len(products)}")

    if args.limit:
        products = products[:args.limit]
        print(f"Limited to {args.limit}")

    if not products:
        print("Nothing to process!")
        return

    batches = [products[i:i + BATCH_SIZE] for i in range(0, len(products), BATCH_SIZE)]
    est_cost = len(batches) * 0.01
    print(f"Batches: {len(batches)} (est. API cost: ~${est_cost:.2f})")

    if args.dry_run:
        print("\n--- DRY RUN ---")

    api_key = get_api_key()

    total_updated = 0
    conn = get_connection() if not args.dry_run else None

    for i, batch in enumerate(batches, 1):
        print(f"\nBatch {i}/{len(batches)} ({len(batch)} products)...")
        name_map = clean_names_batch(batch, api_key)

        if not name_map:
            print("  No results returned, skipping batch")
            continue

        for p in batch:
            pid = str(p["id"])
            clean = name_map.get(pid)
            if clean:
                if args.dry_run:
                    if clean != p["name"]:
                        print(f"  {p['name'][:45]:45s} → {clean}")
                else:
                    conn.execute(
                        "UPDATE products SET display_name = ? WHERE id = ?",
                        (clean, p["id"])
                    )
                total_updated += 1

        if not args.dry_run:
            conn.commit()

        if i < len(batches):
            time.sleep(0.5)

    if conn:
        conn.close()

    print("\n" + "=" * 60)
    print(f"Updated: {total_updated} products")
    print(f"API calls: {len(batches)}")
    if args.dry_run:
        print("DRY RUN — nothing written to DB")


if __name__ == "__main__":
    main()
