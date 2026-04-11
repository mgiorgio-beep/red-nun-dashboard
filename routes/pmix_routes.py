from flask import Blueprint, request, jsonify
from data_store import get_connection
from pmix_matcher import suggest_pmix_mappings

pmix_bp = Blueprint('pmix_bp', __name__)


@pmix_bp.route('/api/pmix/menu-items', methods=['GET'])
def get_menu_items():
    """All distinct menu items with mapping info, revenue, and food cost %."""
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT
            oi.item_name,
            SUM(oi.quantity) as total_sold,
            SUM(oi.price) as total_revenue,
            pm.id as mapping_id,
            pm.recipe_id,
            r.name as recipe_name,
            pm.multiplier,
            r.cost_per_serving,
            r.food_cost_pct as recipe_food_cost_pct
        FROM (
            SELECT item_name, SUM(quantity) as quantity, SUM(price) as price
            FROM order_items
            WHERE voided = 0
              AND item_name IS NOT NULL
              AND item_name != ''
            GROUP BY item_name
        ) oi
        LEFT JOIN pmix_mapping pm ON pm.menu_item_name = oi.item_name
        LEFT JOIN recipes r ON pm.recipe_id = r.id
        GROUP BY oi.item_name
        ORDER BY oi.price DESC
    """)

    items = []
    for row in c.fetchall():
        item_name = row[0]
        total_sold = row[1] or 0
        total_revenue = row[2] or 0
        mapping_id = row[3]
        recipe_id = row[4]
        recipe_name = row[5]
        multiplier = row[6] or 1.0
        cost_per_serving = row[7]
        recipe_fcp = row[8]

        # Calculate food cost % for this specific menu item
        food_cost_pct = None
        if recipe_id and cost_per_serving and cost_per_serving > 0 and total_sold > 0:
            avg_price = total_revenue / total_sold
            if avg_price > 0:
                food_cost_pct = round(cost_per_serving * multiplier / avg_price * 100, 1)

        items.append({
            'menu_item_name': item_name,
            'total_sold': total_sold,
            'total_revenue': round(total_revenue, 2),
            'mapping_id': mapping_id,
            'recipe_id': recipe_id,
            'recipe_name': recipe_name,
            'multiplier': multiplier,
            'food_cost_pct': food_cost_pct,
            'has_cost': bool(cost_per_serving and cost_per_serving > 0)
        })

    conn.close()
    return jsonify(items)


@pmix_bp.route('/api/pmix/map', methods=['POST'])
def create_or_update_mapping():
    """Create or update a PMIX mapping."""
    data = request.get_json()
    menu_item_name = data.get('menu_item_name')
    recipe_id = data.get('recipe_id')
    multiplier = data.get('multiplier', 1.0)

    if not menu_item_name:
        return jsonify({'error': 'menu_item_name required'}), 400

    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        INSERT INTO pmix_mapping (menu_item_name, recipe_id, multiplier, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(menu_item_name) DO UPDATE SET
            recipe_id = excluded.recipe_id,
            multiplier = excluded.multiplier,
            updated_at = datetime('now')
    """, (menu_item_name, recipe_id, multiplier))

    mapping_id = c.lastrowid or None
    if not mapping_id:
        c.execute("SELECT id FROM pmix_mapping WHERE menu_item_name = ?", (menu_item_name,))
        row = c.fetchone()
        mapping_id = row[0] if row else None

    conn.commit()
    conn.close()
    return jsonify({'id': mapping_id, 'menu_item_name': menu_item_name,
                    'recipe_id': recipe_id, 'multiplier': multiplier})


@pmix_bp.route('/api/pmix/map/<int:mapping_id>', methods=['DELETE'])
def delete_mapping(mapping_id):
    """Remove a PMIX mapping."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM pmix_mapping WHERE id = ?", (mapping_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted:
        return jsonify({'deleted': True})
    return jsonify({'error': 'Mapping not found'}), 404


@pmix_bp.route('/api/pmix/auto-suggest', methods=['POST'])
def auto_suggest():
    """Run auto-suggest matching engine."""
    result = suggest_pmix_mappings()
    return jsonify(result)


@pmix_bp.route('/api/pmix/report', methods=['GET'])
def theoretical_food_cost_report():
    """Theoretical food cost report for a date range."""
    start = request.args.get('start', '')
    end = request.args.get('end', '')

    if not start or not end:
        return jsonify({'error': 'start and end query params required (YYYYMMDD)'}), 400

    conn = get_connection()
    c = conn.cursor()

    # Mapped items: join order_items -> pmix_mapping -> recipes
    c.execute("""
        SELECT
            oi.item_name,
            SUM(oi.quantity) as qty_sold,
            SUM(oi.price) as revenue,
            r.name as recipe_name,
            r.cost_per_serving,
            pm.multiplier
        FROM order_items oi
        JOIN pmix_mapping pm ON pm.menu_item_name = oi.item_name
        JOIN recipes r ON pm.recipe_id = r.id
        WHERE oi.voided = 0
          AND oi.business_date BETWEEN ? AND ?
        GROUP BY oi.item_name
        ORDER BY SUM(oi.price) DESC
    """, (start, end))

    items = []
    total_revenue = 0
    total_theoretical_cost = 0

    for row in c.fetchall():
        item_name, qty_sold, revenue, recipe_name, cost_per_serving, multiplier = row
        qty_sold = qty_sold or 0
        revenue = revenue or 0
        cost_per_serving = cost_per_serving or 0
        multiplier = multiplier or 1.0

        theoretical_cost = qty_sold * cost_per_serving * multiplier
        food_cost_pct = round(theoretical_cost / revenue * 100, 1) if revenue > 0 else 0

        items.append({
            'menu_item_name': item_name,
            'qty_sold': qty_sold,
            'revenue': round(revenue, 2),
            'recipe_name': recipe_name,
            'cost_per_serving': round(cost_per_serving, 2),
            'multiplier': multiplier,
            'theoretical_cost': round(theoretical_cost, 2),
            'food_cost_pct': food_cost_pct
        })

        total_revenue += revenue
        total_theoretical_cost += theoretical_cost

    # Unmapped items in the date range
    c.execute("""
        SELECT
            oi.item_name,
            SUM(oi.quantity) as qty_sold,
            SUM(oi.price) as revenue
        FROM order_items oi
        LEFT JOIN pmix_mapping pm ON pm.menu_item_name = oi.item_name
        WHERE oi.voided = 0
          AND oi.business_date BETWEEN ? AND ?
          AND pm.id IS NULL
          AND oi.item_name IS NOT NULL
          AND oi.item_name != ''
        GROUP BY oi.item_name
        ORDER BY SUM(oi.price) DESC
    """, (start, end))

    unmapped_items = []
    unmapped_revenue = 0
    for row in c.fetchall():
        item_name, qty_sold, revenue = row
        revenue = revenue or 0
        unmapped_revenue += revenue
        unmapped_items.append({
            'menu_item_name': item_name,
            'qty_sold': qty_sold or 0,
            'revenue': round(revenue, 2)
        })

    # Also add mapped revenue to grand total for unmapped_pct calc
    grand_total_revenue = total_revenue + unmapped_revenue
    overall_food_cost_pct = round(total_theoretical_cost / total_revenue * 100, 1) if total_revenue > 0 else 0
    unmapped_pct = round(unmapped_revenue / grand_total_revenue * 100, 1) if grand_total_revenue > 0 else 0

    conn.close()
    return jsonify({
        'summary': {
            'total_revenue': round(total_revenue, 2),
            'total_theoretical_cost': round(total_theoretical_cost, 2),
            'overall_food_cost_pct': overall_food_cost_pct,
            'unmapped_revenue': round(unmapped_revenue, 2),
            'unmapped_pct': unmapped_pct,
            'grand_total_revenue': round(grand_total_revenue, 2),
            'mapped_items': len(items),
            'unmapped_items': len(unmapped_items)
        },
        'items': items,
        'unmapped_items': unmapped_items
    })


@pmix_bp.route('/api/pmix/menu-item-detail', methods=['GET'])
def menu_item_detail():
    """Ingredient-level cost drill-down for a mapped menu item."""
    item_name = request.args.get('name', '').strip()
    if not item_name:
        return jsonify({'error': 'name param required'}), 400

    conn = get_connection()

    mapping = conn.execute("""
        SELECT pm.recipe_id, pm.multiplier, r.name as recipe_name,
               r.cost_per_serving, r.food_cost_pct, r.menu_price, r.serving_size
        FROM pmix_mapping pm
        JOIN recipes r ON pm.recipe_id = r.id
        WHERE pm.menu_item_name = ?
    """, (item_name,)).fetchone()

    if not mapping:
        conn.close()
        return jsonify({'error': 'No mapping found for this menu item'}), 404

    m = dict(mapping)

    ingredients = conn.execute("""
        SELECT ri.quantity, ri.unit, ri.yield_pct,
               p.name as product_name, p.recipe_unit,
               vi.purchase_price, vi.price_per_unit, vi.pack_contains,
               vi.vendor_name, vi.pack_size
        FROM recipe_ingredients ri
        LEFT JOIN products p ON ri.product_id = p.id
        LEFT JOIN vendor_items vi ON p.active_vendor_item_id = vi.id
        WHERE ri.recipe_id = ?
        ORDER BY p.name
    """, (m['recipe_id'],)).fetchall()

    ing_list = []
    for i in ingredients:
        d = dict(i)
        ppu = d['price_per_unit'] or (d['purchase_price'] / d['pack_contains'] if d['purchase_price'] and d['pack_contains'] else 0)
        line_cost = round(ppu * (d['quantity'] or 0), 4) if ppu else 0
        yield_pct = d['yield_pct'] or 100
        if yield_pct < 100 and yield_pct > 0:
            line_cost = round(line_cost * (100 / yield_pct), 4)
        ing_list.append({
            'product_name': d['product_name'] or 'Unknown',
            'quantity': d['quantity'],
            'unit': d['unit'],
            'price_per_unit': round(ppu, 4) if ppu else None,
            'line_cost': round(line_cost, 2),
            'vendor_name': d['vendor_name'],
            'pack_size': d['pack_size'],
            'has_price': bool(ppu and ppu > 0)
        })

    conn.close()
    return jsonify({
        'menu_item_name': item_name,
        'recipe_name': m['recipe_name'],
        'recipe_id': m['recipe_id'],
        'multiplier': m['multiplier'],
        'cost_per_serving': m['cost_per_serving'],
        'food_cost_pct': m['food_cost_pct'],
        'menu_price': m['menu_price'],
        'ingredients': ing_list,
        'ingredient_count': len(ing_list)
    })


@pmix_bp.route('/api/pmix/recalc-costs', methods=['POST'])
def recalc_all_costs():
    """Recalculate costs for all active recipes."""
    from recipe_costing import cost_all_recipes
    conn = get_connection()
    try:
        result = cost_all_recipes(conn)
        return jsonify(result)
    finally:
        conn.close()
