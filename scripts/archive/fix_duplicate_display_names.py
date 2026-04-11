#!/usr/bin/env python3
"""
fix_duplicate_display_names.py — Differentiate products that share the same display_name.

Uses vendor item details (pack size, format) to make each product distinguishable.
E.g., 4 products all named "Bud Light" become:
  - "Bud Light, Keg (15.5 gal)"
  - "Bud Light, 24pk Bottles (12oz)"
  - etc.

MANUAL RUN: python3 fix_duplicate_display_names.py [--dry-run] [--limit N]
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
BATCH_SIZE = 40


def get_api_key():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env file")
        sys.exit(1)
    return key


def get_duplicate_groups():
    """Find all products sharing the same display_name, with vendor item details."""
    conn = get_connection()

    # Get groups of products with duplicate display_names
    groups = conn.execute("""
        SELECT LOWER(display_name) as dn, GROUP_CONCAT(id) as ids
        FROM products
        WHERE active = 1 AND display_name IS NOT NULL AND display_name != ''
        GROUP BY LOWER(display_name)
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC
    """).fetchall()

    result = []
    for g in groups:
        product_ids = [int(x) for x in g["ids"].split(",")]
        products = []
        for pid in product_ids:
            p = conn.execute("""
                SELECT p.id, p.name, p.display_name, p.category,
                       vi.vendor_description, vi.pack_size, vi.pack_contains,
                       vi.contains_unit, vi.purchase_price
                FROM products p
                LEFT JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
                WHERE p.id = ?
            """, (pid,)).fetchone()
            if p:
                products.append(dict(p))
        if products:
            result.append(products)

    conn.close()
    return result


def differentiate_batch(groups, api_key):
    """Send a batch of duplicate groups to Claude for differentiation."""

    lines = []
    for group in groups:
        for p in group:
            vi_info = ""
            if p["vendor_description"]:
                vi_info = f" | vendor_desc: {p['vendor_description']}"
            if p["pack_size"]:
                vi_info += f" | pack: {p['pack_size']}"
            if p["pack_contains"]:
                vi_info += f" | contains: {p['pack_contains']} {p['contains_unit'] or ''}"
            if p["purchase_price"]:
                vi_info += f" | ${p['purchase_price']}"
            lines.append(f"{p['id']}|{p['name']}|{p['display_name']}|{p['category']}{vi_info}")
        lines.append("---")  # Group separator

    product_block = "\n".join(lines)

    prompt = f"""You are fixing duplicate product names in a restaurant inventory system.

Multiple products currently share the same display_name. You need to make each one UNIQUE by incorporating the key differentiator (pack format, size, vendor, etc.).

RULES:
1. Keep names SHORT (2-5 words). Use the current display_name as the base.
2. Add a brief differentiator in parentheses or after a comma: "Bud Light, Keg (15.5 gal)" or "Bud Light, 24pk Bottles"
3. For BEER: differentiate by format — keg vs bottles vs cans. Include size. "Bud Light, Keg (1/2 BBL)" vs "Bud Light, 24pk (12oz)"
4. For LIQUOR/WINE: differentiate by bottle size if different, or case size. "Titos Vodka, 750ml" vs "Titos Vodka, 1.75L"
5. For FOOD: differentiate by vendor, grade, or pack size. "Bacon, Sliced (18-22ct)" vs "Bacon, Sliced (Hatfield)"
6. For SUPPLIES: differentiate by size/type. "Take-Out Container, 9x6" vs "Take-Out Container, 9x9"
7. If products are truly identical (same vendor, same pack), still give them unique names (append vendor code snippet).
8. Use title case. Keep it clean and readable.
9. If a product has NO vendor info, append something from the raw name to differentiate.

INPUT FORMAT: id|raw_name|current_display_name|category|vendor_info
Groups are separated by "---"

INPUT:
{product_block}

Return ONLY a JSON object mapping id (as string) to new unique display_name. No markdown, no backticks."""

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
    parser = argparse.ArgumentParser(description="Fix duplicate product display names")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to DB")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only N groups (0 = all)")
    args = parser.parse_args()

    print("Fix Duplicate Display Names")
    print("=" * 60)

    groups = get_duplicate_groups()
    total_products = sum(len(g) for g in groups)
    print(f"Duplicate groups: {len(groups)} ({total_products} products)")

    if args.limit:
        groups = groups[:args.limit]
        total_products = sum(len(g) for g in groups)
        print(f"Limited to {args.limit} groups ({total_products} products)")

    if not groups:
        print("Nothing to fix!")
        return

    # Batch groups — aim for ~40 products per API call
    batches = []
    current_batch = []
    current_count = 0
    for g in groups:
        if current_count + len(g) > BATCH_SIZE and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_count = 0
        current_batch.append(g)
        current_count += len(g)
    if current_batch:
        batches.append(current_batch)

    est_cost = len(batches) * 0.01
    print(f"Batches: {len(batches)} (est. API cost: ~${est_cost:.2f})")

    if args.dry_run:
        print("\n--- DRY RUN ---")

    api_key = get_api_key()

    total_updated = 0
    conn = get_connection() if not args.dry_run else None

    for i, batch in enumerate(batches, 1):
        batch_products = sum(len(g) for g in batch)
        print(f"\nBatch {i}/{len(batches)} ({batch_products} products, {len(batch)} groups)...")
        name_map = differentiate_batch(batch, api_key)

        if not name_map:
            print("  No results returned, skipping batch")
            continue

        for g in batch:
            for p in g:
                pid = str(p["id"])
                new_name = name_map.get(pid)
                if new_name:
                    old_name = p["display_name"]
                    if args.dry_run:
                        if new_name != old_name:
                            print(f"  [{p['id']}] {old_name:30s} -> {new_name}")
                    else:
                        conn.execute(
                            "UPDATE products SET display_name = ? WHERE id = ?",
                            (new_name, p["id"])
                        )
                    total_updated += 1

        if not args.dry_run and conn:
            conn.commit()

        if i < len(batches):
            time.sleep(0.5)

    if conn:
        conn.close()

    print("\n" + "=" * 60)
    print(f"Updated: {total_updated} products")
    print(f"API calls: {len(batches)}")
    if args.dry_run:
        print("DRY RUN - nothing written to DB")


if __name__ == "__main__":
    main()
