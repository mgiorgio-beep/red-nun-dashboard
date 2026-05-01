"""
Print Queue routes — unified clearinghouse for all checks needing print.

Pulls pending items from three sources:
  - ap_payments         (vendor checks)
  - payroll_checks      (employee paper checks)
  - manual_checks       (one-off / non-AP checks)

A row is "in the queue" iff:
  - check_number IS NULL, AND
  - source-specific eligibility:
      ap:      status='pending' AND payment_method='check'
      payroll: voided=0 AND printed_at IS NULL
      manual:  voided=0 AND printed_at IS NULL

Print flow:
  1. User picks rows + starting check number on the /print-checks page.
  2. POST /api/print-queue/print assigns sequential numbers, marks rows printed,
     increments check_config.check_number_next, generates ONE multi-page PDF
     (one check per page, in submission order), returns the PDF.

Blueprint: print_queue_bp at /api/print-queue/*
"""

from datetime import datetime, date
from flask import Blueprint, jsonify, request, send_file

from integrations.toast.data_store import get_connection
from routes.auth_routes import admin_required, admin_or_accountant_required


print_queue_bp = Blueprint("print_queue_bp", __name__)


# ─── helpers ───────────────────────────────────────────────────────────────────

def _get_check_config(conn, location):
    """Return check_config row for a location, falling back to first row."""
    row = conn.execute(
        "SELECT * FROM check_config WHERE location = ?", (location,)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM check_config ORDER BY id LIMIT 1"
        ).fetchone()
    return row


def _ap_location_for_payment(conn, payment_id):
    """Derive a location for an ap_payments row from its linked invoices.
    Returns the location string if ALL linked invoices share one, else None.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT si.location
        FROM ap_payment_invoices pi
        JOIN scanned_invoices si ON si.id = pi.invoice_id
        WHERE pi.payment_id = ? AND si.location IS NOT NULL AND si.location != ''
        """,
        (payment_id,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["location"]
    return None  # no linked invoice, or mixed locations


# ─── GET /api/print-queue ──────────────────────────────────────────────────────

@print_queue_bp.route("/api/print-queue")
@admin_or_accountant_required
def get_print_queue():
    """Return all items pending check print, optionally filtered by location.

    Query params:
      location       — 'chatham' | 'dennis' (optional; if set, filters payroll
                       and manual to that location, and excludes AP rows whose
                       derived location is the OTHER location)

    Response:
      {
        "location": "chatham",
        "next_check_number": 1042,
        "items": [
          {
            "source": "ap" | "payroll" | "manual",
            "id": 123,
            "payee": "US Foods",
            "amount": 4523.17,
            "memo": "Weekly produce",
            "date": "2026-04-29",
            "location": "chatham" | "dennis" | null,
            "extra": "...source-specific subtitle..."
          },
          ...
        ]
      }
    """
    location = (request.args.get("location") or "").strip().lower() or None

    conn = get_connection()
    items = []

    # ── AP (vendor) checks ────────────────────────────────────────────────────
    ap_rows = conn.execute(
        """
        SELECT id, vendor_name, payment_date, amount, memo, payment_method,
               status, check_number, created_at
        FROM ap_payments
        WHERE check_number IS NULL
          AND status = 'pending'
          AND payment_method = 'check'
        ORDER BY payment_date ASC, id ASC
        """
    ).fetchall()

    for r in ap_rows:
        loc = _ap_location_for_payment(conn, r["id"])
        # If user selected a location, hide AP rows that we're sure belong to the OTHER location.
        # Show AP rows with no derivable location (loc is None) in both tabs so the user can route them.
        if location and loc and loc != location:
            continue

        # Build a small subtitle of linked invoice numbers (up to 3)
        inv_rows = conn.execute(
            """
            SELECT si.invoice_number
            FROM ap_payment_invoices pi
            JOIN scanned_invoices si ON si.id = pi.invoice_id
            WHERE pi.payment_id = ?
            ORDER BY si.invoice_date DESC
            LIMIT 3
            """,
            (r["id"],),
        ).fetchall()
        inv_nums = [str(ir["invoice_number"]) for ir in inv_rows if ir["invoice_number"]]
        extra = ("Inv " + ", ".join(inv_nums)) if inv_nums else ""

        items.append({
            "source": "ap",
            "id": r["id"],
            "payee": r["vendor_name"],
            "amount": float(r["amount"] or 0),
            "memo": r["memo"] or "",
            "date": r["payment_date"] or (r["created_at"] or "")[:10],
            "location": loc,
            "extra": extra,
        })

    # ── Payroll checks ────────────────────────────────────────────────────────
    pr_where = ["voided = 0", "check_number IS NULL", "printed_at IS NULL"]
    pr_params = []
    if location:
        pr_where.append("location = ?")
        pr_params.append(location)

    pr_rows = conn.execute(
        f"""
        SELECT id, employee_name, location, net_pay, gross_pay, total_hours,
               check_number, voided, printed_at, created_at
        FROM payroll_checks
        WHERE {' AND '.join(pr_where)}
        ORDER BY created_at ASC, id ASC
        """,
        pr_params,
    ).fetchall()

    for r in pr_rows:
        items.append({
            "source": "payroll",
            "id": r["id"],
            "payee": r["employee_name"],
            "amount": float(r["net_pay"] or 0),
            "memo": "",
            "date": (r["created_at"] or "")[:10],
            "location": r["location"],
            "extra": (
                f"{float(r['total_hours'] or 0):.2f} hrs · "
                f"gross ${float(r['gross_pay'] or 0):,.2f}"
            ),
        })

    # ── Manual checks ─────────────────────────────────────────────────────────
    mc_where = ["voided = 0", "check_number IS NULL", "printed_at IS NULL"]
    mc_params = []
    if location:
        mc_where.append("location = ?")
        mc_params.append(location)

    mc_rows = conn.execute(
        f"""
        SELECT id, payee_name, location, amount, memo, check_number, voided,
               printed_at, created_at
        FROM manual_checks
        WHERE {' AND '.join(mc_where)}
        ORDER BY created_at ASC, id ASC
        """,
        mc_params,
    ).fetchall()

    for r in mc_rows:
        items.append({
            "source": "manual",
            "id": r["id"],
            "payee": r["payee_name"],
            "amount": float(r["amount"] or 0),
            "memo": r["memo"] or "",
            "date": (r["created_at"] or "")[:10],
            "location": r["location"],
            "extra": "Manual check",
        })

    # Suggested next check number (per location if specified, else first config row)
    config = _get_check_config(conn, location) if location else conn.execute(
        "SELECT * FROM check_config ORDER BY id LIMIT 1"
    ).fetchone()
    next_num = (config["check_number_next"] if config else None) or 1001

    conn.close()

    # Sort: oldest date first, then source, then id
    items.sort(key=lambda x: (x["date"] or "", x["source"], x["id"]))

    return jsonify({
        "location": location,
        "next_check_number": int(next_num),
        "items": items,
        "count": len(items),
    })


# ─── POST /api/print-queue/print ───────────────────────────────────────────────

@print_queue_bp.route("/api/print-queue/print", methods=["POST"])
@admin_required
def print_queue_batch():
    """Assign sequential check numbers, mark rows printed, generate one PDF.

    Body:
      {
        "location": "chatham",
        "starting_check_number": 1042,
        "items": [
          {"source": "ap",      "id": 123},
          {"source": "payroll", "id": 45},
          {"source": "manual",  "id": 7},
          ...
        ]
      }

    Numbers assigned in submission order (so the user controls sequence by
    ordering the array in the request).

    Response: PDF stream (one page per check).
    """
    # Reuse the existing single-check renderer — supports ap-style payment dicts,
    # which manual checks already mimic.
    from check_printer import generate_check_pdf, generate_payroll_check_pdf

    data = request.get_json(silent=True) or {}
    location = (data.get("location") or "chatham").strip().lower()
    items = data.get("items") or []
    try:
        starting = int(data.get("starting_check_number") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "starting_check_number must be an integer"}), 400
    if starting <= 0:
        return jsonify({"error": "starting_check_number is required"}), 400
    if not items:
        return jsonify({"error": "No items selected"}), 400

    conn = get_connection()
    config = _get_check_config(conn, location)
    if not config:
        conn.close()
        return jsonify({"error": "Check config not set up for this location"}), 400
    config_d = dict(config)

    next_num = starting
    rendered = []  # list of (check_number, generator_callable)

    for it in items:
        source = (it.get("source") or "").strip().lower()
        try:
            row_id = int(it.get("id"))
        except (TypeError, ValueError):
            continue

        check_num_str = str(next_num)

        if source == "ap":
            payment = conn.execute(
                "SELECT * FROM ap_payments WHERE id = ?", (row_id,)
            ).fetchone()
            if not payment or payment["check_number"]:
                continue

            invoices = conn.execute(
                """
                SELECT pi.amount_applied, si.invoice_number, si.invoice_date, si.total
                FROM ap_payment_invoices pi
                JOIN scanned_invoices si ON si.id = pi.invoice_id
                WHERE pi.payment_id = ?
                """,
                (row_id,),
            ).fetchall()
            vendor_bp = conn.execute(
                "SELECT * FROM vendor_bill_pay WHERE vendor_name = ?",
                (payment["vendor_name"],),
            ).fetchone()

            # Mark printed + assign number
            conn.execute(
                "UPDATE ap_payments SET check_number = ?, status = 'printed' WHERE id = ?",
                (check_num_str, row_id),
            )
            try:
                conn.execute(
                    "UPDATE vendor_payments SET check_number = ?, status = 'printed', "
                    "updated_at = ? WHERE ap_payment_id = ?",
                    (check_num_str, datetime.now().isoformat(), row_id),
                )
            except Exception:
                pass

            rendered.append((
                "ap",
                check_num_str,
                {
                    "payment": dict(payment),
                    "invoices": [dict(i) for i in invoices],
                    "vendor_info": dict(vendor_bp) if vendor_bp else None,
                },
            ))

        elif source == "payroll":
            pr = conn.execute(
                "SELECT * FROM payroll_checks WHERE id = ?", (row_id,)
            ).fetchone()
            if not pr or pr["check_number"] or pr["voided"]:
                continue

            conn.execute(
                "UPDATE payroll_checks SET check_number = ?, "
                "printed_at = datetime('now') WHERE id = ?",
                (check_num_str, row_id),
            )

            rendered.append((
                "payroll",
                check_num_str,
                {"payroll": dict(pr)},
            ))

        elif source == "manual":
            mc = conn.execute(
                "SELECT * FROM manual_checks WHERE id = ?", (row_id,)
            ).fetchone()
            if not mc or mc["check_number"] or mc["voided"]:
                continue

            conn.execute(
                "UPDATE manual_checks SET check_number = ?, "
                "printed_at = datetime('now') WHERE id = ?",
                (check_num_str, row_id),
            )

            # Build payment + vendor_info shape that generate_check_pdf expects
            payment = {
                "vendor_name": mc["payee_name"],
                "amount": mc["amount"],
                "payment_date": (mc["created_at"] or "")[:10],
                "memo": mc["memo"] or "",
            }
            vendor_info = None
            if mc["payee_address_1"]:
                vendor_info = {
                    "payment_recipient": mc["payee_name"],
                    "remit_address_1": mc["payee_address_1"],
                    "remit_address_2": mc["payee_address_2"],
                    "remit_city": mc["payee_city"],
                    "remit_state": mc["payee_state"],
                    "remit_zip": mc["payee_zip"],
                }

            rendered.append((
                "manual",
                check_num_str,
                {
                    "payment": payment,
                    "invoices": [],
                    "vendor_info": vendor_info,
                },
            ))

        else:
            continue  # unknown source, skip silently

        next_num += 1

    if not rendered:
        conn.close()
        return jsonify({"error": "No printable items found"}), 400

    # Update the per-location next-check-number counter
    conn.execute(
        "UPDATE check_config SET check_number_next = ? WHERE location = ?",
        (next_num, location),
    )
    conn.commit()
    conn.close()

    # ── Render one multi-page PDF ────────────────────────────────────────────
    # We use generate_check_pdf / generate_payroll_check_pdf one at a time onto
    # a single canvas. Both helpers take an output_path and write to disk; to
    # combine them into one PDF we render to per-source temp files then merge.
    import os
    import tempfile

    # Try to import a PDF merger; fall back to one-pdf-per-check zipped if not available.
    try:
        from pypdf import PdfWriter
    except Exception:
        try:
            from PyPDF2 import PdfWriter  # older alias
        except Exception:
            PdfWriter = None  # type: ignore

    out_path = (
        f"/tmp/print_queue_{location}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    )

    # Render each check to its own temp file
    temp_paths = []
    try:
        for source, check_num, payload in rendered:
            tmp = tempfile.NamedTemporaryFile(
                prefix=f"chk_{source}_{check_num}_", suffix=".pdf", delete=False
            )
            tmp.close()
            if source == "payroll":
                generate_payroll_check_pdf(
                    payroll=payload["payroll"],
                    config=config_d,
                    check_number=check_num,
                    output_path=tmp.name,
                )
            else:  # ap or manual
                generate_check_pdf(
                    payment=payload["payment"],
                    invoices=payload.get("invoices") or [],
                    config=config_d,
                    vendor_info=payload.get("vendor_info"),
                    check_number=check_num,
                    output_path=tmp.name,
                )
            temp_paths.append(tmp.name)

        if PdfWriter is None:
            # No merger available — return the FIRST check; user will need pypdf
            # installed for multi-check batch. Surface this as a clear error.
            for p in temp_paths[1:]:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            return jsonify({
                "error": "pypdf not installed on server; cannot merge multi-check PDF. "
                         "Install with: pip install pypdf"
            }), 500

        writer = PdfWriter()
        for p in temp_paths:
            writer.append(p)
        with open(out_path, "wb") as f:
            writer.write(f)
    finally:
        for p in temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    # TODO: optional QBO push — push each printed check as a Check transaction
    # via integrations/quickbooks/qb_push.py so the bank register reflects it.

    download_name = (
        f"checks_{location}_{rendered[0][1]}-{rendered[-1][1]}_"
        f"{date.today().isoformat()}.pdf"
    )
    return send_file(out_path, mimetype="application/pdf", download_name=download_name)
