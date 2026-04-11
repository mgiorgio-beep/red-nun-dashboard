#!/usr/bin/env python3
"""
auto_fill_pack_contains.py — Parse vendor item names/pack_size to auto-fill pack_contains.

Converts common patterns:
  K-15.5 GAL HB → 1984 fl_oz (15.5 gal * 128)
  K-5.16 GAL SB → 660.48 fl_oz (5.16 gal * 128)
  24 12OZ / B-24 12OZ → 288 fl_oz (24 * 12)
  4/6 12OZ → 288 fl_oz (4 * 6 * 12)
  C-4/6 12OZ → 288 fl_oz
  6/CS (liquor) → 6 bottles, each 750ml = 152.16 fl_oz total per bottle
  750ml → 25.36 fl_oz
  1.75L → 59.17 fl_oz
  1L → 33.81 fl_oz

MANUAL RUN: python3 auto_fill_pack_contains.py [--dry-run]
"""

import re
import argparse
from data_store import get_connection

# Conversion factors
GAL_TO_FLOZ = 128.0
ML_TO_FLOZ = 0.033814
LITER_TO_FLOZ = 33.814
OZ_TO_FLOZ = 1.0  # fluid oz = oz for beverages


def parse_pack_info(vendor_desc, pack_size, category):
    """
    Try to parse pack_contains (in fl_oz for beverages, oz/lb/each for food)
    from the vendor description and pack_size fields.
    Returns (pack_contains, contains_unit) or (None, None).
    """
    desc = (vendor_desc or '').upper()
    pack = (pack_size or '').upper()
    combined = f"{desc} {pack}"

    # === KEGS ===
    # K-15.5 GAL, 1/2BBL, 15.5GAL
    m = re.search(r'K[- ]?([\d.]+)\s*GAL', combined)
    if m:
        gal = float(m.group(1))
        return round(gal * GAL_TO_FLOZ, 2), 'fl_oz'

    m = re.search(r'1/2\s*BBL', combined)
    if m:
        return round(15.5 * GAL_TO_FLOZ, 2), 'fl_oz'

    m = re.search(r'1/4\s*BBL', combined)
    if m:
        return round(7.75 * GAL_TO_FLOZ, 2), 'fl_oz'

    m = re.search(r'1/6\s*BBL', combined)
    if m:
        return round(5.167 * GAL_TO_FLOZ, 2), 'fl_oz'

    # === CASES OF CANS/BOTTLES (beverages only) ===
    if category in ('BEER', 'NA_BEVERAGES', 'LIQUOR', 'WINE', 'LIQUOR_WINE_BEER'):
        # "4/6 12OZ" or "4/6/12 OZ" or "4-6PK 16OZ" — multi-pack (check first, more specific)
        m = re.search(r'(\d+)\s*[-/]\s*(\d+)\s*(?:PK\s*)?[/\s]*(\d+)\s*(?:FL\s*)?OZ', combined)
        if m:
            packs = int(m.group(1))
            per_pack = int(m.group(2))
            size_oz = int(m.group(3))
            if 1 <= packs <= 20 and 1 <= per_pack <= 30 and 1 <= size_oz <= 64:
                return round(packs * per_pack * size_oz, 2), 'fl_oz'

        # "B-24 12OZ", "24 12OZ", "C-24 12OZ" — simple case
        m = re.search(r'[BC]?-?(\d+)\s+(\d+)\s*(?:FL\s*)?OZ', combined)
        if m:
            count = int(m.group(1))
            size_oz = int(m.group(2))
            if 4 <= count <= 100 and 4 <= size_oz <= 64:
                return round(count * size_oz, 2), 'fl_oz'

    # === LIQUOR BOTTLES ===
    # "6/CS" for liquor = 6 x 750ml bottles
    if category in ('LIQUOR', 'LIQUOR_WINE_BEER'):
        m = re.search(r'(\d+)\s*/\s*CS', combined)
        if m:
            count = int(m.group(1))
            # Standard liquor bottle = 750ml = 25.36 fl_oz
            return round(count * 750 * ML_TO_FLOZ, 2), 'fl_oz'

        # "6/C" same thing
        m = re.search(r'(\d+)\s*/\s*C\b', combined)
        if m:
            count = int(m.group(1))
            return round(count * 750 * ML_TO_FLOZ, 2), 'fl_oz'

        # Single bottle — if no pack info and price looks like single bottle ($15-$50)
        # Check for ml/L in name
        m = re.search(r'(\d+)\s*ML', combined)
        if m:
            ml = int(m.group(1))
            return round(ml * ML_TO_FLOZ, 2), 'fl_oz'

        m = re.search(r'([\d.]+)\s*L\b', combined)
        if m:
            liters = float(m.group(1))
            if liters < 10:  # sanity check
                return round(liters * LITER_TO_FLOZ, 2), 'fl_oz'

    # === WINE ===
    if category == 'WINE':
        m = re.search(r'(\d+)\s*/\s*C\b', combined)
        if m:
            count = int(m.group(1))
            return round(count * 750 * ML_TO_FLOZ, 2), 'fl_oz'
        m = re.search(r'(\d+)\s*ML', combined)
        if m:
            ml = int(m.group(1))
            return round(ml * ML_TO_FLOZ, 2), 'fl_oz'

    # === NA BEVERAGES ===
    if category == 'NA_BEVERAGES':
        # "12/750" = 12 bottles of 750ml
        m = re.search(r'(\d+)\s*/\s*(\d{3,4})\b', combined)
        if m:
            count = int(m.group(1))
            ml = int(m.group(2))
            if 100 <= ml <= 2000:
                return round(count * ml * ML_TO_FLOZ, 2), 'fl_oz'

        # "C-3/8 14.9OZ" — 3 packs of 8 at 14.9oz
        m = re.search(r'C?-?(\d+)\s*/\s*(\d+)\s+([\d.]+)\s*OZ', combined)
        if m:
            packs = int(m.group(1))
            per = int(m.group(2))
            oz = float(m.group(3))
            return round(packs * per * oz, 2), 'fl_oz'

    # === FOOD — parse weight-based packs ===
    if category in ('FOOD', 'TOGO_SUPPLIES', 'KITCHEN_SUPPLIES', 'DR_SUPPLIES'):
        # "6/5LB" or "6/5 LB" or "4/5 LB B" — cases of lb bags
        m = re.search(r'(\d+)\s*/\s*([\d.]+)\s*LB', combined)
        if m:
            count = int(m.group(1))
            lbs = float(m.group(2))
            if 1 <= count <= 20 and 0.5 <= lbs <= 50:
                return round(count * lbs, 2), 'lb'

        # "1/48 CNT" or "12/3 CNT" — explicit count packs (from pack_size)
        m = re.search(r'(\d+)\s*/\s*(\d+)\s*(?:CNT|EA)', pack)
        if m:
            packs = int(m.group(1))
            count = int(m.group(2))
            if 1 <= packs <= 20 and 1 <= count <= 1000:
                return round(packs * count, 2), 'each'

        # "250 EA T" or "250CNT" in pack_size
        m = re.search(r'(\d+)\s*(?:EA|CNT)\b', pack)
        if m:
            count = int(m.group(1))
            if 10 <= count <= 2000:
                return round(count, 2), 'each'

        # "120 CT" or "200 CT" or "48 CNT" in description — but NOT "18-22 CT" (that's slices/lb)
        # Only match standalone count patterns like "SLCD 120 CT" or "SKN MED 200 CT"
        m = re.search(r'\b(\d+)\s*(?:CT|CNT)\b', desc)
        if m:
            count = int(m.group(1))
            # Skip range patterns like "18-22 CT" (slices per lb) and small counts
            range_match = re.search(r'\d+-\d+\s*CT', desc)
            if not range_match and 20 <= count <= 500:
                return round(count, 2), 'each'

    return None, None


def main():
    parser = argparse.ArgumentParser(description="Auto-fill pack_contains from vendor item names")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to DB")
    args = parser.parse_args()

    conn = get_connection()

    # Get all vendor items missing pack_contains that have a price
    items = conn.execute("""
        SELECT vi.id, vi.vendor_description, vi.pack_size, vi.purchase_price,
               p.category, p.id as product_id, p.name as product_name
        FROM vendor_items vi
        JOIN products p ON vi.product_id = p.id
        WHERE p.active = 1
          AND (vi.pack_contains IS NULL OR vi.pack_contains = 0)
          AND vi.purchase_price > 0
        ORDER BY p.category, vi.vendor_description
    """).fetchall()

    print(f"Vendor items missing pack_contains: {len(items)}")
    if args.dry_run:
        print("--- DRY RUN ---\n")

    filled = 0
    skipped = 0

    for item in items:
        contains, unit = parse_pack_info(
            item['vendor_description'],
            item['pack_size'],
            item['category']
        )

        if contains and contains > 0:
            price_per = item['purchase_price'] / contains
            if args.dry_run:
                print(f"  [{item['category']:6s}] {item['vendor_description'][:50]:50s} "
                      f"pack_size={item['pack_size'] or '':15s} "
                      f"=> {contains} {unit} (${price_per:.4f}/{unit})")
            else:
                conn.execute("""
                    UPDATE vendor_items
                    SET pack_contains = ?, contains_unit = ?, price_per_unit = ?
                    WHERE id = ?
                """, (contains, unit, price_per, item['id']))
            filled += 1
        else:
            skipped += 1

    if not args.dry_run:
        conn.commit()

    print(f"\n{'Would fill' if args.dry_run else 'Filled'}: {filled}")
    print(f"Could not parse: {skipped}")

    if args.dry_run:
        print("\nDRY RUN — nothing written")

    conn.close()


if __name__ == "__main__":
    main()
