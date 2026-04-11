"""
Digital Chalkboard — Daily Specials API
Red Nun Analytics

Tables:
  daily_specials — specials shown on the chalkboard TV display

Routes (blueprint: specials_bp):
  GET    /api/specials              — list active specials (or all if ?all=1)
  POST   /api/specials              — create a new special
  PUT    /api/specials/<id>         — update a special
  DELETE /api/specials/<id>         — delete a special
  POST   /api/specials/reorder      — update display_order for multiple specials
"""

import logging
from flask import Blueprint, request, jsonify
from data_store import get_connection

logger = logging.getLogger(__name__)

specials_bp = Blueprint("specials", __name__)

# ──────────────────────────────────────────────────────────────────────────────
# Table init (called from server.py startup)
# ──────────────────────────────────────────────────────────────────────────────

def init_specials_tables():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_specials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT    NOT NULL,
            description   TEXT    DEFAULT '',
            price         TEXT    DEFAULT '',
            category      TEXT    DEFAULT '',
            display_order INTEGER DEFAULT 0,
            active        INTEGER DEFAULT 1,
            created_at    TEXT    DEFAULT (datetime('now')),
            updated_at    TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Daily specials table initialized")


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/specials
# ──────────────────────────────────────────────────────────────────────────────

@specials_bp.route("/api/specials", methods=["GET"])
def get_specials():
    """
    List specials.
    ?all=1 returns inactive ones too (admin view).
    ?active=1 (default) returns only active ones (TV display).
    """
    show_all = request.args.get("all", "0") == "1"
    conn = get_connection()

    if show_all:
        rows = conn.execute(
            "SELECT * FROM daily_specials ORDER BY display_order ASC, id ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM daily_specials WHERE active = 1 ORDER BY display_order ASC, id ASC"
        ).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/specials
# ──────────────────────────────────────────────────────────────────────────────

@specials_bp.route("/api/specials", methods=["POST"])
def create_special():
    """Create a new special."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    conn = get_connection()

    # Set display_order to max + 1
    max_order = conn.execute(
        "SELECT COALESCE(MAX(display_order), 0) FROM daily_specials"
    ).fetchone()[0]

    cursor = conn.execute(
        """
        INSERT INTO daily_specials (title, description, price, category, display_order, active)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            (data.get("description") or "").strip(),
            (data.get("price") or "").strip(),
            (data.get("category") or "").strip(),
            max_order + 1,
            1 if data.get("active", True) else 0,
        ),
    )
    conn.commit()
    new_id = cursor.lastrowid

    row = conn.execute(
        "SELECT * FROM daily_specials WHERE id = ?", (new_id,)
    ).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


# ──────────────────────────────────────────────────────────────────────────────
# PUT /api/specials/<id>
# ──────────────────────────────────────────────────────────────────────────────

@specials_bp.route("/api/specials/<int:special_id>", methods=["PUT"])
def update_special(special_id):
    """Update a special (title, description, price, category, active)."""
    data = request.get_json(silent=True) or {}

    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM daily_specials WHERE id = ?", (special_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "not found"}), 404

    allowed = ["title", "description", "price", "category", "active", "display_order"]
    fields = []
    values = []
    for key in allowed:
        if key in data:
            val = data[key]
            if key in ("title", "description", "price", "category"):
                val = (val or "").strip()
            fields.append(f"{key} = ?")
            values.append(val)

    if not fields:
        conn.close()
        return jsonify({"error": "no valid fields provided"}), 400

    fields.append("updated_at = datetime('now')")
    values.append(special_id)

    conn.execute(
        f"UPDATE daily_specials SET {', '.join(fields)} WHERE id = ?", values
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM daily_specials WHERE id = ?", (special_id,)
    ).fetchone()
    conn.close()
    return jsonify(dict(row))


# ──────────────────────────────────────────────────────────────────────────────
# DELETE /api/specials/<id>
# ──────────────────────────────────────────────────────────────────────────────

@specials_bp.route("/api/specials/<int:special_id>", methods=["DELETE"])
def delete_special(special_id):
    """Permanently delete a special."""
    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM daily_specials WHERE id = ?", (special_id,)
    ).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "not found"}), 404

    conn.execute("DELETE FROM daily_specials WHERE id = ?", (special_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/specials/reorder
# ──────────────────────────────────────────────────────────────────────────────

@specials_bp.route("/api/specials/reorder", methods=["POST"])
def reorder_specials():
    """
    Update display_order for multiple specials.
    Body: {"order": [id1, id2, id3, ...]}  — ordered list of IDs top to bottom
    """
    data = request.get_json(silent=True) or {}
    order = data.get("order", [])
    if not order:
        return jsonify({"error": "order list is required"}), 400

    conn = get_connection()
    for i, special_id in enumerate(order):
        conn.execute(
            "UPDATE daily_specials SET display_order = ?, updated_at = datetime('now') WHERE id = ?",
            (i, special_id),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})
