"""
Product Helpers — Red Nun Analytics

Core functions for the product pipeline. Used by invoice confirm flow,
recipe costing, and Product Setup UI.

Architecture:
  products (canonical) → vendor_items (vendor-specific) → purchase_price
  recipe_ingredients → products → vendor_items → cost
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def get_or_create_product(name, category='FOOD', conn=None):
    """Get or create a canonical product in the products table.
    Returns products.id."""
    if not conn:
        from data_store import get_connection
        conn = get_connection()

    row = conn.execute(
        "SELECT id FROM products WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) LIMIT 1",
        (name,)
    ).fetchone()
    if row:
        return row["id"]

    conn.execute("""
        INSERT INTO products (name, category, active, setup_complete, created_at, updated_at)
        VALUES (?, ?, 1, 0, datetime('now'), datetime('now'))
    """, (name, category.upper()))
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    logger.info(f"Created canonical product: {name} (id={new_id})")
    return new_id


def find_vendor_item(vendor_name, vendor_product_name, vendor_item_code=None, conn=None):
    """Look up a vendor_items row.
    Tries vendor_item_code first (exact match), then vendor_name + description.
    Returns dict or None."""
    if not conn:
        from data_store import get_connection
        conn = get_connection()

    # Path 1: vendor_item_code match (most reliable)
    if vendor_item_code:
        row = conn.execute("""
            SELECT * FROM vendor_items
            WHERE vendor_item_code = ? AND (
                vendor_name = ? OR vendor_id IN (
                    SELECT id FROM vendors WHERE LOWER(name) = LOWER(?)
                )
            )
            LIMIT 1
        """, (vendor_item_code, vendor_name, vendor_name)).fetchone()
        if row:
            return dict(row)

    # Path 2: vendor_name + description match
    if vendor_product_name:
        row = conn.execute("""
            SELECT * FROM vendor_items
            WHERE LOWER(TRIM(vendor_description)) = LOWER(TRIM(?))
            AND (
                vendor_name = ? OR vendor_id IN (
                    SELECT id FROM vendors WHERE LOWER(name) = LOWER(?)
                )
            )
            LIMIT 1
        """, (vendor_product_name, vendor_name, vendor_name)).fetchone()
        if row:
            return dict(row)

    return None


def upsert_vendor_item(vendor_name, vendor_product_name, purchase_price,
                       vendor_item_code=None, pack_size=None, pack_unit=None,
                       pack_contains=None, contains_unit=None,
                       category='FOOD', invoice_id=None, invoice_date=None,
                       conn=None):
    """Create or update a vendor_items row.

    If vendor_item_code matches existing row, update price even if name differs.
    If pack_contains provided, recalculate price_per_unit.
    Only update price if invoice_date >= last_seen_date (prevents older invoices overwriting).

    Returns vendor_items.id
    """
    if not conn:
        from data_store import get_connection
        conn = get_connection()

    today = datetime.now().strftime("%Y-%m-%d")

    # Find existing vendor item
    existing = find_vendor_item(vendor_name, vendor_product_name, vendor_item_code, conn)

    if existing:
        vi_id = existing["id"]

        # Only update price if this invoice is newer or same date
        last_seen = existing.get("last_seen_date") or ""
        inv_date = invoice_date or today
        if last_seen and inv_date < last_seen:
            logger.debug(f"Skipping price update for {vendor_product_name}: older invoice ({inv_date} < {last_seen})")
            return vi_id

        # Calculate price_per_unit if we have pack_contains
        pc = pack_contains or existing.get("pack_contains")
        price_per_unit = None
        if pc and pc > 0 and purchase_price:
            price_per_unit = round(purchase_price / pc, 4)

        conn.execute("""
            UPDATE vendor_items SET
                purchase_price = COALESCE(?, purchase_price),
                price_per_unit = COALESCE(?, price_per_unit),
                vendor_item_code = COALESCE(?, vendor_item_code),
                pack_size = COALESCE(?, pack_size),
                pack_unit = COALESCE(?, pack_unit),
                pack_contains = COALESCE(?, pack_contains),
                contains_unit = COALESCE(?, contains_unit),
                last_seen_date = ?,
                last_invoice_id = COALESCE(?, last_invoice_id),
                vendor_description = COALESCE(?, vendor_description),
                updated_at = datetime('now')
            WHERE id = ?
        """, (purchase_price, price_per_unit, vendor_item_code,
              pack_size, pack_unit, pack_contains, contains_unit,
              inv_date, invoice_id, vendor_product_name, vi_id))

        # Update products.active_vendor_item_id + current_price
        product_id = existing.get("product_id")
        if product_id and existing.get("is_active"):
            conn.execute("""
                UPDATE products SET current_price = ?, updated_at = datetime('now')
                WHERE id = ? AND active_vendor_item_id = ?
            """, (purchase_price, product_id, vi_id))

        conn.commit()
        return vi_id

    else:
        # New vendor item — create product first if needed, then vendor_item
        product_id = get_or_create_product(vendor_product_name, category, conn)

        # Resolve vendor_id
        vendor_id = None
        if vendor_name:
            v = conn.execute(
                "SELECT id FROM vendors WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (vendor_name,)
            ).fetchone()
            if v:
                vendor_id = v["id"]

        price_per_unit = None
        if pack_contains and pack_contains > 0 and purchase_price:
            price_per_unit = round(purchase_price / pack_contains, 4)

        conn.execute("""
            INSERT INTO vendor_items
                (product_id, vendor_id, vendor_name, vendor_description,
                 vendor_item_code, purchase_price, price_per_unit,
                 pack_size, pack_unit, pack_contains, contains_unit,
                 is_active, last_seen_date, last_invoice_id,
                 match_confidence, match_method)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, 'new')
        """, (product_id, vendor_id, vendor_name, vendor_product_name,
              vendor_item_code, purchase_price, price_per_unit,
              pack_size, pack_unit, pack_contains, contains_unit,
              invoice_date or today, invoice_id))

        vi_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Set as active vendor item, deactivate others for this product
        conn.execute(
            "UPDATE vendor_items SET is_active = 0 WHERE product_id = ? AND id != ?",
            (product_id, vi_id)
        )
        conn.execute("""
            UPDATE products SET active_vendor_item_id = ?, current_price = ?,
                                updated_at = datetime('now')
            WHERE id = ?
        """, (vi_id, purchase_price, product_id))

        conn.commit()
        logger.info(f"Created vendor_item {vi_id} for {vendor_product_name} ({vendor_name})")
        return vi_id


def get_product_cost(product_id, conn=None):
    """Get the best (active vendor) cost for a canonical product,
    factoring in yield_pct.

    Returns dict: {price_per_unit, yield_pct, effective_cost, source}
    or None if no pricing available.
    """
    if not conn:
        from data_store import get_connection
        conn = get_connection()

    row = conn.execute("""
        SELECT p.yield_pct, vi.price_per_unit, vi.purchase_price,
               vi.pack_contains, vi.vendor_name
        FROM products p
        LEFT JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
        WHERE p.id = ?
    """, (product_id,)).fetchone()

    if not row:
        return None

    row = dict(row)
    ppu = row.get("price_per_unit") or 0
    yield_pct = row.get("yield_pct") or 1.0

    if not ppu and row.get("purchase_price") and row.get("pack_contains"):
        ppu = row["purchase_price"] / row["pack_contains"]

    if ppu <= 0:
        return {"price_per_unit": 0, "yield_pct": yield_pct,
                "effective_cost": 0, "source": "no_price"}

    effective = round(ppu / yield_pct, 4) if yield_pct > 0 else ppu

    return {
        "price_per_unit": round(ppu, 4),
        "yield_pct": yield_pct,
        "effective_cost": effective,
        "source": "vendor_item",
        "vendor_name": row.get("vendor_name"),
    }


def parse_pack_size(raw_string):
    """Parse a pack size string into structured data.

    Returns dict: {packs, size, unit, units_per_case} or None
    """
    import re

    if not raw_string or not isinstance(raw_string, str):
        return None

    s = raw_string.strip().upper()
    if not s:
        return None

    # Unit normalization map
    UNIT_MAP = {
        'LB': 'LB', 'LBS': 'LB', 'POUND': 'LB', 'POUNDS': 'LB',
        'OZ': 'OZ', 'OUNCE': 'OZ', 'OUNCES': 'OZ',
        'GAL': 'GAL', 'GALLON': 'GAL', 'GALLONS': 'GAL',
        'EA': 'EA', 'EACH': 'EA', 'CT': 'EA', 'COUNT': 'EA', 'PC': 'EA', 'PCS': 'EA',
        'FL OZ': 'FL_OZ', 'FLOZ': 'FL_OZ',
        'QT': 'QT', 'QUART': 'QT', 'QUARTS': 'QT',
        'PT': 'PT', 'PINT': 'PT', 'PINTS': 'PT',
        'L': 'L', 'LITER': 'L', 'LITRE': 'L', 'LITERS': 'L',
        'ML': 'ML', 'MILLILITER': 'ML',
        'KG': 'KG', 'KILOGRAM': 'KG',
        'G': 'G', 'GRAM': 'G', 'GRAMS': 'G',
        'DOZ': 'DOZ', 'DOZEN': 'DOZ',
    }

    # Units that can't tell us what's inside
    OPAQUE_UNITS = {'CS', 'CASE', 'CASES', 'BX', 'BOX', 'PKG', 'PACKAGE', 'BAG', 'BTL', 'CAN', 'JUG', 'JAR', 'TUB'}

    def normalize_unit(u):
        u = u.strip().upper()
        return UNIT_MAP.get(u)

    # Pattern 1: N/N UNIT or N/NUNIT  (e.g., "4/10 LB", "2/5LB")
    m = re.match(r'^(\d+(?:\.\d+)?)\s*[/xX×\-]\s*(\d+(?:\.\d+)?)\s*(.+)$', s)
    if m:
        packs = float(m.group(1))
        size = float(m.group(2))
        unit_raw = m.group(3).strip()
        unit = normalize_unit(unit_raw)
        if unit:
            upc = packs * size
            if unit == 'DOZ':
                upc = packs * size * 12
                unit = 'EA'
            return {"packs": packs, "size": size, "unit": unit, "units_per_case": upc}

    # Pattern 2: N/UNIT (e.g., "6/GAL" = 6 × 1 GAL, "1/EA")
    m = re.match(r'^(\d+(?:\.\d+)?)\s*[/]\s*([A-Z][A-Z ]*?)$', s)
    if m:
        packs = float(m.group(1))
        unit_raw = m.group(2).strip()
        unit = normalize_unit(unit_raw)
        if unit:
            upc = packs
            if unit == 'DOZ':
                upc = packs * 12
                unit = 'EA'
            return {"packs": packs, "size": 1.0, "unit": unit, "units_per_case": upc}

    # Pattern 3: N UNIT or NUNIT (e.g., "50 LB", "50LB")
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Z][A-Z ]*?)$', s)
    if m:
        size = float(m.group(1))
        unit_raw = m.group(2).strip()
        unit = normalize_unit(unit_raw)
        if unit:
            upc = size
            if unit == 'DOZ':
                upc = size * 12
                unit = 'EA'
            return {"packs": 1.0, "size": size, "unit": unit, "units_per_case": upc}

    # No match — opaque or unrecognized
    return None
