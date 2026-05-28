#!/usr/bin/env python3
"""
Receipt classifier + invoice matcher for auto-pay payment receipts.

Sits in front of the regular invoice email pipeline. Given a Gmail message
(dict, as returned by Gmail API's messages.get(format='full')), decide whether
it's a payment receipt; if so, extract vendor/amount/date/invoice_no and find
the matching open invoice in `scanned_invoices`.

Tier 1 (silent auto-apply) vendors are listed in `RECEIPT_SIGNATURES` below.
A message that matches a Tier 1 signature AND finds exactly one open invoice
in the matcher's window is eligible for auto-apply.

Anything else (Tier 1 with no/multi match, or non-Tier-1 receipt) returns a
ClassifiedReceipt with status='needs_review' and is meant to be surfaced on
the Payments page for manual confirm.

This module is read-only and pure — no DB writes. The caller (poller or
billpay route) is responsible for applying the payment via the existing
`mark-paid-external` machinery in routes/billpay_routes.py.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List

logger = logging.getLogger(__name__)

# ── Receipt signatures ────────────────────────────────────────────────────────
#
# Each signature: regex on From + regex on Subject + a body/subject parser.
# `tier` is "auto_apply" (Tier 1) or "needs_review" (Tier 2). The poller uses
# this to decide whether to apply silently or hold for manual confirmation.

# Tier 1 vendors confirmed from samples in dashboard@rednun.com inbox.
RECEIPT_SIGNATURES = [
    {
        "key": "suburban_supply_qb",
        "from_regex": re.compile(r"quickbooks@notification\.intuit\.com", re.I),
        "subject_regex": re.compile(r"^Payment Receipt from SUBURBAN SUPPLY", re.I),
        "vendor_canonical": "Suburban Supply",
        "tier": "auto_apply",
        "payment_method": "debit_card_autopay",
        # Amount lives only in the PDF attachment. We don't OCR here in the
        # classifier — caller passes attachment text if available, else this
        # signature returns amount=None and the receipt is held for review.
        "parser": "pdf_attachment",
    },
    {
        "key": "tiger_exchange",
        "from_regex": re.compile(r"@tigerexchange\.us", re.I),
        "subject_regex": re.compile(r"Payment Receipt.*Tiger Exchange", re.I),
        "vendor_canonical": "Tiger Exchange",
        "tier": "auto_apply",
        "payment_method": "debit_card_autopay",
        "parser": "body_regex",
        # "Your Payment receipt -chg cc on file for 132.33 is attached."
        "amount_regex": re.compile(r"for\s+\$?([\d,]+\.\d{2})\s+is attached", re.I),
    },
    {
        "key": "quickbooks_payments",
        "from_regex": re.compile(r"^[\"']?QuickBooks Payments[\"']?\s*<quickbooks@notification\.intuit\.com>", re.I),
        "subject_regex": re.compile(r"^Payment confirmation: Invoice #", re.I),
        # vendor canonical comes from the subject — see parse_quickbooks_payments
        "vendor_canonical": None,
        "tier": "auto_apply",
        "payment_method": "qb_autopay",
        "parser": "quickbooks_payments",
    },
    {
        "key": "barrows_waste",
        "from_regex": re.compile(r"no-reply@servicecore\.com", re.I),
        "subject_regex": re.compile(r"^Payment Receipt P\d+", re.I),
        "vendor_canonical": "Barrows Waste Systems",
        "tier": "auto_apply",
        "payment_method": "card_via_portal",
        "parser": "body_regex",
        # "Paid: 500.00"  (also appears as "$500.00" elsewhere — accept both)
        "amount_regex": re.compile(r"Paid:\s*\$?([\d,]+\.\d{2})", re.I),
    },
]

# Subject patterns we should always treat as "not a receipt" (statements,
# invoices, marketing) even if other heuristics would match.
NON_RECEIPT_SUBJECT_DENYLIST = [
    re.compile(r"^Statement from", re.I),
    re.compile(r"^Invoice \d+ from", re.I),
    re.compile(r"^Inv[a-z]*\s*\d+ from", re.I),
]


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class ClassifiedReceipt:
    """The classifier's read of a single Gmail message."""
    message_id: str
    signature_key: str
    vendor_canonical: str
    amount: Optional[float]
    invoice_number: Optional[str]
    payment_date: Optional[str]       # ISO YYYY-MM-DD
    payment_method: str
    tier: str                          # "auto_apply" or "needs_review"
    raw_subject: str
    raw_from: str
    parse_notes: List[str] = field(default_factory=list)


@dataclass
class MatchResult:
    """The matcher's verdict for a ClassifiedReceipt."""
    receipt: ClassifiedReceipt
    matched_invoice_id: Optional[int]
    candidate_count: int               # 0 = no match, 1 = clean, >1 = ambiguous
    candidates: List[dict] = field(default_factory=list)
    decision: str = ""                 # "auto_apply" | "needs_review" | "no_match"
    reason: str = ""


# ── Header / body helpers ─────────────────────────────────────────────────────

def _get_header(headers: list, name: str) -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def _get_text_body(payload: dict) -> str:
    """Return best-effort plaintext body from a Gmail message payload."""
    import base64

    def walk(part) -> Optional[str]:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        filename = part.get("filename") or ""
        if data and not filename:
            try:
                decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:
                return None
            if mime == "text/plain":
                return decoded
        for sub in part.get("parts", []) or []:
            r = walk(sub)
            if r:
                return r
        return None

    return walk(payload) or ""


def _parse_date_header(date_str: str) -> Optional[str]:
    """Parse RFC 2822 date header into ISO YYYY-MM-DD."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


# ── Per-signature parsers ─────────────────────────────────────────────────────

def _parse_amount_with_regex(text: str, regex: re.Pattern) -> Optional[float]:
    m = regex.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None


def _parse_quickbooks_payments(subject: str, body_text: str) -> dict:
    """
    Parse a QuickBooks Payments confirmation.
    Subject: "Payment confirmation: Invoice #15857-(Fore and Aft, Inc.)"
    Body snippet: "You paid $695.00 to Fore and Aft, Inc. on 05/27/2026 ..."
    """
    out = {"amount": None, "invoice_number": None, "vendor_canonical": None}

    m = re.search(r"Invoice #([^-)\s]+)", subject)
    if m:
        out["invoice_number"] = m.group(1).strip()

    m = re.search(r"Invoice #[^-]+-\(([^)]+)\)", subject)
    if m:
        out["vendor_canonical"] = m.group(1).strip()

    m = re.search(r"You paid\s+\$?([\d,]+\.\d{2})", body_text or "")
    if m:
        try:
            out["amount"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    return out


# ── Main classifier ───────────────────────────────────────────────────────────

def classify_message(msg: dict, pdf_text: Optional[str] = None) -> Optional[ClassifiedReceipt]:
    """
    Classify a Gmail message as a payment receipt.

    Args:
        msg: Gmail API message dict (format='full').
        pdf_text: Optional already-extracted text from the first PDF attachment,
                  used by signatures whose amount lives in the PDF (Suburban).

    Returns:
        A ClassifiedReceipt, or None if the message is not a recognized receipt.
    """
    payload = msg.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    from_hdr = _get_header(headers, "From")
    subject = _get_header(headers, "Subject")
    date_hdr = _get_header(headers, "Date")
    message_id = msg.get("id", "")

    # If this is a forward, the original sender is the one we want to match.
    # Heuristic: forwarded messages have "Fwd:" or "FW:" in the subject and
    # the original "From: ..." line appears in the body.
    is_forward = bool(re.match(r"^\s*Fwd?:\s", subject, re.I))
    body_text = _get_text_body(payload)
    effective_from = from_hdr
    effective_subject = subject
    if is_forward:
        m = re.search(r"From:\s*([^\r\n]+?)<([^>]+)>", body_text)
        if m:
            effective_from = f"{m.group(1).strip()}<{m.group(2).strip()}>"
        m = re.search(r"Subject:\s*([^\r\n]+)", body_text)
        if m:
            effective_subject = m.group(1).strip()

    # Hard denylist — never classify a Statement or Invoice notification as a receipt
    for pat in NON_RECEIPT_SUBJECT_DENYLIST:
        if pat.search(effective_subject):
            return None

    for sig in RECEIPT_SIGNATURES:
        if not sig["from_regex"].search(effective_from):
            continue
        if not sig["subject_regex"].search(effective_subject):
            continue

        notes = []
        if is_forward:
            notes.append("classified-on-forwarded-message")

        # Run the parser
        amount = None
        invoice_number = None
        vendor_canonical = sig["vendor_canonical"]
        parser = sig["parser"]

        if parser == "body_regex":
            amount = _parse_amount_with_regex(body_text, sig["amount_regex"])
            if amount is None:
                notes.append("amount-regex-no-match")
        elif parser == "quickbooks_payments":
            parsed = _parse_quickbooks_payments(effective_subject, body_text)
            amount = parsed["amount"]
            invoice_number = parsed["invoice_number"]
            vendor_canonical = parsed["vendor_canonical"] or vendor_canonical
        elif parser == "pdf_attachment":
            if pdf_text:
                # Reuse the same amount regex pattern as bodies — most PDFs
                # surface "Total" or "Amount Paid" near the dollar figure.
                m = re.search(r"(?:Total|Amount(?:\s+Paid)?|Paid)[:\s]+\$?([\d,]+\.\d{2})",
                              pdf_text, re.I)
                if m:
                    try:
                        amount = float(m.group(1).replace(",", ""))
                    except ValueError:
                        pass
            if amount is None:
                notes.append("pdf-attachment-amount-unparsed")

        return ClassifiedReceipt(
            message_id=message_id,
            signature_key=sig["key"],
            vendor_canonical=vendor_canonical or "(unknown)",
            amount=amount,
            invoice_number=invoice_number,
            payment_date=_parse_date_header(date_hdr),
            payment_method=sig["payment_method"],
            tier=sig["tier"],
            raw_subject=subject,
            raw_from=from_hdr,
            parse_notes=notes,
        )

    return None


# ── Matcher ───────────────────────────────────────────────────────────────────

def find_matching_invoice(receipt: ClassifiedReceipt, conn,
                          amount_tolerance: float = 0.01,
                          date_window_days: int = 60) -> MatchResult:
    """
    Find an open invoice in scanned_invoices that matches this receipt.

    Strategy:
      1. If we know the invoice_number, use it as a direct lookup (vendor + num).
      2. Else match by vendor + amount within ±tolerance + date within window
         + payment_status unpaid.

    Returns a MatchResult with decision = auto_apply / needs_review / no_match.
    """
    if receipt.amount is None or receipt.amount <= 0:
        return MatchResult(receipt=receipt, matched_invoice_id=None,
                           candidate_count=0, decision="no_match",
                           reason="amount-unknown")

    cur = conn.cursor()

    # Strategy 1 — direct lookup by invoice_number
    candidates = []
    if receipt.invoice_number:
        rows = cur.execute(
            """SELECT id, vendor_name, invoice_number, invoice_date, total,
                      COALESCE(balance, total) AS balance, payment_status, location
                 FROM scanned_invoices
                WHERE invoice_number = ?
                  AND (payment_status != 'paid' OR payment_status IS NULL)""",
            (receipt.invoice_number,),
        ).fetchall()
        candidates = [dict(r) for r in rows]

    # Strategy 2 — fuzzy match on vendor + amount + recent date
    if not candidates:
        amount_lo = receipt.amount * (1 - amount_tolerance)
        amount_hi = receipt.amount * (1 + amount_tolerance)
        date_lo = None
        if receipt.payment_date:
            try:
                pd = datetime.strptime(receipt.payment_date, "%Y-%m-%d")
                date_lo = (pd - timedelta(days=date_window_days)).strftime("%Y-%m-%d")
            except ValueError:
                pass

        sql = """SELECT id, vendor_name, invoice_number, invoice_date, total,
                        COALESCE(balance, total) AS balance, payment_status, location
                   FROM scanned_invoices
                  WHERE LOWER(vendor_name) LIKE ?
                    AND COALESCE(balance, total) BETWEEN ? AND ?
                    AND (payment_status != 'paid' OR payment_status IS NULL)"""
        params = [f"%{(receipt.vendor_canonical or '').lower()}%", amount_lo, amount_hi]
        if date_lo:
            sql += " AND invoice_date >= ?"
            params.append(date_lo)
        sql += " ORDER BY invoice_date DESC"

        rows = cur.execute(sql, params).fetchall()
        candidates = [dict(r) for r in rows]

    if not candidates:
        return MatchResult(receipt=receipt, matched_invoice_id=None,
                           candidate_count=0, decision="no_match",
                           reason=f"no open invoice for {receipt.vendor_canonical} @ ${receipt.amount:.2f}",
                           candidates=[])

    if len(candidates) == 1:
        decision = "auto_apply" if receipt.tier == "auto_apply" else "needs_review"
        return MatchResult(receipt=receipt, matched_invoice_id=candidates[0]["id"],
                           candidate_count=1, decision=decision,
                           reason="single match", candidates=candidates)

    # Multiple candidates — always hold for review even if Tier 1
    return MatchResult(receipt=receipt, matched_invoice_id=None,
                       candidate_count=len(candidates), decision="needs_review",
                       reason=f"ambiguous: {len(candidates)} open invoices match",
                       candidates=candidates)
