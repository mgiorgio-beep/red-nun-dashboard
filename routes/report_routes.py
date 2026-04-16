"""
Reports — exportable CSV/PDF reports for accountants and managers.
Blueprint: report_bp at /api/reports/* and /reports page.
"""

import csv
import io
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request, Response, send_from_directory, make_response
from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required

logger = logging.getLogger(__name__)
report_bp = Blueprint("reports", __name__)
ET = ZoneInfo("America/New_York")

LOC_LABELS = {"dennis": "Dennis Port", "chatham": "Chatham"}


# ------------------------------------------------------------------
# Page route
# ------------------------------------------------------------------

@report_bp.route("/reports")
@login_required
def reports_page():
    from flask import current_app
    resp = make_response(send_from_directory(current_app.static_folder, "reports.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ------------------------------------------------------------------
# Report catalog (tells the UI what's available)
# ------------------------------------------------------------------

REPORT_CATALOG = [
    {
        "key": "daily_sales",
        "title": "Daily Sales Summary",
        "description": "Revenue by category, discounts, tax, tips, and tender breakdown",
        "icon": "revenue",
        "frequency": "Daily",
    },
    {
        "key": "monthly_sales",
        "title": "Monthly Sales Report",
        "description": "Daily totals with category breakdown for the period",
        "icon": "revenue",
        "frequency": "Monthly",
    },
    {
        "key": "sales_tax",
        "title": "Sales Tax Summary",
        "description": "Tax collected by location for filing",
        "icon": "accounting",
        "frequency": "Monthly",
    },
    {
        "key": "ap_aging",
        "title": "AP Aging Report",
        "description": "Outstanding invoices by vendor — current, 31-60, 61-90, 90+ days",
        "icon": "invoices",
        "frequency": "Weekly",
    },
    {
        "key": "vendor_spend",
        "title": "Vendor Spend Report",
        "description": "Total invoiced by vendor for the period, with category breakdown",
        "icon": "vendors",
        "frequency": "Monthly",
    },
    {
        "key": "payment_history",
        "title": "Payment History",
        "description": "All payments made — vendor, amount, method, check number",
        "icon": "billpay",
        "frequency": "Monthly",
    },
    {
        "key": "cogs",
        "title": "COGS Report",
        "description": "Cost of goods sold by category (food, beer, wine, liquor, supplies)",
        "icon": "foodcost",
        "frequency": "Monthly",
    },
    {
        "key": "pour_cost",
        "title": "Pour Cost Report",
        "description": "Beverage COGS vs revenue — beer, wine, liquor cost percentages",
        "icon": "bevcost",
        "frequency": "Monthly",
    },
    {
        "key": "invoice_detail",
        "title": "Invoice Detail Report",
        "description": "All confirmed invoices with line items for the period",
        "icon": "scan",
        "frequency": "On-demand",
    },
    {
        "key": "sales_journal",
        "title": "Sales Journal Export",
        "description": "Journal entries for QuickBooks reconciliation",
        "icon": "accounting",
        "frequency": "Monthly",
    },
]


@report_bp.route("/api/reports/catalog")
@login_required
def api_catalog():
    return jsonify(REPORT_CATALOG)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _csv_response(rows, headers, filename):
    """Build a CSV download response."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _pdf_response(title, headers, rows, filename, landscape=False):
    """Build a PDF download response using reportlab."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape as ls_mode
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    buf = io.BytesIO()
    pagesize = ls_mode(letter) if landscape else letter
    doc = SimpleDocTemplate(buf, pagesize=pagesize, topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(title, styles["Title"]))
    elements.append(Spacer(1, 12))

    # Build table data
    table_data = [headers] + [[str(c) if c is not None else "" for c in row] for row in rows]

    t = Table(table_data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.15, 0.15, 0.15)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.Color(0.85, 0.85, 0.85)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.Color(0.97, 0.97, 0.97)]),
    ]))
    elements.append(t)

    doc.build(elements)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _parse_params():
    """Extract common report params from query string."""
    location = request.args.get("location", "dennis")
    start = request.args.get("start")
    end = request.args.get("end")
    fmt = request.args.get("format", "csv")

    now = datetime.now(ET)
    if not start:
        start = now.replace(day=1).strftime("%Y-%m-%d")
    if not end:
        end = now.strftime("%Y-%m-%d")

    return location, start, end, fmt


def _fmt_money(val):
    if val is None:
        return "$0.00"
    return f"${val:,.2f}"


def _bdate_to_iso(bdate):
    """Convert YYYYMMDD to YYYY-MM-DD."""
    if not bdate or len(bdate) != 8:
        return bdate
    return f"{bdate[:4]}-{bdate[4:6]}-{bdate[6:]}"


# ------------------------------------------------------------------
# 1. Daily Sales Summary
# ------------------------------------------------------------------

@report_bp.route("/api/reports/daily_sales")
@login_required
def report_daily_sales():
    location, start, end, fmt = _parse_params()
    start_bd = start.replace("-", "")
    end_bd = end.replace("-", "")
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            o.business_date,
            COALESCE(SUM(o.total_amount), 0) as gross_sales,
            COALESCE(SUM(o.discount_amount), 0) as discounts,
            COALESCE(SUM(o.net_amount), 0) as net_sales,
            COALESCE(SUM(o.tax_amount), 0) as tax,
            COALESCE(SUM(o.tip_amount), 0) as tips,
            COUNT(*) as order_count
        FROM orders o
        WHERE o.location = ? AND o.business_date >= ? AND o.business_date <= ?
        GROUP BY o.business_date
        ORDER BY o.business_date
    """, (location, start_bd, end_bd)).fetchall()
    conn.close()

    headers = ["Date", "Orders", "Gross Sales", "Discounts", "Net Sales", "Tax", "Tips"]
    data = []
    for r in rows:
        data.append([
            _bdate_to_iso(r["business_date"]),
            r["order_count"],
            _fmt_money(r["gross_sales"]),
            _fmt_money(r["discounts"]),
            _fmt_money(r["net_sales"]),
            _fmt_money(r["tax"]),
            _fmt_money(r["tips"]),
        ])

    # Totals row
    if data:
        data.append([
            "TOTAL", sum(r["order_count"] for r in rows),
            _fmt_money(sum(r["gross_sales"] for r in rows)),
            _fmt_money(sum(r["discounts"] for r in rows)),
            _fmt_money(sum(r["net_sales"] for r in rows)),
            _fmt_money(sum(r["tax"] for r in rows)),
            _fmt_money(sum(r["tips"] for r in rows)),
        ])

    title = f"Daily Sales Summary — {loc_label} — {start} to {end}"
    fname = f"DailySales_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf", landscape=True)
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 2. Monthly Sales Report (by category)
# ------------------------------------------------------------------

@report_bp.route("/api/reports/monthly_sales")
@login_required
def report_monthly_sales():
    location, start, end, fmt = _parse_params()
    start_bd = start.replace("-", "")
    end_bd = end.replace("-", "")
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            oi.business_date,
            COALESCE(oi.menu_group, 'Uncategorized') as category,
            COALESCE(SUM(oi.quantity * oi.price), 0) as revenue,
            COALESCE(SUM(oi.quantity), 0) as qty
        FROM order_items oi
        WHERE oi.location = ? AND oi.business_date >= ? AND oi.business_date <= ?
          AND oi.voided = 0
        GROUP BY oi.business_date, oi.menu_group
        ORDER BY oi.business_date, oi.menu_group
    """, (location, start_bd, end_bd)).fetchall()
    conn.close()

    headers = ["Date", "Category", "Items Sold", "Revenue"]
    data = []
    total_rev = 0
    total_qty = 0
    for r in rows:
        rev = r["revenue"] or 0
        qty = int(r["qty"] or 0)
        total_rev += rev
        total_qty += qty
        data.append([
            _bdate_to_iso(r["business_date"]),
            r["category"],
            qty,
            _fmt_money(rev),
        ])

    if data:
        data.append(["TOTAL", "", total_qty, _fmt_money(total_rev)])

    title = f"Monthly Sales Report — {loc_label} — {start} to {end}"
    fname = f"MonthlySales_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf", landscape=True)
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 3. Sales Tax Summary
# ------------------------------------------------------------------

@report_bp.route("/api/reports/sales_tax")
@login_required
def report_sales_tax():
    location, start, end, fmt = _parse_params()
    start_bd = start.replace("-", "")
    end_bd = end.replace("-", "")
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            business_date,
            COALESCE(SUM(net_amount), 0) as net_sales,
            COALESCE(SUM(tax_amount), 0) as tax_collected
        FROM orders
        WHERE location = ? AND business_date >= ? AND business_date <= ?
        GROUP BY business_date
        ORDER BY business_date
    """, (location, start_bd, end_bd)).fetchall()
    conn.close()

    headers = ["Date", "Net Sales", "Tax Collected"]
    data = []
    total_sales = 0
    total_tax = 0
    for r in rows:
        ns = r["net_sales"] or 0
        tx = r["tax_collected"] or 0
        total_sales += ns
        total_tax += tx
        data.append([_bdate_to_iso(r["business_date"]), _fmt_money(ns), _fmt_money(tx)])

    if data:
        data.append(["TOTAL", _fmt_money(total_sales), _fmt_money(total_tax)])

    title = f"Sales Tax Summary — {loc_label} — {start} to {end}"
    fname = f"SalesTax_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf")
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 4. AP Aging Report
# ------------------------------------------------------------------

@report_bp.route("/api/reports/ap_aging")
@login_required
def report_ap_aging():
    location, start, end, fmt = _parse_params()
    loc_label = LOC_LABELS.get(location, location)
    today = date.today().isoformat()

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            si.vendor_name,
            si.invoice_number,
            si.invoice_date,
            si.total,
            COALESCE(bp.total_paid, 0) as paid
        FROM scanned_invoices si
        LEFT JOIN (
            SELECT invoice_id, SUM(amount) as total_paid
            FROM billpay_payments
            WHERE status != 'voided'
            GROUP BY invoice_id
        ) bp ON bp.invoice_id = si.id
        WHERE si.location = ? AND si.status = 'confirmed'
        ORDER BY si.vendor_name, si.invoice_date
    """, (location,)).fetchall()
    conn.close()

    headers = ["Vendor", "Invoice #", "Invoice Date", "Total", "Paid", "Balance", "Age (days)", "Bucket"]
    data = []
    for r in rows:
        total = r["total"] or 0
        paid = r["paid"] or 0
        balance = total - paid
        if balance <= 0.01:
            continue  # fully paid

        inv_date = r["invoice_date"] or today
        try:
            age = (date.today() - date.fromisoformat(inv_date)).days
        except (ValueError, TypeError):
            age = 0

        if age <= 30:
            bucket = "Current"
        elif age <= 60:
            bucket = "31-60"
        elif age <= 90:
            bucket = "61-90"
        else:
            bucket = "90+"

        data.append([
            r["vendor_name"], r["invoice_number"], inv_date,
            _fmt_money(total), _fmt_money(paid), _fmt_money(balance),
            age, bucket,
        ])

    title = f"AP Aging Report — {loc_label} — as of {today}"
    fname = f"APAging_{location}_{today}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf", landscape=True)
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 5. Vendor Spend Report
# ------------------------------------------------------------------

@report_bp.route("/api/reports/vendor_spend")
@login_required
def report_vendor_spend():
    location, start, end, fmt = _parse_params()
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            vendor_name,
            COUNT(*) as invoice_count,
            COALESCE(SUM(subtotal), 0) as subtotal,
            COALESCE(SUM(tax), 0) as tax,
            COALESCE(SUM(total), 0) as total
        FROM scanned_invoices
        WHERE location = ? AND status = 'confirmed'
          AND invoice_date >= ? AND invoice_date <= ?
        GROUP BY vendor_name
        ORDER BY total DESC
    """, (location, start, end)).fetchall()
    conn.close()

    headers = ["Vendor", "Invoices", "Subtotal", "Tax", "Total"]
    data = []
    grand_total = 0
    for r in rows:
        t = r["total"] or 0
        grand_total += t
        data.append([
            r["vendor_name"], r["invoice_count"],
            _fmt_money(r["subtotal"]), _fmt_money(r["tax"]), _fmt_money(t),
        ])

    if data:
        data.append(["TOTAL", sum(r["invoice_count"] for r in rows),
                      "", "", _fmt_money(grand_total)])

    title = f"Vendor Spend Report — {loc_label} — {start} to {end}"
    fname = f"VendorSpend_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf")
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 6. Payment History
# ------------------------------------------------------------------

@report_bp.route("/api/reports/payment_history")
@login_required
def report_payment_history():
    location, start, end, fmt = _parse_params()
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            bp.payment_date,
            si.vendor_name,
            si.invoice_number,
            bp.amount,
            bp.method,
            bp.check_number,
            bp.memo
        FROM billpay_payments bp
        LEFT JOIN scanned_invoices si ON si.id = bp.invoice_id
        WHERE (si.location = ? OR si.location IS NULL)
          AND bp.payment_date >= ? AND bp.payment_date <= ?
          AND bp.status != 'voided'
        ORDER BY bp.payment_date, si.vendor_name
    """, (location, start, end)).fetchall()
    conn.close()

    headers = ["Date", "Vendor", "Invoice #", "Amount", "Method", "Check #", "Memo"]
    data = []
    total = 0
    for r in rows:
        amt = r["amount"] or 0
        total += amt
        data.append([
            r["payment_date"], r["vendor_name"] or "—", r["invoice_number"] or "—",
            _fmt_money(amt), r["method"] or "—", r["check_number"] or "—",
            r["memo"] or "",
        ])

    if data:
        data.append(["TOTAL", "", "", _fmt_money(total), "", "", ""])

    title = f"Payment History — {loc_label} — {start} to {end}"
    fname = f"Payments_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf", landscape=True)
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 7. COGS Report
# ------------------------------------------------------------------

@report_bp.route("/api/reports/cogs")
@login_required
def report_cogs():
    location, start, end, fmt = _parse_params()
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            sii.category_type,
            COUNT(DISTINCT si.id) as invoice_count,
            COALESCE(SUM(sii.total_price), 0) as total_cost
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON si.id = sii.invoice_id
        WHERE si.location = ? AND si.status = 'confirmed'
          AND si.invoice_date >= ? AND si.invoice_date <= ?
        GROUP BY sii.category_type
        ORDER BY total_cost DESC
    """, (location, start, end)).fetchall()
    conn.close()

    category_labels = {
        "FOOD": "Food",
        "BEER": "Beer",
        "WINE": "Wine",
        "LIQUOR": "Liquor",
        "NA_BEVERAGES": "NA Beverages",
        "KITCHEN_SUPPLIES": "Kitchen Supplies",
        "DR_SUPPLIES": "Dining Room Supplies",
        "TOGO_SUPPLIES": "To-Go Supplies",
        "NON_COGS": "Non-COGS",
    }

    headers = ["Category", "Invoices", "Total Cost"]
    data = []
    grand_total = 0
    for r in rows:
        cost = r["total_cost"] or 0
        grand_total += cost
        label = category_labels.get(r["category_type"], r["category_type"] or "Uncategorized")
        data.append([label, r["invoice_count"], _fmt_money(cost)])

    if data:
        data.append(["TOTAL", "", _fmt_money(grand_total)])

    title = f"COGS Report — {loc_label} — {start} to {end}"
    fname = f"COGS_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf")
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 8. Pour Cost Report
# ------------------------------------------------------------------

@report_bp.route("/api/reports/pour_cost")
@login_required
def report_pour_cost():
    location, start, end, fmt = _parse_params()
    start_bd = start.replace("-", "")
    end_bd = end.replace("-", "")
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()

    # COGS from invoices by beverage category
    cogs_rows = conn.execute("""
        SELECT
            sii.category_type,
            COALESCE(SUM(sii.total_price), 0) as cost
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON si.id = sii.invoice_id
        WHERE si.location = ? AND si.status = 'confirmed'
          AND si.invoice_date >= ? AND si.invoice_date <= ?
          AND sii.category_type IN ('BEER', 'WINE', 'LIQUOR')
        GROUP BY sii.category_type
    """, (location, start, end)).fetchall()

    # Revenue from order_items — use menu_group-based classification
    # Get all beverage revenue grouped by menu_group
    rev_rows = conn.execute("""
        SELECT
            oi.menu_group,
            COALESCE(SUM(oi.quantity * oi.price), 0) as revenue
        FROM order_items oi
        WHERE oi.location = ? AND oi.business_date >= ? AND oi.business_date <= ?
          AND oi.voided = 0
        GROUP BY oi.menu_group
    """, (location, start_bd, end_bd)).fetchall()
    conn.close()

    # Classify menu_groups into beer/wine/liquor
    beer_kw = ["beer", "lager", "ipa", "ale", "stout", "draft", "seltzer", "truly", "white claw", "cider"]
    wine_kw = ["wine", "cab", "pinot", "chardonnay", "prosecco", "rose", "merlot", "champagne"]
    liquor_kw = ["cocktail", "margarita", "martini", "mojito", "old fashioned", "whiskey", "vodka",
                 "rum", "tequila", "gin", "shot", "spirit", "manhattan", "negroni", "daiquiri"]

    rev_map = {"BEER": 0, "WINE": 0, "LIQUOR": 0}
    for r in rev_rows:
        mg = (r["menu_group"] or "").lower()
        rev = r["revenue"] or 0
        if any(k in mg for k in beer_kw):
            rev_map["BEER"] += rev
        elif any(k in mg for k in wine_kw):
            rev_map["WINE"] += rev
        elif any(k in mg for k in liquor_kw):
            rev_map["LIQUOR"] += rev

    cogs_map = {r["category_type"]: r["cost"] for r in cogs_rows}

    headers = ["Category", "Revenue", "COGS", "Gross Profit", "Pour Cost %"]
    data = []
    total_rev = 0
    total_cogs = 0
    for cat, label in [("BEER", "Beer"), ("WINE", "Wine"), ("LIQUOR", "Liquor")]:
        rev = rev_map.get(cat, 0)
        cost = cogs_map.get(cat, 0)
        profit = rev - cost
        pct = f"{(cost / rev * 100):.1f}%" if rev > 0 else "—"
        total_rev += rev
        total_cogs += cost
        data.append([label, _fmt_money(rev), _fmt_money(cost), _fmt_money(profit), pct])

    total_profit = total_rev - total_cogs
    total_pct = f"{(total_cogs / total_rev * 100):.1f}%" if total_rev > 0 else "—"
    data.append(["TOTAL", _fmt_money(total_rev), _fmt_money(total_cogs), _fmt_money(total_profit), total_pct])

    title = f"Pour Cost Report — {loc_label} — {start} to {end}"
    fname = f"PourCost_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf")
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 9. Invoice Detail Report
# ------------------------------------------------------------------

@report_bp.route("/api/reports/invoice_detail")
@login_required
def report_invoice_detail():
    location, start, end, fmt = _parse_params()
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    rows = conn.execute("""
        SELECT
            si.invoice_date,
            si.vendor_name,
            si.invoice_number,
            sii.product_name,
            sii.quantity,
            sii.unit,
            sii.unit_price,
            sii.total_price,
            sii.category_type
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON si.id = sii.invoice_id
        WHERE si.location = ? AND si.status = 'confirmed'
          AND si.invoice_date >= ? AND si.invoice_date <= ?
        ORDER BY si.invoice_date, si.vendor_name, si.invoice_number
    """, (location, start, end)).fetchall()
    conn.close()

    headers = ["Date", "Vendor", "Invoice #", "Product", "Qty", "Unit", "Unit Price", "Total", "Category"]
    data = []
    grand_total = 0
    for r in rows:
        tp = r["total_price"] or 0
        grand_total += tp
        data.append([
            r["invoice_date"], r["vendor_name"], r["invoice_number"],
            r["product_name"], r["quantity"] or "", r["unit"] or "",
            _fmt_money(r["unit_price"]), _fmt_money(tp),
            r["category_type"] or "",
        ])

    if data:
        data.append(["", "", "", "", "", "", "TOTAL", _fmt_money(grand_total), ""])

    title = f"Invoice Detail Report — {loc_label} — {start} to {end}"
    fname = f"InvoiceDetail_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf", landscape=True)
    return _csv_response(data, headers, fname + ".csv")


# ------------------------------------------------------------------
# 10. Sales Journal Export
# ------------------------------------------------------------------

@report_bp.route("/api/reports/sales_journal")
@login_required
def report_sales_journal():
    location, start, end, fmt = _parse_params()
    loc_label = LOC_LABELS.get(location, location)

    conn = get_connection()
    entries = conn.execute("""
        SELECT e.id, e.entry_date, e.je_name, e.status, e.total_debits, e.total_credits,
               e.qbo_txn_id
        FROM qb_journal_entries e
        WHERE e.location = ? AND e.entry_date >= ? AND e.entry_date <= ?
              AND e.entry_type = 'sales_journal'
        ORDER BY e.entry_date
    """, (location, start, end)).fetchall()

    # Get line items for all entries
    entry_ids = [e["id"] for e in entries]
    lines = []
    if entry_ids:
        placeholders = ",".join("?" * len(entry_ids))
        lines = conn.execute(f"""
            SELECT li.entry_id, li.journal_name, li.qbo_account, li.debit, li.credit, li.memo
            FROM qb_journal_line_items li
            WHERE li.entry_id IN ({placeholders})
            ORDER BY li.entry_id, li.sort_order
        """, entry_ids).fetchall()
    conn.close()

    headers = ["Date", "JE Name", "Status", "Line", "QBO Account", "Debit", "Credit", "QBO Txn ID"]
    data = []
    for e in entries:
        entry_lines = [l for l in lines if l["entry_id"] == e["id"]]
        for li in entry_lines:
            data.append([
                e["entry_date"], e["je_name"], e["status"],
                li["journal_name"], li["qbo_account"] or "UNMAPPED",
                _fmt_money(li["debit"]) if li["debit"] else "",
                _fmt_money(li["credit"]) if li["credit"] else "",
                e["qbo_txn_id"] or "",
            ])

    title = f"Sales Journal — {loc_label} — {start} to {end}"
    fname = f"SalesJournal_{location}_{start}_to_{end}"
    if fmt == "pdf":
        return _pdf_response(title, headers, data, fname + ".pdf", landscape=True)
    return _csv_response(data, headers, fname + ".csv")
