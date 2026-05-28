#!/usr/bin/env python3
"""
Receipt classifier + invoice matcher for auto-pay payment receipts.

Sits in front of the regular invoice email pipeline. Given a Gmail message
(dict, as returned by Gmail API's messages.get(format='full')), decide whether
it's a payment receipt; if so, extract vendor / total amount / one or more
line items (invoice_number, amount) / date / payment method.

Multi-invoice receipts (L. Knife, Colonial, US Foods) are first-class: the
`line_items` list carries 1+ entries. Single-invoice receipts (Tiger, Barrows,
QB Payments, Suburban, PFG) have exactly one entry. The matcher and poller
treat both the same — they iterate line items and try to match each one.

Tier 1 (silent auto-apply) vendors are listed in `RECEIPT_SIGNATURES` below.
A line item that matches a Tier 1 signature AND finds exactly one open
invoice in the matcher is eligible for auto-apply.

This module is read-only and pure — no DB writes. The caller (poller or
billpay route) is responsible for applying the payment via the existing
`mark-paid-external` machinery in routes/billpay_routes.py.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class ReceiptLineItem:
    invoice_number: str
    amount: float


@dataclass
class ClassifiedReceipt:
    """The classifier's read of a single Gmail message."""
    message_id: str
    signature_key: str
    vendor_canonical: str
    total_amount: Optional[float]
    line_items: List[ReceiptLineItem] = field(default_factory=list)
    payment_date: Optional[str] = None        # ISO YYYY-MM-DD
    payment_method: str = ""
    tier: str = "auto_apply"
    raw_subject: str = ""
    raw_from: str = ""
    parse_notes: List[str] = field(default_factory=list)

    # Back-compat accessors so older callers still work
    @property
    def amount(self) -> Optional[float]:
        if self.total_amount is not None:
            return self.total_amount
        if self.line_items:
            return self.line_items[0].amount
        return None

    @property
    def invoice_number(self) -> Optional[str]:
        if len(self.line_items) == 1:
            return self.line_items[0].invoice_number
        return None


@dataclass
class MatchResult:
    """The matcher's verdict for a single line item."""
    line_item: ReceiptLineItem
    matched_invoice_id: Optional[int]
    candidate_count: int
    candidates: List[dict] = field(default_factory=list)
    decision: str = ""        # "auto_apply" | "needs_review" | "no_match"
    reason: str = ""


# ── Header / body helpers ─────────────────────────────────────────────────────

def _get_header(headers: list, name: str) -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def _get_text_body(payload: dict) -> str:
    """
    Return best-effort plaintext body from a Gmail message payload.
    Prefers text/plain; falls back to a cheap HTML→text strip when only
    text/html is available (e.g. QuickBooks Payments emails are HTML-only).
    """
    import base64

    found_plain = []
    found_html = []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        filename = part.get("filename") or ""
        if data and not filename:
            try:
                decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            except Exception:
                decoded = None
            if decoded:
                if mime == "text/plain":
                    found_plain.append(decoded)
                elif mime == "text/html":
                    found_html.append(decoded)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)

    if found_plain:
        return "\n".join(found_plain)
    if found_html:
        html = "\n".join(found_html)
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
        text = re.sub(r"</tr\s*>", "\n", text, flags=re.I)
        text = re.sub(r"</td\s*>", " | ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
        text = re.sub(r"\s+", " ", text).strip()
        return text
    return ""


def _parse_date_header(date_str: str) -> Optional[str]:
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


# Total amount regex for VTInfo-platform receipts (L. Knife, Colonial)
_VTINFO_TOTAL_REGEX = re.compile(
    r"received\s+your\s+pending\s+ACH\s+payment\s+of\s+\$?([\d,]+\.\d{2})",
    re.I,
)
# Invoice row in VTInfo body — invoice number (digits, sometimes alphanum)
# followed by an amount, possibly with a credits column in between.
# Lines look like:  "552223 $565.40"   or   "206074 -$7.00"   or "500561  $430.10"
# The amount can be: $X.XX, -$X.XX, $-X.XX, -X.XX, X.XX
_VTINFO_LINE_REGEX = re.compile(
    r"^\s*([A-Z]{0,3}\d{4,})\s+(-?\$?-?[\d,]+\.\d{2})\s*$",
    re.M,
)


def _parse_money(s: str) -> Optional[float]:
    """Parse a money string like '$1,416.20', '-$7.00', or '3494.33'."""
    s = (s or "").replace("$", "").replace(",", "").strip()
    # Handle accidental double negative ("--7.00")
    s = re.sub(r"^--", "-", s)
    try:
        return float(s)
    except ValueError:
        return None


def _parse_vtinfo_invoice_table(body_text: str) -> Tuple[Optional[float], List[ReceiptLineItem]]:
    """L. Knife and Colonial (both noreply@vtinfo.com) share this body format."""
    total = None
    m = _VTINFO_TOTAL_REGEX.search(body_text or "")
    if m:
        total = _parse_money(m.group(1))

    items: List[ReceiptLineItem] = []
    for m in _VTINFO_LINE_REGEX.finditer(body_text or ""):
        inv_no = m.group(1).strip()
        amt = _parse_money(m.group(2))
        if amt is None:
            continue
        # Skip lines that look like a date (e.g. "05/26/2026") — defensive.
        if "/" in inv_no or "." in inv_no:
            continue
        items.append(ReceiptLineItem(invoice_number=inv_no, amount=amt))

    return total, items


# US Foods ACH remit format
_USFOODS_TOTAL_REGEX = re.compile(
    r"Total\s+transaction\s+amount\s*=\s*\$?([\d,]+\.\d{2})",
    re.I,
)
# Body lines like: "2413406 05/04/26 INVOICE       3,494.33 EFT0528 ..."
_USFOODS_LINE_REGEX = re.compile(
    r"^\s*(\d{6,})\s+\d{2}/\d{2}/\d{2}\s+(?:INVOICE|CREDIT|CM|ADJ)\s+\$?(-?[\d,]+\.\d{2})",
    re.M | re.I,
)


def _parse_usfoods_remit(body_text: str) -> Tuple[Optional[float], List[ReceiptLineItem]]:
    total = None
    m = _USFOODS_TOTAL_REGEX.search(body_text or "")
    if m:
        try:
            total = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    items: List[ReceiptLineItem] = []
    for m in _USFOODS_LINE_REGEX.finditer(body_text or ""):
        try:
            items.append(ReceiptLineItem(
                invoice_number=m.group(1).strip(),
                amount=float(m.group(2).replace(",", "")),
            ))
        except ValueError:
            continue

    return total, items


# PFG via Billfire — single total, no invoice breakdown in email
_PFG_AMOUNT_REGEX = re.compile(
    r"Payment\s+amount\s*\$?([\d,]+\.\d{2})",
    re.I,
)


def _parse_pfg_billfire(body_text: str) -> Tuple[Optional[float], List[ReceiptLineItem]]:
    """PFG's Billfire emails show one total — no per-invoice breakdown."""
    amt = _parse_amount_with_regex(body_text, _PFG_AMOUNT_REGEX)
    return amt, []   # empty line items — matcher will fuzzy-match on total


# ── Receipt signatures ────────────────────────────────────────────────────────

RECEIPT_SIGNATURES = [
    {
        "key": "suburban_supply_qb",
        "from_regex": re.compile(r"quickbooks@notification\.intuit\.com", re.I),
        "subject_regex": re.compile(r"^Payment Receipt from SUBURBAN SUPPLY", re.I),
        "vendor_canonical": "Suburban Supply",
        "tier": "auto_apply",
        "payment_method": "debit_card_autopay",
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
        "amount_regex": re.compile(r"for\s+\$?([\d,]+\.\d{2})\s+is attached", re.I),
    },
    {
        "key": "quickbooks_payments",
        "from_regex": re.compile(r"^[\"']?QuickBooks Payments[\"']?\s*<quickbooks@notification\.intuit\.com>", re.I),
        "subject_regex": re.compile(r"^Payment confirmation: Invoice #", re.I),
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
        "amount_regex": re.compile(r"Paid:\s*\$?([\d,]+\.\d{2})", re.I),
    },
    {
        "key": "lknife_vtinfo",
        "from_regex": re.compile(r"noreply@vtinfo\.com", re.I),
        "subject_regex": re.compile(r"Payment confirmation from L Knife", re.I),
        "vendor_canonical": "L. Knife & Son",
        "tier": "auto_apply",
        "payment_method": "ach_via_portal",
        "parser": "vtinfo_table",
    },
    {
        "key": "colonial_vtinfo",
        "from_regex": re.compile(r"noreply@vtinfo\.com", re.I),
        "subject_regex": re.compile(r"Colonial.*Payment Confirmation", re.I),
        "vendor_canonical": "Colonial Wholesale Beverage",
        "tier": "auto_apply",
        "payment_method": "ach_via_portal",
        "parser": "vtinfo_table",
    },
    {
        "key": "usfoods_ach_remit",
        "from_regex": re.compile(r"usfoods-notification@usfoods\.com", re.I),
        "subject_regex": re.compile(r"^ACH Remit Advice", re.I),
        "vendor_canonical": "US Foods",
        "tier": "auto_apply",
        "payment_method": "ach_remit",
        "parser": "usfoods_remit",
    },
    {
        "key": "pfg_billfire",
        "from_regex": re.compile(r"no-reply@valet\.billfire\.com", re.I),
        "subject_regex": re.compile(r"^Click2Pay confirmation", re.I),
        "vendor_canonical": "Performance Foodservice",
        "tier": "auto_apply",
        "payment_method": "ach_via_billfire",
        "parser": "pfg_billfire",
    },
]

# Subjects we should never treat as receipts even if other heuristics match.
NON_RECEIPT_SUBJECT_DENYLIST = [
    re.compile(r"^Statement from", re.I),
    re.compile(r"^Invoice \d+ from", re.I),
    re.compile(r"^Inv[a-z]*\s*\d+ from", re.I),
    re.compile(r"^Pay invoice\b", re.I),
    re.compile(r"^US Foods Open AR", re.I),
]


# ── Main classifier ───────────────────────────────────────────────────────────

def classify_message(msg: dict, pdf_text: Optional[str] = None) -> Optional[ClassifiedReceipt]:
    """
    Classify a Gmail message as a payment receipt.

    Returns a ClassifiedReceipt with one or more line items, or None if the
    message is not a recognized receipt.
    """
    payload = msg.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    from_hdr = _get_header(headers, "From")
    subject = _get_header(headers, "Subject")
    date_hdr = _get_header(headers, "Date")
    message_id = msg.get("id", "")

    is_forward = bool(re.match(r"^\s*Fwd?:\s", subject, re.I))
    body_text = _get_text_body(payload)
    snippet = msg.get("snippet", "") or ""
    body_text = (snippet + "\n" + body_text).strip()

    effective_from = from_hdr
    effective_subject = subject
    if is_forward:
        m = re.search(r"From:\s*([^\r\n]+?)<([^>]+)>", body_text)
        if m:
            effective_from = f"{m.group(1).strip()}<{m.group(2).strip()}>"
        # Capture the original Subject — Gmail wraps long subjects across
        # multiple lines in forwarded bodies, so grab everything until we hit
        # the next header (To:/From:/Date:/Cc:) or a blank line.
        m = re.search(
            r"Subject:\s*(.+?)(?:\r?\n(?:To|From|Date|Cc|Bcc|Reply-To):\s|\r?\n\r?\n)",
            body_text,
            re.S,
        )
        if m:
            effective_subject = re.sub(r"\s+", " ", m.group(1)).strip()

    # Match candidates — for forwards we ALSO try the outer "Fwd:" subject
    # because the body-extracted one can be truncated by quoting or wrapping.
    subjects_to_try = [effective_subject]
    if is_forward and subject not in subjects_to_try:
        subjects_to_try.append(subject)

    # Deny on either subject (so we don't accidentally classify a forwarded
    # Statement / Invoice email as a receipt).
    for pat in NON_RECEIPT_SUBJECT_DENYLIST:
        if any(pat.search(s) for s in subjects_to_try):
            return None

    for sig in RECEIPT_SIGNATURES:
        if not sig["from_regex"].search(effective_from):
            continue
        if not any(sig["subject_regex"].search(s) for s in subjects_to_try):
            continue

        notes = []
        if is_forward:
            notes.append("classified-on-forwarded-message")

        total_amount: Optional[float] = None
        line_items: List[ReceiptLineItem] = []
        vendor_canonical = sig["vendor_canonical"]
        parser = sig["parser"]

        if parser == "body_regex":
            amt = _parse_amount_with_regex(body_text, sig["amount_regex"])
            if amt is not None:
                total_amount = amt
                line_items = []  # no per-invoice breakdown
            else:
                notes.append("amount-regex-no-match")

        elif parser == "quickbooks_payments":
            parsed = _parse_quickbooks_payments(effective_subject, body_text)
            total_amount = parsed["amount"]
            vendor_canonical = parsed["vendor_canonical"] or vendor_canonical
            if parsed["invoice_number"] and parsed["amount"] is not None:
                line_items = [ReceiptLineItem(
                    invoice_number=parsed["invoice_number"],
                    amount=parsed["amount"],
                )]

        elif parser == "pdf_attachment":
            if pdf_text:
                m = re.search(r"(?:Total|Amount(?:\s+Paid)?|Paid)[:\s]+\$?([\d,]+\.\d{2})",
                              pdf_text, re.I)
                if m:
                    try:
                        total_amount = float(m.group(1).replace(",", ""))
                    except ValueError:
                        pass
            if total_amount is None:
                notes.append("pdf-attachment-amount-unparsed")

        elif parser == "vtinfo_table":
            total_amount, line_items = _parse_vtinfo_invoice_table(body_text)
            if total_amount is None:
                notes.append("vtinfo-total-unparsed")
            if not line_items:
                notes.append("vtinfo-line-items-unparsed")

        elif parser == "usfoods_remit":
            total_amount, line_items = _parse_usfoods_remit(body_text)
            if total_amount is None:
                notes.append("usfoods-total-unparsed")
            if not line_items:
                notes.append("usfoods-line-items-unparsed")

        elif parser == "pfg_billfire":
            total_amount, line_items = _parse_pfg_billfire(body_text)
            if total_amount is None:
                notes.append("pfg-amount-unparsed")

        return ClassifiedReceipt(
            message_id=message_id,
            signature_key=sig["key"],
            vendor_canonical=vendor_canonical or "(unknown)",
            total_amount=total_amount,
            line_items=line_items,
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
                          date_window_days: int = 60) -> List[MatchResult]:
    """
    Find open invoices that match this receipt.

    If line_items is non-empty: try a direct invoice_number lookup for each.
    Returns one MatchResult per line item.

    If line_items is empty: fall back to fuzzy match on the total amount.
    Returns a single MatchResult representing the whole receipt.
    """
    cur = conn.cursor()
    results: List[MatchResult] = []

    if receipt.line_items:
        for li in receipt.line_items:
            results.append(_match_one_line_item(cur, receipt, li, amount_tolerance))
        return results

    # No line items — fuzzy-match the receipt total against any open invoice
    # for this vendor (legacy behavior, used by Tiger / Barrows / PFG / Suburban).
    if receipt.total_amount is None or receipt.total_amount <= 0:
        synthetic = ReceiptLineItem(invoice_number="", amount=receipt.total_amount or 0)
        return [MatchResult(line_item=synthetic, matched_invoice_id=None,
                            candidate_count=0, decision="no_match",
                            reason="amount-unknown")]

    return [_fuzzy_match(cur, receipt, amount_tolerance, date_window_days)]


def _match_one_line_item(cur, receipt: ClassifiedReceipt, li: ReceiptLineItem,
                         amount_tolerance: float) -> MatchResult:
    # Skip credit lines (negative amounts) — those reduce a balance,
    # they shouldn't try to "find a matching open invoice".
    if li.amount < 0:
        return MatchResult(line_item=li, matched_invoice_id=None,
                           candidate_count=0, decision="no_match",
                           reason="credit-line-skipped (negative amount)")

    rows = cur.execute(
        """SELECT id, vendor_name, invoice_number, invoice_date, total,
                  COALESCE(balance, total) AS balance, payment_status, location
             FROM scanned_invoices
            WHERE invoice_number = ?
              AND (payment_status != 'paid' OR payment_status IS NULL)""",
        (li.invoice_number,),
    ).fetchall()
    candidates = [dict(r) for r in rows]

    if not candidates:
        return MatchResult(line_item=li, matched_invoice_id=None,
                           candidate_count=0, decision="no_match",
                           reason=f"no open invoice #{li.invoice_number}")

    if len(candidates) == 1:
        c = candidates[0]
        # Sanity-check the amount before auto-applying — if balance is way off,
        # hold for review rather than silently applying the wrong amount.
        balance = float(c["balance"] or 0)
        if balance > 0:
            ratio = li.amount / balance
            if ratio < (1 - amount_tolerance) or ratio > (1 + amount_tolerance):
                return MatchResult(line_item=li, matched_invoice_id=None,
                                   candidate_count=1, candidates=candidates,
                                   decision="needs_review",
                                   reason=f"amount mismatch: receipt ${li.amount} vs invoice balance ${balance}")
        decision = "auto_apply" if receipt.tier == "auto_apply" else "needs_review"
        return MatchResult(line_item=li, matched_invoice_id=c["id"],
                           candidate_count=1, candidates=candidates,
                           decision=decision, reason="single match")

    return MatchResult(line_item=li, matched_invoice_id=None,
                       candidate_count=len(candidates), candidates=candidates,
                       decision="needs_review",
                       reason=f"ambiguous: {len(candidates)} invoices share #{li.invoice_number}")


def _fuzzy_match(cur, receipt: ClassifiedReceipt,
                 amount_tolerance: float, date_window_days: int) -> MatchResult:
    li = ReceiptLineItem(invoice_number="", amount=receipt.total_amount or 0)

    amount_lo = receipt.total_amount * (1 - amount_tolerance)
    amount_hi = receipt.total_amount * (1 + amount_tolerance)
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
        return MatchResult(line_item=li, matched_invoice_id=None,
                           candidate_count=0, decision="no_match",
                           reason=f"no open invoice for {receipt.vendor_canonical} @ ${receipt.total_amount:.2f}",
                           candidates=[])

    if len(candidates) == 1:
        decision = "auto_apply" if receipt.tier == "auto_apply" else "needs_review"
        return MatchResult(line_item=li, matched_invoice_id=candidates[0]["id"],
                           candidate_count=1, decision=decision,
                           reason="single match (fuzzy)", candidates=candidates)

    return MatchResult(line_item=li, matched_invoice_id=None,
                       candidate_count=len(candidates), decision="needs_review",
                       reason=f"ambiguous: {len(candidates)} open invoices match",
                       candidates=candidates)
