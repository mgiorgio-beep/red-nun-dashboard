"""
Migrate Inventory Tables
Creates products, vendors, inventory, and recipe tables.
Seeds from existing me_products and me_vendors data.
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


def create_tables():
    """Create all inventory-related tables."""
    conn = get_connection()
    conn.executescript("""
        -- ============================================
        -- VENDORS
        -- ============================================
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            contact_name TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            payment_terms TEXT DEFAULT 'Net 30',
            account_number TEXT,
            notes TEXT,
            active INTEGER DEFAULT 1,
            me_vendor_id TEXT,
            me_central_vendor_id TEXT,
            location TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- ============================================
        -- PRODUCTS
        -- ============================================
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            subcategory TEXT,
            unit TEXT,
            pack_size INTEGER,
            pack_unit TEXT,
            inventory_unit TEXT,
            unit_conversion REAL DEFAULT 1,
            preferred_vendor_id INTEGER,
            current_price REAL DEFAULT 0,
            par_level REAL,
            reorder_point REAL,
            storage_location TEXT,
            notes TEXT,
            active INTEGER DEFAULT 1,
            me_product_id TEXT,
            me_category_id TEXT,
            location TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (preferred_vendor_id) REFERENCES vendors(id)
        );

        -- Product-vendor pricing (multiple vendors per product)
        CREATE TABLE IF NOT EXISTS product_vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            vendor_id INTEGER NOT NULL,
            unit_price REAL,
            unit TEXT,
            last_invoice_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id),
            FOREIGN KEY (vendor_id) REFERENCES vendors(id),
            UNIQUE(product_id, vendor_id)
        );

        -- ============================================
        -- INVENTORY
        -- ============================================
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            quantity REAL DEFAULT 0,
            unit TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id),
            UNIQUE(product_id, location)
        );

        CREATE TABLE IF NOT EXISTS inventory_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            movement_type TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT,
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        -- Inventory count sessions (for tracking full counts)
        CREATE TABLE IF NOT EXISTS inventory_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            count_date TEXT NOT NULL,
            status TEXT DEFAULT 'in_progress',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS inventory_count_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            count_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            expected_quantity REAL,
            counted_quantity REAL,
            variance REAL,
            unit TEXT,
            notes TEXT,
            FOREIGN KEY (count_id) REFERENCES inventory_counts(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        -- ============================================
        -- RECIPES
        -- ============================================
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT,
            serving_size REAL DEFAULT 1,
            serving_unit TEXT DEFAULT 'portion',
            menu_price REAL,
            prep_time_minutes INTEGER,
            notes TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            unit TEXT,
            notes TEXT,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS recipe_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER UNIQUE NOT NULL,
            total_food_cost REAL,
            cost_per_serving REAL,
            food_cost_percentage REAL,
            calculated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
        );

        -- ============================================
        -- STORAGE LOCATIONS
        -- ============================================
        CREATE TABLE IF NOT EXISTS storage_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            location TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()
    print("✅ All tables created")


def seed_vendors():
    """Seed vendors from me_vendors."""
    conn = get_connection()

    # Check if already seeded
    existing = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    if existing > 0:
        print(f"⏭️  Vendors already seeded ({existing} exist), skipping")
        conn.close()
        return

    # Get unique vendors (deduplicate across locations)
    me_vendors = conn.execute("""
        SELECT vendor_id, vendor_name, central_vendor_id, account_numbers, location
        FROM me_vendors
        ORDER BY vendor_name
    """).fetchall()

    # Deduplicate by name (keep first occurrence)
    seen = {}
    for v in me_vendors:
        name = v['vendor_name']
        if name not in seen:
            seen[name] = v

    inserted = 0
    for name, v in sorted(seen.items()):
        # Guess category from products
        cat_row = conn.execute("""
            SELECT category_type, COUNT(*) as cnt
            FROM me_products
            WHERE product_id IN (
                SELECT DISTINCT product_id FROM me_invoice_items
                WHERE vendor_id = ? OR vendor_id IN (
                    SELECT vendor_id FROM me_vendors WHERE vendor_name = ?
                )
            )
            GROUP BY category_type
            ORDER BY cnt DESC
            LIMIT 1
        """, (v['vendor_id'], name)).fetchone()

        category = cat_row['category_type'] if cat_row else None

        conn.execute("""
            INSERT INTO vendors (name, category, account_number, me_vendor_id,
                               me_central_vendor_id, location, active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (
            name,
            category,
            v['account_numbers'] or None,
            v['vendor_id'],
            v['central_vendor_id'] or None,
            v['location']
        ))
        inserted += 1

    conn.commit()
    conn.close()
    print(f"✅ Seeded {inserted} vendors")


def seed_products():
    """Seed products from me_products."""
    conn = get_connection()

    existing = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if existing > 0:
        print(f"⏭️  Products already seeded ({existing} exist), skipping")
        conn.close()
        return

    # Build vendor name -> id lookup
    vendor_lookup = {}
    for v in conn.execute("SELECT id, name FROM vendors").fetchall():
        vendor_lookup[v['name'].lower()] = v['id']

    # Get unique products (deduplicate across locations)
    me_products = conn.execute("""
        SELECT product_id, product_name, category_id, category_name,
               category_type, report_by_unit, latest_price, location
        FROM me_products
        ORDER BY product_name
    """).fetchall()

    seen = {}
    for p in me_products:
        name = p['product_name']
        if name not in seen:
            seen[name] = p

    inserted = 0
    for name, p in sorted(seen.items()):
        # Parse unit info from report_by_unit
        unit = p['report_by_unit'] or 'each'
        pack_size = None
        inventory_unit = None
        unit_conversion = 1.0

        cat = (p['category_type'] or '').upper()
        unit_lower = unit.lower()

        # Set inventory units based on category
        if cat == 'BEER':
            if 'keg' in unit_lower:
                inventory_unit = 'keg'
            else:
                inventory_unit = 'bottle'
                pack_size = 24
                unit_conversion = 24
        elif cat == 'LIQUOR':
            inventory_unit = 'bottle'
            if 'case' in unit_lower:
                pack_size = 12
                unit_conversion = 12
        elif cat == 'WINE':
            inventory_unit = 'bottle'
            if 'case' in unit_lower:
                pack_size = 12
                unit_conversion = 12
        elif cat == 'NA_BEVERAGES':
            inventory_unit = 'can'
            if 'case' in unit_lower:
                pack_size = 24
                unit_conversion = 24
        elif cat == 'FOOD':
            if 'lb' in unit_lower:
                inventory_unit = 'lb'
            elif 'oz' in unit_lower:
                inventory_unit = 'oz'
            else:
                inventory_unit = 'each'
        else:
            inventory_unit = 'each'

        conn.execute("""
            INSERT INTO products (name, category, subcategory, unit, pack_size,
                                inventory_unit, unit_conversion, current_price,
                                me_product_id, me_category_id, location, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            name,
            cat or 'OTHER',
            p['category_name'],
            unit,
            pack_size,
            inventory_unit,
            unit_conversion,
            p['latest_price'] or 0,
            p['product_id'],
            p['category_id'],
            p['location']
        ))
        inserted += 1

    conn.commit()
    conn.close()
    print(f"✅ Seeded {inserted} products")


def seed_storage_locations():
    """Create default storage locations for both restaurants."""
    conn = get_connection()

    existing = conn.execute("SELECT COUNT(*) FROM storage_locations").fetchone()[0]
    if existing > 0:
        print(f"⏭️  Storage locations already seeded ({existing} exist), skipping")
        conn.close()
        return

    locations = [
        ('Walk-in Cooler', 'dennis'),
        ('Walk-in Cooler', 'chatham'),
        ('Dry Storage', 'dennis'),
        ('Dry Storage', 'chatham'),
        ('Bar - Dennis', 'dennis'),
        ('Bar - Chatham', 'chatham'),
        ('Freezer', 'dennis'),
        ('Freezer', 'chatham'),
        ('Front Line', 'dennis'),
        ('Front Line', 'chatham'),
    ]

    for name, loc in locations:
        conn.execute(
            "INSERT INTO storage_locations (name, location) VALUES (?, ?)",
            (name, loc)
        )

    conn.commit()
    conn.close()
    print(f"✅ Seeded {len(locations)} storage locations")


def print_summary():
    """Print migration summary."""
    conn = get_connection()

    print("\n" + "=" * 60)
    print("  INVENTORY DATABASE SUMMARY")
    print("=" * 60)

    tables = [
        'vendors', 'products', 'product_vendors', 'inventory',
        'inventory_movements', 'inventory_counts', 'inventory_count_items',
        'recipes', 'recipe_ingredients', 'recipe_costs', 'storage_locations'
    ]

    for t in tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:30} {count:>6} rows")
        except:
            print(f"  {t:30}  ERROR")

    # Products by category
    print("\n  Products by Category:")
    cats = conn.execute("""
        SELECT category, COUNT(*) as cnt
        FROM products WHERE active = 1
        GROUP BY category ORDER BY cnt DESC
    """).fetchall()
    for c in cats:
        print(f"    {c['category']:20} {c['cnt']:>4}")

    print("=" * 60)
    conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("\n" + "=" * 60)
    print("  Red Nun Inventory Migration")
    print("=" * 60)

    print("\n⏳ Step 1: Creating tables...")
    create_tables()

    print("\n⏳ Step 2: Seeding vendors...")
    seed_vendors()

    print("\n⏳ Step 3: Seeding products...")
    seed_products()

    print("\n⏳ Step 4: Seeding storage locations...")
    seed_storage_locations()

    print_summary()

    print("\n🎉 Migration complete!")
    print("   Restart the server: systemctl restart rednun")
    print("   Then check: https://dashboard.rednun.com/manage")


if __name__ == "__main__":
    main()
