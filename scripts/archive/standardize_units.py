"""
Standardize Product Units
Converts all existing units to standard values for consistency.
"""

import os
import sqlite3
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "toast_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# Standard unit mappings
PURCHASE_UNIT_MAP = {
    # Each variations
    'ea': 'each',
    'each': 'each',
    'Each': 'each',

    # Case variations
    'case': 'case',
    'Case': 'case',
    'cs': 'case',

    # Pound variations
    'lb': 'lb',
    'pound': 'lb',
    'Pound': 'lb',
    'lbs': 'lb',
    'pounds': 'lb',

    # Ounce variations
    'oz': 'oz',
    'ounce': 'oz',
    'Ounce': 'oz',
    'ounces': 'oz',

    # Gallon variations
    'gal': 'gal',
    'gallon': 'gal',
    'Gallon': 'gal',
    'gallons': 'gal',

    # Liter variations
    'liter': 'liter',
    'Liter': 'liter',
    'l': 'liter',
    'L': 'liter',

    # Bottle variations (treat as each for purchase)
    'bottle': 'each',
    'Bottle': 'each',
    'btl': 'each',

    # Bag/Box
    'bag': 'bag',
    'Bag': 'bag',
    'box': 'box',
    'Box': 'box',

    # Pallet
    'pallet': 'pallet',
    'Pallet': 'pallet',
}

INVENTORY_UNIT_MAP = {
    # Each
    'ea': 'each',
    'each': 'each',
    'Each': 'each',

    # Bottle
    'bottle': 'bottle',
    'Bottle': 'bottle',
    'btl': 'bottle',

    # Can
    'can': 'can',
    'Can': 'can',
    'cans': 'can',

    # Keg
    'keg': 'keg',
    'Keg': 'keg',
    'KEG': 'keg',

    # Pound
    'lb': 'lb',
    'pound': 'lb',
    'Pound': 'lb',
    'lbs': 'lb',

    # Ounce
    'oz': 'oz',
    'ounce': 'oz',
    'ounces': 'oz',

    # Gallon
    'gal': 'gal',
    'gallon': 'gal',
    'Gallon': 'gal',

    # Liter
    'liter': 'liter',
    'l': 'liter',
    'L': 'liter',

    # Serving/Portion
    'serving': 'serving',
    'portion': 'portion',
}


def standardize_purchase_units():
    """Standardize purchase unit field."""
    logger.info("Standardizing purchase units...")
    conn = get_connection()
    cursor = conn.cursor()

    products = conn.execute("""
        SELECT id, name, unit FROM products WHERE active = 1
    """).fetchall()

    updated = 0
    special_cases = 0

    for p in products:
        unit = p['unit'] or ''
        unit_lower = unit.lower()

        # Handle special keg formats
        if 'keg' in unit_lower or 'bbl' in unit_lower:
            if '1/2' in unit or '15.5' in unit or 'hb' in unit_lower:
                new_unit = 'keg (1/2 BBL)'
            elif '1/6' in unit or '5.16' in unit or 'sb' in unit_lower:
                new_unit = 'keg (1/6 BBL)'
            elif '1/4' in unit or '7.75' in unit:
                new_unit = 'keg (1/4 BBL)'
            else:
                new_unit = 'keg (1/2 BBL)'  # Default to half barrel
            special_cases += 1

        # Handle special bottle size formats (convert to liter for purchase)
        elif 'bottle' in unit_lower and ('liter' in unit_lower or 'milliliter' in unit_lower or 'ml' in unit_lower):
            if '750' in unit or '750ml' in unit_lower:
                new_unit = 'liter'  # 750ml = 0.75L, but we'll track as standard bottle
            elif '1.75' in unit or 'handle' in unit_lower:
                new_unit = 'liter'
            else:
                new_unit = 'liter'
            special_cases += 1

        # Handle count-specific formats (e.g., "200 Each")
        elif unit.strip().split()[0].isdigit():
            # Extract just the unit part, ignore the count
            parts = unit.split()
            if len(parts) > 1:
                base_unit = parts[1].lower()
                new_unit = PURCHASE_UNIT_MAP.get(base_unit, 'each')
            else:
                new_unit = 'each'
            special_cases += 1

        # Standard mapping
        else:
            new_unit = PURCHASE_UNIT_MAP.get(unit.lower(), PURCHASE_UNIT_MAP.get(unit, None))

        if new_unit and new_unit != unit:
            cursor.execute("""
                UPDATE products SET unit = ? WHERE id = ?
            """, (new_unit, p['id']))
            updated += 1

    conn.commit()
    conn.close()
    logger.info(f"  Updated {updated} purchase units ({special_cases} special cases)")
    return updated


def standardize_inventory_units():
    """Standardize inventory_unit field."""
    logger.info("Standardizing inventory units...")
    conn = get_connection()
    cursor = conn.cursor()

    products = conn.execute("""
        SELECT id, name, inventory_unit FROM products WHERE active = 1
    """).fetchall()

    updated = 0

    for p in products:
        inv_unit = p['inventory_unit'] or ''

        # Standard mapping
        new_unit = INVENTORY_UNIT_MAP.get(inv_unit.lower(), INVENTORY_UNIT_MAP.get(inv_unit, None))

        if new_unit and new_unit != inv_unit:
            cursor.execute("""
                UPDATE products SET inventory_unit = ? WHERE id = ?
            """, (new_unit, p['id']))
            updated += 1

    conn.commit()
    conn.close()
    logger.info(f"  Updated {updated} inventory units")
    return updated


def print_summary():
    """Print summary of standardized units."""
    conn = get_connection()

    print("\n" + "=" * 100)
    print("  STANDARDIZED UNITS SUMMARY")
    print("=" * 100)

    print("\n📦 PURCHASE UNITS:")
    purchase = conn.execute("""
        SELECT unit, COUNT(*) as count
        FROM products
        WHERE active = 1
        GROUP BY unit
        ORDER BY count DESC
    """).fetchall()

    for row in purchase:
        print(f"  {row['unit']:20} {row['count']:3} products")

    print("\n📊 INVENTORY UNITS:")
    inventory = conn.execute("""
        SELECT inventory_unit, COUNT(*) as count
        FROM products
        WHERE active = 1 AND inventory_unit IS NOT NULL
        GROUP BY inventory_unit
        ORDER BY count DESC
    """).fetchall()

    for row in inventory:
        print(f"  {row['inventory_unit']:20} {row['count']:3} products")

    print("\n" + "=" * 100)
    conn.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 100)
    print("  Standardize Product Units")
    print("=" * 100)

    try:
        print("\n⏳ Standardizing units...")
        purchase_updated = standardize_purchase_units()
        inventory_updated = standardize_inventory_units()

        print(f"\n✅ Standardization complete!")
        print(f"   • {purchase_updated} purchase units standardized")
        print(f"   • {inventory_updated} inventory units standardized")

        print_summary()

        print("\n💡 Standard units:")
        print("   Purchase: each, case, lb, oz, gal, liter, keg (1/2 BBL), keg (1/6 BBL), bag, box")
        print("   Inventory: each, bottle, can, keg, lb, oz, gal, liter, serving, portion")

        print("\n🎉 View standardized units:")
        print("   http://159.65.180.102:8080/manage")

    except Exception as e:
        logger.error(f"Failed: {e}", exc_info=True)
        print(f"\n❌ Failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
