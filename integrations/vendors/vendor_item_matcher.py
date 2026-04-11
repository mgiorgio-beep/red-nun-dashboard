"""
Vendor Item Matcher — Red Nun Analytics

Core matching engine for linking invoice line items to canonical products.
Runs on every invoice confirm to auto-link high-confidence matches and
surface suggestions for manual review.

Thresholds:
  >= 90  -> 'auto' (link immediately)
  70-89  -> 'suggest' (needs human review)
  < 70   -> 'new' (create new product)
"""

import logging
from datetime import datetime
from rapidfuzz import fuzz, process
from product_name_mapper import normalize, shares_key_token

logger = logging.getLogger(__name__)

# Packaging/descriptor words that appear on many products and must NOT
# be treated as shared identity tokens by the key-token guard.
# "PACKER" is the classic example — appears on both ASPARAGUS and ORANGE.
DESCRIPTOR_STOPWORDS = {
    'packer', 'fresh', 'slcd', 'diced', 'whole', 'case', 'cases', 'each',
    'large', 'small', 'medium', 'extra', 'grade', 'frozen', 'frozn', 'refr',
    'rstd', 'ripe', 'vine', 'cross', 'round', 'bulk', 'pack', 'unit',
    'natural', 'organic', 'choice', 'select', 'fancy', 'prime', 'brand',
    'style', 'type', 'bnch', 'bunch', 'piece', 'pieces', 'count',
    'pound', 'ounce', 'gallon', 'glnvw', 'ryko', 'sgl', 'sngl',
}


def match_vendor_item_to_product(invoice_item, conn):
    """
    Match a single invoice line item to the best canonical product.

    Args:
        invoice_item: dict with keys: product_name, unit_price, quantity, unit, pack_size, vendor_item_code
        conn: SQLite connection (must have Row factory)

    Returns:
        {
            'match_type': 'auto' | 'suggest' | 'new',
            'product_id': int or None,
            'product_name': str or None,
            'score': float,
            'vendor_item_data': dict
        }
    """
    item_name = invoice_item.get("product_name", "") or ""
    vendor_item_code = invoice_item.get("vendor_item_code")

    if not item_name.strip():
        return {
            "match_type": "new",
            "product_id": None,
            "product_name": None,
            "score": 0.0,
            "vendor_item_data": _build_vendor_item_data(invoice_item),
        }

    # PATH 0: vendor_item_code exact match (most reliable)
    if vendor_item_code:
        vi = conn.execute("""
            SELECT vi.product_id, p.name
            FROM vendor_items vi
            JOIN products p ON vi.product_id = p.id
            WHERE vi.vendor_item_code = ?
            LIMIT 1
        """, (vendor_item_code,)).fetchone()
        if vi:
            logger.info(f"Vendor code match: code={vendor_item_code} -> product #{vi['product_id']} ({vi['name']})")
            return {
                "match_type": "auto",
                "product_id": vi["product_id"],
                "product_name": vi["name"],
                "score": 100.0,
                "vendor_item_data": _build_vendor_item_data(invoice_item),
            }

    # Load all active product names for matching
    rows = conn.execute("SELECT id, name FROM products WHERE active = 1").fetchall()
    if not rows:
        rows = conn.execute("SELECT id, name FROM products").fetchall()
    if not rows:
        return {
            "match_type": "new",
            "product_id": None,
            "product_name": None,
            "score": 0.0,
            "vendor_item_data": _build_vendor_item_data(invoice_item),
        }

    product_names    = [r["name"] for r in rows]
    product_ids      = [r["id"]   for r in rows]
    normalized_names = [normalize(n) for n in product_names]
    norm_item        = normalize(item_name)

    # Find best match using rapidfuzz WRatio
    result = process.extractOne(
        norm_item,
        normalized_names,
        scorer=fuzz.WRatio,
        score_cutoff=0,
    )

    if result is None:
        return {
            "match_type": "new",
            "product_id": None,
            "product_name": None,
            "score": 0.0,
            "vendor_item_data": _build_vendor_item_data(invoice_item),
        }

    matched_norm, score, idx = result
    product_id   = product_ids[idx]
    product_name = product_names[idx]

    # Guard against false positives: require at least one shared non-descriptor token.
    # This catches ASPARAGUS/PACKER matching ORANGE/PACKER — "packer" is a stopword
    # so neither side has meaningful tokens in common → rejected as 'new'.
    if score >= 70 and not shares_key_token(
        item_name, product_name, stopwords=DESCRIPTOR_STOPWORDS
    ):
        logger.debug(
            f"Stopword guard blocked: '{item_name}' -> '{product_name}' "
            f"(score {score:.1f}, no shared non-descriptor tokens)"
        )
        return {
            "match_type": "new",
            "product_id": None,
            "product_name": None,
            "score": round(score, 1),
            "vendor_item_data": _build_vendor_item_data(invoice_item),
        }

    if score >= 90:
        match_type = "auto"
    elif score >= 70:
        match_type = "suggest"
    else:
        match_type   = "new"
        product_id   = None
        product_name = None

    return {
        "match_type":      match_type,
        "product_id":      product_id,
        "product_name":    product_name,
        "score":           round(score, 1),
        "vendor_item_data": _build_vendor_item_data(invoice_item),
    }


def _build_vendor_item_data(invoice_item):
    """Extract vendor item fields from an invoice line item dict."""
    return {
        "vendor_description": invoice_item.get("product_name", ""),
        "purchase_price":     invoice_item.get("unit_price") or invoice_item.get("total_price"),
        "pack_size":          invoice_item.get("pack_size"),
        "pack_unit":          invoice_item.get("unit"),
        "quantity":           invoice_item.get("quantity"),
        "vendor_item_code":   invoice_item.get("vendor_item_code"),
    }


def create_or_update_vendor_item(product_id, invoice_item, vendor_name, conn):
    """
    Create or update a vendor_item for a confirmed product match.

    If a vendor_item already exists for this product + vendor -> update price.
    If not -> create new vendor_item.
    Always sets this vendor_item as active and deactivates others for same product.

    Returns: vendor_item_id
    """
    from product_helpers import parse_pack_size

    description = invoice_item.get("product_name", "")
    price       = invoice_item.get("unit_price") or invoice_item.get("total_price")
    pack_size   = invoice_item.get("pack_size")
    pack_unit   = invoice_item.get("unit")
    vendor_item_code = invoice_item.get("vendor_item_code")
    today       = datetime.now().strftime("%Y-%m-%d")

    # Parse pack size to get pack_contains and contains_unit
    parsed = parse_pack_size(pack_size)
    pack_contains = parsed["units_per_case"] if parsed else None
    contains_unit = parsed["unit"] if parsed else None
    price_per_unit = round(price / pack_contains, 4) if price and pack_contains and pack_contains > 0 else None

    # Resolve vendor_id from vendors table (vendor_name is fallback if not found)
    vendor_id = None
    if vendor_name:
        row = conn.execute(
            "SELECT id FROM vendors WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (vendor_name,)
        ).fetchone()
        if row:
            vendor_id = row["id"]

    # Check if vendor_item already exists: first by vendor_item_code, then by product+vendor
    existing = None
    if vendor_item_code:
        existing = conn.execute("""
            SELECT id FROM vendor_items
            WHERE vendor_item_code = ?
            AND (vendor_id = ? OR vendor_name = ? OR (vendor_id IS NULL AND vendor_name IS NULL))
            LIMIT 1
        """, (vendor_item_code, vendor_id, vendor_name)).fetchone()
    if not existing:
        existing = conn.execute("""
            SELECT id FROM vendor_items
            WHERE product_id = ?
            AND (vendor_id = ? OR (vendor_id IS NULL AND vendor_name = ?))
        """, (product_id, vendor_id, vendor_name)).fetchone()

    if existing:
        vendor_item_id = existing["id"]
        conn.execute("""
            UPDATE vendor_items
            SET purchase_price      = ?,
                price_per_unit      = COALESCE(?, price_per_unit),
                last_seen_date      = ?,
                updated_at          = datetime('now'),
                pack_size           = COALESCE(?, pack_size),
                pack_unit           = COALESCE(?, pack_unit),
                pack_contains       = COALESCE(?, pack_contains),
                contains_unit       = COALESCE(?, contains_unit),
                vendor_item_code    = COALESCE(?, vendor_item_code),
                vendor_description  = COALESCE(?, vendor_description)
            WHERE id = ?
        """, (price, price_per_unit, today, pack_size, pack_unit,
              pack_contains, contains_unit, vendor_item_code, description, vendor_item_id))
    else:
        conn.execute("""
            INSERT INTO vendor_items
              (product_id, vendor_id, vendor_name, vendor_description,
               vendor_item_code, purchase_price, price_per_unit,
               pack_size, pack_unit, pack_contains, contains_unit,
               is_active, last_seen_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (product_id, vendor_id, vendor_name, description,
              vendor_item_code, price, price_per_unit,
              pack_size, pack_unit, pack_contains, contains_unit, today))
        vendor_item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Set this vendor_item active, deactivate all others for this product
    conn.execute(
        "UPDATE vendor_items SET is_active = 0 WHERE product_id = ? AND id != ?",
        (product_id, vendor_item_id)
    )
    conn.execute(
        "UPDATE vendor_items SET is_active = 1 WHERE id = ?",
        (vendor_item_id,)
    )

    # Update products.active_vendor_item_id + current_price (backward compat)
    conn.execute("""
        UPDATE products
        SET active_vendor_item_id = ?,
            current_price         = ?
        WHERE id = ?
    """, (vendor_item_id, price, product_id))

    return vendor_item_id


def process_invoice_items(invoice_id, conn):
    """
    Process all line items from a confirmed invoice.

    Auto-links high-confidence matches, saves suggestions for human review,
    creates new products for unmatched items.

    Returns: {'auto_matched': int, 'suggestions': int, 'new_products': int}
    """
    invoice = conn.execute(
        "SELECT vendor_name FROM scanned_invoices WHERE id = ?",
        (invoice_id,)
    ).fetchone()
    vendor_name = invoice["vendor_name"] if invoice else None

    items = conn.execute(
        "SELECT * FROM scanned_invoice_items WHERE invoice_id = ?",
        (invoice_id,)
    ).fetchall()

    counts = {"auto_matched": 0, "suggestions": 0, "new_products": 0}

    for item in items:
        item_dict = dict(item)
        result    = match_vendor_item_to_product(item_dict, conn)

        if result["match_type"] == "auto":
            create_or_update_vendor_item(
                result["product_id"], item_dict, vendor_name, conn
            )
            counts["auto_matched"] += 1
            logger.info(
                f"Auto-matched: '{item_dict.get('product_name')}' -> "
                f"product #{result['product_id']} ({result['score']}%)"
            )

        elif result["match_type"] == "suggest":
            conn.execute("""
                INSERT INTO vendor_item_suggestions
                  (invoice_id, invoice_item_id, product_id, product_name,
                   vendor_description, score, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """, (
                invoice_id,
                item_dict.get("id"),
                result["product_id"],
                result["product_name"],
                item_dict.get("product_name", ""),
                result["score"],
            ))
            counts["suggestions"] += 1
            logger.info(
                f"Suggestion: '{item_dict.get('product_name')}' -> "
                f"'{result['product_name']}' ({result['score']}%)"
            )

        else:
            product_id = _create_product_from_invoice_item(item_dict, conn)
            if product_id:
                create_or_update_vendor_item(
                    product_id, item_dict, vendor_name, conn
                )
                counts["new_products"] += 1
            logger.info(
                f"New product: '{item_dict.get('product_name')}' -> #{product_id}"
            )

    conn.commit()
    return counts


def _create_product_from_invoice_item(item_dict, conn):
    """Create a new canonical product from an unmatched invoice line item."""
    name = item_dict.get("product_name", "").strip()
    if not name:
        return None

    # Don't create duplicates
    existing = conn.execute(
        "SELECT id FROM products WHERE LOWER(name) = LOWER(?) LIMIT 1",
        (name,)
    ).fetchone()
    if existing:
        return existing["id"]

    category      = item_dict.get("category_type", "FOOD") or "FOOD"
    price         = item_dict.get("unit_price") or item_dict.get("total_price") or 0
    unit          = item_dict.get("unit", "")
    pack_size_str = item_dict.get("pack_size", "")

    conn.execute("""
        INSERT INTO products (name, category, current_price, unit, pack_size,
                              active, setup_complete)
        VALUES (?, ?, ?, ?, ?, 1, 0)
    """, (name, category.upper(), price, unit, pack_size_str))

    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
