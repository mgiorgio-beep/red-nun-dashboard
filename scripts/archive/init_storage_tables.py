"""
Initialize Storage Location Tables
Creates tables for managing storage locations and product-location mappings.
"""

import sqlite3
from data_store import get_connection

def init_storage_tables():
    """Create storage location tables."""
    conn = get_connection()

    # Create storage_locations table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS storage_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create product_storage_locations junction table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_storage_locations (
            product_id INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (product_id, location_id),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (location_id) REFERENCES storage_locations(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()
    print("✅ Storage location tables created successfully!")

if __name__ == '__main__':
    init_storage_tables()
