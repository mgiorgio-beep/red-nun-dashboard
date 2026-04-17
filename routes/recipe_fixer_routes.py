"""
Recipe Cost Fixer — routes.

Walks the user through zero-cost recipes one at a time so they can fill in
missing quantities, fix broken product links, and set per-product unit
conversions. All costing is delegated to integrations.recipes.recipe_costing —
this blueprint is UI + persistence only.

Page:      GET /recipes/fixer
API:       /api/recipe-fixer/*
"""

import logging
from flask import Blueprint, request, jsonify, send_from_directory
from routes.auth_routes import login_required
from integrations.toast.data_store import get_connection
from integrations.recipes.recipe_costing import cost_ingredient, cost_recipe

logger = logging.getLogger(__name__)

recipe_fixer_bp = Blueprint("recipe_fixer", __name__)


# ------------------------------------------------------------------
# One-time migration
# ------------------------------------------------------------------

def _ensure_schema():
    """Idempotent: add recipes.needs_research if it doesn't exist."""
    conn = get_connection()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()]
        if "needs_research" not in cols:
            conn.execute("ALTER TABLE recipes ADD COLUMN needs_research INTEGER DEFAULT 0")
            conn.commit()
            logger.info("recipe_fixer: added recipes.needs_research column")
    finally:
        conn.close()


_ensure_schema()


# ------------------------------------------------------------------
# Canonical unit list — keep in sync with recipe_costing.WEIGHT_TO_OZ /
# VOLUME_TO_FLOZ. Anything outside this set requires a per-product conversion.
# ------------------------------------------------------------------

CANONICAL_UNITS = [
    "fl_oz", "oz", "lb", "g", "kg",
    "ml", "l",
    "tsp", "tbsp", "cup", "pt", "qt", "gal",
    "each", "slice", "pinch",
]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _worklist_rows(conn, include_research=False):
    """Zero-cost active recipes, ordered by the brief's rules:
       1. needs_research = 0 first
       2. then by linked/total ingredient ratio DESC
       3. then alphabetical.
    """
    research_filter = "" if include_research else "AND COALESCE(r.needs_research, 0) = 0"
    return conn.execute(f"""
        SELECT
            r.id,
            r.name,
            COALESCE(r.needs_research, 0) AS needs_research,
            COUNT(ri.id) AS total_ings,
            SUM(CASE WHEN p.id IS NOT NULL THEN 1 ELSE 0 END) AS linked_ings
        FROM recipes r
        LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
        LEFT JOIN products p ON p.id = ri.product_id
        WHERE r.active = 1
          AND COALESCE(r.total_cost, 0) = 0
          {research_filter}
        GROUP BY r.id, r.name, r.needs_research
        ORDER BY
            COALESCE(r.needs_research, 0) ASC,
            (CASE WHEN COUNT(ri.id) > 0
                  THEN CAST(SUM(CASE WHEN p.id IS NOT NULL THEN 1 ELSE 0 END) AS REAL)
                       / COUNT(ri.id)
                  ELSE 0 END) DESC,
            r.name COLLATE NOCASE ASC
    """).fetchall()


def _load_ingredient_row(conn, ingredient_id):
    """Return a single ingredient joined with product + active vendor item,
    ready to feed into cost_ingredient()."""
    return conn.execute("""
        SELECT ri.id, ri.recipe_id, ri.product_id, ri.product_name,
               ri.quantity, ri.unit, ri.yield_pct,
               p.name          AS p_name,
               p.display_name  AS p_display_name,
               p.current_price AS p_current_price,
               p.unit          AS p_unit,
               p.pack_size     AS p_pack_size,
               p.pack_unit     AS p_pack_unit,
               p.inventory_unit AS p_inventory_unit,
               p.active_vendor_item_id AS p_active_vi_id,
               p.yield_pct     AS product_yield_pct,
               vi.purchase_price AS vi_purchase_price,
               vi.price_per_unit AS vi_price_per_unit,
               vi.pack_contains  AS vi_pack_contains,
               vi.contains_unit  AS vi_contains_unit,
               vi.pack_size      AS vi_pack_size,
               vi.pack_unit      AS vi_pack_unit
        FROM recipe_ingredients ri
        LEFT JOIN products     p  ON p.id = ri.product_id
        LEFT JOIN vendor_items vi ON vi.id = p.active_vendor_item_id
        WHERE ri.id = ?
    """, (ingredient_id,)).fetchone()


def _ingredient_payload(conn, row):
    """Shape an ingredient row for the client, including live cost + source."""
    d = dict(row)
    product_linked = bool(d["product_id"]) and d["p_name"] is not None
    cost_info = cost_ingredient({
        "product_id": d["product_id"],
        "quantity": d["quantity"],
        "unit": d["unit"],
    }, conn)
    return {
        "id": d["id"],
        "recipe_id": d["recipe_id"],
        "product_name_text": d["product_name"],  # autopopulator's captured name
        "quantity": d["quantity"] or 0,
        "unit": d["unit"] or "",
        "yield_pct": d["yield_pct"] or 100,
        "product_id": d["product_id"],
        "product_linked": product_linked,
        "product": ({
            "id": d["product_id"],
            "name": d["p_name"],
            "display_name": d["p_display_name"],
            "current_price": d["p_current_price"],
            "unit": d["p_unit"],
            "pack_size": d["p_pack_size"],
            "pack_unit": d["p_pack_unit"],
            "inventory_unit": d["p_inventory_unit"],
            "active_vendor_item": ({
                "id": d["p_active_vi_id"],
                "purchase_price": d["vi_purchase_price"],
                "price_per_unit": d["vi_price_per_unit"],
                "pack_contains": d["vi_pack_contains"],
                "contains_unit": d["vi_contains_unit"],
                "pack_size": d["vi_pack_size"],
                "pack_unit": d["vi_pack_unit"],
            } if d["p_active_vi_id"] else None),
        } if product_linked else None),
        "line_cost": cost_info["cost"],
        "unit_price": cost_info["unit_price"],
        "cost_source": cost_info["source"],
        "needs_conversion": cost_info["source"] == "no_conversion",
    }


# ------------------------------------------------------------------
# Page
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/recipes/fixer")
@login_required
def recipe_fixer_page():
    return send_from_directory("static", "recipe_fixer.html")


# ------------------------------------------------------------------
# API — worklist
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/api/recipe-fixer/worklist", methods=["GET"])
@login_required
def api_worklist():
    include_research = request.args.get("include_research") == "1"
    conn = get_connection()
    try:
        rows = _worklist_rows(conn, include_research=include_research)
        fixed_count = conn.execute("""
            SELECT COUNT(*) FROM recipes
            WHERE active = 1 AND COALESCE(total_cost, 0) > 0
        """).fetchone()[0]
        total_zero = conn.execute("""
            SELECT COUNT(*) FROM recipes
            WHERE active = 1 AND COALESCE(total_cost, 0) = 0
        """).fetchone()[0]
        needs_research_count = conn.execute("""
            SELECT COUNT(*) FROM recipes
            WHERE active = 1 AND COALESCE(total_cost, 0) = 0
              AND COALESCE(needs_research, 0) = 1
        """).fetchone()[0]
        queue = [{
            "id": r["id"],
            "name": r["name"],
            "linked_ings": r["linked_ings"],
            "total_ings": r["total_ings"],
            "needs_research": bool(r["needs_research"]),
        } for r in rows]
        next_id = queue[0]["id"] if queue else None
        return jsonify({
            "fixed": fixed_count,
            "total_zero": total_zero,
            "remaining": len(queue),
            "needs_research": needs_research_count,
            "next_recipe_id": next_id,
            "queue": queue,
        })
    finally:
        conn.close()


# ------------------------------------------------------------------
# API — recipe detail
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/api/recipe-fixer/recipe/<int:recipe_id>", methods=["GET"])
@login_required
def api_recipe(recipe_id):
    conn = get_connection()
    try:
        recipe = conn.execute("""
            SELECT id, name, category, location, menu_price,
                   serving_size, serving_unit,
                   yield_qty, yield_unit,
                   total_cost, cost_per_serving, food_cost_pct,
                   COALESCE(needs_research, 0) AS needs_research,
                   last_costed_at
            FROM recipes WHERE id = ?
        """, (recipe_id,)).fetchone()
        if not recipe:
            return jsonify({"error": "Recipe not found"}), 404

        ing_rows = conn.execute("""
            SELECT id FROM recipe_ingredients WHERE recipe_id = ? ORDER BY id
        """, (recipe_id,)).fetchall()
        ingredients = []
        for ir in ing_rows:
            row = _load_ingredient_row(conn, ir["id"])
            if row:
                ingredients.append(_ingredient_payload(conn, row))

        return jsonify({
            "recipe": dict(recipe),
            "ingredients": ingredients,
            "canonical_units": CANONICAL_UNITS,
        })
    finally:
        conn.close()


# ------------------------------------------------------------------
# API — product search (for inline picker)
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/api/recipe-fixer/product-search", methods=["GET"])
@login_required
def api_product_search():
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    like_any = f"%{q}%"
    like_pre = f"{q}%"
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT id, name, display_name, current_price,
                   active_vendor_item_id, pack_unit, inventory_unit, unit
            FROM products
            WHERE active = 1
              AND (LOWER(name) LIKE ? OR LOWER(COALESCE(display_name, '')) LIKE ?)
            ORDER BY
                CASE
                    WHEN LOWER(name) = ? OR LOWER(COALESCE(display_name, '')) = ? THEN 0
                    WHEN LOWER(name) LIKE ? OR LOWER(COALESCE(display_name, '')) LIKE ? THEN 1
                    ELSE 2
                END,
                name COLLATE NOCASE
            LIMIT 10
        """, (like_any, like_any, q, q, like_pre, like_pre)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ------------------------------------------------------------------
# API — update single ingredient (quantity / unit / product_id)
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/api/recipe-fixer/ingredient/<int:ingredient_id>",
                      methods=["POST"])
@login_required
def api_update_ingredient(ingredient_id):
    data = request.get_json(silent=True) or {}
    fields, params = [], []
    if "quantity" in data:
        try:
            fields.append("quantity = ?"); params.append(float(data["quantity"] or 0))
        except (TypeError, ValueError):
            return jsonify({"error": "quantity must be numeric"}), 400
    if "unit" in data:
        fields.append("unit = ?"); params.append((data["unit"] or "").strip())
    if "product_id" in data:
        pid = data["product_id"]
        if pid in (None, "", 0, "0"):
            fields.append("product_id = ?"); params.append(0)
        else:
            try:
                fields.append("product_id = ?"); params.append(int(pid))
            except (TypeError, ValueError):
                return jsonify({"error": "product_id must be an integer"}), 400

    if not fields:
        return jsonify({"error": "nothing to update"}), 400

    params.append(ingredient_id)
    conn = get_connection()
    try:
        cur = conn.execute(
            f"UPDATE recipe_ingredients SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        if cur.rowcount == 0:
            return jsonify({"error": "ingredient not found"}), 404
        conn.commit()
        row = _load_ingredient_row(conn, ingredient_id)
        return jsonify(_ingredient_payload(conn, row))
    finally:
        conn.close()


# ------------------------------------------------------------------
# API — recost a whole recipe (persists totals)
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/api/recipe-fixer/recipe/<int:recipe_id>/recost",
                      methods=["POST"])
@login_required
def api_recost(recipe_id):
    conn = get_connection()
    try:
        result = cost_recipe(recipe_id, conn)
        if result is None:
            return jsonify({"error": "Recipe not found"}), 404
        return jsonify({
            "recipe_id": recipe_id,
            "total_cost": result["total_cost"],
            "cost_per_serving": result["cost_per_serving"],
            "food_cost_pct": result["food_cost_pct"],
            "ingredients": result["ingredients"],
        })
    finally:
        conn.close()


# ------------------------------------------------------------------
# API — mark / unmark needs_research
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/api/recipe-fixer/recipe/<int:recipe_id>/skip",
                      methods=["POST"])
@login_required
def api_skip(recipe_id):
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE recipes SET needs_research = 1 WHERE id = ?", (recipe_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Recipe not found"}), 404
        return jsonify({"ok": True, "needs_research": True})
    finally:
        conn.close()


@recipe_fixer_bp.route("/api/recipe-fixer/recipe/<int:recipe_id>/unskip",
                      methods=["POST"])
@login_required
def api_unskip(recipe_id):
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE recipes SET needs_research = 0 WHERE id = ?", (recipe_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Recipe not found"}), 404
        return jsonify({"ok": True, "needs_research": False})
    finally:
        conn.close()


# ------------------------------------------------------------------
# API — per-product unit conversions (addendum)
# ------------------------------------------------------------------

@recipe_fixer_bp.route("/api/recipe-fixer/conversion", methods=["GET"])
@login_required
def api_get_conversion():
    try:
        product_id = int(request.args.get("product_id", ""))
    except ValueError:
        return jsonify({"error": "product_id required"}), 400
    from_unit = (request.args.get("from_unit") or "").strip().lower()
    if not from_unit:
        return jsonify({"error": "from_unit required"}), 400
    conn = get_connection()
    try:
        prod = conn.execute("""
            SELECT id, name, display_name, unit, pack_size, pack_unit,
                   inventory_unit, recipe_unit, active_vendor_item_id
            FROM products WHERE id = ?
        """, (product_id,)).fetchone()
        if not prod:
            return jsonify({"error": "product not found"}), 404
        vi = conn.execute("""
            SELECT id, purchase_price, pack_size, pack_unit,
                   pack_contains, contains_unit
            FROM vendor_items WHERE id = ?
        """, (prod["active_vendor_item_id"],)).fetchone() if prod["active_vendor_item_id"] else None
        existing = conn.execute("""
            SELECT id, from_qty, from_unit, to_qty, to_unit, source
            FROM product_unit_conversions
            WHERE product_id = ? AND LOWER(from_unit) = ?
        """, (product_id, from_unit)).fetchone()
        return jsonify({
            "product": dict(prod),
            "active_vendor_item": dict(vi) if vi else None,
            "conversion": dict(existing) if existing else None,
            "canonical_units": CANONICAL_UNITS,
        })
    finally:
        conn.close()


@recipe_fixer_bp.route("/api/recipe-fixer/conversion", methods=["POST"])
@login_required
def api_save_conversion():
    data = request.get_json(silent=True) or {}
    try:
        product_id = int(data["product_id"])
        from_qty = float(data.get("from_qty") or 1)
        to_qty = float(data["to_qty"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "product_id, to_qty required numerically"}), 400
    from_unit = (data.get("from_unit") or "").strip().lower()
    to_unit = (data.get("to_unit") or "").strip().lower()
    if not from_unit or not to_unit:
        return jsonify({"error": "from_unit and to_unit required"}), 400
    if from_qty <= 0 or to_qty <= 0:
        return jsonify({"error": "quantities must be positive"}), 400

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO product_unit_conversions
                (product_id, from_qty, from_unit, to_qty, to_unit, source)
            VALUES (?, ?, ?, ?, ?, 'manual_fixer_ui')
            ON CONFLICT(product_id, from_unit) DO UPDATE SET
                from_qty = excluded.from_qty,
                to_qty   = excluded.to_qty,
                to_unit  = excluded.to_unit,
                source   = 'manual_fixer_ui'
        """, (product_id, from_qty, from_unit, to_qty, to_unit))
        conn.commit()
        saved = conn.execute("""
            SELECT id, product_id, from_qty, from_unit, to_qty, to_unit, source
            FROM product_unit_conversions
            WHERE product_id = ? AND LOWER(from_unit) = ?
        """, (product_id, from_unit)).fetchone()
        return jsonify(dict(saved))
    finally:
        conn.close()


@recipe_fixer_bp.route("/api/recipe-fixer/conversion/<int:conv_id>",
                      methods=["DELETE"])
@login_required
def api_delete_conversion(conv_id):
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM product_unit_conversions WHERE id = ?", (conv_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "conversion not found"}), 404
        return jsonify({"ok": True})
    finally:
        conn.close()
