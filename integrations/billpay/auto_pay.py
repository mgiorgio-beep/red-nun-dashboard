"""
Auto-pay: when a confirmed invoice belongs to an auto-pay-flagged vendor,
create the payment, assign a check number, generate the check PDF, and queue
it for the Windows print agent.

Top-level entry: process_invoice_for_auto_pay(invoice_id)

Guardrails (in evaluation order):
    1. vendor_eligible       — vendor exists, bill_pay_enabled, auto_pay=1,
                                payment_method='check'
    2. invoice_state         — status='confirmed', not voided, total > 0
    3. duplicate_check       — same vendor+invoice_number isn't already paid
    4. existing_payment      — invoice not already linked to an ap_payment
    5. anomaly_check         — total within ±25% of trailing 3-month avg

Each decision is logged to `auto_pay_decisions`. Failures never raise to the
caller — confirm_invoice() must keep working even if this code crashes.

NB: Anthropic API is NOT called from here. All work is deterministic.
"""

import os
import logging
from datetime import datetime
from integrations.toast.data_store import get_connection

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Configuration constants — tune here, not in DB.
# ──────────────────────────────────────────────────────────────────────────────

# Anomaly: if the invoice total is more than ±ANOMALY_PCT off the trailing
# AVG_WINDOW_DAYS average for that vendor, skip auto-pay and route to
# manual review. The user explicitly asked for 25%.
ANOMALY_PCT = 0.25
AVG_WINDOW_DAYS = 90  # ~3 months

# Minimum number of historical invoices required to run the anomaly check.
# If we have fewer, we still auto-pay (don't block the very first one).
ANOMALY_MIN_HISTORY = 2

# Vendors whose bills are inherently variable month-to-month (utilities,
# fuel, metered services). The amount-anomaly check is skipped for these —
# we trust the invoice OCR and let auto-pay through regardless of deviation.
# Match is case-insensitive on the canonical vendor_name.
ANOMALY_SKIP_VENDORS = {
    "sprague operating resources llc",   # utility — amount varies month-to-month with usage
}

# Where check PDFs land. Read by the print agent via the dashboard API.
CHECK_PDF_DIR = "/var/lib/rednun/check_pdfs"


# ──────────────────────────────────────────────────────────────────────────────
# Decision logging
# ──────────────────────────────────────────────────────────────────────────────

def _log_decision(conn, invoice_id, vendor_name, total, decision, reason,
                  details=None, ap_payment_id=None, check_number=None,
                  print_job_id=None):
    """Write an auto_pay_decisions row. Decision is one of 'paid'|'skipped'|'error'."""
    conn.execute(
        """
        INSERT INTO auto_pay_decisions
        (invoice_id, vendor_name, invoice_total, decision, reason, details,
         ap_payment_id, check_number, print_job_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (invoice_id, vendor_name, total, decision, reason, details,
         ap_payment_id, check_number, print_job_id),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Guardrails
# ──────────────────────────────────────────────────────────────────────────────

def _check_vendor_eligible(conn, invoice_row):
    """Vendor must be auto_pay=1, bill_pay_enabled=1, payment_method='check'."""
    vendor = invoice_row["vendor_name"]
    bp = conn.execute(
        "SELECT auto_pay, bill_pay_enabled, payment_method FROM vendor_bill_pay "
        "WHERE vendor_name = ?",
        (vendor,),
    ).fetchone()
    if not bp:
        return (False, "no_vendor_bill_pay_record", f"vendor '{vendor}' has no bill-pay setup")
    if not bp["bill_pay_enabled"]:
        return (False, "vendor_bill_pay_disabled", None)
    if not bp["auto_pay"]:
        return (False, "vendor_not_auto_pay", None)
    if (bp["payment_method"] or "check") != "check":
        return (False, "vendor_payment_method_not_check",
                f"payment_method={bp['payment_method']}")
    return (True, None, None)


def _check_invoice_state(invoice_row):
    """Invoice must be confirmed, have a positive total, and not be voided."""
    if invoice_row["status"] != "confirmed":
        return (False, "invoice_not_confirmed",
                f"status={invoice_row['status']}")
    if not invoice_row["total"] or float(invoice_row["total"]) <= 0:
        return (False, "invoice_zero_total", None)
    # Already paid?
    if (invoice_row["payment_status"] or "") == "paid":
        return (False, "invoice_already_paid", None)
    return (True, None, None)


def _check_duplicate(conn, invoice_row):
    """Same vendor + invoice_number already exists and is paid → skip."""
    invnum = (invoice_row["invoice_number"] or "").strip()
    if not invnum:
        return (True, None, None)  # no number to dedup against; allow
    row = conn.execute(
        """
        SELECT id, payment_status FROM scanned_invoices
        WHERE vendor_name = ?
          AND TRIM(COALESCE(invoice_number, '')) = ?
          AND id != ?
          AND payment_status = 'paid'
        LIMIT 1
        """,
        (invoice_row["vendor_name"], invnum, invoice_row["id"]),
    ).fetchone()
    if row:
        return (False, "duplicate_invoice_already_paid",
                f"matches scanned_invoices.id={row['id']}")
    return (True, None, None)


def _check_existing_payment(conn, invoice_row):
    """Invoice already linked to an ap_payment → skip."""
    row = conn.execute(
        "SELECT payment_id FROM ap_payment_invoices WHERE invoice_id = ? LIMIT 1",
        (invoice_row["id"],),
    ).fetchone()
    if row:
        return (False, "already_linked_to_payment",
                f"ap_payment_id={row['payment_id']}")
    return (True, None, None)


def _check_anomaly(conn, invoice_row):
    """Total must be within ±ANOMALY_PCT of trailing AVG_WINDOW_DAYS avg."""
    vendor = invoice_row["vendor_name"]
    total = float(invoice_row["total"])
    # Variable-amount vendors (utilities, fuel) bypass the anomaly guardrail.
    if (vendor or "").strip().lower() in ANOMALY_SKIP_VENDORS:
        return (True, None, "vendor_in_anomaly_skip_list")
    # Pull recent confirmed totals for this vendor (excluding the current one).
    rows = conn.execute(
        f"""
        SELECT total FROM scanned_invoices
        WHERE vendor_name = ?
          AND status = 'confirmed'
          AND id != ?
          AND DATE(COALESCE(invoice_date, confirmed_at, created_at)) >=
              DATE('now', '-{AVG_WINDOW_DAYS} days')
          AND total > 0
        """,
        (vendor, invoice_row["id"]),
    ).fetchall()
    history = [float(r["total"]) for r in rows if r["total"]]
    if len(history) < ANOMALY_MIN_HISTORY:
        return (True, None, f"insufficient_history (n={len(history)})")
    avg = sum(history) / len(history)
    if avg <= 0:
        return (True, None, None)
    deviation = abs(total - avg) / avg
    if deviation > ANOMALY_PCT:
        return (False, "anomaly_amount_out_of_range",
                f"total=${total:,.2f} avg=${avg:,.2f} deviation={deviation:.0%}")
    return (True, None, f"within_range (avg=${avg:,.2f}, dev={deviation:.0%})")


def should_auto_pay(conn, invoice_row):
    """Run all guardrails in order. Returns (eligible: bool, reason: str, details: str|None).

    `reason` is 'eligible' on the happy path, else a machine-readable code.
    """
    for fn in (_check_vendor_eligible, _check_invoice_state, _check_duplicate,
               _check_existing_payment, _check_anomaly):
        # The vendor-eligible check needs both conn and invoice_row;
        # the others vary — keep their signatures explicit.
        if fn is _check_vendor_eligible:
            ok, reason, details = fn(conn, invoice_row)
        elif fn is _check_invoice_state:
            ok, reason, details = fn(invoice_row)
        else:
            ok, reason, details = fn(conn, invoice_row)
        if not ok:
            return (False, reason, details)
    return (True, "eligible", None)


# ──────────────────────────────────────────────────────────────────────────────
# Execution (payment + check PDF + print job)
# ──────────────────────────────────────────────────────────────────────────────

def _next_check_number(conn, location):
    """Atomically grab and increment the next check number for the location."""
    cfg = conn.execute(
        "SELECT id, check_number_next FROM check_config WHERE location = ?",
        (location,),
    ).fetchone()
    if not cfg:
        cfg = conn.execute(
            "SELECT id, check_number_next FROM check_config ORDER BY id LIMIT 1"
        ).fetchone()
    if not cfg:
        raise RuntimeError("No check_config row found")
    num = cfg["check_number_next"] or 1001
    conn.execute(
        "UPDATE check_config SET check_number_next = ? WHERE id = ?",
        (num + 1, cfg["id"]),
    )
    return str(num)


def _generate_check_pdf(payment_dict, invoices, config_dict, vendor_info,
                        check_number, output_path):
    """Wrapper around check_printer.generate_check_pdf with safe imports."""
    from check_printer import generate_check_pdf
    generate_check_pdf(
        payment=payment_dict,
        invoices=invoices,
        config=config_dict,
        vendor_info=vendor_info,
        check_number=check_number,
        output_path=output_path,
    )


def execute_auto_pay(conn, invoice_row):
    """Create ap_payment, link invoice, mirror to vendor_payments, generate PDF,
    queue print_job. Returns (ap_payment_id, check_number, print_job_id, pdf_path).

    Raises on hard errors (caller logs them as 'error' decisions).
    """
    cur = conn.cursor()
    invoice_id = invoice_row["id"]
    vendor = invoice_row["vendor_name"]
    total = float(invoice_row["total"])
    location = invoice_row["location"] or "chatham"
    today = datetime.now().strftime("%Y-%m-%d")

    # 1) Check config for the invoice's location
    config = conn.execute(
        "SELECT * FROM check_config WHERE location = ?", (location,)
    ).fetchone()
    if not config:
        config = conn.execute(
            "SELECT * FROM check_config ORDER BY id LIMIT 1"
        ).fetchone()
    if not config:
        raise RuntimeError("check_config has no rows — set up Check Setup first")

    # 2) Vendor bill-pay record (for remit address)
    vendor_bp = conn.execute(
        "SELECT * FROM vendor_bill_pay WHERE vendor_name = ?", (vendor,)
    ).fetchone()

    # 3) Assign check number atomically
    check_number = _next_check_number(conn, location)

    # 4) Build memo
    invnum = (invoice_row["invoice_number"] or "").strip()
    memo = f"Inv #{invnum}" if invnum else ""

    # 5) Create ap_payment
    cur.execute(
        """
        INSERT INTO ap_payments
        (vendor_name, payment_date, amount, payment_method,
         check_number, memo, status, auto_paid)
        VALUES (?, ?, ?, 'check', ?, ?, 'printed', 1)
        """,
        (vendor, today, total, check_number, memo),
    )
    ap_payment_id = cur.lastrowid

    # 6) Link invoice
    cur.execute(
        """
        INSERT INTO ap_payment_invoices (payment_id, invoice_id, amount_applied)
        VALUES (?, ?, ?)
        """,
        (ap_payment_id, invoice_id, total),
    )

    # 7) Mark invoice paid
    cur.execute(
        """
        UPDATE scanned_invoices
        SET amount_paid = COALESCE(amount_paid, 0) + ?,
            balance     = 0,
            payment_status = 'paid',
            paid_date   = ?
        WHERE id = ?
        """,
        (total, today, invoice_id),
    )

    # 8) Mirror into vendor_payments (best-effort — matches Record Payment flow)
    try:
        ref = f"CHK-{check_number}"
        cur.execute(
            """
            INSERT INTO vendor_payments
            (vendor, location, payment_date, payment_ref, payment_method,
             payment_total, check_number, memo, status, source, ap_payment_id)
            VALUES (?, ?, ?, ?, 'check', ?, ?, ?, 'printed', 'check', ?)
            """,
            (vendor, location, today, ref, total, check_number, memo, ap_payment_id),
        )
        vp_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO vendor_payment_invoices
            (payment_id, invoice_number, invoice_date, due_date, amount_paid)
            VALUES (?, ?, ?, ?, ?)
            """,
            (vp_id, invoice_row["invoice_number"], invoice_row["invoice_date"],
             invoice_row["due_date"], total),
        )
    except Exception as e:
        logger.warning(f"vendor_payments mirror failed for AP #{ap_payment_id}: {e}")

    # 9) Generate the PDF
    os.makedirs(CHECK_PDF_DIR, exist_ok=True)
    pdf_path = os.path.join(
        CHECK_PDF_DIR,
        f"auto_check_{ap_payment_id}_{check_number}.pdf",
    )
    payment_dict = {
        "vendor_name": vendor,
        "amount": total,
        "payment_date": today,
        "memo": memo,
        "check_number": check_number,
    }
    invoices = [{
        "invoice_number": invoice_row["invoice_number"],
        "invoice_date": invoice_row["invoice_date"],
        "total": total,
        "amount_applied": total,
    }]
    _generate_check_pdf(
        payment_dict=payment_dict,
        invoices=invoices,
        config_dict=dict(config),
        vendor_info=dict(vendor_bp) if vendor_bp else None,
        check_number=check_number,
        output_path=pdf_path,
    )

    # 10) Queue print_job
    cur.execute(
        """
        INSERT INTO print_jobs
        (kind, payment_id, check_number, location, pdf_path, status)
        VALUES ('check', ?, ?, ?, ?, 'pending')
        """,
        (ap_payment_id, check_number, location, pdf_path),
    )
    print_job_id = cur.lastrowid

    return ap_payment_id, check_number, print_job_id, pdf_path


# ──────────────────────────────────────────────────────────────────────────────
# Top-level orchestrator — called from confirm_invoice()
# ──────────────────────────────────────────────────────────────────────────────

def process_invoice_for_auto_pay(invoice_id):
    """Decide and execute auto-pay for an invoice. Never raises.

    Caller (confirm_invoice) wraps in its own try/except too — this is
    defence in depth.
    """
    conn = None
    try:
        conn = get_connection()
        invoice_row = conn.execute(
            "SELECT * FROM scanned_invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        if not invoice_row:
            logger.warning(f"auto_pay: invoice #{invoice_id} not found")
            return

        # Skip noisy logging for the common case of non-auto-pay vendors.
        # We still log a decision row for ANY confirmed invoice so the daily
        # email shows totals — but mark non-eligible cases distinctly.
        eligible, reason, details = should_auto_pay(conn, invoice_row)

        if not eligible:
            _log_decision(
                conn, invoice_id, invoice_row["vendor_name"], invoice_row["total"],
                "skipped", reason, details,
            )
            conn.commit()
            if reason not in ("vendor_not_auto_pay", "no_vendor_bill_pay_record"):
                # Only chat-log if it's an interesting skip (auto-pay vendor
                # that hit a guardrail). Skipping every non-auto-pay vendor
                # would spam the logs.
                logger.info(
                    f"auto_pay: invoice #{invoice_id} ({invoice_row['vendor_name']}) "
                    f"skipped: {reason} {details or ''}"
                )
            return

        # Eligible — execute.
        try:
            ap_id, check_num, job_id, pdf_path = execute_auto_pay(conn, invoice_row)
            _log_decision(
                conn, invoice_id, invoice_row["vendor_name"], invoice_row["total"],
                "paid", "auto_paid",
                f"check #{check_num} → print_job #{job_id} → {pdf_path}",
                ap_payment_id=ap_id, check_number=check_num,
                print_job_id=job_id,
            )
            conn.commit()
            logger.info(
                f"auto_pay: invoice #{invoice_id} ({invoice_row['vendor_name']}) "
                f"PAID — check #{check_num}, ap_payment #{ap_id}, "
                f"print_job #{job_id}"
            )
        except Exception as exec_err:
            conn.rollback()
            _log_decision(
                conn, invoice_id, invoice_row["vendor_name"], invoice_row["total"],
                "error", "execute_failed", str(exec_err),
            )
            conn.commit()
            logger.exception(f"auto_pay: execute failed for invoice #{invoice_id}")

    except Exception:
        logger.exception(f"auto_pay: top-level failure for invoice #{invoice_id}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
