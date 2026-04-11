"""
Inventory Management Routes
Handles products, vendors, inventory, and recipes
"""

from flask import Blueprint, jsonify, request
from data_store import get_connection
from auth_routes import login_required
from datetime import datetime

inventory_bp = Blueprint('inventory', __name__, url_prefix='/api/inventory')

# ============================================
# PRODUCTS
# ============================================

@inventory_bp.route('/products', methods=['GET'])
def get_products():
    """Get all products with optional filters, including product_costing canonicals"""
    conn = get_connection()
    category = request.args.get('category')
    search = request.args.get('search', '')
    active_only = request.args.get('active', 'true') == 'true'

    if search:
        query = """
            SELECT p.*, v.name as vendor_name
            FROM products p
            LEFT JOIN vendors v ON p.preferred_vendor_id = v.id
            WHERE (p.name LIKE ? OR p.display_name LIKE ? OR p.subcategory LIKE ?)
        """
        params = [f'%{search}%', f'%{search}%', f'%{search}%']
        if active_only:
            query += " AND p.active = 1"
        if category:
            query += " AND p.category = ?"
            params.append(category)
        query += " ORDER BY p.name"
        rows = conn.execute(query, params).fetchall()
    else:
        # No search — original behavior (products only)
        query = """
            SELECT p.*, v.name as vendor_name, 'products' as source
            FROM products p
            LEFT JOIN vendors v ON p.preferred_vendor_id = v.id
            WHERE 1=1
        """
        params = []
        if active_only:
            query += " AND p.active = 1"
        if category:
            query += " AND p.category = ?"
            params.append(category)
        query += " ORDER BY p.name"
        rows = conn.execute(query, params).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@inventory_bp.route('/products/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """Get single product with pricing from all vendors"""
    conn = get_connection()

    # Get product details
    product = conn.execute("""
        SELECT p.*, v.name as vendor_name
        FROM products p
        LEFT JOIN vendors v ON p.preferred_vendor_id = v.id
        WHERE p.id = ?
    """, (product_id,)).fetchone()

    if not product:
        conn.close()
        return jsonify({'error': 'Product not found'}), 404

    # Get vendor prices
    vendor_prices = conn.execute("""
        SELECT pv.*, v.name as vendor_name
        FROM product_vendors pv
        JOIN vendors v ON pv.vendor_id = v.id
        WHERE pv.product_id = ?
        ORDER BY pv.unit_price
    """, (product_id,)).fetchall()

    # Get current inventory
    inventory = conn.execute("""
        SELECT * FROM inventory WHERE product_id = ?
    """, (product_id,)).fetchall()

    conn.close()

    result = dict(product)
    result['vendor_prices'] = [dict(r) for r in vendor_prices]
    result['inventory'] = [dict(r) for r in inventory]

    return jsonify(result)


@inventory_bp.route('/products', methods=['POST'])
def create_product():
    """Create a new product"""
    data = request.json
    conn = get_connection()

    cursor = conn.execute("""
        INSERT INTO products (name, category, subcategory, unit, pack_size, pack_unit,
                             preferred_vendor_id, current_price, par_level, reorder_point,
                             storage_location, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get('name'),
        data.get('category'),
        data.get('subcategory'),
        data.get('unit'),
        data.get('pack_size'),
        data.get('pack_unit'),
        data.get('preferred_vendor_id'),
        data.get('current_price'),
        data.get('par_level'),
        data.get('reorder_point'),
        data.get('storage_location'),
        data.get('notes')
    ))

    product_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({'id': product_id, 'message': 'Product created'}), 201


@inventory_bp.route('/products/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    """Update a product"""
    data = request.json
    conn = get_connection()

    conn.execute("""
        UPDATE products
        SET name=?, display_name=?, category=?, subcategory=?, unit=?, pack_size=?, pack_unit=?,
            preferred_vendor_id=?, current_price=?, par_level=?, reorder_point=?,
            storage_location=?, notes=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (
        data.get('name'),
        data.get('display_name') or None,
        data.get('category'),
        data.get('subcategory'),
        data.get('unit'),
        data.get('pack_size'),
        data.get('pack_unit'),
        data.get('preferred_vendor_id'),
        data.get('current_price'),
        data.get('par_level'),
        data.get('reorder_point'),
        data.get('storage_location'),
        data.get('notes'),
        product_id
    ))

    conn.commit()
    conn.close()

    return jsonify({'message': 'Product updated'})


@inventory_bp.route('/products/<int:product_id>/vendor-items', methods=['GET'])
def get_product_vendor_items(product_id):
    """Get all vendor items linked to a product"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT vi.*, v.name as vendor_name
        FROM vendor_items vi
        LEFT JOIN vendors v ON vi.vendor_id = v.id
        WHERE vi.product_id = ?
        ORDER BY vi.purchase_price DESC
    """, (product_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@inventory_bp.route('/products/<int:product_id>/active-vendor-item', methods=['PUT'])
def set_active_vendor_item(product_id):
    """Set which vendor item is the active one for pricing"""
    data = request.json
    vi_id = data.get('vendor_item_id')
    conn = get_connection()

    # Verify vendor item belongs to this product
    vi = conn.execute("SELECT id, purchase_price, pack_contains, contains_unit, price_per_unit FROM vendor_items WHERE id = ? AND product_id = ?",
                      (vi_id, product_id)).fetchone()
    if not vi:
        conn.close()
        return jsonify({'error': 'Vendor item not found for this product'}), 404

    conn.execute("UPDATE products SET active_vendor_item_id = ? WHERE id = ?", (vi_id, product_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Active vendor item updated'})


@inventory_bp.route('/products/<int:product_id>/merge', methods=['POST'])
def merge_product(product_id):
    """Merge another product into this one. Moves vendor items + recipe ingredients."""
    data = request.json
    merge_id = data.get('merge_product_id')
    if not merge_id or merge_id == product_id:
        return jsonify({'error': 'Invalid merge product'}), 400

    conn = get_connection()

    # Verify both exist
    keep = conn.execute("SELECT id, name FROM products WHERE id = ?", (product_id,)).fetchone()
    merge = conn.execute("SELECT id, name FROM products WHERE id = ?", (merge_id,)).fetchone()
    if not keep or not merge:
        conn.close()
        return jsonify({'error': 'Product not found'}), 404

    # Move vendor items
    vi_moved = conn.execute(
        "UPDATE vendor_items SET product_id = ? WHERE product_id = ?",
        (product_id, merge_id)
    ).rowcount

    # Move recipe ingredients
    ing_moved = conn.execute(
        "UPDATE recipe_ingredients SET product_id = ? WHERE product_id = ?",
        (product_id, merge_id)
    ).rowcount

    # Deactivate merged product
    conn.execute("UPDATE products SET active = 0 WHERE id = ?", (merge_id,))

    # If the keep product has no active_vendor_item_id, set one from the moved items
    keep_vi = conn.execute("SELECT active_vendor_item_id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not keep_vi['active_vendor_item_id']:
        best_vi = conn.execute("""
            SELECT id FROM vendor_items WHERE product_id = ? AND purchase_price > 0
            ORDER BY purchase_price DESC LIMIT 1
        """, (product_id,)).fetchone()
        if best_vi:
            conn.execute("UPDATE products SET active_vendor_item_id = ? WHERE id = ?",
                         (best_vi['id'], product_id))

    conn.commit()
    conn.close()

    return jsonify({
        'message': f'Merged "{merge["name"]}" into product {product_id}',
        'vendor_items_moved': vi_moved,
        'ingredients_moved': ing_moved
    })


# ============================================
# VENDORS
# ============================================

@inventory_bp.route('/vendors', methods=['GET'])
def get_vendors():
    """Get all vendors"""
    conn = get_connection()
    active_only = request.args.get('active', 'true') == 'true'

    query = "SELECT * FROM vendors WHERE 1=1"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY name"

    rows = conn.execute(query).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@inventory_bp.route('/vendors/<int:vendor_id>', methods=['GET'])
def get_vendor(vendor_id):
    """Get vendor with products and spending history"""
    conn = get_connection()

    vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if not vendor:
        conn.close()
        return jsonify({'error': 'Vendor not found'}), 404

    # Get products from this vendor
    products = conn.execute("""
        SELECT p.* FROM products p
        WHERE p.preferred_vendor_id = ?
        ORDER BY p.name
    """, (vendor_id,)).fetchall()

    # Get spending from invoices
    spending = conn.execute("""
        SELECT
            COUNT(*) as invoice_count,
            SUM(total) as total_spent,
            MIN(invoice_date) as first_invoice,
            MAX(invoice_date) as last_invoice
        FROM invoice_scans
        WHERE vendor_name = ?
    """, (vendor['name'],)).fetchone()

    conn.close()

    result = dict(vendor)
    result['products'] = [dict(p) for p in products]
    result['spending'] = dict(spending) if spending else {}

    return jsonify(result)


@inventory_bp.route('/vendors', methods=['POST'])
def create_vendor():
    """Create a new vendor"""
    data = request.json
    conn = get_connection()

    cursor = conn.execute("""
        INSERT INTO vendors (name, category, contact_name, email, phone, address,
                           payment_terms, account_number, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get('name'),
        data.get('category'),
        data.get('contact_name'),
        data.get('email'),
        data.get('phone'),
        data.get('address'),
        data.get('payment_terms'),
        data.get('account_number'),
        data.get('notes')
    ))

    vendor_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({'id': vendor_id, 'message': 'Vendor created'}), 201


# ============================================
# INVENTORY
# ============================================

@inventory_bp.route('/stock', methods=['GET'])
def get_inventory():
    """Get current inventory levels"""
    conn = get_connection()
    location = request.args.get('location')
    low_stock = request.args.get('low_stock') == 'true'

    query = """
        SELECT i.*, p.name as product_name, p.category, p.unit, p.par_level
        FROM inventory i
        JOIN products p ON i.product_id = p.id
        WHERE 1=1
    """
    params = []

    if location:
        query += " AND i.location = ?"
        params.append(location)

    if low_stock:
        query += " AND i.quantity <= p.par_level"

    query += " ORDER BY p.name"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@inventory_bp.route('/stock/adjust', methods=['POST'])
def adjust_inventory():
    """Manually adjust inventory"""
    data = request.json
    conn = get_connection()

    product_id = data.get('product_id')
    location = data.get('location')
    quantity = data.get('quantity')
    movement_type = data.get('type', 'ADJUSTMENT')
    notes = data.get('notes', '')

    # Update inventory
    conn.execute("""
        INSERT INTO inventory (product_id, location, quantity, unit, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(product_id, location) DO UPDATE SET
            quantity = quantity + ?,
            updated_at = CURRENT_TIMESTAMP
    """, (product_id, location, quantity, data.get('unit', 'ea'), quantity))

    # Log movement
    conn.execute("""
        INSERT INTO inventory_movements
        (product_id, location, movement_type, quantity, unit, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (product_id, location, movement_type, quantity, data.get('unit', 'ea'), notes))

    conn.commit()
    conn.close()

    return jsonify({'message': 'Inventory adjusted'})


# ============================================
# RECIPES
# ============================================

@inventory_bp.route('/recipes', methods=['GET'])
def get_recipes():
    """Get all recipes with linked menu items and ingredient counts"""
    conn = get_connection()
    category = request.args.get('category')

    query = """
        SELECT r.*,
               COUNT(ri.id) as ingredient_count
        FROM recipes r
        LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
        WHERE r.active = 1
    """
    params = []

    if category:
        query += " AND r.category = ?"
        params.append(category)

    query += " GROUP BY r.id ORDER BY r.name"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@inventory_bp.route('/recipes/<int:recipe_id>', methods=['GET'])
def get_recipe(recipe_id):
    """Get recipe with ingredients, cost, and linked menu item"""
    conn = get_connection()

    recipe = conn.execute("""
        SELECT r.*, mi.name as menu_item_name
        FROM recipes r
        LEFT JOIN menu_items mi ON r.menu_item_guid = mi.guid AND r.location = mi.location
        WHERE r.id = ?
    """, (recipe_id,)).fetchone()

    if not recipe:
        conn.close()
        return jsonify({'error': 'Recipe not found'}), 404

    # Get ingredients with vendor item pricing (Session 38: use vendor_items chain)
    ingredients = conn.execute("""
        SELECT ri.*, p.name as product_name, p.display_name as product_display_name,
               p.current_price, p.recipe_unit as product_recipe_unit,
               vi.purchase_price as vi_price,
               vi.price_per_unit as vi_price_per_unit,
               vi.pack_contains as vi_pack_contains
        FROM recipe_ingredients ri
        LEFT JOIN products p ON ri.product_id = p.id
        LEFT JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
        WHERE ri.recipe_id = ?
    """, (recipe_id,)).fetchall()

    enriched = []
    for i in ingredients:
        d = dict(i)
        # Calculate cost_per_recipe_unit from active vendor item
        vi_ppu = d.get("vi_price_per_unit") or 0
        vi_price = d.get("vi_price") or 0
        vi_pack = d.get("vi_pack_contains") or 0
        if vi_ppu and vi_ppu > 0:
            d["cost_per_recipe_unit"] = vi_ppu
        elif vi_price and vi_pack and vi_pack > 0:
            d["cost_per_recipe_unit"] = round(vi_price / vi_pack, 4)
        d["recipe_unit"] = d.get("product_recipe_unit")
        enriched.append(d)

    # Get cost
    cost = conn.execute("""
        SELECT * FROM recipe_costs WHERE recipe_id = ?
    """, (recipe_id,)).fetchone()

    conn.close()

    result = dict(recipe)
    result['ingredients'] = enriched
    result['cost'] = dict(cost) if cost else None

    return jsonify(result)


@inventory_bp.route('/recipes', methods=['POST'])
def create_recipe():
    """Create a new recipe"""
    data = request.json
    conn = get_connection()

    cursor = conn.execute("""
        INSERT INTO recipes (name, description, category, serving_size, serving_unit,
                           menu_price, prep_time_minutes, notes, menu_item_guid, location)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get('name'),
        data.get('description'),
        data.get('category'),
        data.get('serving_size', 1),
        data.get('serving_unit', 'portion'),
        data.get('menu_price'),
        data.get('prep_time_minutes'),
        data.get('notes'),
        data.get('menu_item_guid'),
        data.get('location')
    ))

    recipe_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({'id': recipe_id, 'message': 'Recipe created'}), 201


@inventory_bp.route('/recipes/<int:recipe_id>', methods=['PUT', 'PATCH'])
def update_recipe(recipe_id):
    """Update recipe details including menu item link"""
    data = request.json
    conn = get_connection()

    # Check if recipe exists
    exists = conn.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not exists:
        conn.close()
        return jsonify({'error': 'Recipe not found'}), 404

    # Build update query dynamically based on provided fields
    fields = []
    values = []

    allowed_fields = ['name', 'description', 'category', 'serving_size', 'serving_unit',
                     'menu_price', 'prep_time_minutes', 'notes', 'menu_item_guid', 'location', 'active', 'yield_qty', 'yield_unit', 'shelf_life_qty', 'shelf_life_unit', 'is_inventoried', 'restricted_locations', 'equipment', 'method_steps']

    for field in allowed_fields:
        if field in data:
            fields.append(f"{field} = ?")
            values.append(data[field])

    if not fields:
        conn.close()
        return jsonify({'error': 'No fields to update'}), 400

    # Add updated_at
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(recipe_id)

    query = f"UPDATE recipes SET {', '.join(fields)} WHERE id = ?"
    conn.execute(query, values)
    conn.commit()
    conn.close()

    return jsonify({'message': 'Recipe updated'}), 200


@inventory_bp.route('/recipes/<int:recipe_id>/ingredients', methods=['POST'])
def add_recipe_ingredient(recipe_id):
    """Add ingredient to recipe"""
    data = request.json
    conn = get_connection()

    conn.execute("""
        INSERT INTO recipe_ingredients (recipe_id, product_id, product_name, quantity, unit, notes, yield_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        recipe_id,
        data.get('product_id') or 0,
        data.get('product_name'),
        data.get('quantity'),
        data.get('unit'),
        data.get('notes'),
        data.get('yield_pct', 100)
    ))

    conn.commit()
    conn.close()
    return jsonify({'message': 'Ingredient added'}), 201

@inventory_bp.route("/recipes/<int:recipe_id>/ingredients/<int:ingredient_id>", methods=["DELETE"])
def delete_recipe_ingredient(recipe_id, ingredient_id):
    """Remove ingredient from recipe"""
    conn = get_connection()
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ? AND id = ?", (recipe_id, ingredient_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "Ingredient removed"}), 200


@inventory_bp.route('/recipes/<int:recipe_id>/menu-links', methods=['GET'])
def get_recipe_menu_links(recipe_id):
    """Get menu item links for a recipe"""
    conn = get_connection()

    links = conn.execute("""
        SELECT rml.*, mi.name as menu_item_name
        FROM recipe_menu_links rml
        LEFT JOIN menu_items mi ON rml.menu_item_guid = mi.guid AND rml.location = mi.location
        WHERE rml.recipe_id = ?
    """, (recipe_id,)).fetchall()

    conn.close()
    return jsonify([dict(link) for link in links])


@inventory_bp.route('/recipes/<int:recipe_id>/menu-links', methods=['POST'])
def add_recipe_menu_link(recipe_id):
    """Add or update menu item link for a recipe at a specific location"""
    data = request.json
    location = data.get('location')
    menu_item_guid = data.get('menu_item_guid')

    if not location or not menu_item_guid:
        return jsonify({'error': 'location and menu_item_guid required'}), 400

    conn = get_connection()

    # Check if recipe exists
    recipe = conn.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not recipe:
        conn.close()
        return jsonify({'error': 'Recipe not found'}), 404

    # Insert or update link
    conn.execute("""
        INSERT INTO recipe_menu_links (recipe_id, location, menu_item_guid)
        VALUES (?, ?, ?)
        ON CONFLICT(recipe_id, location) DO UPDATE SET
            menu_item_guid = ?,
            created_at = CURRENT_TIMESTAMP
    """, (recipe_id, location, menu_item_guid, menu_item_guid))

    conn.commit()
    conn.close()

    return jsonify({'message': 'Menu link saved'}), 201


@inventory_bp.route('/recipes/<int:recipe_id>/menu-links/<location>', methods=['DELETE'])
def delete_recipe_menu_link(recipe_id, location):
    """Remove menu item link for a recipe at a specific location"""
    conn = get_connection()

    conn.execute("""
        DELETE FROM recipe_menu_links
        WHERE recipe_id = ? AND location = ?
    """, (recipe_id, location))

    conn.commit()
    conn.close()

    return jsonify({'message': 'Menu link removed'}), 200


# Weight units — base unit: oz (weight)
WEIGHT_TO_OZ = {
    'oz': 1.0, 'ounce': 1.0, 'ounces': 1.0,
    'lb': 16.0, 'lbs': 16.0, 'pound': 16.0, 'pounds': 16.0,
    'g': 0.035274, 'gram': 0.035274, 'grams': 0.035274,
    'kg': 35.274, 'kilogram': 35.274,
}

# Volume units — base unit: fl oz (volume)
VOLUME_TO_FLOZ = {
    'fl oz': 1.0, 'floz': 1.0, 'fluid oz': 1.0, 'fluid ounce': 1.0,
    'ml': 0.033814, 'milliliter': 0.033814, 'millilitre': 0.033814,
    'l': 33.814, 'liter': 33.814, 'litre': 33.814,
    'tsp': 0.16667, 'teaspoon': 0.16667,
    'tbsp': 0.5, 'tablespoon': 0.5,
    'cup': 8.0, 'cups': 8.0,
    'pt': 16.0, 'pint': 16.0,
    'qt': 32.0, 'quart': 32.0,
    'gal': 128.0, 'gallon': 128.0, 'gallons': 128.0,
    'gl': 128.0,
}

WEIGHT_UNITS = set(WEIGHT_TO_OZ.keys())
VOLUME_UNITS = set(VOLUME_TO_FLOZ.keys())


def resolve_ingredient_cost(product, quantity, recipe_unit, conversions_by_product):
    """
    Resolve the cost of one recipe ingredient.
    Returns dict with line_cost, needs_conversion, cost_per_recipe_unit.

    Paths:
      1. recipe unit == purchase unit → direct multiply
      2a. both weight units → auto-convert via WEIGHT_TO_OZ
      2b. both volume units → auto-convert via VOLUME_TO_FLOZ
      3. conversion table row exists → apply it (handles each→oz, gal→oz, etc.)
      4. no resolution possible → flag for human input with modal_question hint
    """
    recipe_unit_clean = (recipe_unit or '').strip().lower()
    pack_unit  = (product.get('pack_unit') or '').strip().lower()
    prod_unit  = (product.get('purchase_unit') or product.get('unit') or '').strip().lower()
    price      = product.get('current_price') or 0
    pack_size  = product.get('pack_size') or 1
    product_id = product['id']

    # PATH 1: same unit or no recipe unit → direct multiply
    if not recipe_unit_clean or recipe_unit_clean == prod_unit:
        cost = quantity * price
        return {'line_cost': round(cost, 4), 'needs_conversion': False,
                'cost_per_recipe_unit': round(price, 4)}

    # PATH 2a: both weight units → auto-convert
    ru_wt = WEIGHT_TO_OZ.get(recipe_unit_clean)
    pu_wt = WEIGHT_TO_OZ.get(pack_unit)
    if ru_wt and pu_wt and pack_size > 0 and price > 0:
        cost_per_oz = price / (pack_size * pu_wt)
        cost_per_ru = cost_per_oz * ru_wt
        line_cost   = quantity * cost_per_ru
        return {'line_cost': round(line_cost, 4), 'needs_conversion': False,
                'cost_per_recipe_unit': round(cost_per_ru, 4)}

    # PATH 2b: both volume units → auto-convert
    ru_vol = VOLUME_TO_FLOZ.get(recipe_unit_clean)
    pu_vol = VOLUME_TO_FLOZ.get(pack_unit)
    if ru_vol and pu_vol and pack_size > 0 and price > 0:
        cost_per_floz = price / (pack_size * pu_vol)
        cost_per_ru   = cost_per_floz * ru_vol
        line_cost     = quantity * cost_per_ru
        return {'line_cost': round(line_cost, 4), 'needs_conversion': False,
                'cost_per_recipe_unit': round(cost_per_ru, 4)}

    # PATH 3: check conversions table
    prod_conversions = conversions_by_product.get(product_id, {})
    conv = prod_conversions.get(recipe_unit_clean)
    # Also try lookup by pack_unit (e.g. "1 gallon = 136 oz" stored as from_unit=gallon)
    if not conv:
        conv = prod_conversions.get(pack_unit)

    if conv:
        from_qty  = conv['from_qty'] or 1
        to_qty    = conv['to_qty'] or 0
        to_unit   = (conv['to_unit'] or '').strip().lower()
        from_unit = (conv['from_unit'] or '').strip().lower()

        # "1 gallon = 136 oz" — volume purchase, weight recipe
        if (from_unit == pack_unit and to_unit in WEIGHT_UNITS
                and recipe_unit_clean in WEIGHT_UNITS and pack_size > 0 and price > 0):
            weight_per_pu = (to_qty / from_qty) * WEIGHT_TO_OZ[to_unit]
            cost_per_oz   = price / (pack_size * weight_per_pu)
            cost_per_ru   = cost_per_oz * WEIGHT_TO_OZ[recipe_unit_clean]
            line_cost     = quantity * cost_per_ru
            return {'line_cost': round(line_cost, 4), 'needs_conversion': False,
                    'cost_per_recipe_unit': round(cost_per_ru, 4)}

        # "1 shot = 1.5 fl oz" — countable recipe unit, volume purchase
        if (from_unit == recipe_unit_clean and to_unit in VOLUME_UNITS
                and pu_vol and pack_size > 0 and price > 0):
            floz_per_ru   = (to_qty / from_qty) * VOLUME_TO_FLOZ[to_unit]
            cost_per_floz = price / (pack_size * pu_vol)
            cost_per_ru   = cost_per_floz * floz_per_ru
            line_cost     = quantity * cost_per_ru
            return {'line_cost': round(line_cost, 4), 'needs_conversion': False,
                    'cost_per_recipe_unit': round(cost_per_ru, 4)}

        # "1 each = 1.1 oz" — countable recipe unit, weight purchase
        if (from_unit == recipe_unit_clean and to_unit in WEIGHT_UNITS
                and pu_wt and pack_size > 0 and price > 0):
            oz_per_ru   = (to_qty / from_qty) * WEIGHT_TO_OZ[to_unit]
            cost_per_oz = price / (pack_size * pu_wt)
            cost_per_ru = cost_per_oz * oz_per_ru
            line_cost   = quantity * cost_per_ru
            return {'line_cost': round(line_cost, 4), 'needs_conversion': False,
                    'cost_per_recipe_unit': round(cost_per_ru, 4)}

    # PATH 4: cannot resolve — need human input
    is_volume_to_weight = (pack_unit in VOLUME_UNITS and recipe_unit_clean in WEIGHT_UNITS)
    if is_volume_to_weight:
        modal_question = 'volume_to_weight'
        modal_hint     = f'How much does 1 {pack_unit} of this product weigh?'
    else:
        modal_question = 'countable_to_weight'
        modal_hint     = f'How much does 1 {recipe_unit_clean} weigh?'

    return {
        'line_cost': None,
        'needs_conversion': True,
        'cost_per_recipe_unit': None,
        'missing': {
            'product_id':     product_id,
            'product_name':   product.get('product_name') or product.get('name', ''),
            'recipe_unit':    recipe_unit_clean,
            'pack_unit':      pack_unit,
            'pack_size':      pack_size,
            'purchase_price': price,
            'modal_question': modal_question,
            'modal_hint':     modal_hint
        }
    }


@inventory_bp.route('/recipes/<int:recipe_id>/cost', methods=['GET', 'POST'])
def calculate_recipe_cost(recipe_id):
    """Calculate recipe cost using product_unit_conversions table"""
    conn = get_connection()

    recipe = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not recipe:
        conn.close()
        return jsonify({'error': 'Recipe not found'}), 404
    recipe = dict(recipe)

    ingredients = conn.execute("""
        SELECT ri.id as ingredient_id, ri.quantity, ri.unit, ri.yield_pct,
               p.id as id, p.name as product_name,
               p.current_price, p.unit as purchase_unit,
               p.pack_size, p.pack_unit
        FROM recipe_ingredients ri
        JOIN products p ON ri.product_id = p.id
        WHERE ri.recipe_id = ?
    """, (recipe_id,)).fetchall()

    # Load all conversions for products in this recipe in one query
    product_ids = [dict(i)['id'] for i in ingredients]
    conversions_by_product = {}
    if product_ids:
        placeholders = ','.join('?' * len(product_ids))
        conv_rows = conn.execute(
            f"SELECT product_id, from_qty, from_unit, to_qty, to_unit "
            f"FROM product_unit_conversions WHERE product_id IN ({placeholders})",
            product_ids
        ).fetchall()
        for row in [dict(r) for r in conv_rows]:
            pid = row['product_id']
            if pid not in conversions_by_product:
                conversions_by_product[pid] = {}
            conversions_by_product[pid][row['from_unit'].strip().lower()] = row

    total_cost          = 0
    missing_conversions = []
    ingredient_costs    = []

    for ing in [dict(i) for i in ingredients]:
        result = resolve_ingredient_cost(
            ing, ing['quantity'] or 0, ing['unit'], conversions_by_product
        )
        if result['needs_conversion']:
            missing_conversions.append(result['missing'])
        else:
            total_cost += result['line_cost'] or 0
        ingredient_costs.append({
            'ingredient_id':        ing['ingredient_id'],
            'product_id':           ing['id'],
            'product_name':         ing['product_name'],
            'quantity':             ing['quantity'],
            'unit':                 ing['unit'],
            'line_cost':            result['line_cost'],
            'cost_per_recipe_unit': result.get('cost_per_recipe_unit'),
            'needs_conversion':     result['needs_conversion']
        })

    menu_price       = recipe.get('menu_price') or 0
    serving_size     = recipe.get('serving_size') or 1
    cost_per_serving = total_cost / serving_size
    food_cost_pct    = (cost_per_serving / menu_price * 100) if menu_price > 0 else 0

    if not missing_conversions:
        conn.execute("""
            INSERT INTO recipe_costs (recipe_id, total_food_cost, cost_per_serving, food_cost_percentage)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(recipe_id) DO UPDATE SET
                total_food_cost = ?,
                cost_per_serving = ?,
                food_cost_percentage = ?,
                calculated_at = CURRENT_TIMESTAMP
        """, (recipe_id, total_cost, cost_per_serving, food_cost_pct,
              total_cost, cost_per_serving, food_cost_pct))
        # Also update the recipes table so pmix mapping can read it
        conn.execute("""
            UPDATE recipes SET cost_per_serving = ?, food_cost_pct = ?
            WHERE id = ?
        """, (cost_per_serving, food_cost_pct, recipe_id))
        conn.commit()

    conn.close()
    return jsonify({
        'total_cost':           round(total_cost, 2),
        'cost_per_serving':     round(cost_per_serving, 2),
        'food_cost_percentage': round(food_cost_pct, 1),
        'ingredient_costs':     ingredient_costs,
        'missing_conversions':  missing_conversions
    })


@inventory_bp.route('/products/<int:product_id>/conversions', methods=['GET'])
def get_product_conversions(product_id):
    """Get all unit conversions for a product with cost preview"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, from_qty, from_unit, to_qty, to_unit, created_at
        FROM product_unit_conversions
        WHERE product_id = ?
        ORDER BY from_unit
    """, (product_id,)).fetchall()
    product = conn.execute(
        "SELECT current_price, pack_size, pack_unit FROM products WHERE id = ?",
        (product_id,)
    ).fetchone()
    conn.close()

    result = []
    for row in [dict(r) for r in rows]:
        cost_preview = None
        if product:
            p    = dict(product)
            pack_unit_key = (p.get('pack_unit') or '').strip().lower()
            to_unit_key   = (row['to_unit'] or 'oz').strip().lower()
            price    = p.get('current_price') or 0
            pack_sz  = p.get('pack_size') or 1
            # Cost preview: convert through weight or volume base
            pu_wt   = WEIGHT_TO_OZ.get(pack_unit_key)
            conv_wt = WEIGHT_TO_OZ.get(to_unit_key)
            pu_vol  = VOLUME_TO_FLOZ.get(pack_unit_key)
            conv_vol = VOLUME_TO_FLOZ.get(to_unit_key)
            if pu_wt and conv_wt and price and pack_sz:
                cost_per_oz  = price / (pack_sz * pu_wt)
                cost_preview = round(cost_per_oz * (row['to_qty'] / row['from_qty']) * conv_wt, 4)
            elif pu_vol and conv_vol and price and pack_sz:
                cost_per_floz = price / (pack_sz * pu_vol)
                cost_preview  = round(cost_per_floz * (row['to_qty'] / row['from_qty']) * conv_vol, 4)
        row['cost_preview'] = cost_preview
        result.append(row)
    return jsonify(result)


@inventory_bp.route('/products/<int:product_id>/conversions', methods=['POST'])
def add_product_conversion(product_id):
    """Add or update a unit conversion for a product"""
    data      = request.get_json()
    from_qty  = float(data.get('from_qty', 1))
    from_unit = str(data.get('from_unit', '')).strip().lower()
    to_qty    = float(data.get('to_qty', 0))
    to_unit   = str(data.get('to_unit', 'oz')).strip().lower()
    source    = data.get('source') or None  # NULL = human entered (highest trust)

    if not from_unit or to_qty <= 0:
        return jsonify({'error': 'from_unit and to_qty required'}), 400

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO product_unit_conversions
                (product_id, from_qty, from_unit, to_qty, to_unit, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id, from_unit) DO UPDATE SET
                from_qty = excluded.from_qty,
                to_qty   = excluded.to_qty,
                to_unit  = excluded.to_unit,
                source   = excluded.source
        """, (product_id, from_qty, from_unit, to_qty, to_unit, source))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@inventory_bp.route('/products/conversions/<int:conversion_id>', methods=['DELETE'])
def delete_product_conversion(conversion_id):
    """Delete a unit conversion row"""
    conn = get_connection()
    conn.execute("DELETE FROM product_unit_conversions WHERE id = ?", (conversion_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ============================================
# MENU ITEMS
# ============================================

@inventory_bp.route('/menu-items', methods=['GET'])
def get_menu_items():
    """Get menu items for linking to recipes"""
    location = request.args.get('location')
    conn = get_connection()

    query = "SELECT guid, name, menu_name, menu_group_name, price, location FROM menu_items"
    params = []

    if location:
        query += " WHERE location = ?"
        params.append(location)

    query += " ORDER BY name"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ============================================
# INVENTORY COUNTS (batch)
# ============================================

@inventory_bp.route('/counts', methods=['POST'])
def create_count_session():
    """Create a new count session"""
    data = request.json
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO inventory_counts (location, count_date, status, notes, created_by)
        VALUES (?, date('now'), 'in_progress', ?, ?)
    """, (
        data.get('location'),
        data.get('notes', ''),
        data.get('created_by', 'admin')
    ))
    count_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': count_id, 'message': 'Count session created'}), 201


@inventory_bp.route('/counts/batch', methods=['POST'])
def save_batch_count():
    """Save a batch of inventory counts"""
    data = request.json
    count_id = data.get('count_id')
    location = data.get('location')
    items = data.get('items', [])

    if not location or not items:
        return jsonify({'error': 'location and items required'}), 400

    conn = get_connection()

    for item in items:
        pid = item['product_id']
        qty = item['quantity']
        unit = item.get('unit', 'ea')

        # Get previous quantity for variance
        prev = conn.execute(
            "SELECT quantity FROM inventory WHERE product_id = ? AND location = ?",
            (pid, location)
        ).fetchone()
        prev_qty = prev['quantity'] if prev else None
        variance = (qty - prev_qty) if prev_qty is not None else None

        # Save count item if we have a session
        if count_id:
            conn.execute("""
                INSERT INTO inventory_count_items
                (count_id, product_id, expected_quantity, counted_quantity, variance, unit)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (count_id, pid, prev_qty, qty, variance, unit))

        # Update current inventory (replace, not add)
        conn.execute("""
            INSERT INTO inventory (product_id, location, quantity, unit, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(product_id, location) DO UPDATE SET
                quantity = ?,
                unit = ?,
                updated_at = CURRENT_TIMESTAMP
        """, (pid, location, qty, unit, qty, unit))

        # Log movement
        conn.execute("""
            INSERT INTO inventory_movements
            (product_id, location, movement_type, quantity, unit, notes, created_at)
            VALUES (?, ?, 'COUNT', ?, ?, ?, CURRENT_TIMESTAMP)
        """, (pid, location, qty, unit,
              'Variance: ' + str(round(variance, 2)) if variance is not None else 'Initial count'))

    # Mark session complete
    if count_id:
        conn.execute("""
            UPDATE inventory_counts
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (count_id,))

    conn.commit()
    conn.close()

    return jsonify({'message': f'{len(items)} items counted', 'count_id': count_id})


@inventory_bp.route('/counts/history', methods=['GET'])
def get_count_history():
    """Get recent count sessions with item counts"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT c.*,
            (SELECT COUNT(*) FROM inventory_count_items WHERE count_id = c.id) as item_count
        FROM inventory_counts c
        ORDER BY c.created_at DESC
        LIMIT 20
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@inventory_bp.route('/counts/<int:count_id>', methods=['GET'])
def get_count_detail(count_id):
    """Get count session with all items"""
    conn = get_connection()
    count = conn.execute("SELECT * FROM inventory_counts WHERE id = ?", (count_id,)).fetchone()
    if not count:
        conn.close()
        return jsonify({'error': 'Count not found'}), 404

    items = conn.execute("""
        SELECT ci.*, p.name as product_name, p.category, p.inventory_unit
        FROM inventory_count_items ci
        JOIN products p ON ci.product_id = p.id
        WHERE ci.count_id = ?
        ORDER BY p.category, p.name
    """, (count_id,)).fetchall()

    conn.close()
    result = dict(count)
    result['items'] = [dict(i) for i in items]
    return jsonify(result)


@inventory_bp.route('/products/<int:product_id>/setup', methods=['POST'])
def update_setup_status(product_id):
    """Mark a product's setup as complete or incomplete"""
    data = request.json
    conn = get_connection()
    conn.execute("UPDATE products SET setup_complete = ? WHERE id = ?", 
                 (1 if data.get('complete') else 0, product_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Updated', 'setup_complete': data.get('complete')})


@inventory_bp.route('/products/setup-status', methods=['GET'])
def get_setup_status():
    """Get setup status for all products"""
    location = request.args.get('location')
    conn = get_connection()
    if location:
        products = conn.execute("""
            SELECT p.id, p.name, p.category, p.unit, p.inventory_unit, p.setup_complete,
                   COUNT(psl.id) as storage_count
            FROM products p
            LEFT JOIN product_storage_locations psl ON psl.product_id = p.id
            LEFT JOIN storage_locations sl ON sl.id = psl.storage_location_id
            WHERE p.active = 1 AND (sl.location = ? OR psl.id IS NULL)
            GROUP BY p.id
            ORDER BY p.setup_complete ASC, p.category, p.name
        """, (location,)).fetchall()
    else:
        products = conn.execute("""
            SELECT p.id, p.name, p.category, p.unit, p.inventory_unit, p.setup_complete,
                   COUNT(psl.id) as storage_count
            FROM products p
            LEFT JOIN product_storage_locations psl ON psl.product_id = p.id
            WHERE p.active = 1
            GROUP BY p.id
            ORDER BY p.setup_complete ASC, p.category, p.name
        """).fetchall()
    conn.close()
    
    result = []
    for p in products:
        needs = []
        if not p['unit'] or p['unit'] in ('', 'Other'):
            needs.append('purchase_unit')
        if not p['inventory_unit'] or p['inventory_unit'] in ('', 'each'):
            needs.append('inventory_unit')
        if p['storage_count'] == 0:
            needs.append('storage')
        
        result.append({
            **dict(p),
            'needs_setup': needs,
            'is_ready': len(needs) == 0
        })
    
    return jsonify(result)


# ============================================
# COUNT SESSIONS
# ============================================

@inventory_bp.route('/count/sheet', methods=['GET'])
@login_required
def get_count_sheet():
    """Get the count sheet template - products organized by storage area and section."""
    location = request.args.get('location', 'dennis')
    conn = get_connection()
    
    # Get storage areas for this location
    areas = conn.execute("""
        SELECT sl.id, sl.name
        FROM storage_locations sl
        WHERE sl.location = ?
        ORDER BY sl.name
    """, (location,)).fetchall()
    
    result = []
    for area in areas:
        # Get sections for this area
        sections = conn.execute("""
            SELECT id, name, sort_order FROM storage_sections
            WHERE storage_location_id = ?
            ORDER BY sort_order, name
        """, (area['id'],)).fetchall()
        
        # Get products in this area, grouped by section
        products = conn.execute("""
            SELECT p.id, p.name, p.category, p.inventory_unit, p.unit,
                   psl.sort_order, psl.section_id, psl.id as psl_id
            FROM product_storage_locations psl
            JOIN products p ON p.id = psl.product_id
            WHERE psl.storage_location_id = ? AND p.active = 1
            ORDER BY psl.section_id NULLS FIRST, psl.sort_order, p.name
        """, (area['id'],)).fetchall()
        
        result.append({
            'id': area['id'],
            'name': area['name'],
            'sections': [dict(s) for s in sections],
            'products': [dict(p) for p in products]
        })
    
    conn.close()
    return jsonify(result)

@inventory_bp.route('/count/sections', methods=['POST'])
@login_required
def add_section():
    """Add a sub-section to a storage area."""
    data = request.json
    conn = get_connection()
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM storage_sections WHERE storage_location_id = ?",
        (data['storage_location_id'],)
    ).fetchone()[0]
    cursor = conn.execute(
        "INSERT INTO storage_sections (storage_location_id, name, sort_order) VALUES (?, ?, ?)",
        (data['storage_location_id'], data['name'], max_order + 1)
    )
    conn.commit()
    section_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': section_id, 'name': data['name']})

@inventory_bp.route('/count/sections/<int:section_id>', methods=['DELETE'])
@login_required
def delete_section(section_id):
    """Delete a section and unassign products from it."""
    conn = get_connection()
    conn.execute("UPDATE product_storage_locations SET section_id = NULL WHERE section_id = ?", (section_id,))
    conn.execute("DELETE FROM storage_sections WHERE id = ?", (section_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Deleted'})

@inventory_bp.route('/count/assign-section', methods=['POST'])
@login_required
def assign_product_section():
    """Assign a product to a section within its storage area."""
    data = request.json
    conn = get_connection()
    conn.execute(
        "UPDATE product_storage_locations SET section_id = ? WHERE id = ?",
        (data.get('section_id'), data['psl_id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@inventory_bp.route('/count/start', methods=['POST'])
@login_required
def start_count():
    """Start a new count session - locks the current sheet as a snapshot."""
    data = request.json
    location = data.get('location', 'dennis')
    conn = get_connection()
    
    # Check for existing in-progress count
    existing = conn.execute(
        "SELECT id FROM count_sessions WHERE location = ? AND status = 'in_progress'",
        (location,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'A count is already in progress', 'session_id': existing['id']}), 400
    
    # Create session
    cursor = conn.execute(
        "INSERT INTO count_sessions (location, started_by) VALUES (?, ?)",
        (location, 'manager')
    )
    session_id = cursor.lastrowid
    
    # Snapshot current sheet into count_items
    items = conn.execute("""
        SELECT psl.product_id, psl.storage_location_id, psl.section_id, p.inventory_unit
        FROM product_storage_locations psl
        JOIN products p ON p.id = psl.product_id
        JOIN storage_locations sl ON sl.id = psl.storage_location_id
        WHERE sl.location = ? AND p.active = 1
        ORDER BY sl.name, psl.section_id, psl.sort_order
    """, (location,)).fetchall()
    
    for item in items:
        conn.execute("""
            INSERT INTO count_items (session_id, product_id, storage_location_id, section_id, expected_unit)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, item['product_id'], item['storage_location_id'], item['section_id'], item['inventory_unit']))
    
    conn.commit()
    conn.close()
    return jsonify({'session_id': session_id, 'item_count': len(items)})

@inventory_bp.route('/count/session/<int:session_id>', methods=['GET'])
@login_required
def get_count_session(session_id):
    """Get a count session with all items grouped by area and section."""
    conn = get_connection()
    session = conn.execute("SELECT * FROM count_sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        conn.close()
        return jsonify({'error': 'Session not found'}), 404
    
    items = conn.execute("""
        SELECT ci.*, p.name as product_name, p.category, p.inventory_unit,
               sl.name as area_name, ss.name as section_name
        FROM count_items ci
        JOIN products p ON p.id = ci.product_id
        JOIN storage_locations sl ON sl.id = ci.storage_location_id
        LEFT JOIN storage_sections ss ON ss.id = ci.section_id
        WHERE ci.session_id = ?
        ORDER BY sl.name, ss.sort_order, p.name
    """, (session_id,)).fetchall()
    
    conn.close()
    return jsonify({
        'session': dict(session),
        'items': [dict(i) for i in items]
    })

@inventory_bp.route('/count/session/<int:session_id>/item/<int:item_id>', methods=['PUT'])
@login_required
def update_count_item(session_id, item_id):
    """Update a count for a specific item."""
    data = request.json
    conn = get_connection()
    conn.execute("""
        UPDATE count_items SET count_qty = ?, counted_at = CURRENT_TIMESTAMP, counted_by = ?
        WHERE id = ? AND session_id = ?
    """, (data.get('count_qty'), data.get('counted_by', 'manager'), item_id, session_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@inventory_bp.route('/count/session/<int:session_id>/complete', methods=['POST'])
@login_required
def complete_count(session_id):
    """Mark a count session as complete."""
    conn = get_connection()
    conn.execute("""
        UPDATE count_sessions SET status = 'completed', completed_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (session_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@inventory_bp.route('/count/active', methods=['GET'])
@login_required
def get_active_count():
    """Check if there is an active count session for a location."""
    location = request.args.get('location', 'dennis')
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM count_sessions WHERE location = ? AND status = 'in_progress'",
        (location,)
    ).fetchone()
    conn.close()
    if session:
        return jsonify(dict(session))
    return jsonify(None)


@inventory_bp.route('/count/reorder', methods=['POST'])
@login_required
def reorder_count_sheet():
    """Reorder products and reassign sections on the count sheet."""
    data = request.json
    updates = data.get('updates', [])
    conn = get_connection()
    for u in updates:
        conn.execute(
            "UPDATE product_storage_locations SET sort_order = ?, section_id = ? WHERE id = ?",
            (u['sort_order'], u.get('section_id'), u['psl_id'])
        )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'updated': len(updates)})

@inventory_bp.route("/recipes/<int:recipe_id>/ingredients/bulk", methods=["PUT"])
def bulk_save_ingredients(recipe_id):
    """Replace all ingredients for a recipe"""
    data = request.json
    ingredients = data.get('ingredients', [])
    conn = get_connection()
    # Delete all existing ingredients
    conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    # Insert new ones
    for ing in ingredients:
        pid = ing.get('product_id') or 0
        pname = ing.get('product_name', '')
        qty = ing.get('quantity')
        if not pid and not pname: continue
        conn.execute("""
            INSERT INTO recipe_ingredients (recipe_id, product_id, product_name, quantity, unit, notes, yield_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (recipe_id, pid, pname, qty, ing.get('unit',''), ing.get('notes',''), ing.get('yield_pct', 100)))
    conn.commit()
    conn.close()
    return jsonify({"message": "Ingredients saved", "count": len(ingredients)}), 200


# ── Recipe Costing Endpoints (Session 13B) ──

@inventory_bp.route('/recipes/<int:recipe_id>/cost-breakdown', methods=['GET'])
def get_recipe_cost_breakdown(recipe_id):
    """Get full recipe cost breakdown using vendor item prices."""
    from recipe_costing import cost_recipe
    conn = get_connection()
    try:
        result = cost_recipe(recipe_id, conn)
        if not result:
            return jsonify({'error': 'Recipe not found'}), 404
        return jsonify(result)
    finally:
        conn.close()


@inventory_bp.route('/recipes/cost-all', methods=['POST'])
def recalculate_all_recipes():
    """Recalculate costs for all active recipes."""
    from recipe_costing import cost_all_recipes
    conn = get_connection()
    try:
        result = cost_all_recipes(conn)
        return jsonify(result)
    finally:
        conn.close()


@inventory_bp.route('/recipes/<int:recipe_id>/menu-price', methods=['PUT'])
def update_recipe_menu_price(recipe_id):
    """Update menu price and servings, then recalculate cost."""
    from recipe_costing import cost_recipe
    data = request.json or {}
    conn = get_connection()
    try:
        recipe = conn.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if not recipe:
            return jsonify({'error': 'Recipe not found'}), 404

        fields = []
        values = []
        if 'menu_price' in data:
            fields.append('menu_price = ?')
            values.append(float(data['menu_price']))
        if 'servings' in data:
            fields.append('serving_size = ?')
            values.append(float(data['servings']))

        if fields:
            values.append(recipe_id)
            conn.execute(f"UPDATE recipes SET {', '.join(fields)} WHERE id = ?", values)
            conn.commit()

        result = cost_recipe(recipe_id, conn)
        return jsonify(result)
    finally:
        conn.close()


# ============================================
# RECIPE AUTO-POPULATE (Claude API)
# ============================================

@inventory_bp.route('/recipes/auto-populate', methods=['POST'])
@login_required
def auto_populate_recipes():
    """Auto-populate recipe ingredients from menu descriptions via Claude API.
    Only processes recipes with 0 ingredients. Never overwrites existing."""
    import json as _json
    import os
    import time as _time
    import requests as _requests
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    food_only = request.json.get("food_only", True) if request.json else True

    conn = get_connection()
    try:
        # Get canonical products
        products = conn.execute("""
            SELECT id, name, category, recipe_unit
            FROM products WHERE active = 1 ORDER BY name
        """).fetchall()
        products = [dict(p) for p in products]

        # Get recipes needing ingredients
        where_extra = "AND r.category = 'FOOD'" if food_only else ""
        recipes = conn.execute(f"""
            SELECT r.id, r.name, r.description, r.category, r.menu_price
            FROM recipes r
            LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
            WHERE 1=1 {where_extra}
            GROUP BY r.id
            HAVING COUNT(ri.id) = 0
            ORDER BY r.name
        """).fetchall()
        recipes = [dict(r) for r in recipes]

        if not recipes:
            return jsonify({"message": "All recipes already have ingredients",
                            "recipes_processed": 0, "ingredients_added": 0})

        # Build product lookup
        canonical_lookup = {}
        for p in products:
            canonical_lookup[p["name"].lower().strip()] = p["id"]

        product_list = "\n".join(f"- {p['name']}" for p in products)

        total_ingredients = 0
        total_matched = 0
        total_new = 0
        recipes_processed = 0
        api_calls = 0

        for recipe in recipes:
            desc = recipe.get("description") or ""
            name = recipe["name"]
            cat = recipe["category"]

            # Skip single-pour beverages without descriptions
            if cat in ('BEER', 'WINE', 'NA_BEVERAGES') and not desc:
                continue

            desc_part = f"\nDESCRIPTION: {desc}" if desc else ""
            prompt = f"""You are a restaurant recipe parser for a bar & grill called Red Nun.
Given a menu item name (and optional description), identify the likely ingredients.

MENU ITEM: {name}{desc_part}
CATEGORY: {cat}
PRICE: ${recipe.get('menu_price') or 0:.2f}

CANONICAL PRODUCT LIST (match to these when possible):
{product_list}

RULES:
1. Match each ingredient to a product from the list above when possible. Use the EXACT name.
2. If no product matches, set canonical_product_name to null and provide a clean suggested_name.
3. Include ALL components: proteins, bread/buns, cheese, vegetables, sauces, dressings, garnishes.
4. Suggest a recipe_unit: oz, lb, ea, fl_oz, cup, tbsp, tsp, slice, portion.
5. For single-pour beverages (beer, wine, soda), return [].
6. For merchandise or non-food items, return [].

Return ONLY valid JSON array. No markdown, no backticks, no explanation:
[{{"canonical_product_name": "Exact Name or null", "suggested_name": "Clean Name or null", "suggested_unit": "oz", "notes": "brief note"}}]"""

            try:
                resp = _requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01"
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 1000,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=30
                )
                resp.raise_for_status()
                api_calls += 1

                text = ""
                for block in resp.json().get("content", []):
                    if block.get("type") == "text":
                        text += block["text"]
                text = text.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

                ingredients = _json.loads(text)
            except Exception:
                continue

            if not ingredients:
                continue

            for ing in ingredients:
                canon_name = ing.get("canonical_product_name")
                suggested = ing.get("suggested_name")
                unit = ing.get("suggested_unit", "ea")
                notes = ing.get("notes", "")
                product_name = canon_name or suggested or "Unknown"
                product_id = 0

                if canon_name:
                    pid = canonical_lookup.get(canon_name.lower().strip())
                    if pid:
                        product_id = pid
                        total_matched += 1
                    else:
                        total_new += 1
                else:
                    total_new += 1

                try:
                    conn.execute("""
                        INSERT INTO recipe_ingredients
                        (recipe_id, product_id, product_name, quantity, unit, yield_pct, notes)
                        VALUES (?, ?, ?, 0, ?, 100, ?)
                    """, (recipe["id"], product_id, product_name, unit, notes))
                    total_ingredients += 1
                except Exception:
                    pass

            recipes_processed += 1
            conn.commit()

            # Rate limit
            _time.sleep(0.5)

        return jsonify({
            "recipes_processed": recipes_processed,
            "ingredients_added": total_ingredients,
            "matched_canonical": total_matched,
            "new_products": total_new,
            "api_calls": api_calls,
            "est_cost": round(api_calls * 0.005, 2)
        })
    finally:
        conn.close()


@inventory_bp.route('/recipes/auto-populate/preview', methods=['GET'])
@login_required
def auto_populate_preview():
    """Preview how many recipes would be processed."""
    food_only = request.args.get("food_only", "true").lower() == "true"
    conn = get_connection()
    try:
        where_extra = "AND r.category = 'FOOD'" if food_only else ""
        row = conn.execute(f"""
            SELECT COUNT(DISTINCT r.id) as cnt
            FROM recipes r
            LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
            WHERE 1=1 {where_extra}
            GROUP BY r.id
            HAVING COUNT(ri.id) = 0
        """).fetchall()
        count = len(row)
        return jsonify({
            "recipes_needing_ingredients": count,
            "est_cost": round(count * 0.005, 2),
            "food_only": food_only
        })
    finally:
        conn.close()
