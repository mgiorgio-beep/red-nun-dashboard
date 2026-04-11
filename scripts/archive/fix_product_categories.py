"""
Fix Product Categories
Re-categorize products based on vendor type and product name patterns.
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


# Vendor categorization rules
VENDOR_CATEGORIES = {
    # Liquor/Wine/Beer Distributors
    'L. Knife': 'BEVERAGE',
    'Knife': 'BEVERAGE',
    'Martignetti': 'BEVERAGE',
    "Southern Glazer": 'BEVERAGE',
    'Glazer': 'BEVERAGE',
    'Atlantic Beverage': 'BEVERAGE',
    'Horizon Beverage': 'BEVERAGE',
    'Colonial Wholesale': 'BEVERAGE',
    'Craft Collective': 'BEVERAGE',
    'Cape Cod Beer': 'BEVERAGE',

    # Food Distributors
    'US Foods': 'FOOD',
    'Reinhart': 'FOOD',
    'Performance Food': 'FOOD',
    'Sysco': 'FOOD',
    "Chef's Warehouse": 'FOOD',
    'Cape Fish': 'FOOD',

    # Supplies
    'Cintas': 'SUPPLIES',
    'UniFirst': 'SUPPLIES',
    'Cozzini': 'SUPPLIES',
}


# Product name patterns for beverage categorization
BEER_PATTERNS = [
    'beer', 'ale', 'ipa', 'lager', 'stout', 'pilsner', 'porter',
    'bud light', 'budweiser', 'coors', 'miller', 'corona', 'heineken', 'hein ',
    'sam adams', 'guinness', 'stella', 'modelo', 'pacifico',
    'keg', 'draft', 'draught', 'k-', ' hb', ' sb', '1/2bbl', '1/6bbl', '1/2 bbl',
    'michelob', 'busch', 'pabst', 'pbr', 'yuengling', 'blue moon',
    'cape cod beer', 'harpoon', 'dogfish', 'stone', 'sierra nevada',
    'hog island', 'maine beer', 'fiddlehead', 'downeast', 'artifact',
    'lite ', 'devil purse', 'two roads', 'wormtown', 'high noon', 'surfside',
    'truly', 'white claw', 'bud lt', 'coors lt', 'miller lt'
]

WINE_PATTERNS = [
    'wine', 'chardonnay', 'chardonny', 'chard ', 'cabernet', 'cab ', 'merlot',
    'pinot', 'pin noir', 'sauvignon', 'sb sp', 'sauv blanc',
    'malbec', 'zinfandel', 'riesling', 'moscato', 'prosecco', 'champagne',
    'red wine', 'white wine', 'rose', 'sparkling', 'kim crawford',
    'josh cellars', 'meiomi', 'simi ', 'glen ellen', 'torada'
]

LIQUOR_PATTERNS = [
    'vodka', 'vod ', 'whiskey', 'whisky', 'bourbon', 'scotch', 'rum', 'gin', 'tequila',
    'cognac', 'brandy', 'liqueur', 'schnapps', 'schnap ', 'amaretto', 'kahlua',
    'baileys', 'bailey', 'carolans', 'fireball', 'jager', 'patron', 'hennessy',
    'jack dan', 'jameson', 'captain morgan', 'capt morgan', 'bacardi', 'smirnoff',
    'grey goose', 'tanqueray', 'jose cuervo', 'crown royal', 'absolut', 'stoli',
    'dewars', 'casamigos', 'espolon', 'skrewball', 'three olives', 'tullamore',
    'triple sec', 'irish cream', 'barr hill', 'beefeater', 'bulleit', 'brady',
    'bombay'
]

NA_BEVERAGE_PATTERNS = [
    'coke', 'pepsi', 'sprite', 'fanta', 'dr pepper', 'root beer',
    'ginger ale', 'tonic', 'club soda', 'soda', 'juice', 'lemonade',
    'iced tea', 'athletic', 'non-alcoholic', 'n/a beer', 'na beer',
    'real blackberry', 'real cream', 'mixer', 'mix '
]


def categorize_by_name(product_name):
    """Determine category based on product name patterns."""
    name_lower = product_name.lower()

    # Check for NA beverages first (Athletic beer is non-alcoholic)
    if 'athletic' in name_lower or 'best day brewing' in name_lower:
        return 'NA_BEVERAGES'

    # Check patterns in order of specificity
    for pattern in LIQUOR_PATTERNS:
        if pattern in name_lower:
            return 'LIQUOR'

    for pattern in WINE_PATTERNS:
        if pattern in name_lower:
            return 'WINE'

    for pattern in BEER_PATTERNS:
        if pattern in name_lower:
            return 'BEER'

    for pattern in NA_BEVERAGE_PATTERNS:
        if pattern in name_lower:
            return 'NA_BEVERAGES'

    return None


def fix_categories():
    """Fix product categories based on vendors and product names."""
    logger.info("Analyzing and fixing product categories...")
    conn = get_connection()
    cursor = conn.cursor()

    # Get all products with their vendors
    products = conn.execute("""
        SELECT p.id, p.name, p.category, v.name as vendor_name
        FROM products p
        LEFT JOIN vendors v ON p.preferred_vendor_id = v.id
        WHERE p.active = 1
        ORDER BY v.name, p.name
    """).fetchall()

    logger.info(f"  Found {len(products)} products to check")

    changes = {
        'BEER': 0,
        'WINE': 0,
        'LIQUOR': 0,
        'NA_BEVERAGES': 0,
        'FOOD': 0,
        'SUPPLIES': 0
    }

    for product in products:
        new_category = None
        reason = None

        # Step 1: Check if vendor is a beverage distributor
        if product['vendor_name']:
            for vendor_key, vendor_type in VENDOR_CATEGORIES.items():
                if vendor_key.lower() in product['vendor_name'].lower():
                    if vendor_type == 'BEVERAGE':
                        # Beverage vendor - categorize by product name
                        new_category = categorize_by_name(product['name'])
                        if new_category:
                            reason = f"Beverage vendor + name pattern"
                    elif vendor_type == 'FOOD':
                        # Food vendor - keep as food unless it's clearly beverage
                        beverage_cat = categorize_by_name(product['name'])
                        if beverage_cat:
                            new_category = beverage_cat
                            reason = f"Food vendor but name matches beverage"
                        else:
                            new_category = 'FOOD'
                            reason = f"Food vendor"
                    elif vendor_type == 'SUPPLIES':
                        new_category = 'SUPPLIES'
                        reason = f"Supplies vendor"
                    break

        # Step 2: If no vendor match, categorize by name only
        if not new_category:
            name_category = categorize_by_name(product['name'])
            if name_category:
                new_category = name_category
                reason = "Name pattern match"

        # Update if category changed
        if new_category and new_category != product['category']:
            cursor.execute("""
                UPDATE products
                SET category = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (new_category, product['id']))

            changes[new_category] += 1
            logger.debug(f"  {product['name'][:50]:50} {product['category']:8} -> {new_category:8} ({reason})")

    conn.commit()
    conn.close()

    return changes


def print_summary():
    """Print summary of categories by vendor."""
    conn = get_connection()

    print("\n" + "=" * 100)
    print("  PRODUCT CATEGORIES BY VENDOR")
    print("=" * 100)

    # Get stats by vendor and category
    stats = conn.execute("""
        SELECT
            v.name as vendor_name,
            p.category,
            COUNT(*) as count
        FROM products p
        JOIN vendors v ON p.preferred_vendor_id = v.id
        WHERE p.active = 1
        GROUP BY v.name, p.category
        ORDER BY v.name, count DESC
    """).fetchall()

    current_vendor = None
    for row in stats:
        if row['vendor_name'] != current_vendor:
            print(f"\n{row['vendor_name']}")
            current_vendor = row['vendor_name']
        print(f"  {row['category']:15} {row['count']:3} products")

    # Overall totals
    print("\n" + "=" * 100)
    print("  OVERALL TOTALS")
    print("=" * 100)

    totals = conn.execute("""
        SELECT category, COUNT(*) as count
        FROM products
        WHERE active = 1
        GROUP BY category
        ORDER BY count DESC
    """).fetchall()

    for row in totals:
        print(f"  {row['category']:15} {row['count']:3} products")

    print("\n" + "=" * 100)
    conn.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 100)
    print("  Fix Product Categories")
    print("=" * 100)

    try:
        changes = fix_categories()

        print(f"\n✅ Categories updated!")
        print(f"\n📊 Changes made:")
        for category, count in sorted(changes.items(), key=lambda x: -x[1]):
            if count > 0:
                print(f"  {category:15} {count:3} products updated")

        print_summary()

        print("\n🎉 View updated categories at:")
        print("   http://159.65.180.102:8080/manage")

    except Exception as e:
        logger.error(f"Failed: {e}", exc_info=True)
        print(f"\n❌ Failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
