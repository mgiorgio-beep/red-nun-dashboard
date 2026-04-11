"""
Import Products from Invoice Line Items
Adds products to catalog from actual invoice purchases that aren't in the catalog yet.

This creates a complete product catalog based on what you actually buy.
"""

import os
import sqlite3
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "toast_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def import_products_from_invoices():
    """Import unique products from invoice line items."""
    logger.info("Importing products from invoice line items...")
    conn = get_connection()
    cursor = conn.cursor()

    # Get vendor map
    vendors = conn.execute("SELECT id, name FROM vendors").fetchall()
    vendor_map = {v['name']: v['id'] for v in vendors}

    # Get unique products from invoice items
    unique_items = conn.execute("""
        SELECT DISTINCT
            sii.product_name,
            sii.category_type,
            AVG(sii.unit_price) as avg_price,
            COUNT(*) as purchase_count,
            MAX(si.invoice_date) as last_purchase,
            si.vendor_name,
            GROUP_CONCAT(DISTINCT si.vendor_name) as all_vendors
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE sii.product_name IS NOT NULL
            AND sii.unit_price > 0
        GROUP BY sii.product_name, sii.category_type
        ORDER BY purchase_count DESC
    """).fetchall()

    logger.info(f"  Found {len(unique_items)} unique products in invoices")

    # Get existing products to avoid duplicates
    existing_products = conn.execute("SELECT name FROM products").fetchall()
    existing_names = {p['name'].lower() for p in existing_products}

    created = 0
    skipped = 0

    # Category mapping
    category_map = {
        'LIQUOR': 'LIQUOR',
        'BEER': 'BEER',
        'WINE': 'WINE',
        'NA_BEVERAGES': 'NA_BEVERAGES',
        'FOOD': 'FOOD',
        None: 'FOOD'  # Default to FOOD for uncategorized items
    }

    for item in unique_items:
        # Check if product already exists (case-insensitive)
        if item['product_name'].lower() in existing_names:
            skipped += 1
            continue

        # Determine category
        category = category_map.get(item['category_type'], 'FOOD')

        # Get preferred vendor
        vendor_id = vendor_map.get(item['vendor_name'])

        # Determine unit from product name patterns
        unit = 'ea'  # default
        name_lower = item['product_name'].lower()
        if any(x in name_lower for x in ['lb', 'pound']):
            unit = 'lb'
        elif any(x in name_lower for x in ['oz', 'ounce']):
            unit = 'oz'
        elif any(x in name_lower for x in ['case', 'cs']):
            unit = 'case'
        elif any(x in name_lower for x in ['gal', 'gallon']):
            unit = 'gal'
        elif any(x in name_lower for x in ['bottle', 'btl']):
            unit = 'bottle'

        # Create product
        cursor.execute("""
            INSERT INTO products (
                name, category, unit, current_price,
                preferred_vendor_id, last_price_update,
                notes, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            item['product_name'],
            category,
            unit,
            item['avg_price'],
            vendor_id,
            item['last_purchase'],
            f"Auto-imported from invoices. Purchased {item['purchase_count']} times. Vendors: {item['all_vendors']}"
        ))

        product_id = cursor.lastrowid
        created += 1

        # Create vendor pricing record
        if vendor_id:
            cursor.execute("""
                INSERT INTO product_vendors (
                    product_id, vendor_id, unit_price,
                    last_purchased, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (product_id, vendor_id, item['avg_price'], item['last_purchase']))

    conn.commit()
    logger.info(f"  Created {created} new products, skipped {skipped} existing")
    conn.close()

    return created, skipped


def link_all_vendor_pricing():
    """Link all invoice items to their products with vendor pricing."""
    logger.info("Linking vendor pricing from all invoices...")
    conn = get_connection()
    cursor = conn.cursor()

    # Get product map
    products = conn.execute("SELECT id, name FROM products").fetchall()
    product_map = {p['name'].lower(): p['id'] for p in products}

    # Get vendor map
    vendors = conn.execute("SELECT id, name FROM vendors").fetchall()
    vendor_map = {v['name']: v['id'] for v in vendors}

    # Get all invoice items grouped by product+vendor
    pricing_data = conn.execute("""
        SELECT
            sii.product_name,
            si.vendor_name,
            AVG(sii.unit_price) as avg_price,
            MAX(si.invoice_date) as last_purchase,
            COUNT(*) as purchase_count
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON sii.invoice_id = si.id
        WHERE sii.unit_price > 0
        GROUP BY sii.product_name, si.vendor_name
    """).fetchall()

    logger.info(f"  Processing {len(pricing_data)} product-vendor combinations...")

    created = 0
    updated = 0

    for price in pricing_data:
        product_id = product_map.get(price['product_name'].lower())
        vendor_id = vendor_map.get(price['vendor_name'])

        if not product_id or not vendor_id:
            continue

        # Check if pricing record exists
        existing = conn.execute("""
            SELECT id, unit_price FROM product_vendors
            WHERE product_id = ? AND vendor_id = ?
        """, (product_id, vendor_id)).fetchone()

        if existing:
            # Update if price changed
            if abs(existing['unit_price'] - price['avg_price']) > 0.01:
                cursor.execute("""
                    UPDATE product_vendors
                    SET unit_price = ?,
                        last_purchased = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (price['avg_price'], price['last_purchase'], existing['id']))
                updated += 1
        else:
            # Create new pricing record
            cursor.execute("""
                INSERT INTO product_vendors (
                    product_id, vendor_id, unit_price,
                    last_purchased, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (product_id, vendor_id, price['avg_price'], price['last_purchase']))
            created += 1

    conn.commit()
    logger.info(f"  Created {created} pricing records, updated {updated}")
    conn.close()

    return created, updated


def print_summary():
    """Print summary of products and pricing."""
    conn = get_connection()

    print("\n" + "=" * 80)
    print("  PRODUCT CATALOG SUMMARY")
    print("=" * 80)

    # Total products by category
    print("\n📦 PRODUCTS BY CATEGORY:")
    by_cat = conn.execute("""
        SELECT
            category,
            COUNT(*) as count,
            ROUND(AVG(current_price), 2) as avg_price,
            COUNT(CASE WHEN preferred_vendor_id IS NOT NULL THEN 1 END) as with_vendor
        FROM products
        WHERE active = 1
        GROUP BY category
        ORDER BY count DESC
    """).fetchall()

    total_products = 0
    total_with_vendor = 0
    for cat in by_cat:
        total_products += cat['count']
        total_with_vendor += cat['with_vendor']
        print(f"  {cat['category']:15} {cat['count']:3} products "
              f"({cat['with_vendor']:3} with vendors, avg ${cat['avg_price']:7.2f})")

    print(f"\n  TOTAL: {total_products} products, {total_with_vendor} have vendor pricing")

    # Vendor pricing coverage
    print("\n💰 VENDOR PRICING:")
    pricing_stats = conn.execute("""
        SELECT
            COUNT(DISTINCT pv.product_id) as products_with_pricing,
            COUNT(DISTINCT pv.vendor_id) as vendors_with_products,
            COUNT(*) as total_pricing_records
        FROM product_vendors pv
    """).fetchone()

    print(f"  {pricing_stats['products_with_pricing']} products have pricing from "
          f"{pricing_stats['vendors_with_products']} vendors")
    print(f"  {pricing_stats['total_pricing_records']} total product-vendor pricing records")

    # Top products by purchase frequency
    print("\n🔥 TOP 10 MOST FREQUENTLY PURCHASED:")
    top = conn.execute("""
        SELECT
            p.name,
            p.category,
            v.name as vendor,
            p.current_price,
            COUNT(sii.id) as purchase_count
        FROM products p
        LEFT JOIN vendors v ON p.preferred_vendor_id = v.id
        JOIN scanned_invoice_items sii ON LOWER(sii.product_name) = LOWER(p.name)
        GROUP BY p.id
        ORDER BY purchase_count DESC
        LIMIT 10
    """).fetchall()

    for t in top:
        vendor_name = t['vendor'] or 'No vendor'
        print(f"  {t['name'][:45]:45} {t['category']:8} ${t['current_price']:7.2f}  "
              f"({t['purchase_count']}x from {vendor_name[:20]})")

    print("\n" + "=" * 80)
    conn.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("\n" + "=" * 80)
    print("  Import Products from Invoice Line Items")
    print("=" * 80)

    try:
        # Step 1: Import missing products
        print("\n⏳ Step 1: Importing products from invoices...")
        created, skipped = import_products_from_invoices()

        # Step 2: Link all vendor pricing
        print("\n⏳ Step 2: Linking vendor pricing...")
        pricing_created, pricing_updated = link_all_vendor_pricing()

        print(f"\n✅ Import complete!")
        print(f"   • {created} new products added to catalog")
        print(f"   • {pricing_created} vendor pricing records created")
        print(f"   • {pricing_updated} pricing records updated")

        # Print summary
        print_summary()

        print("\n🎉 View your complete product catalog:")
        print("   http://159.65.180.102:8080/manage")

    except Exception as e:
        logger.error(f"Import failed: {e}", exc_info=True)
        print(f"\n❌ Failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
