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
    location: Optional[str] = None            # 'chatham' / 'dennis' / None if not detectable
    payment_date: Optional[str] = None        # ISO YYYY-MM-DD
    payment_method: str = ""
    tier: str = "auto_apply"
    raw_subject: str = ""
    raw_from: str = ""
    parse_notes: List[str] = field(default_factory=list)
    require_invoice_for_auto: bool = False

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


# ── Cintas (support@cintas.com) ───────────────────────────────────────────────
# Two formats, both HTML-only (so _get_text_body flattens them to one line with
# " | " cell separators — we parse that flattened form, not per-line):
#   "myCintas Payment Confirmation" — table with per-invoice rows
#       (Invoice Date | Account | Invoice # | Invoice Total | Payment). The
#       Account cell carries the payer code (0025041129=Dennis, 0025003893=Chatham).
#   "myCintas Autopay Confirmation" — weekly autopay summary, lump total only
#       ("Autopay Amount Processed: $X"), NO invoice numbers.
_CINTAS_ROW_REGEX = re.compile(
    r"(\d{2}/\d{2}/\d{4})\s*\|\s*(.+?)\s*\|\s*(\d{6,})\s*\|\s*\$?([\d,]+\.\d{2})\s*\|\s*\$?([\d,]+\.\d{2})"
)
_CINTAS_AUTOPAY_AMOUNT_REGEX = re.compile(
    r"Autopay\s+Amount\s+Processed:\s*\$?([\d,]+\.\d{2})", re.I
)


def _parse_cintas_payment_confirmation(body_text: str) -> Tuple[Optional[float], List[ReceiptLineItem]]:
    """Per-invoice rows — use the 'Payment' column (amount actually paid)."""
    items: List[ReceiptLineItem] = []
    for m in _CINTAS_ROW_REGEX.finditer(body_text or ""):
        amt = _parse_money(m.group(5))
        if amt is None:
            continue
        items.append(ReceiptLineItem(invoice_number=m.group(3).strip(), amount=amt))
    total = round(sum(li.amount for li in items), 2) if items else None
    return total, items


def _parse_cintas_autopay(body_text: str) -> Tuple[Optional[float], List[ReceiptLineItem]]:
    """Weekly autopay summary — lump total only, no invoice breakdown."""
    amt = _parse_amount_with_regex(body_text, _CINTAS_AUTOPAY_AMOUNT_REGEX)
    return amt, []


# Tiger Exchange — the payment receipt PDF carries the invoice number (the email
# body only has the amount). Pull the digits so we can do a direct invoice match.
# Tiger's receipt PDF has an "Invoices Paid" table; each row is:
#   <date> <invoice#> $<amount due> $<amount applied>
# e.g. "6/1/2026 36166 $132.69 $132.69". Capture every row (one receipt can
# pay more than one invoice).
_TIGER_PDF_ROW_REGEX = re.compile(
    r"\d{1,2}/\d{1,2}/\d{4}\s+(\d{4,7})\s+\$[\d,]+\.\d{2}\s+\$([\d,]+\.\d{2})"
)


def _parse_tiger_pdf_rows(pdf_text: str) -> List[ReceiptLineItem]:
    items: List[ReceiptLineItem] = []
    for m in _TIGER_PDF_ROW_REGEX.finditer(pdf_text or ""):
        amt = _parse_money(m.group(2))
        if amt is not None:
            items.append(ReceiptLineItem(invoice_number=str(int(m.group(1))), amount=amt))
    return items
# ── Location extractors ──────────────────────────────────────────────────────
#
# Each returns 'chatham', 'dennis', or None.

def _loc_lknife(subject: str, body_text: str) -> Optional[str]:
    """L. Knife: '(AR034)' = Chatham, '(AR035)' = Dennis."""
    if re.search(r"\bAR034\b", body_text or ""):
        return "chatham"
    if re.search(r"\bAR035\b", body_text or ""):
        return "dennis"
    return None


def _loc_colonial(subject: str, body_text: str) -> Optional[str]:
    """Colonial: '(R2560)' = Chatham (Red Nun Bar & Grill).
    Dennis Colonial uses a different code we don't have yet — return None there."""
    if re.search(r"\bR2560\b", body_text or "") or "RED NUN BAR & GRILL" in (body_text or "").upper():
        return "chatham"
    if "DENNIS" in (body_text or "").upper() or "DENNISPORT" in (body_text or "").upper():
        return "dennis"
    return None


def _loc_usfoods(subject: str, body_text: str) -> Optional[str]:
    """US Foods: customer codes 91097345 = Dennis, 90541301 = Chatham."""
    if re.search(r"\b91097345\b", body_text or "") or "DENNISPORT" in (body_text or "").upper():
        return "dennis"
    if re.search(r"\b90541301\b", body_text or "") or "CHATHAM" in (body_text or "").upper():
        return "chatham"
    return None


def _loc_pfg_billfire(subject: str, body_text: str) -> Optional[str]:
    """PFG/Billfire: subject contains 'CHAT' or 'DENNIS PORT'."""
    s = (subject or "").upper()
    b = (body_text or "").upper()
    if "DENNIS PORT" in s or "DENNIS PORT" in b or "DENNIS" in s:
        return "dennis"
    if "CHAT" in s or "CHATHAM" in b:
        return "chatham"
    return None


def _loc_suburban(subject: str, body_text: str) -> Optional[str]:
    """Suburban Supply: body says 'Red Nun- Dennisport - AUTO' or '...Chatham- Auto Pay'."""
    b = (body_text or "").upper()
    if "DENNISPORT" in b or "DENNIS PORT" in b:
        return "dennis"
    if "CHATHAM" in b:
        return "chatham"
    return None


def _loc_cintas(subject: str, body_text: str) -> Optional[str]:
    """Cintas payer codes: 0025041129 = Dennis, 0025003893 = Chatham."""
    b = (body_text or "").upper()
    if "0025041129" in b or "DENNIS PORT" in b or "DENNISPORT" in b:
        return "dennis"
    if "0025003893" in b or "CHATHAM" in b:
        return "chatham"
    return None


def _loc_none(subject: str, body_text: str) -> Optional[str]:
    """Signatures where the receipt carries no location info (Tiger, Barrows, QB Payments)."""
    return None


# ── Receipt signatures ────────────────────────────────────────────────────────

# Tier policy (post-2026-05-28 incident):
#   tier='auto_apply'    → signature produces explicit invoice_number line items
#                          AND we trust direct-lookup matches. Safe to auto-apply.
#   tier='needs_review'  → signature produces only a total (no per-invoice
#                          breakdown), so matcher must fuzzy-match on amount+vendor.
#                          NEVER auto-applies, always held for manual confirmation
#                          on the payments page. Required for PFG/Billfire after
#                          the 2026-05-28 wrong-bank mis-match.

RECEIPT_SIGNATURES = [
    {
        "key": "suburban_supply_qb",
        "from_regex": re.compile(r"quickbooks@notification\.intuit\.com", re.I),
        "subject_regex": re.compile(r"^Payment Receipt from SUBURBAN SUPPLY", re.I),
        "vendor_canonical": "Suburban Supply",
        "tier": "needs_review",  # PDF amount only, no invoice number — Tier 2
        "payment_method": "debit_card_autopay",
        "parser": "pdf_attachment",
        "location_extractor": _loc_suburban,
    },
    {
        "key": "tiger_exchange",
        "from_regex": re.compile(r"@tigerexchange\.us", re.I),
        "subject_regex": re.compile(r"Payment Receipt.*Tiger Exchange", re.I),
        "vendor_canonical": "Tiger Exchange",
        "tier": "auto_apply",  # invoice # comes from the attached PDF (parsed below)
        "require_invoice_for_auto": True,  # never auto-apply on amount-only fuzzy match
        "payment_method": "debit_card_autopay",
        "parser": "tiger_pdf",
        "amount_regex": re.compile(r"for\s+\$?([\d,]+\.\d{2})\s+is attached", re.I),
        "location_extractor": _loc_none,
    },
    {
        "key": "quickbooks_payments",
        "from_regex": re.compile(r"^[\"']?QuickBooks Payments[\"']?\s*<quickbooks@notification\.intuit\.com>", re.I),
        "subject_regex": re.compile(r"^Payment confirmation: Invoice #", re.I),
        "vendor_canonical": None,
        "tier": "auto_apply",  # Invoice # in subject — direct lookup
        "payment_method": "qb_autopay",
        "parser": "quickbooks_payments",
        "location_extractor": _loc_none,
    },
    {
        "key": "barrows_waste",
        "from_regex": re.compile(r"no-reply@servicecore\.com", re.I),
        "subject_regex": re.compile(r"^Payment Receipt P\d+", re.I),
        "vendor_canonical": "Barrows Waste Systems",
        "tier": "needs_review",  # No invoice number in receipt — Tier 2
        "payment_method": "card_via_portal",
        "parser": "body_regex",
        "amount_regex": re.compile(r"Paid:\s*\$?([\d,]+\.\d{2})", re.I),
        "location_extractor": _loc_none,
    },
    {
        "key": "lknife_vtinfo",
        "from_regex": re.compile(r"noreply@vtinfo\.com", re.I),
        "subject_regex": re.compile(r"Payment confirmation from L Knife", re.I),
        "vendor_canonical": "L. Knife & Son",
        "tier": "auto_apply",
        "payment_method": "ach_via_portal",
        "parser": "vtinfo_table",
        "location_extractor": _loc_lknife,
    },
    {
        "key": "colonial_vtinfo",
        "from_regex": re.compile(r"noreply@vtinfo\.com", re.I),
        "subject_regex": re.compile(r"Colonial.*Payment Confirmation", re.I),
        "vendor_canonical": "Colonial Wholesale Beverage",
        "tier": "auto_apply",
        "payment_method": "ach_via_portal",
        "parser": "vtinfo_table",
        "location_extractor": _loc_colonial,
    },
    {
        "key": "usfoods_ach_remit",
        "from_regex": re.compile(r"usfoods-notification@usfoods\.com", re.I),
        "subject_regex": re.compile(r"^ACH Remit Advice", re.I),
        "vendor_canonical": "US Foods",
        "tier": "auto_apply",
        "payment_method": "ach_remit",
        "parser": "usfoods_remit",
        "location_extractor": _loc_usfoods,
    },
    {
        "key": "pfg_billfire",
        "from_regex": re.compile(r"no-reply@valet\.billfire\.com", re.I),
        "subject_regex": re.compile(r"^Click2Pay confirmation", re.I),
        "vendor_canonical": "Performance Foodservice",
        "tier": "needs_review",  # Only total, no breakdown — Tier 2 after wrong-bank incident
        "payment_method": "ach_via_billfire",
        "parser": "pfg_billfire",
        "location_extractor": _loc_pfg_billfire,
    },
    {
        "key": "cintas_payment_confirmation",
        "from_regex": re.compile(r"support@cintas\.com", re.I),
        "subject_regex": re.compile(r"myCintas Payment Confirmation", re.I),
        "vendor_canonical": "Cintas",
        "tier": "auto_apply",   # per-invoice # + amount table — exact direct match
        "payment_method": "ach_autopay",
        "parser": "cintas_payment_confirmation",
        "location_extractor": _loc_cintas,
    },
    {
        "key": "cintas_autopay",
        "from_regex": re.compile(r"support@cintas\.com", re.I),
        "subject_regex": re.compile(r"myCintas Autopay Confirmation", re.I),
        "vendor_canonical": "Cintas",
        "tier": "auto_apply",   # 2026-07-22 per Mike: auto-mark Cintas autopay — fuzzy match still requires a UNIQUE open Cintas invoice at the payer’s location within ±1% of the amount; anything ambiguous stays needs_review
        "payment_method": "ach_autopay",
        "parser": "cintas_autopay",
        "location_extractor": _loc_cintas,
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

        elif parser == "cintas_payment_confirmation":
            total_amount, line_items = _parse_cintas_payment_confirmation(body_text)
            if not line_items:
                notes.append("cintas-line-items-unparsed")

        elif parser == "cintas_autopay":
            total_amount, line_items = _parse_cintas_autopay(body_text)
            if total_amount is None:
                notes.append("cintas-autopay-amount-unparsed")

        elif parser == "tiger_pdf":
            amt = _parse_amount_with_regex(body_text, sig["amount_regex"])
            if amt is not None:
                total_amount = amt
            if pdf_text:
                items = _parse_tiger_pdf_rows(pdf_text)
                if items:
                    line_items = items
                    total_amount = round(sum(li.amount for li in items), 2)
            if not line_items:
                notes.append("tiger-needs-pdf")

        # Extract location for this signature. Try effective_subject (the
        # body-extracted one if forwarded) and the body text together.
        loc_extractor = sig.get("location_extractor") or _loc_none
        # For multi-line forwarded subjects, also concat the outer subject.
        loc_subject = (effective_subject or "") + " " + (subject or "")
        location = loc_extractor(loc_subject, body_text)
        if location is None:
            notes.append("location-unknown")

        return ClassifiedReceipt(
            message_id=message_id,
            signature_key=sig["key"],
            vendor_canonical=vendor_canonical or "(unknown)",
            total_amount=total_amount,
            line_items=line_items,
            location=location,
            payment_date=_parse_date_header(date_hdr),
            payment_method=sig["payment_method"],
            tier=sig["tier"],
            raw_subject=subject,
            raw_from=from_hdr,
            parse_notes=notes,
            require_invoice_for_auto=sig.get("require_invoice_for_auto", False),
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

    sql = ("""SELECT id, vendor_name, invoice_number, invoice_date, total,
                     COALESCE(balance, total) AS balance, payment_status, location
                FROM scanned_invoices
               WHERE invoice_number = ?
                 AND (payment_status != 'paid' OR payment_status IS NULL)""")
    params = [li.invoice_number]
    if receipt.location:
        sql += " AND location = ?"
        params.append(receipt.location)

    rows = cur.execute(sql, params).fetchall()
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
    if receipt.location:
        sql += " AND location = ?"
        params.append(receipt.location)
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
        can_auto = receipt.tier == "auto_apply" and not receipt.require_invoice_for_auto
        decision = "auto_apply" if can_auto else "needs_review"
        return MatchResult(line_item=li, matched_invoice_id=candidates[0]["id"],
                           candidate_count=1, decision=decision,
                           reason="single match (fuzzy)", candidates=candidates)

    return MatchResult(line_item=li, matched_invoice_id=None,
                       candidate_count=len(candidates), decision="needs_review",
                       reason=f"ambiguous: {len(candidates)} open invoices match",
                       candidates=candidates)
