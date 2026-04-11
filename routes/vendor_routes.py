"""
Vendor Item Management Routes — Red Nun Analytics

Endpoints for managing vendor items, reviewing suggestions from
the matching engine, and comparing vendor prices for products.

Blueprint: vendor_bp
Mount: /api/vendor-items/* and /api/products/*/vendor-comparison
"""

import logging
from flask import Blueprint, request, jsonify
from data_store import get_connection
from vendor_item_matcher import create_or_update_vendor_item

logger = logging.getLogger(__name__)

vendor_bp = Blueprint("vendor_items", __name__)


# ── STATIC routes first (before dynamic /<id> routes) ──


@vendor_bp.route("/api/vendor-items/suggestions", methods=["GET"])
def list_suggestions():
    """List pending vendor item suggestions, optionally filtered by invoice_id."""
    conn = get_connection()
    try:
        invoice_id = request.args.get("invoice_id")
        if invoice_id:
            rows = conn.execute("""
                SELECT vis.*, si.vendor_name
                FROM vendor_item_suggestions vis
                LEFT JOIN scanned_invoices si ON vis.invoice_id = si.id
                WHERE vis.status = 'pending' AND vis.invoice_id = ?
                ORDER BY vis.score DESC
            """, (invoice_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT vis.*, si.vendor_name
                FROM vendor_item_suggestions vis
                LEFT JOIN scanned_invoices si ON vis.invoice_id = si.id
                WHERE vis.status = 'pending'
                ORDER BY vis.created_at DESC, vis.score DESC
            """).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@vendor_bp.route("/api/vendor-items/approve-suggestion/<int:suggestion_id>", methods=["POST"])
def approve_suggestion(suggestion_id):
    """
    Approve a suggestion: link the invoice item to the suggested product
    and create/update the vendor_item.
    """
    conn = get_connection()
    try:
        suggestion = conn.execute(
            "SELECT * FROM vendor_item_suggestions WHERE id = ?",
            (suggestion_id,)
        ).fetchone()

        if not suggestion:
            return jsonify({"error": "Suggestion not found"}), 404

        if suggestion["status"] != "pending":
            return jsonify({"error": f"Suggestion already {suggestion['status']}"}), 400

        product_id = suggestion["product_id"]
        invoice_id = suggestion["invoice_id"]
        invoice_item_id = suggestion["invoice_item_id"]

        # Get the invoice item data
        item = None
        if invoice_item_id:
            item = conn.execute(
                "SELECT * FROM scanned_invoice_items WHERE id = ?",
                (invoice_item_id,)
            ).fetchone()

        if not item:
            # Try to find by matching vendor_description to product_name
            item = conn.execute("""
                SELECT * FROM scanned_invoice_items
                WHERE invoice_id = ? AND product_name = ?
                LIMIT 1
            """, (invoice_id, suggestion["vendor_description"])).fetchone()

        if not item:
            return jsonify({"error": "Invoice item not found"}), 404

        # Get vendor name from invoice
        invoice = conn.execute(
            "SELECT vendor_name FROM scanned_invoices WHERE id = ?",
            (invoice_id,)
        ).fetchone()
        vendor_name = invoice["vendor_name"] if invoice else None

        # Create/update vendor item
        item_dict = dict(item)
        vendor_item_id = create_or_update_vendor_item(
            product_id, item_dict, vendor_name, conn
        )

        # Mark suggestion as approved
        conn.execute(
            "UPDATE vendor_item_suggestions SET status = 'approved' WHERE id = ?",
            (suggestion_id,)
        )
        conn.commit()

        return jsonify({
            "status": "ok",
            "vendor_item_id": vendor_item_id,
            "product_id": product_id,
            "message": f"Linked to product #{product_id}",
        })
    except Exception as e:
        logger.error(f"Approve suggestion error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@vendor_bp.route("/api/vendor-items/reject-suggestion/<int:suggestion_id>", methods=["POST"])
def reject_suggestion(suggestion_id):
    """
    Reject a suggestion: create a new product from the invoice item instead
    of linking to the suggested one.
    """
    conn = get_connection()
    try:
        suggestion = conn.execute(
            "SELECT * FROM vendor_item_suggestions WHERE id = ?",
            (suggestion_id,)
        ).fetchone()

        if not suggestion:
            return jsonify({"error": "Suggestion not found"}), 404

        if suggestion["status"] != "pending":
            return jsonify({"error": f"Suggestion already {suggestion['status']}"}), 400

        invoice_id = suggestion["invoice_id"]
        invoice_item_id = suggestion["invoice_item_id"]
        vendor_description = suggestion["vendor_description"]

        # Get the invoice item data
        item = None
        if invoice_item_id:
            item = conn.execute(
                "SELECT * FROM scanned_invoice_items WHERE id = ?",
                (invoice_item_id,)
            ).fetchone()

        if not item:
            item = conn.execute("""
                SELECT * FROM scanned_invoice_items
                WHERE invoice_id = ? AND product_name = ?
                LIMIT 1
            """, (invoice_id, vendor_description)).fetchone()

        # Create new product
        name = vendor_description or "Unknown Product"
        category = "FOOD"
        price = 0
        unit = ""

        if item:
            item_dict = dict(item)
            category = (item_dict.get("category_type") or "FOOD").upper()
            price = item_dict.get("unit_price") or item_dict.get("total_price") or 0
            unit = item_dict.get("unit") or ""

        # Check if product already exists
        existing = conn.execute(
            "SELECT id FROM products WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (name,)
        ).fetchone()

        if existing:
            product_id = existing["id"]
        else:
            conn.execute("""
                INSERT INTO products (name, category, current_price, unit, active, setup_complete)
                VALUES (?, ?, ?, ?, 1, 0)
            """, (name, category, price, unit))
            product_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Create vendor_item for new product
        if item:
            invoice_row = conn.execute(
                "SELECT vendor_name FROM scanned_invoices WHERE id = ?",
                (invoice_id,)
            ).fetchone()
            vendor_name = invoice_row["vendor_name"] if invoice_row else None
            create_or_update_vendor_item(product_id, dict(item), vendor_name, conn)

        # Mark suggestion as rejected (new product created)
        conn.execute(
            "UPDATE vendor_item_suggestions SET status = 'new' WHERE id = ?",
            (suggestion_id,)
        )
        conn.commit()

        return jsonify({
            "status": "ok",
            "product_id": product_id,
            "product_name": name,
            "message": f"New product created: {name}",
        })
    except Exception as e:
        logger.error(f"Reject suggestion error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── DYNAMIC routes (after static ones) ──


@vendor_bp.route("/api/vendor-items/by-product/<int:product_id>", methods=["GET"])
def vendor_items_by_product(product_id):
    """Get all vendor items for a specific product."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT vi.*, COALESCE(v.name, vi.vendor_name) as resolved_vendor_name
            FROM vendor_items vi
            LEFT JOIN vendors v ON vi.vendor_id = v.id
            WHERE vi.product_id = ?
            ORDER BY vi.is_active DESC, vi.last_seen_date DESC
        """, (product_id,)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@vendor_bp.route("/api/vendor-items/<int:vendor_item_id>/set-active", methods=["POST"])
def set_active_vendor_item(vendor_item_id):
    """Change which vendor item is active for a product."""
    conn = get_connection()
    try:
        vi = conn.execute(
            "SELECT * FROM vendor_items WHERE id = ?",
            (vendor_item_id,)
        ).fetchone()

        if not vi:
            return jsonify({"error": "Vendor item not found"}), 404

        product_id = vi["product_id"]

        # Deactivate all others, activate this one
        conn.execute(
            "UPDATE vendor_items SET is_active = 0 WHERE product_id = ?",
            (product_id,)
        )
        conn.execute(
            "UPDATE vendor_items SET is_active = 1 WHERE id = ?",
            (vendor_item_id,)
        )

        # Update product's active_vendor_item_id and current_price
        conn.execute("""
            UPDATE products
            SET active_vendor_item_id = ?, current_price = ?
            WHERE id = ?
        """, (vendor_item_id, vi["purchase_price"], product_id))

        conn.commit()

        return jsonify({
            "status": "ok",
            "message": f"Vendor item #{vendor_item_id} is now active",
            "product_id": product_id,
        })
    except Exception as e:
        logger.error(f"Set active error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@vendor_bp.route("/api/products/<int:product_id>/vendor-comparison", methods=["GET"])
def vendor_comparison(product_id):
    """
    Get all vendors and prices for a product, sorted cheapest first.
    Foundation for price intelligence (Session 35).
    """
    conn = get_connection()
    try:
        product = conn.execute(
            "SELECT id, name, category FROM products WHERE id = ?",
            (product_id,)
        ).fetchone()

        if not product:
            return jsonify({"error": "Product not found"}), 404

        rows = conn.execute("""
            SELECT vi.id, COALESCE(v.name, vi.vendor_name) as vendor_name,
                   vi.vendor_description, vi.purchase_price, vi.price_per_unit,
                   vi.last_seen_date, vi.is_active, vi.pack_size, vi.pack_unit
            FROM vendor_items vi
            LEFT JOIN vendors v ON vi.vendor_id = v.id
            WHERE vi.product_id = ?
            ORDER BY vi.purchase_price ASC
        """, (product_id,)).fetchall()

        return jsonify({
            "product": {
                "id": product["id"],
                "name": product["name"],
                "category": product["category"],
            },
            "vendor_items": [dict(r) for r in rows],
        })
    finally:
        conn.close()
