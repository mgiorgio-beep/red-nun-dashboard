"""
Products on several shelves, each shelf counted separately (Mike, 2026-10-05).

  1. product_storage_locations: UNIQUE(product_id, storage_location_id) -> one row per
     product per SHELF: unique index on (product_id, storage_location_id, IFNULL(section_id, -1)).
     Kitchen Well and Server Well are both shelves of the Bar, so the old rule made
     "+ Add product" on the Server Well move Tito's off the Kitchen Well.
  2. inventory_count_items gets storage_location_id + section_id: a count line is one
     product on one shelf; Complete adds the shelves up per product.

Idempotent. Back up first:
  sqlite3 /var/lib/rednun/toast_data.db ".backup /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db"
Run: venv/bin/python3 scripts/shelf_spots_2026_10_05.py [--apply]
"""
import sys
sys.path.insert(0, '/opt/red-nun-dashboard')
from integrations.toast.data_store import get_connection

APPLY = '--apply' in sys.argv
c = get_connection()

sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'product_storage_locations'").fetchone()[0]
old_rule = 'UNIQUE(product_id, storage_location_id)' in sql.replace(' ,', ',').replace(', ', ',').replace(',', ', ')
print('product_storage_locations still one-per-area:', old_rule)
cols = [r['name'] for r in c.execute("PRAGMA table_info(inventory_count_items)")]
need_cols = [x for x in ('storage_location_id', 'section_id') if x not in cols]
print('inventory_count_items missing:', need_cols)

if not APPLY:
    print('dry run; pass --apply')
    sys.exit()

n0 = c.execute("SELECT COUNT(*) FROM product_storage_locations").fetchone()[0]
c.execute("PRAGMA foreign_keys = OFF")
c.execute("BEGIN")
if old_rule:
    c.execute("""
        CREATE TABLE product_storage_locations_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            storage_location_id INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            section_id INTEGER REFERENCES storage_sections(id),
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (storage_location_id) REFERENCES storage_locations(id) ON DELETE CASCADE
        )""")
    c.execute("""INSERT INTO product_storage_locations_new (id, product_id, storage_location_id, sort_order, created_at, section_id)
                 SELECT id, product_id, storage_location_id, sort_order, created_at, section_id FROM product_storage_locations""")
    c.execute("DROP TABLE product_storage_locations")
    c.execute("ALTER TABLE product_storage_locations_new RENAME TO product_storage_locations")
c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_psl_product_shelf
             ON product_storage_locations (product_id, storage_location_id, IFNULL(section_id, -1))""")
c.execute("CREATE INDEX IF NOT EXISTS ix_psl_location ON product_storage_locations (storage_location_id)")
for col in need_cols:
    c.execute(f"ALTER TABLE inventory_count_items ADD COLUMN {col} INTEGER")
n1 = c.execute("SELECT COUNT(*) FROM product_storage_locations").fetchone()[0]
assert n0 == n1, (n0, n1)
c.execute("COMMIT")
c.execute("PRAGMA foreign_keys = ON")
print(f'applied: {n1} shelf rows kept; count lines now carry their shelf')
