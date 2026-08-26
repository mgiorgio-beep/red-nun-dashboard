"""
Ingest guard — near-duplicate / transposition detection for newly ingested invoices.

Born from the 2026-08-26 L. Knife reconciliation: a $1,431.50 credit applied
against invoice #581876 was re-keyed as its own payable "#081876" (transposed
leading digit) and sat in open AP for weeks — nothing stopped it from being
paid. This module runs BEFORE an invoice becomes a live payable (the
pending -> confirmed transition, and the create-manual path). Any hit holds
the invoice in Pending Review with a human-readable reason naming the
colliding invoice; a held invoice never reaches auto_pay or check printing
because both require status = 'confirmed'.

Rules (same vendor, case-insensitive; same location unless noted):
  1. Amount equals an existing invoice's partial amount_paid — the exact shape
     of the 081876 bug (a credit/partial payment re-entered as an invoice).
  2. Invoice number within edit distance 1 (substitution / insertion /
     deletion / adjacent transposition) of an existing number for the same
     vendor (any location), corroborated by same amount, same subtotal, or
     same date+location. Sequential-sibling numbers are suppressed: vendors
     legitimately issue several consecutive numbers per delivery run
     (L. Knife weekly pairs, SG/PFG/US Foods same-day routes, Glanola's
     month-prefixed pairs), so numbers whose digits differ by <= 10 as
     integers are treated as siblings, not typos. A transposed or misread
     digit anywhere but the tail produces a numeric delta far above 10.
  3. Exact duplicate shape: same vendor + location + amount + invoice date
     under a different number.
  4. Foot-check failure: line items + tax off from the stated total by more
     than $0.05. Originally shipped as max($100, 5%) because US Foods CSVs
     carried $2-$90 gaps from fee lines absent from the CSV export; since
     2026-08-26 the USF parser synthesizes a "Fees & tax (unitemized)" line
     for that residual, and re-measurement showed 0 gapped CSV invoices in
     90 days. Tightened to $0.05 (matching the OCR path's validate_extraction
     gate) — on 90 days of data this holds exactly one invoice, a genuine
     Sprague OCR error ($24.68) that validation's best-of-pretax/posttax
     comparison had let through.

Tuning was validated against the last 90 days of confirmed invoices
(401 invoices, all vendors, both locations): 0 false positives, and a
re-insert of the 081876 row is caught by rules 1 and 2.
"""

import re
import logging

logger = logging.getLogger(__name__)

# How far back to look for colliding invoices. Bounds the scan; a phantom
# re-keyed from an invoice older than this is out of AP's active window anyway.
CANDIDATE_WINDOW_DAYS = 400

# Rule 4 thresholds — see module docstring. Tightened 2026-08-26 from
# max($100, 5%) after the USF fee-residual fix zeroed all CSV foot gaps.
FOOT_GAP_ABS = 0.05
FOOT_GAP_PCT = 0.0


def _osa_leq1(a, b):
    """True if optimal-string-alignment distance <= 1: one substitution,
    insertion, deletion, or adjacent transposition."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diffs = [i for i in range(la) if a[i] != b[i]]
        if len(diffs) == 1:
            return True  # substitution
        if (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]):
            return True  # adjacent transposition
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]  # single insertion


def _sequential_siblings(a, b):
    """Numbers a vendor plausibly issued in the same delivery run.
    Digits-only compare so 'JUNE18653'/'JUNE18652' count as siblings."""
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    if not da or not db or len(da) != len(db):
        return False
    try:
        return abs(int(da) - int(db)) <= 10
    except ValueError:
        return False


def _r2(cur):
    return round(float(cur or 0), 2)


def check_invoice(conn, invoice):
    """Run all guard rules for one invoice against existing confirmed invoices.

    Args:
        conn: DB connection (Row factory).
        invoice: dict with vendor_name, location, invoice_number, invoice_date,
                 total, subtotal, tax; optional id (excluded from matching) and
                 items_sum (line items total; queried by id if absent).

    Returns:
        list of hit dicts: {"rule", "reason", "existing_id", "existing_number"}.
        Empty list = clean.
    """
    hits = []
    vendor = (invoice.get("vendor_name") or "").strip()
    location = (invoice.get("location") or "").strip()
    inv_num = str(invoice.get("invoice_number") or "").strip().upper()
    inv_date = invoice.get("invoice_date")
    total = _r2(invoice.get("total"))
    subtotal = _r2(invoice.get("subtotal"))
    exclude_id = invoice.get("id") or -1
    if not vendor:
        return hits

    candidates = conn.execute(
        """
        SELECT id, invoice_number, invoice_date, location, total, subtotal,
               COALESCE(amount_paid, 0) AS amount_paid
        FROM scanned_invoices
        WHERE vendor_name = ? COLLATE NOCASE
          AND status = 'confirmed'
          AND id != ?
          AND DATE(COALESCE(invoice_date, created_at)) >= DATE('now', ?)
        """,
        (vendor, exclude_id, f"-{CANDIDATE_WINDOW_DAYS} days"),
    ).fetchall()

    for e in candidates:
        e_num = str(e["invoice_number"] or "").strip().upper()
        e_loc = (e["location"] or "").strip()
        e_total = _r2(e["total"])
        e_paid = _r2(e["amount_paid"])

        # Rule 1 — amount equals an existing invoice's PARTIAL amount_paid
        # (a credit or partial payment re-keyed as its own invoice).
        if (total > 0 and e_paid > 0 and e_paid == total and e_paid != e_total
                and e_loc == location):
            hits.append({
                "rule": 1,
                "existing_id": e["id"],
                "existing_number": e["invoice_number"],
                "reason": (
                    f"Total ${total:,.2f} equals the partial payment/credit already "
                    f"applied to invoice #{e['invoice_number']} (paid ${e_paid:,.2f} "
                    f"of ${e_total:,.2f}) — likely a payment or credit re-keyed as "
                    f"its own invoice, not a new bill."
                ),
            })

        if e_num and inv_num and e_num != inv_num:
            # Rule 2 — invoice number one edit away, with corroboration.
            if _osa_leq1(inv_num, e_num) and not _sequential_siblings(inv_num, e_num):
                same_amount = total != 0 and total == e_total
                same_subtotal = subtotal != 0 and subtotal == _r2(e["subtotal"])
                same_date_loc = (inv_date and inv_date == e["invoice_date"]
                                 and e_loc == location)
                if same_amount or same_subtotal or same_date_loc:
                    why = ("same amount" if same_amount
                           else "same subtotal" if same_subtotal
                           else "same date and location")
                    hits.append({
                        "rule": 2,
                        "existing_id": e["id"],
                        "existing_number": e["invoice_number"],
                        "reason": (
                            f"Invoice number #{invoice.get('invoice_number')} is one "
                            f"typo/transposition away from existing invoice "
                            f"#{e['invoice_number']} ({e['invoice_date']}, "
                            f"${e_total:,.2f}) with {why} — possible mis-keyed number."
                        ),
                    })

            # Rule 3 — same vendor + location + amount + date, different number.
            if (e_loc == location and inv_date and inv_date == e["invoice_date"]
                    and total != 0 and total == e_total):
                hits.append({
                    "rule": 3,
                    "existing_id": e["id"],
                    "existing_number": e["invoice_number"],
                    "reason": (
                        f"Same vendor, location, date ({inv_date}) and amount "
                        f"(${total:,.2f}) as existing invoice #{e['invoice_number']} "
                        f"under a different number — possible duplicate."
                    ),
                })

    # Rule 4 — egregious foot-check failure.
    if total > 0:
        items_sum = invoice.get("items_sum")
        if items_sum is None and invoice.get("id"):
            row = conn.execute(
                "SELECT ROUND(SUM(total_price), 2) AS s FROM scanned_invoice_items "
                "WHERE invoice_id = ?",
                (invoice["id"],),
            ).fetchone()
            items_sum = row["s"] if row else None
        if items_sum is not None:
            gap = round(abs(_r2(items_sum) + _r2(invoice.get("tax")) - total), 2)
            if gap > max(FOOT_GAP_ABS, FOOT_GAP_PCT * total):
                hits.append({
                    "rule": 4,
                    "existing_id": None,
                    "existing_number": None,
                    "reason": (
                        f"Line items (${_r2(items_sum):,.2f}) + tax "
                        f"(${_r2(invoice.get('tax')):,.2f}) are ${gap:,.2f} off the "
                        f"stated total ${total:,.2f} — extraction may have merged or "
                        f"dropped lines."
                    ),
                })

    return hits


def format_hold_reasons(hits):
    """One human-readable line per hit, for notes / validation issues."""
    return [f"[guard rule {h['rule']}] {h['reason']}" for h in hits]
