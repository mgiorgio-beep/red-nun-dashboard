"""
Connect Products to Vendors with Pricing
Analyzes invoice line items to link products with vendor pricing and set preferred vendors.

This script:
1. Matches invoice line items to products in catalog
2. Creates vendor pricing records in product_vendors table
3. Sets preferred vendor for each product (best price or most recent)
4. Updates current_price in products table
"""

import os
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv
from difflib import SequenceMatcher

load_dotenv()
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "toast_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def similarity(a, b):
    """Calculate similarity ratio between two strings."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def match_product(item_name, products, threshold=0.7):
    """
    Find best matching product from catalog for an invoice item.
    Returns (product_id, confidence) or (None, 0) if no good match.
    """
    best_match = None
    best_score = 0

    for prod in products:
        score = similarity(item_name, prod['name'])
        if score > best_score:
            best_score = score
            best_match = prod

    if best_score >= threshold:
        return (best_match['id'], best_score)
    return (None, 0)


def analyze_invoice_items():
    """Analyze invoice items and match to products."""
    logger.info("Analyzing invoice line items...")
    conn = get_connection()

    # Get all products
    products = conn.execute("SELECT id, name, category FROM products").fetchall()
    products_dict = {p['id']: p for p in products}

    # Get all vendors
    vendors = conn.execute("SELECT id, name FROM vendors").fetchall()
    vendor_map = {v['name']: v['id'] for v in vendors}

    # Get invoice items with vendor info
    items = conn.execute("""
        SELECT
            sii.product_name,
            sii.unit_price,
            sii.quantity,
            sii.category_type,
            si.vendor_name,
            si.invoice_date,
            si.location
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE sii.unit_price > 0
        ORDER BY si.invoice_date DESC
    """).fetchall()

    logger.info(f"  Found {len(items)} invoice line items to analyze")

    # Match items to products and collect vendor pricing
    matched = 0
    unmatched = []
    product_vendor_prices = {}  # {(product_id, vendor_id): [prices]}

    for item in items:
        vendor_id = vendor_map.get(item['vendor_name'])
        if not vendor_id:
            continue

        # Try to match item to product
        product_id, confidence = match_product(item['product_name'], products, threshold=0.7)

        if product_id:
            matched += 1
            key = (product_id, vendor_id)
            if key not in product_vendor_prices:
                product_vendor_prices[key] = {
                    'prices': [],
                    'quantities': [],
                    'dates': [],
                    'product_name': item['product_name']
                }
            product_vendor_prices[key]['prices'].append(item['unit_price'])
            product_vendor_prices[key]['quantities'].append(item['quantity'])
            product_vendor_prices[key]['dates'].append(item['invoice_date'])
        else:
            unmatched.append({
                'name': item['product_name'],
                'vendor': item['vendor_name'],
                'price': item['unit_price'],
                'category': item['category_type']
            })

    logger.info(f"  Matched {matched} items to products ({len(unmatched)} unmatched)")

    conn.close()
    return product_vendor_prices, unmatched, products_dict, vendor_map


def create_vendor_pricing(product_vendor_prices):
    """Create product_vendors records with pricing."""
    logger.info("Creating vendor pricing records...")
    conn = get_connection()
    cursor = conn.cursor()

    created = 0
    updated = 0

    for (product_id, vendor_id), data in product_vendor_prices.items():
        # Calculate average price (could also use most recent)
        avg_price = sum(data['prices']) / len(data['prices'])
        most_recent_date = max(data['dates'])

        # Check if record exists
        existing = conn.execute("""
            SELECT id, unit_price FROM product_vendors
            WHERE product_id = ? AND vendor_id = ?
        """, (product_id, vendor_id)).fetchone()

        if existing:
            # Update if price changed significantly (>5%)
            if abs(existing['unit_price'] - avg_price) / existing['unit_price'] > 0.05:
                cursor.execute("""
                    UPDATE product_vendors
                    SET unit_price = ?,
                        last_purchased = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (avg_price, most_recent_date, existing['id']))
                updated += 1
        else:
            # Create new record
            cursor.execute("""
                INSERT INTO product_vendors (
                    product_id, vendor_id, unit_price,
                    last_purchased, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (product_id, vendor_id, avg_price, most_recent_date))
            created += 1

    conn.commit()
    logger.info(f"  Created {created} new pricing records, updated {updated}")
    conn.close()
    return created, updated


def set_preferred_vendors(product_vendor_prices, products_dict):
    """Set preferred vendor for each product (lowest price or most frequent)."""
    logger.info("Setting preferred vendors...")
    conn = get_connection()
    cursor = conn.cursor()

    # Get vendor IDs
    vendors = conn.execute("SELECT id, name FROM vendors").fetchall()
    vendor_names = {v['id']: v['name'] for v in vendors}

    # Group by product
    product_vendors = {}
    for (product_id, vendor_id), data in product_vendor_prices.items():
        if product_id not in product_vendors:
            product_vendors[product_id] = []

        avg_price = sum(data['prices']) / len(data['prices'])
        purchase_count = len(data['prices'])
        most_recent = max(data['dates'])

        product_vendors[product_id].append({
            'vendor_id': vendor_id,
            'avg_price': avg_price,
            'purchase_count': purchase_count,
            'most_recent': most_recent
        })

    updated = 0
    for product_id, vendor_options in product_vendors.items():
        # Choose vendor with lowest average price
        best_vendor = min(vendor_options, key=lambda x: x['avg_price'])

        # Update product with preferred vendor and current price
        cursor.execute("""
            UPDATE products
            SET preferred_vendor_id = ?,
                current_price = ?,
                last_price_update = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            best_vendor['vendor_id'],
            best_vendor['avg_price'],
            best_vendor['most_recent'],
            product_id
        ))
        updated += 1

    conn.commit()
    logger.info(f"  Set preferred vendors for {updated} products")
    conn.close()
    return updated


def print_summary():
    """Print summary of product-vendor connections."""
    conn = get_connection()

    print("\n" + "=" * 80)
    print("  PRODUCT-VENDOR CONNECTION SUMMARY")
    print("=" * 80)

    # Products with vendors
    print("\n📊 PRODUCTS WITH VENDOR PRICING:")
    stats = conn.execute("""
        SELECT
            p.category,
            COUNT(DISTINCT p.id) as product_count,
            COUNT(DISTINCT pv.vendor_id) as vendor_count,
            ROUND(AVG(p.current_price), 2) as avg_price
        FROM products p
        LEFT JOIN product_vendors pv ON p.id = pv.product_id
        WHERE p.preferred_vendor_id IS NOT NULL
        GROUP BY p.category
        ORDER BY product_count DESC
    """).fetchall()

    for s in stats:
        print(f"  {s['category']:15} {s['product_count']:2} products, "
              f"{s['vendor_count']:2} vendors, avg ${s['avg_price']:.2f}")

    # Total products with pricing
    total_with_pricing = conn.execute("""
        SELECT COUNT(*) as cnt FROM products WHERE preferred_vendor_id IS NOT NULL
    """).fetchone()['cnt']

    total_products = conn.execute("SELECT COUNT(*) as cnt FROM products").fetchone()['cnt']

    print(f"\n  Total: {total_with_pricing}/{total_products} products have vendor pricing")

    # Products without vendors (need manual entry)
    print("\n📝 PRODUCTS WITHOUT VENDOR PRICING (need manual entry):")
    no_vendors = conn.execute("""
        SELECT name, category, unit
        FROM products
        WHERE preferred_vendor_id IS NULL
        ORDER BY category, name
        LIMIT 15
    """).fetchall()

    if no_vendors:
        for p in no_vendors:
            print(f"  {p['category']:15} {p['name'][:45]:45} ({p['unit']})")
        if len(no_vendors) == 15:
            remaining = conn.execute("""
                SELECT COUNT(*) as cnt FROM products WHERE preferred_vendor_id IS NULL
            """).fetchone()['cnt']
            print(f"  ... and {remaining - 15} more")
    else:
        print("  ✅ All products have vendor pricing!")

    # Sample vendor pricing comparison
    print("\n💰 SAMPLE VENDOR PRICE COMPARISONS:")
    comparisons = conn.execute("""
        SELECT
            p.name as product_name,
            p.category,
            v.name as vendor_name,
            pv.unit_price,
            CASE WHEN p.preferred_vendor_id = v.id THEN '✓' ELSE '' END as preferred
        FROM product_vendors pv
        JOIN products p ON pv.product_id = p.id
        JOIN vendors v ON pv.vendor_id = v.id
        WHERE p.id IN (
            SELECT product_id FROM product_vendors
            GROUP BY product_id HAVING COUNT(*) > 1
            LIMIT 5
        )
        ORDER BY p.name, pv.unit_price
    """).fetchall()

    current_product = None
    for c in comparisons:
        if c['product_name'] != current_product:
            print(f"\n  {c['product_name'][:50]}")
            current_product = c['product_name']
        pref = " ← PREFERRED" if c['preferred'] else ""
        print(f"    {c['vendor_name'][:35]:35} ${c['unit_price']:7.2f}{pref}")

    print("\n" + "=" * 80)

    conn.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 80)
    print("  Connecting Products to Vendors with Pricing")
    print("=" * 80)

    try:
        # Step 1: Analyze and match
        print("\n⏳ Step 1: Analyzing invoice items and matching to products...")
        product_vendor_prices, unmatched, products_dict, vendor_map = analyze_invoice_items()

        if not product_vendor_prices:
            print("\n❌ No matches found. This could mean:")
            print("   - Product names in invoices don't match catalog")
            print("   - No invoice line items imported")
            sys.exit(1)

        # Step 2: Create vendor pricing
        print("\n⏳ Step 2: Creating vendor pricing records...")
        created, updated = create_vendor_pricing(product_vendor_prices)

        # Step 3: Set preferred vendors
        print("\n⏳ Step 3: Setting preferred vendors (best prices)...")
        products_updated = set_preferred_vendors(product_vendor_prices, products_dict)

        print("\n✅ Connection complete!")

        # Show unmatched items
        if unmatched:
            print(f"\n⚠️  {len(unmatched)} invoice items couldn't be matched to products")
            print("    (These may need to be added to your product catalog)")
            print("\n    Top unmatched items:")
            seen = set()
            for item in unmatched[:10]:
                key = item['name']
                if key not in seen:
                    cat = item['category'] or 'UNCATEGORIZED'
                    print(f"      {cat:12} {item['name'][:50]:50} ${item['price']:.2f}")
                    seen.add(key)

        # Print summary
        print_summary()

        print("\n🎉 View updated pricing in Management Hub:")
        print("   http://159.65.180.102:8080/manage")

    except Exception as e:
        logger.error(f"Connection failed: {e}", exc_info=True)
        print(f"\n❌ Failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
