-- Red Nun Analytics v2 Schema
-- Comprehensive inventory, vendor, and recipe management system

-- ============================================
-- VENDORS
-- ============================================
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT, -- FOOD, BEVERAGE, SUPPLIES, etc.
    contact_name TEXT,
    email TEXT,
    phone TEXT,
    address TEXT,
    payment_terms TEXT, -- NET30, NET60, COD, etc.
    account_number TEXT,
    notes TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vendors_name ON vendors(name);
CREATE INDEX IF NOT EXISTS idx_vendors_category ON vendors(category);

-- ============================================
-- PRODUCTS (Master Catalog)
-- ============================================
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT, -- FOOD, BEER, LIQUOR, WINE, NA_BEVERAGES, SUPPLIES
    subcategory TEXT, -- Meat, Produce, Spirits, Beer, etc.
    unit TEXT, -- ea, lb, oz, case, bottle, etc.
    pack_size REAL, -- e.g., 12 for a 12-pack
    pack_unit TEXT, -- ea, oz, lb
    preferred_vendor_id INTEGER,
    current_price REAL,
    last_price_update TEXT,
    par_level REAL, -- minimum stock level
    reorder_point REAL,
    storage_location TEXT,
    notes TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (preferred_vendor_id) REFERENCES vendors(id)
);

CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_vendor ON products(preferred_vendor_id);

-- ============================================
-- PRODUCT VENDORS (Price tracking per vendor)
-- ============================================
CREATE TABLE IF NOT EXISTS product_vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    vendor_id INTEGER NOT NULL,
    vendor_product_code TEXT, -- vendor's SKU/code for this product
    unit_price REAL NOT NULL,
    pack_size REAL,
    unit TEXT,
    last_purchased TEXT,
    is_preferred INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id),
    FOREIGN KEY (vendor_id) REFERENCES vendors(id),
    UNIQUE(product_id, vendor_id)
);

CREATE INDEX IF NOT EXISTS idx_product_vendors_product ON product_vendors(product_id);
CREATE INDEX IF NOT EXISTS idx_product_vendors_vendor ON product_vendors(vendor_id);

-- ============================================
-- INVENTORY
-- ============================================
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    location TEXT NOT NULL, -- dennis, chatham
    quantity REAL DEFAULT 0,
    unit TEXT NOT NULL,
    last_counted_at TEXT,
    last_counted_by TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id),
    UNIQUE(product_id, location)
);

CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory(product_id);
CREATE INDEX IF NOT EXISTS idx_inventory_location ON inventory(location);

-- ============================================
-- INVENTORY MOVEMENTS
-- ============================================
CREATE TABLE IF NOT EXISTS inventory_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    movement_type TEXT NOT NULL, -- PURCHASE, TRANSFER, WASTE, ADJUSTMENT, COUNT
    quantity REAL NOT NULL, -- positive for additions, negative for removals
    unit TEXT NOT NULL,
    reference_type TEXT, -- invoice, transfer, waste_log, count
    reference_id INTEGER, -- ID of the invoice, transfer, etc.
    cost_per_unit REAL,
    notes TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_movements_product ON inventory_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_movements_date ON inventory_movements(created_at);
CREATE INDEX IF NOT EXISTS idx_movements_type ON inventory_movements(movement_type);

-- ============================================
-- RECIPES
-- ============================================
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT, -- APPETIZER, ENTREE, DESSERT, DRINK, etc.
    serving_size REAL DEFAULT 1,
    serving_unit TEXT DEFAULT 'portion',
    menu_price REAL, -- selling price
    prep_time_minutes INTEGER,
    notes TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recipes_name ON recipes(name);
CREATE INDEX IF NOT EXISTS idx_recipes_category ON recipes(category);

-- ============================================
-- RECIPE INGREDIENTS
-- ============================================
CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    notes TEXT, -- e.g., "diced", "chopped", "optional"
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id);
CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_product ON recipe_ingredients(product_id);

-- ============================================
-- RECIPE COSTING (Calculated food cost)
-- ============================================
CREATE TABLE IF NOT EXISTS recipe_costs (
    recipe_id INTEGER PRIMARY KEY,
    total_food_cost REAL NOT NULL,
    cost_per_serving REAL NOT NULL,
    food_cost_percentage REAL, -- (cost / menu_price) * 100
    calculated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
);

-- ============================================
-- PURCHASE ORDERS (Optional, for future)
-- ============================================
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_number TEXT UNIQUE,
    vendor_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    status TEXT DEFAULT 'DRAFT', -- DRAFT, SENT, RECEIVED, CANCELLED
    order_date TEXT,
    expected_date TEXT,
    received_date TEXT,
    subtotal REAL DEFAULT 0,
    tax REAL DEFAULT 0,
    total REAL DEFAULT 0,
    notes TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (vendor_id) REFERENCES vendors(id)
);

CREATE INDEX IF NOT EXISTS idx_po_vendor ON purchase_orders(vendor_id);
CREATE INDEX IF NOT EXISTS idx_po_status ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS idx_po_date ON purchase_orders(order_date);

-- ============================================
-- PURCHASE ORDER ITEMS
-- ============================================
CREATE TABLE IF NOT EXISTS purchase_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    unit_price REAL NOT NULL,
    total_price REAL NOT NULL,
    received_quantity REAL DEFAULT 0,
    notes TEXT,
    FOREIGN KEY (po_id) REFERENCES purchase_orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_po_items_po ON purchase_order_items(po_id);
CREATE INDEX IF NOT EXISTS idx_po_items_product ON purchase_order_items(product_id);

-- ============================================
-- WASTE LOG
-- ============================================
CREATE TABLE IF NOT EXISTS waste_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    reason TEXT, -- SPOILAGE, PREP, BREAKAGE, OTHER
    cost REAL,
    logged_by TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_waste_product ON waste_log(product_id);
CREATE INDEX IF NOT EXISTS idx_waste_date ON waste_log(created_at);
CREATE INDEX IF NOT EXISTS idx_waste_location ON waste_log(location);

-- ============================================
-- BANK REGISTER (QBO-style per-account register)
-- Tables created/migrated by routes/register_routes.py :: init_register_tables()
-- ============================================

-- Operating bank accounts (Chatham CCF, Dennis CCF, add more as needed)
CREATE TABLE IF NOT EXISTS bank_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                -- e.g. "Cape Cod Five (5975) — Chatham"
    short_name TEXT,                   -- e.g. "Chatham Operating"
    qbo_account_id TEXT,               -- QBO Account.Id for deposit sync
    qbo_account_name TEXT,             -- QBO display name
    location TEXT,                     -- chatham | dennis | null
    account_last4 TEXT,
    opening_balance REAL DEFAULT 0,
    opening_date TEXT,                 -- YYYY-MM-DD
    active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_bank_accounts_loc ON bank_accounts(location);

-- Manual register entries (transfers, fees, interest, adjustments)
CREATE TABLE IF NOT EXISTS manual_bank_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_account_id INTEGER NOT NULL,
    entry_date TEXT NOT NULL,          -- YYYY-MM-DD
    entry_type TEXT NOT NULL,          -- transfer | fee | interest | adjustment | other
    payee TEXT,
    memo TEXT,
    ref_number TEXT,
    amount REAL NOT NULL,              -- signed: positive = deposit, negative = payment
    cleared INTEGER DEFAULT 0,
    cleared_date TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_mbe_account ON manual_bank_entries(bank_account_id);
CREATE INDEX IF NOT EXISTS idx_mbe_date ON manual_bank_entries(entry_date);

-- Local cache of deposits pulled from QBO (Toast CC settlement + cash)
CREATE TABLE IF NOT EXISTS bank_deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_account_id INTEGER NOT NULL,
    deposit_date TEXT NOT NULL,        -- YYYY-MM-DD
    amount REAL NOT NULL,              -- always positive
    description TEXT,
    memo TEXT,
    source TEXT,                       -- 'toast' | 'cash' | 'qbo_other'
    qbo_txn_id TEXT UNIQUE,            -- dedup key
    qbo_txn_type TEXT,
    cleared INTEGER DEFAULT 1,
    cleared_date TEXT,
    synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_bd_account ON bank_deposits(bank_account_id);
CREATE INDEX IF NOT EXISTS idx_bd_date ON bank_deposits(deposit_date);
CREATE INDEX IF NOT EXISTS idx_bd_qbo ON bank_deposits(qbo_txn_id);

-- Additive columns on existing tables (see init_register_tables for backfill logic):
--   ALTER TABLE vendor_payments ADD COLUMN bank_account_id INTEGER
--   ALTER TABLE vendor_payments ADD COLUMN cleared INTEGER DEFAULT 0
--   ALTER TABLE vendor_payments ADD COLUMN cleared_date TEXT
--   ALTER TABLE payroll_checks  ADD COLUMN bank_account_id INTEGER
--   ALTER TABLE payroll_checks  ADD COLUMN cleared INTEGER DEFAULT 0
--   ALTER TABLE payroll_checks  ADD COLUMN cleared_date TEXT
