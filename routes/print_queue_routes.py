"""
Print Queue routes — unified clearinghouse for all checks needing print.

Pulls pending items from FOUR sources:
  - ap_payments              (vendor checks)
  - payroll_checks           (employee paper checks)
  - manual_checks            (one-off / non-AP checks)
  - recurring_bills (due)    (recurring bills whose next due date has arrived
                              and haven't been recorded as paid/skipped yet)

Eligibility per source:
  ap        : status='pending' AND payment_method='check' AND check_number IS NULL
  payroll   : voided=0 AND printed_at IS NULL AND check_number IS NULL
  manual    : voided=0 AND printed_at IS NULL AND check_number IS NULL
  recurring : active=1, payment_method='check', due-on-or-before today (using
              days_before_due as look-ahead), no recurring_bill_payments row
              yet for that (bill, due_date)

Identifier shape:
  ap / payroll / manual : numeric row id
  recurring             : string "{bill_id}:{due_date}"

Print flow:
  1. User picks rows on /print-checks; each row gets a proposed check number
     (sequential default from check_config, individually editable).
  2. Frontend live-checks proposed numbers via POST /api/print-queue/check-conflicts.
  3. POST /api/print-queue/print validates per-item numbers (within-batch + against
     ap_payments, payroll_checks, manual_checks, recurring_bill_payments) BEFORE
     any DB writes. On conflict returns 409 with details.
     On success: assigns numbers, marks rows printed (or for recurring,
     INSERTs a recurring_bill_payments row with status='paid'), increments
     check_config.check_number_next, generates ONE multi-page PDF (one check
     per page in submission order), returns the PDF.

Blueprint: print_queue_bp at /api/print-queue/*
"""

from datetime import datetime, date
from flask import Blueprint, jsonify, request, send_file

from integrations.toast.data_store import get_connection
from routes.auth_routes import admin_required, admin_or_accountant_required


print_queue_bp = Blueprint("print_queue_bp", __name__)


# ─── helpers ───────────────────────────────────────────────────────────────────

def _get_check_config(conn, location):
    row = conn.execute(
        "SELECT * FROM check_config WHERE location = ?", (location,)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM check_config ORDER BY id LIMIT 1"
        ).fetchone()
    return row


def _ap_location_for_payment(conn, payment_id):
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
    return None


def _find_conflicts(conn, location, numbers, exclude_keys=None):
    """Return any check numbers from `numbers` that already exist in the DB.

    AP and recurring_bill_payments are checked globally (no usable location
    column for AP; recurring_bill_payments doesn't carry it directly).
    Payroll/manual are filtered to `location` if provided.

    `exclude_keys` is a set of (source, id) tuples to ignore — for recurring
    items the id is the string "{bill_id}:{due_date}".

    Returns: list of {number, source, id, payee, location} dicts.
    """
    exclude_keys = exclude_keys or set()
    nums = sorted({str(int(n)) for n in numbers if str(n).strip().lstrip("-").isdigit() and int(n) > 0})
    if not nums:
        return []

    placeholders = ",".join("?" * len(nums))
    conflicts = []

    # AP — global
    for r in conn.execute(
        f"SELECT id, vendor_name AS payee, check_number "
        f"FROM ap_payments WHERE check_number IN ({placeholders})",
        nums,
    ).fetchall():
        if ("ap", r["id"]) in exclude_keys:
            continue
        conflicts.append({
            "number": r["check_number"],
            "source": "ap",
            "id": r["id"],
            "payee": r["payee"],
            "location": None,
        })

    # Payroll — same-location
    pr_q = (
        f"SELECT id, employee_name AS payee, check_number, location "
        f"FROM payroll_checks WHERE check_number IN ({placeholders})"
    )
    pr_params = list(nums)
    if location:
        pr_q += " AND location = ?"
        pr_params.append(location)
    for r in conn.execute(pr_q, pr_params).fetchall():
        if ("payroll", r["id"]) in exclude_keys:
            continue
        conflicts.append({
            "number": r["check_number"],
            "source": "payroll",
            "id": r["id"],
            "payee": r["payee"],
            "location": r["location"],
        })

    # Manual — same-location
    mc_q = (
        f"SELECT id, payee_name AS payee, check_number, location "
        f"FROM manual_checks WHERE check_number IN ({placeholders})"
    )
    mc_params = list(nums)
    if location:
        mc_q += " AND location = ?"
        mc_params.append(location)
    for r in conn.execute(mc_q, mc_params).fetchall():
        if ("manual", r["id"]) in exclude_keys:
            continue
        conflicts.append({
            "number": r["check_number"],
            "source": "manual",
            "id": r["id"],
            "payee": r["payee"],
            "location": r["location"],
        })

    # Recurring bill payments — global (table has no location column; lookup
    # via the parent bill if we want a label).
    rec_rows = conn.execute(
        f"""
        SELECT rbp.id, rbp.bill_id, rbp.due_date, rbp.check_number,
               rb.vendor_name, rb.payable_to, rb.location
        FROM recurring_bill_payments rbp
        JOIN recurring_bills rb ON rb.id = rbp.bill_id
        WHERE rbp.check_number IN ({placeholders})
          AND COALESCE(rbp.status, 'paid') != 'voided'
        """,
        nums,
    ).fetchall()
    for r in rec_rows:
        rec_key = f"{r['bill_id']}:{r['due_date']}"
        if ("recurring", rec_key) in exclude_keys:
            continue
        conflicts.append({
            "number": r["check_number"],
            "source": "recurring",
            "id": rec_key,
            "payee": r["payable_to"] or r["vendor_name"],
            "location": r["location"],
        })

    return conflicts


def _amount_for_recurring(conn, bill):
    """Compute the printable amount for a recurring bill: sum of lines if any,
    else the bill.amount column."""
    line_total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM recurring_bill_lines WHERE bill_id = ?",
        (bill["id"],),
    ).fetchone()["t"]
    if line_total and float(line_total) > 0:
        return float(line_total)
    return float(bill["amount"] or 0)


def _due_recurring_for_queue(conn, location):
    """Return list of (bill, due_date_str) pairs that are currently due —
    i.e., due-day has arrived (with days_before_due look-ahead) and there's
    no recurring_bill_payments row for that period yet. Filters to
    payment_method='check' (only paper-check bills go on the print queue).

    Mirrors the logic of /api/billpay/recurring/due so the UI is consistent.
    """
    # Local import to avoid circular import at module load
    from routes.billpay_routes import _is_due_on

    where = ["active = 1", "COALESCE(payment_method, 'check') = 'check'"]
    params = []
    if location:
        where.append("(location = ? OR location = 'both')")
        params.append(location)

    bills = conn.execute(
        "SELECT * FROM recurring_bills WHERE " + " AND ".join(where), params
    ).fetchall()

    today = date.today()
    look_ahead_default = 14  # days; per-bill override via days_before_due
    pairs = []
    seen = set()

    for b in bills:
        b = dict(b)
        days_early = int(b.get("days_before_due") or 0)
        # window = up to today + days_early. If user wants to see 14 days out
        # we walk through that range. days_before_due is the user's "show me
        # this many days early" preference.
        window = max(days_early, 0)
        from datetime import timedelta as _td
        for offset in range(window + 1):
            check_day = today + _td(days=offset)
            due_str = _is_due_on(b, check_day.isoformat())
            if not due_str:
                continue
            key = (b["id"], due_str)
            if key in seen:
                continue
            seen.add(key)
            existing = conn.execute(
                "SELECT id FROM recurring_bill_payments WHERE bill_id=? AND due_date=? "
                "AND COALESCE(status, 'paid') != 'voided'",
                (b["id"], due_str),
            ).fetchone()
            if existing:
                continue
            pairs.append((b, due_str))

    pairs.sort(key=lambda p: p[1])  # oldest due first
    return pairs


# ─── GET /api/print-queue ──────────────────────────────────────────────────────

@print_queue_bp.route("/api/print-queue")
@admin_or_accountant_required
def get_print_queue():
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
        if location and loc and loc != location:
            continue
        inv_rows = conn.execute(
            """
            SELECT si.invoice_number FROM ap_payment_invoices pi
            JOIN scanned_invoices si ON si.id = pi.invoice_id
            WHERE pi.payment_id = ?
            ORDER BY si.invoice_date DESC LIMIT 3
            """,
            (r["id"],),
        ).fetchall()
        inv_nums = [str(ir["invoice_number"]) for ir in inv_rows if ir["invoice_number"]]
        items.append({
            "source": "ap",
            "id": r["id"],
            "payee": r["vendor_name"],
            "amount": float(r["amount"] or 0),
            "memo": r["memo"] or "",
            "date": r["payment_date"] or (r["created_at"] or "")[:10],
            "location": loc,
            "extra": ("Inv " + ", ".join(inv_nums)) if inv_nums else "",
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

    # ── Recurring bills (due) ─────────────────────────────────────────────────
    for bill, due_str in _due_recurring_for_queue(conn, location):
        amount = _amount_for_recurring(conn, bill)
        rec_id = f"{bill['id']}:{due_str}"
        items.append({
            "source": "recurring",
            "id": rec_id,
            "payee": bill.get("payable_to") or bill.get("vendor_name"),
            "amount": amount,
            "memo": bill.get("description") or "",
            "date": due_str,
            "location": bill.get("location"),
            "extra": f"Recurring · due {due_str}",
        })

    # Suggested next check number
    config = _get_check_config(conn, location) if location else conn.execute(
        "SELECT * FROM check_config ORDER BY id LIMIT 1"
    ).fetchone()
    next_num = (config["check_number_next"] if config else None) or 1001

    conn.close()

    items.sort(key=lambda x: (x["date"] or "", x["source"], str(x["id"])))

    return jsonify({
        "location": location,
        "next_check_number": int(next_num),
        "items": items,
        "count": len(items),
    })


# ─── POST /api/print-queue/check-conflicts ─────────────────────────────────────

@print_queue_bp.route("/api/print-queue/check-conflicts", methods=["POST"])
@admin_or_accountant_required
def check_conflicts():
    """Live preview: return any of the proposed numbers that already exist.

    Body: {location, numbers:[int], exclude_items:[{source,id}]}
    Response: {conflicts: [...]}
    """
    data = request.get_json(silent=True) or {}
    location = (data.get("location") or "").strip().lower() or None
    numbers = data.get("numbers") or []
    exclude = data.get("exclude_items") or []
    exclude_keys = set()
    for e in exclude:
        s = e.get("source")
        i = e.get("id")
        if not s or i is None:
            continue
        # recurring uses string ids; others numeric
        if s == "recurring":
            exclude_keys.add((s, str(i)))
        else:
            try:
                exclude_keys.add((s, int(i)))
            except (TypeError, ValueError):
                continue

    conn = get_connection()
    conflicts = _find_conflicts(conn, location, numbers, exclude_keys)
    conn.close()
    return jsonify({"conflicts": conflicts})


# ─── POST /api/print-queue/print ───────────────────────────────────────────────

@print_queue_bp.route("/api/print-queue/print", methods=["POST"])
@admin_required
def print_queue_batch():
    """Validate per-item check numbers and generate ONE multi-page PDF.

    Body:
      {
        "location": "chatham",
        "items": [
          {"source":"ap","id":123,"check_number":1042},
          {"source":"payroll","id":45,"check_number":1043},
          {"source":"manual","id":7,"check_number":1044},
          {"source":"recurring","id":"5:2026-04-15","check_number":1045}
        ]
      }
    """
    from check_printer import generate_check_pdf, generate_payroll_check_pdf

    data = request.get_json(silent=True) or {}
    location = (data.get("location") or "chatham").strip().lower()
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "No items selected"}), 400

    # ── Parse + within-batch dedup ────────────────────────────────────────────
    parsed = []  # list of (source, raw_id, check_num_int, check_num_str)
    seen = {}
    for it in items:
        source = (it.get("source") or "").strip().lower()
        if source not in ("ap", "payroll", "manual", "recurring"):
            return jsonify({"error": f"Unknown source: {source!r}"}), 400

        raw_id = it.get("id")
        if source == "recurring":
            if not isinstance(raw_id, str) or ":" not in raw_id:
                return jsonify({
                    "error": "Recurring items need id like 'bill_id:due_date'"
                }), 400
        else:
            try:
                raw_id = int(raw_id)
            except (TypeError, ValueError):
                return jsonify({"error": "Each item needs an integer id"}), 400

        try:
            num = int(it.get("check_number"))
        except (TypeError, ValueError):
            return jsonify({
                "error": f"Missing or invalid check_number for {source}#{raw_id}"
            }), 400
        if num <= 0:
            return jsonify({
                "error": f"Check number must be positive for {source}#{raw_id}"
            }), 400

        num_s = str(num)
        if num_s in seen:
            other = seen[num_s]
            return jsonify({
                "error": "Duplicate check number within this batch",
                "conflicts": [{
                    "number": num_s,
                    "reason": "duplicate_in_batch",
                    "first_use": {"source": other[0], "id": other[1]},
                    "second_use": {"source": source, "id": raw_id},
                }],
            }), 409
        seen[num_s] = (source, raw_id)
        parsed.append((source, raw_id, num, num_s))

    conn = get_connection()
    config = _get_check_config(conn, location)
    if not config:
        conn.close()
        return jsonify({"error": "Check config not set up for this location"}), 400
    config_d = dict(config)

    # ── DB conflict check ────────────────────────────────────────────────────
    proposed_numbers = [p[2] for p in parsed]
    exclude_keys = set()
    for source, raw_id, _, _ in parsed:
        if source == "recurring":
            exclude_keys.add((source, str(raw_id)))
        else:
            exclude_keys.add((source, raw_id))
    conflicts = _find_conflicts(conn, location, proposed_numbers, exclude_keys)
    if conflicts:
        conn.close()
        return jsonify({
            "error": "One or more check numbers are already in use",
            "conflicts": conflicts,
        }), 409

    # ── Assign + render ──────────────────────────────────────────────────────
    rendered = []  # (source, num_s, payload)
    max_num_used = 0

    today_iso = date.today().isoformat()

    for source, raw_id, num, num_s in parsed:
        if source == "ap":
            payment = conn.execute(
                "SELECT * FROM ap_payments WHERE id = ?", (raw_id,)
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
                (raw_id,),
            ).fetchall()
            vendor_bp = conn.execute(
                "SELECT * FROM vendor_bill_pay WHERE vendor_name = ?",
                (payment["vendor_name"],),
            ).fetchone()
            conn.execute(
                "UPDATE ap_payments SET check_number = ?, status = 'printed' WHERE id = ?",
                (num_s, raw_id),
            )
            try:
                conn.execute(
                    "UPDATE vendor_payments SET check_number = ?, status = 'printed', "
                    "updated_at = ? WHERE ap_payment_id = ?",
                    (num_s, datetime.now().isoformat(), raw_id),
                )
            except Exception:
                pass
            rendered.append((
                "ap", num_s,
                {
                    "payment": dict(payment),
                    "invoices": [dict(i) for i in invoices],
                    "vendor_info": dict(vendor_bp) if vendor_bp else None,
                },
            ))

        elif source == "payroll":
            pr = conn.execute(
                "SELECT * FROM payroll_checks WHERE id = ?", (raw_id,)
            ).fetchone()
            if not pr or pr["check_number"] or pr["voided"]:
                continue
            conn.execute(
                "UPDATE payroll_checks SET check_number = ?, "
                "printed_at = datetime('now') WHERE id = ?",
                (num_s, raw_id),
            )
            rendered.append(("payroll", num_s, {"payroll": dict(pr)}))

        elif source == "manual":
            mc = conn.execute(
                "SELECT * FROM manual_checks WHERE id = ?", (raw_id,)
            ).fetchone()
            if not mc or mc["check_number"] or mc["voided"]:
                continue
            conn.execute(
                "UPDATE manual_checks SET check_number = ?, "
                "printed_at = datetime('now') WHERE id = ?",
                (num_s, raw_id),
            )
            payment = {
                "vendor_name": mc["payee_name"],
                "amount": mc["amount"],
                "payment_date": (mc["created_at"] or "")[:10] or today_iso,
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
                "manual", num_s,
                {"payment": payment, "invoices": [], "vendor_info": vendor_info},
            ))

        elif source == "recurring":
            try:
                bill_id_str, due_date = raw_id.split(":", 1)
                bill_id = int(bill_id_str)
            except ValueError:
                continue
            bill = conn.execute(
                "SELECT * FROM recurring_bills WHERE id = ?", (bill_id,)
            ).fetchone()
            if not bill:
                continue
            # Race check: don't double-record
            already = conn.execute(
                "SELECT id FROM recurring_bill_payments WHERE bill_id=? AND due_date=? "
                "AND COALESCE(status, 'paid') != 'voided'",
                (bill_id, due_date),
            ).fetchone()
            if already:
                continue
            amount = _amount_for_recurring(conn, dict(bill))
            memo = (
                f"{bill['description']}".strip() if bill["description"] else ""
            )

            conn.execute(
                """
                INSERT INTO recurring_bill_payments
                  (bill_id, due_date, status, paid_date, amount_paid,
                   check_number, payment_method, memo)
                VALUES (?, ?, 'paid', ?, ?, ?, 'check', ?)
                """,
                (bill_id, due_date, today_iso, amount, num_s, memo),
            )

            # Look up the vendor's remit address from vendor_bill_pay so the
            # check renderer can stamp it. Prefer the payable_to name (which is
            # who the check is actually made out to); fall back to vendor_name.
            vendor_bp = None
            for name_to_try in (bill["payable_to"], bill["vendor_name"]):
                if not name_to_try:
                    continue
                vendor_bp = conn.execute(
                    "SELECT * FROM vendor_bill_pay WHERE vendor_name = ?",
                    (name_to_try,),
                ).fetchone()
                if vendor_bp:
                    break

            payment = {
                "vendor_name": bill["payable_to"] or bill["vendor_name"],
                "amount": amount,
                "payment_date": today_iso,
                "memo": memo,
            }
            rendered.append((
                "recurring", num_s,
                {
                    "payment": payment,
                    "invoices": [],
                    "vendor_info": dict(vendor_bp) if vendor_bp else None,
                },
            ))

        if num > max_num_used:
            max_num_used = num

    if not rendered:
        conn.close()
        return jsonify({
            "error": "No printable items found (race?). Refresh and try again."
        }), 400

    next_after = max(max_num_used + 1, int(config["check_number_next"] or 0))
    conn.execute(
        "UPDATE check_config SET check_number_next = ? WHERE location = ?",
        (next_after, location),
    )
    conn.commit()
    conn.close()

    # ── Render the PDF ────────────────────────────────────────────────────────
    import os
    import tempfile

    try:
        from pypdf import PdfWriter
    except Exception:
        try:
            from PyPDF2 import PdfWriter
        except Exception:
            PdfWriter = None  # type: ignore

    out_path = (
        f"/tmp/print_queue_{location}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    )

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
            else:
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

    nums_used = [int(n) for _, n, _ in rendered]
    download_name = (
        f"checks_{location}_{min(nums_used)}-{max(nums_used)}_"
        f"{date.today().isoformat()}.pdf"
    )
    return send_file(out_path, mimetype="application/pdf", download_name=download_name, as_attachment=False)
