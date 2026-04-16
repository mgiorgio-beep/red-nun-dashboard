"""
Sales Journal API Routes
Blueprint mounted at /api/sales-journal/* and page route /sales-journal
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Blueprint, jsonify, request, send_from_directory, Response

from integrations.toast.data_store import get_connection
from reports.sales_journal import (
    build_journal_entry,
    persist_journal_entry,
    push_to_qbo,
    export_entries_csv,
    init_sales_journal_tables,
    _make_je_name,
)

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

daily_sales_bp = Blueprint("sales_journal", __name__)

LOCATION_NAMES = {"dennis": "Dennis Port", "chatham": "Chatham"}
PAGE_SIZE = 30


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/sales-journal")
@daily_sales_bp.route("/sales-journal/<int:entry_id>")
def sales_journal_page(entry_id=None):
    from flask import send_from_directory, current_app, make_response
    resp = make_response(send_from_directory(current_app.static_folder, "sales_journal.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------
# Last cron run
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/last-run")
def api_last_run():
    conn = get_connection()
    row = conn.execute("""
        SELECT run_at FROM qb_cron_log ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()
    return jsonify({"last_run": row["run_at"] if row else None})


# ---------------------------------------------------------------------------
# List entries
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/entries")
def api_list_entries():
    location = request.args.get("location", "dennis")
    start = request.args.get("start")
    end = request.args.get("end")
    status_filter = request.args.get("status")      # posted/ready/needs_attention/error/pending
    queue = request.args.get("queue", "false")       # "true" = export queue (non-posted only)
    search = request.args.get("search", "").strip()
    page = int(request.args.get("page", 1))
    entry_type = request.args.get("entry_type", "sales_journal")

    # Default date range: current period (current month)
    now_et = datetime.now(ET)
    if not start:
        start = now_et.replace(day=1).strftime("%Y-%m-%d")
    if not end:
        end = now_et.strftime("%Y-%m-%d")

    conn = get_connection()

    clauses = [
        "location = ?", "entry_date >= ?", "entry_date <= ?", "entry_type = ?"
    ]
    params = [location, start, end, entry_type]

    if queue == "true":
        clauses.append("status IN ('error','needs_attention')")

    if status_filter:
        clauses.append("status = ?")
        params.append(status_filter)

    where_sql = " AND ".join(clauses)

    # Total count (for pagination)
    count_row = conn.execute(
        f"SELECT COUNT(*) as n FROM qb_journal_entries WHERE {where_sql}", params
    ).fetchone()
    total = count_row["n"] if count_row else 0

    # Paginated rows — join orders for actual net sales (SUM of net_amount)
    offset = (page - 1) * PAGE_SIZE
    rows = conn.execute(f"""
        SELECT e.id, e.entry_date, e.je_name, e.balanced, e.total_debits, e.total_credits,
               e.status, e.last_sync_attempt, e.qbo_txn_id,
               COALESCE((SELECT SUM(o.net_amount) FROM orders o
                         WHERE o.location = e.location
                           AND o.business_date = REPLACE(e.entry_date, '-', '')), 0) AS net_sales
        FROM qb_journal_entries e
        WHERE {where_sql}
        ORDER BY e.entry_date DESC
        LIMIT ? OFFSET ?
    """, params + [PAGE_SIZE, offset]).fetchall()

    # Filter by search (in-memory — small result sets)
    entries = [dict(r) for r in rows]
    if search:
        sl = search.lower()
        entries = [
            e for e in entries
            if sl in e["entry_date"]
            or sl in (e["je_name"] or "").lower()
            or sl in (e["status"] or "").lower()
        ]

    # Queue badge counts (outside date range)
    queue_counts = conn.execute("""
        SELECT status, COUNT(*) as n
        FROM qb_journal_entries
        WHERE location=? AND entry_type=? AND status IN ('error','needs_attention')
        GROUP BY status
    """, (location, entry_type)).fetchall()
    badge = {r["status"]: r["n"] for r in queue_counts}

    conn.close()

    return jsonify({
        "entries": entries,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "start": start,
        "end": end,
        "queue_badge": badge,
        "queue_total": sum(badge.values()),
    })


# ---------------------------------------------------------------------------
# Single entry
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/entries/<int:entry_id>")
def api_get_entry(entry_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM qb_journal_entries WHERE id=?
    """, (entry_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    items = conn.execute("""
        SELECT * FROM qb_journal_line_items
        WHERE entry_id=? ORDER BY sort_order
    """, (entry_id,)).fetchall()
    conn.close()

    entry = dict(row)
    entry["balanced"] = bool(entry["balanced"])
    entry["line_items"] = [dict(i) for i in items]
    for li in entry["line_items"]:
        li["mapped"] = bool(li["mapped"])
    return jsonify(entry)


# ---------------------------------------------------------------------------
# Update entry (manual edits in UI)
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/entries/<int:entry_id>", methods=["PUT"])
def api_update_entry(entry_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    conn = get_connection()
    entry_row = conn.execute(
        "SELECT * FROM qb_journal_entries WHERE id=?", (entry_id,)
    ).fetchone()
    if not entry_row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    location = entry_row["location"]
    line_items = data.get("line_items", [])

    # Persist updated line items and save mappings for future entries
    conn.execute("DELETE FROM qb_journal_line_items WHERE entry_id=?", (entry_id,))
    total_debits = 0
    total_credits = 0
    any_unmapped = False

    for idx, li in enumerate(line_items):
        qbo_acc = li.get("qbo_account")
        is_mapped = bool(qbo_acc)
        debit = li.get("debit") or None
        credit = li.get("credit") or None
        if debit: total_debits += float(debit)
        if credit: total_credits += float(credit)
        if not is_mapped: any_unmapped = True

        conn.execute("""
            INSERT INTO qb_journal_line_items
                (entry_id, journal_name, qbo_account, memo, debit, credit, mapped, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry_id, li["journal_name"], qbo_acc,
            li.get("memo", ""), debit, credit, 1 if is_mapped else 0, idx,
        ))

        # Persist mapping for future entries
        if is_mapped:
            conn.execute("""
                INSERT INTO qb_line_mapping (location, journal_name, qbo_account)
                VALUES (?, ?, ?)
                ON CONFLICT(location, journal_name) DO UPDATE SET qbo_account=excluded.qbo_account
            """, (location, li["journal_name"], qbo_acc))

    balanced = abs(total_debits - total_credits) < 0.02
    new_status = "needs_attention" if (any_unmapped or not balanced) else "ready"
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    conn.execute("""
        UPDATE qb_journal_entries
        SET total_debits=?, total_credits=?, balanced=?, status=?, updated_at=?
        WHERE id=?
    """, (
        round(total_debits, 2), round(total_credits, 2),
        1 if balanced else 0, new_status, now, entry_id,
    ))
    conn.commit()
    conn.close()

    return jsonify({
        "id": entry_id,
        "total_debits": round(total_debits, 2),
        "total_credits": round(total_credits, 2),
        "balanced": balanced,
        "status": new_status,
    })


# ---------------------------------------------------------------------------
# Push to QBO
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/entries/<int:entry_id>/fix-mapping", methods=["POST"])
def api_fix_mapping(entry_id):
    """Save a mapping for one unmapped journal_name and update this entry's line items."""
    data = request.get_json() or {}
    journal_name = (data.get("journal_name") or "").strip()
    qbo_account  = (data.get("qbo_account")  or "").strip()
    if not journal_name or not qbo_account:
        return jsonify({"error": "journal_name and qbo_account required"}), 400

    conn = get_connection()
    try:
        # Get entry location
        entry = conn.execute(
            "SELECT location FROM qb_journal_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if not entry:
            return jsonify({"error": "Entry not found"}), 404
        location = entry["location"]

        # Save to permanent mapping
        conn.execute("""
            INSERT INTO qb_line_mapping (location, journal_name, qbo_account)
            VALUES (?, ?, ?)
            ON CONFLICT(location, journal_name) DO UPDATE SET qbo_account=excluded.qbo_account
        """, (location, journal_name, qbo_account))

        # Update all line items with this journal_name in this entry
        conn.execute("""
            UPDATE qb_journal_line_items SET qbo_account=?, mapped=1
            WHERE entry_id=? AND journal_name=?
        """, (qbo_account, entry_id, journal_name))

        # Re-evaluate entry status
        unmapped = conn.execute("""
            SELECT COUNT(*) as n FROM qb_journal_line_items WHERE entry_id=? AND mapped=0
        """, (entry_id,)).fetchone()["n"]
        e = conn.execute("SELECT balanced FROM qb_journal_entries WHERE id=?", (entry_id,)).fetchone()
        new_status = "ready" if (unmapped == 0 and e["balanced"]) else "needs_attention"
        conn.execute(
            "UPDATE qb_journal_entries SET status=?, updated_at=datetime('now') WHERE id=?",
            (new_status, entry_id)
        )
        conn.commit()
        return jsonify({"success": True, "status": new_status, "remaining_unmapped": unmapped})
    finally:
        conn.close()


# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/entries/<int:entry_id>/push", methods=["POST"])
def api_push_entry(entry_id):
    result = push_to_qbo(entry_id)

    conn = get_connection()
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    if result["success"]:
        conn.execute("""
            UPDATE qb_journal_entries
            SET status='posted', qbo_txn_id=?, qbo_error=NULL,
                last_sync_attempt=?, updated_at=?
            WHERE id=?
        """, (result.get("txn_id"), now, now, entry_id))
    else:
        conn.execute("""
            UPDATE qb_journal_entries
            SET status='error', qbo_error=?, last_sync_attempt=?, updated_at=?
            WHERE id=?
        """, (result.get("error"), now, now, entry_id))

    conn.commit()
    conn.close()

    status_code = 200 if result["success"] else 400
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# Delete entry
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/entries/<int:entry_id>", methods=["DELETE"])
def api_delete_entry(entry_id):
    conn = get_connection()
    conn.execute("DELETE FROM qb_journal_entries WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Bulk push (Export Queue)
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/entries/bulk-push", methods=["POST"])
def api_bulk_push():
    data = request.get_json() or {}
    entry_ids = data.get("ids", [])
    results = []
    for eid in entry_ids:
        r = push_to_qbo(eid)
        r["id"] = eid
        results.append(r)
    return jsonify(results)


# ---------------------------------------------------------------------------
# Generate entry on demand (manual trigger for a specific date)
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/generate", methods=["POST"])
def api_generate_entry():
    data = request.get_json() or {}
    location = data.get("location", "dennis")
    entry_date = data.get("entry_date")

    if not entry_date:
        yesterday = (datetime.now(ET) - timedelta(days=1)).strftime("%Y-%m-%d")
        entry_date = yesterday

    try:
        entry = build_journal_entry(location, entry_date)
        entry_id = persist_journal_entry(entry)
        entry["id"] = entry_id
        return jsonify(entry)
    except Exception as e:
        logger.error(f"generate_entry error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/export")
def api_export_csv():
    location = request.args.get("location", "dennis")
    now_et = datetime.now(ET)
    start = request.args.get("start", now_et.replace(day=1).strftime("%Y-%m-%d"))
    end = request.args.get("end", now_et.strftime("%Y-%m-%d"))

    loc_label = "Dennis" if location == "dennis" else "Chatham"
    filename = f"SalesJournal_{loc_label}_{start}_to_{end}.csv"
    csv_data = export_entries_csv(location, start, end)

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# QBO chart of accounts (for account dropdown in edit mode)
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/accounts")
def api_accounts():
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, name, account_type
        FROM qb_accounts
        ORDER BY account_type, name
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Queue stale-items banner count (entries outside current date filter)
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/api/sales-journal/stale-count")
def api_stale_count():
    location = request.args.get("location", "dennis")
    start = request.args.get("start")
    end = request.args.get("end")

    if not start or not end:
        return jsonify({"ready": 0, "needs_attention": 0, "error": 0})

    conn = get_connection()
    rows = conn.execute("""
        SELECT status, COUNT(*) as n
        FROM qb_journal_entries
        WHERE location=? AND entry_type='sales_journal'
          AND status IN ('needs_attention', 'error')
          AND (entry_date < ? OR entry_date > ?)
        GROUP BY status
    """, (location, start, end)).fetchall()
    conn.close()

    counts = {r["status"]: r["n"] for r in rows}
    return jsonify({
        "ready": 0,
        "needs_attention": counts.get("needs_attention", 0),
        "error": counts.get("error", 0),
        "total": sum(counts.values()),
    })


# ---------------------------------------------------------------------------
# Sales Mapping page
# ---------------------------------------------------------------------------

@daily_sales_bp.route("/sales-mapping")
def sales_mapping_page():
    from flask import send_from_directory, current_app, make_response
    resp = make_response(send_from_directory(current_app.static_folder, "sales_mapping.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


CANONICAL_LINES = [
    {"journal_name": "Gross Sales: Beer",        "side": "credit", "hint": "e.g. Beer= Sales"},
    {"journal_name": "Gross Sales: Wine",         "side": "credit", "hint": "e.g. Wine- Sales"},
    {"journal_name": "Gross Sales: Liquor",       "side": "credit", "hint": "e.g. Liquor- Sales"},
    {"journal_name": "Gross Sales: NA Beverage",  "side": "credit", "hint": "e.g. NA Beverages- Sales"},
    {"journal_name": "Gross Sales: Food",         "side": "credit", "hint": "e.g. Food Sales"},
    {"journal_name": "Summary: Tax",              "side": "credit", "hint": "e.g. Sales Tax Payable"},
    {"journal_name": "Summary: Tips",             "side": "credit", "hint": "e.g. Payroll Tips"},
    {"journal_name": "Tenders: Cash",             "side": "debit",  "hint": "e.g. Cash Sales"},
    {"journal_name": "Tenders: Visa",             "side": "debit",  "hint": "e.g. Credit Card Sales"},
    {"journal_name": "Tenders: Mastercard",       "side": "debit",  "hint": "e.g. Credit Card Sales"},
    {"journal_name": "Tenders: Amex",             "side": "debit",  "hint": "e.g. Credit Card Sales"},
    {"journal_name": "Tenders: Discover",         "side": "debit",  "hint": "e.g. Credit Card Sales"},
    {"journal_name": "Tenders: Credit",           "side": "debit",  "hint": "e.g. Credit Card Sales (catch-all)"},
    {"journal_name": "Tenders: Gift Card",        "side": "debit",  "hint": "e.g. Gift Certificates"},
    {"journal_name": "Tenders: House Account",    "side": "debit",  "hint": "e.g. House_Account"},
    {"journal_name": "Tenders: Other",            "side": "debit",  "hint": "e.g. Other payments"},
    {"journal_name": "Discounts: Total",          "side": "debit",  "hint": "e.g. Discounts/Refunds Given"},
    {"journal_name": "Summary: Gift Card Sold",  "side": "credit", "hint": "e.g. Gift Certificates (liability when gift card sold)"},
    {"journal_name": "Summary: Other",            "side": "credit", "hint": "e.g. Refunds/Adjustments (payments exceed sales)"},
]


@daily_sales_bp.route("/api/sales-journal/mapping")
def api_get_mapping():
    location = request.args.get("location", "chatham")
    conn = get_connection()
    existing_map = {r["journal_name"]: r["qbo_account"] for r in conn.execute(
        "SELECT journal_name, qbo_account FROM qb_line_mapping WHERE location=?", (location,)
    ).fetchall()}

    # Discover any new journal names from actual entries not in CANONICAL_LINES
    canonical_names = {l["journal_name"] for l in CANONICAL_LINES}
    seen = conn.execute("""
        SELECT DISTINCT li.journal_name, li.debit, li.credit
        FROM qb_journal_line_items li
        JOIN qb_journal_entries e ON e.id = li.entry_id
        WHERE e.location = ?
    """, (location,)).fetchall()
    conn.close()

    dynamic = []
    for row in seen:
        jname = row["journal_name"]
        if jname not in canonical_names:
            side = "debit" if row["debit"] else "credit"
            dynamic.append({"journal_name": jname, "side": side, "hint": "auto-discovered from Toast data"})
            canonical_names.add(jname)

    result = []
    for line in CANONICAL_LINES + dynamic:
        result.append({**line, "qbo_account": existing_map.get(line["journal_name"])})
    return jsonify(result)


@daily_sales_bp.route("/api/sales-journal/mapping", methods=["POST"])
def api_save_mapping():
    data = request.get_json() or {}
    location = data.get("location", "chatham")
    mappings = data.get("mappings", [])

    conn = get_connection()
    saved = 0
    for m in mappings:
        jname = m.get("journal_name", "").strip()
        qbo   = m.get("qbo_account", "").strip() if m.get("qbo_account") else None
        if not jname:
            continue
        if qbo:
            conn.execute("""
                INSERT INTO qb_line_mapping (location, journal_name, qbo_account)
                VALUES (?, ?, ?)
                ON CONFLICT(location, journal_name) DO UPDATE SET qbo_account=excluded.qbo_account
            """, (location, jname, qbo))
        else:
            conn.execute(
                "DELETE FROM qb_line_mapping WHERE location=? AND journal_name=?",
                (location, jname)
            )
        saved += 1

    conn.commit()
    conn.close()
    return jsonify({"saved": saved})
