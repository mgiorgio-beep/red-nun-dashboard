"""
Add Inventory Unit System
Adds inventory_unit and unit_conversion fields to products table.

This allows:
- Purchase Unit: How you buy it (case, keg, etc.)
- Inventory Unit: How you count it (bottle, lb, etc.)
- Conversion: How many inventory units in one purchase unit
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


def add_columns():
    """Add inventory_unit and unit_conversion columns."""
    logger.info("Adding inventory unit columns...")
    conn = get_connection()
    cursor = conn.cursor()

    # Add columns if they don't exist
    try:
        cursor.execute("""
            ALTER TABLE products ADD COLUMN inventory_unit TEXT
        """)
        logger.info("  Added inventory_unit column")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            logger.info("  inventory_unit column already exists")
        else:
            raise

    try:
        cursor.execute("""
            ALTER TABLE products ADD COLUMN unit_conversion REAL DEFAULT 1
        """)
        logger.info("  Added unit_conversion column")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            logger.info("  unit_conversion column already exists")
        else:
            raise

    conn.commit()
    conn.close()


def set_default_inventory_units():
    """Set sensible default inventory units based on category and current unit."""
    logger.info("Setting default inventory units...")
    conn = get_connection()
    cursor = conn.cursor()

    products = conn.execute("""
        SELECT id, name, category, unit, pack_size
        FROM products
        WHERE active = 1
    """).fetchall()

    updated = 0

    for p in products:
        inv_unit = None
        conversion = 1.0
        unit_lower = (p['unit'] or '').lower()

        # Determine inventory unit based on category and current unit
        if p['category'] == 'BEER':
            if 'keg' in unit_lower or 'bbl' in unit_lower or 'gal' in unit_lower:
                inv_unit = 'keg'
                conversion = 1
            elif 'case' in unit_lower or p['pack_size']:
                inv_unit = 'bottle'
                conversion = p['pack_size'] or 24  # Default case = 24 bottles
            else:
                inv_unit = 'bottle'
                conversion = 1

        elif p['category'] == 'LIQUOR':
            if 'case' in unit_lower:
                inv_unit = 'bottle'
                conversion = p['pack_size'] or 12  # Default case = 12 bottles
            elif 'liter' in unit_lower or 'bottle' in unit_lower:
                inv_unit = 'bottle'
                conversion = 1
            else:
                inv_unit = 'bottle'
                conversion = 1

        elif p['category'] == 'WINE':
            if 'case' in unit_lower:
                inv_unit = 'bottle'
                conversion = p['pack_size'] or 12
            else:
                inv_unit = 'bottle'
                conversion = 1

        elif p['category'] == 'NA_BEVERAGES':
            if 'case' in unit_lower or p['pack_size']:
                inv_unit = 'can'
                conversion = p['pack_size'] or 24
            elif 'gal' in unit_lower:
                inv_unit = 'gallon'
                conversion = 1
            else:
                inv_unit = 'can'
                conversion = 1

        elif p['category'] == 'FOOD':
            if 'lb' in unit_lower or 'pound' in unit_lower:
                inv_unit = 'lb'
                conversion = 1
            elif 'oz' in unit_lower:
                inv_unit = 'oz'
                conversion = 1
            elif 'case' in unit_lower:
                inv_unit = 'each'
                conversion = p['pack_size'] or 1
            else:
                inv_unit = 'each'
                conversion = 1

        elif p['category'] == 'SUPPLIES':
            inv_unit = 'each'
            conversion = 1

        if inv_unit:
            cursor.execute("""
                UPDATE products
                SET inventory_unit = ?,
                    unit_conversion = ?
                WHERE id = ?
            """, (inv_unit, conversion, p['id']))
            updated += 1

    conn.commit()
    logger.info(f"  Set inventory units for {updated} products")
    conn.close()
    return updated


def print_summary():
    """Print summary of inventory units."""
    conn = get_connection()

    print("\n" + "=" * 100)
    print("  INVENTORY UNIT SUMMARY")
    print("=" * 100)

    # Summary by category
    summary = conn.execute("""
        SELECT
            category,
            inventory_unit,
            COUNT(*) as count,
            AVG(unit_conversion) as avg_conversion
        FROM products
        WHERE active = 1 AND inventory_unit IS NOT NULL
        GROUP BY category, inventory_unit
        ORDER BY category, count DESC
    """).fetchall()

    current_cat = None
    for row in summary:
        if row['category'] != current_cat:
            print(f"\n{row['category']}:")
            current_cat = row['category']
        print(f"  Count by {row['inventory_unit']:10} — {row['count']:3} products "
              f"(avg conversion: {row['avg_conversion']:.1f})")

    # Examples
    print("\n" + "=" * 100)
    print("  EXAMPLE CONVERSIONS")
    print("=" * 100)

    examples = conn.execute("""
        SELECT name, category, unit as purchase_unit, inventory_unit,
               unit_conversion, pack_size
        FROM products
        WHERE active = 1
          AND inventory_unit IS NOT NULL
          AND unit_conversion > 1
        ORDER BY category, unit_conversion DESC
        LIMIT 15
    """).fetchall()

    for ex in examples:
        conversion_text = f"1 {ex['purchase_unit']} = {ex['unit_conversion']:.0f} {ex['inventory_unit']}"
        print(f"  {ex['name'][:45]:45} {conversion_text}")

    print("\n" + "=" * 100)
    conn.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 100)
    print("  Add Inventory Unit System")
    print("=" * 100)

    try:
        # Step 1: Add columns
        print("\n⏳ Step 1: Adding columns to database...")
        add_columns()

        # Step 2: Set defaults
        print("\n⏳ Step 2: Setting default inventory units...")
        updated = set_default_inventory_units()

        print(f"\n✅ Inventory unit system added!")
        print(f"   • {updated} products configured with inventory units")

        # Print summary
        print_summary()

        print("\n💡 How it works:")
        print("   • Purchase Unit: How you BUY it (case, keg, etc.)")
        print("   • Inventory Unit: How you COUNT it (bottle, lb, etc.)")
        print("   • Conversion: 1 purchase unit = X inventory units")
        print("\n   Example: Buy 'case' → Count by 'bottle' → 1 case = 24 bottles")

        print("\n🎉 You can now edit products to adjust units:")
        print("   http://159.65.180.102:8080/manage")

    except Exception as e:
        logger.error(f"Failed: {e}", exc_info=True)
        print(f"\n❌ Failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
