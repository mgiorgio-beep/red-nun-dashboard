"""
Bank Statement Processor — Red Nun Analytics

Parses Cape Cod Five PDF bank statements into structured transaction lists
using pdfplumber against the embedded text layer.

Output schema:
    {
        "bank":             "cape_cod_five" | "unknown",
        "account_last4":    "5975" | "2757" | None,
        "period_start":     "YYYY-MM-DD",
        "period_end":       "YYYY-MM-DD",
        "beginning_balance": float,
        "ending_balance":    float,
        "total_debits":      float,
        "total_credits":     float,
        "transactions": [
            {
                "date":        "YYYY-MM-DD",
                "description": str,
                "memo":        str,
                "debit":       float,   # 0 if credit
                "credit":      float,   # 0 if debit
                "balance":     float|None,
                "ref":         str,     # check number if present
                "tx_type":     "check" | "deposit" | "ach_debit" | "ach_credit" | "fee" | "other"
            },
            ...
        ],
        "warnings": [str, ...]   # e.g. ["sum mismatch: parsed debits 1000.00 vs statement 1100.00"]
    }

Strategy
--------
1. Open PDF with pdfplumber. Extract text from every page.
2. Detect bank format from the first page header.
3. For Cape Cod Five: parse the running 'DAILY ACTIVITY' table — date, desc,
   debit-with-trailing-dash, credit, running balance. Multi-line memos roll
   up into the previous transaction.
4. Validate parsed totals against the 'Statement Summary' section.

This module ONLY parses. It never writes to the database. The route layer
calls `parse_bank_statement_pdf(file_bytes)` and decides what to do with the
result.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date as _date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Public entry point ──────────────────────────────────────────────────────

def parse_bank_statement_pdf(file_bytes: bytes, hint_year: int | None = None) -> dict:
    """Parse a bank statement PDF.

    Args:
        file_bytes: raw PDF bytes (uploaded file).
        hint_year: year to assume for MM/DD dates if statement period header
                   can't be detected. Defaults to current year.

    Returns the schema described in the module docstring. Raises ValueError
    only on truly unparseable input (e.g. non-PDF). Returns a result with
    `warnings` populated when totals don't reconcile.
    """
    try:
        import pdfplumber  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "pdfplumber not installed in venv — `pip install pdfplumber`"
        ) from e

    full_text, page_texts = _extract_text(file_bytes)

    if not full_text.strip():
        return _empty_result(
            warnings=["PDF text layer is empty — likely a scanned statement. OCR fallback not yet implemented."]
        )

    bank = _detect_bank(full_text)

    if bank == "cape_cod_five":
        return _parse_cape_cod_five(full_text, page_texts, hint_year)

    # Unknown format — return raw text in a warning so we can iterate.
    return _empty_result(
        bank="unknown",
        warnings=[f"Bank format not recognized. First 200 chars: {full_text[:200]!r}"],
    )


# ─── PDF text extraction ─────────────────────────────────────────────────────

def _extract_text(file_bytes: bytes) -> tuple[str, list[str]]:
    """Extract text from every page of a PDF. Returns (joined_text, [per_page_text])."""
    import pdfplumber

    page_texts: list[str] = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            try:
                t = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
            except Exception:
                t = ""
            page_texts.append(t)
    return "\n".join(page_texts), page_texts


# ─── Bank detection ──────────────────────────────────────────────────────────

def _detect_bank(text: str) -> str:
    upper = text.upper()
    if (
        "CAPE COD" in upper
        or "CAPECOD5" in upper
        or "CAPECODFIVE" in upper
        or "CAPE COD 5" in upper
    ):
        return "cape_cod_five"
    return "unknown"


# ─── Cape Cod Five parser ────────────────────────────────────────────────────

# Patterns
_RE_PERIOD = re.compile(
    r"(?:Statement\s*Period|Statement\s*Dates|Period|Statement\s*Date)[\s:]*"
    r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:to|through|thru|-|—|–)\s*(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_RE_BEGIN_BAL = re.compile(
    r"(?:Beginning|Previous)\s+Balance[\s:\.]*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE
)
_RE_END_BAL = re.compile(
    r"(?:Ending|New|Current|Closing)\s+Balance[\s:\.]*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE
)
# CCF prints totals as e.g. "165 Checks/Debits 92,398.46" — count + label + amount.
# Also handle the more conventional "Total Debits/Withdrawals" labels other banks use.
_RE_TOT_DEBITS = re.compile(
    r"(?:(?:\d+\s+)?(?:Checks/Debits|Debits/Checks)|Total\s+(?:Debits|Withdrawals|Subtractions))"
    r"[\s:\.]*\$?\s*([\d,]+\.\d{2})",
    re.IGNORECASE,
)
_RE_TOT_CREDITS = re.compile(
    r"(?:(?:\d+\s+)?(?:Deposits/Credits|Credits/Deposits)|Total\s+(?:Credits|Deposits|Additions))"
    r"[\s:\.]*\$?\s*([\d,]+\.\d{2})",
    re.IGNORECASE,
)
_RE_ACCT_LAST4 = re.compile(
    r"Account[^\d]{0,30}(\d{4})\b|"
    r"\*+\s*(\d{4})\b|"
    r"x+\s*(\d{4})\b",
    re.IGNORECASE,
)

# A Cape Cod Five transaction line looks like:
#   "1/02 Deposit                                     105.00       34,118.57"
#   "1/02 MEBillPay Cozzini Bros., I                   28.90-      54,768.99"
#   "1/29 TAX 7shifts                                    .27-      30,620.57"  ← sub-dollar
# A debit ends the amount with a trailing '-'. A credit has no suffix. The
# amount may be sub-dollar (e.g. ".27") so we allow zero digits before the
# decimal as long as there are two digits after. Running balance is always
# present at end of line. Date may be 1- or 2-digit.
_RE_TX_LINE = re.compile(
    r"^\s*(?P<date>\d{1,2}/\d{1,2})"          # M/DD or MM/DD
    r"\s+(?P<desc>.+?)"                        # non-greedy desc
    r"\s+(?P<amt>[\d,]*\.\d{2})(?P<sign>-?)"   # amount (incl. sub-dollar) + optional debit marker
    r"\s+(?P<bal>[\d,]+\.\d{2})\s*$",          # running balance (always >= $1.00 in practice)
)

# Some statements split debit/credit into two columns. Handle that as a
# secondary pattern: "DATE DESC DEBIT CREDIT BALANCE" with one of debit/credit
# blank. (Keep simple — first regex handles the primary CCF layout.)

# Section markers — used to know when we're in the activity table vs summary.
_SECTION_START_PATTERNS = (
    "DAILY ACTIVITY",
    "ACCOUNT ACTIVITY",
    "TRANSACTION DETAIL",
    "ACTIVITY SUMMARY",
)
# Things that mark the END of the running daily-activity table. After these,
# stop accepting transaction or memo lines. CCF in particular ends with
# "--- CHECKS IN NUMBER ORDER ---" (a separate table that re-lists checks
# in a totally different format we don't want to re-parse), then the polite
# closing line.
_SECTION_END_PATTERNS = (
    "CHECKS IN NUMBER ORDER",
    "STATEMENT SUMMARY",
    "DAILY BALANCES",
    "DAILY BALANCE SUMMARY",
    "SERVICE CHARGES",
    "INTEREST RATE SUMMARY",
    "OVERDRAFT NOTICE",
    "THANK YOU FOR BANKING",
)

# Lines we should skip even if they're inside the activity section (they're
# page-break repeats, column headers, footers, or stray noise — never memo
# content for a real transaction).
_SKIP_PATTERNS = (
    # Column header row — repeats every page
    re.compile(r"^\s*Date\s+Description\s+(Debit|Withdrawals)", re.IGNORECASE),
    re.compile(r"^\s*(Description|Debit|Credit|Balance|Withdrawals|Deposits)\s+", re.IGNORECASE),
    # CCF page header: "Date 1/30/26 Page 2"
    re.compile(r"^\s*Date\s+\d{1,2}/\d{1,2}/\d{2,4}\s+Page\s+\d+", re.IGNORECASE),
    # Generic page numbering
    re.compile(r"^\s*Page[:]?\s+\d+(\s*of\s*\d+)?\s*$", re.IGNORECASE),
    # CCF account banner on continuation pages
    re.compile(r"^\s*Business\s+I\s+Checking\s+Acct\s+Ending", re.IGNORECASE),
    # FDIC footer
    re.compile(r"^\s*MEMBER\s+FDIC\s*$", re.IGNORECASE),
    # Another DAILY ACTIVITY repeat already handled below, but match here too as safety
)


def _parse_cape_cod_five(full_text: str, page_texts: list[str], hint_year: int | None) -> dict:
    warnings: list[str] = []

    # 1. Header info -----------------------------------------------------------
    period_start, period_end, year = _extract_period(full_text, hint_year)
    if not year:
        # safety fallback — assume current year
        year = datetime.now().year
        warnings.append("Could not detect statement period; assumed current year for MM/DD dates.")

    last4 = _extract_account_last4(full_text)
    begin_bal = _extract_money(_RE_BEGIN_BAL, full_text)
    end_bal = _extract_money(_RE_END_BAL, full_text)
    tot_debits_stmt = _extract_money(_RE_TOT_DEBITS, full_text)
    tot_credits_stmt = _extract_money(_RE_TOT_CREDITS, full_text)

    # 2. Walk lines, in/out of activity section -------------------------------
    transactions: list[dict] = []
    in_activity = False

    pending: dict | None = None  # last tx awaiting memo lines

    for raw_line in full_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        upper = line.upper().strip()

        # Section toggling
        if any(p in upper for p in _SECTION_START_PATTERNS):
            # CCF repeats "DAILY ACTIVITY" at the top of every continuation
            # page. If we're already mid-section don't drop the in-flight tx.
            if not in_activity:
                in_activity = True
                pending = None
            continue
        if any(p in upper for p in _SECTION_END_PATTERNS):
            if pending:
                transactions.append(pending)
                pending = None
            in_activity = False
            continue

        if not in_activity:
            continue

        if any(p.match(line) for p in _SKIP_PATTERNS):
            continue

        m = _RE_TX_LINE.match(line)
        if m:
            # Flush prior pending memo
            if pending:
                transactions.append(pending)

            mmdd = m.group("date")
            desc = m.group("desc").strip()
            amt_str = m.group("amt")
            sign = m.group("sign")
            bal_str = m.group("bal")

            amount = _money(amt_str)
            balance = _money(bal_str)
            debit = amount if sign == "-" else 0.0
            credit = amount if sign != "-" else 0.0

            # Heuristic correction: even without a sign, if running balance went
            # DOWN vs prior running balance, this was a debit. Some PDFs lose
            # the trailing '-' to a tab or whitespace artifact.
            if sign != "-" and transactions:
                prev_bal = transactions[-1].get("balance")
                if prev_bal is not None and balance + 0.005 < prev_bal:
                    # Looks like an outflow that lost its sign
                    debit, credit = amount, 0.0

            tx_date = _to_iso_date(mmdd, year)
            ref = _extract_check_ref(desc)
            tx_type = _classify_tx(desc, debit, credit)

            pending = {
                "date": tx_date,
                "description": desc,
                "memo": "",
                "debit": round(debit, 2),
                "credit": round(credit, 2),
                "balance": round(balance, 2),
                "ref": ref,
                "tx_type": tx_type,
            }
        elif pending is not None:
            # Continuation memo line
            extra = line.strip()
            if extra and not extra.upper().startswith(("DATE", "DESCRIPTION")):
                pending["memo"] = (pending["memo"] + " | " + extra).strip(" |") if pending["memo"] else extra

    if pending:
        transactions.append(pending)

    # 3. Validate totals ------------------------------------------------------
    parsed_debits = round(sum(t["debit"] for t in transactions), 2)
    parsed_credits = round(sum(t["credit"] for t in transactions), 2)

    if tot_debits_stmt is not None and abs(parsed_debits - tot_debits_stmt) > 0.05:
        warnings.append(
            f"debit total mismatch: parsed {parsed_debits:.2f} vs statement {tot_debits_stmt:.2f}"
        )
    if tot_credits_stmt is not None and abs(parsed_credits - tot_credits_stmt) > 0.05:
        warnings.append(
            f"credit total mismatch: parsed {parsed_credits:.2f} vs statement {tot_credits_stmt:.2f}"
        )
    if begin_bal is not None and end_bal is not None:
        expected_end = round(begin_bal - parsed_debits + parsed_credits, 2)
        if abs(expected_end - end_bal) > 0.05:
            warnings.append(
                f"ending balance mismatch: parsed→{expected_end:.2f} vs statement {end_bal:.2f}"
            )

    return {
        "bank": "cape_cod_five",
        "account_last4": last4,
        "period_start": period_start,
        "period_end": period_end,
        "beginning_balance": begin_bal,
        "ending_balance": end_bal,
        "total_debits": tot_debits_stmt if tot_debits_stmt is not None else parsed_debits,
        "total_credits": tot_credits_stmt if tot_credits_stmt is not None else parsed_credits,
        "transactions": transactions,
        "warnings": warnings,
    }


# ─── Header / money helpers ──────────────────────────────────────────────────

def _extract_period(text: str, hint_year: int | None) -> tuple[str | None, str | None, int | None]:
    """Returns (period_start_iso, period_end_iso, year_for_mmdd_lines)."""
    m = _RE_PERIOD.search(text)
    if m:
        try:
            d1 = _parse_short_date(m.group(1))
            d2 = _parse_short_date(m.group(2))
            return d1.isoformat(), d2.isoformat(), d2.year
        except Exception:
            pass

    # Fallback: look for a year on a line containing 'Statement'
    m2 = re.search(r"Statement[^\n]{0,40}(20\d{2})", text, re.IGNORECASE)
    if m2:
        return None, None, int(m2.group(1))
    return None, None, hint_year


def _parse_short_date(s: str) -> _date:
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date: {s!r}")


def _to_iso_date(mmdd: str, year: int) -> str:
    try:
        d = datetime.strptime(f"{mmdd}/{year}", "%m/%d/%Y").date()
    except ValueError:
        return mmdd  # fallback raw
    return d.isoformat()


def _money(s: str) -> float:
    return float(s.replace(",", "")) if s else 0.0


def _extract_money(pattern: re.Pattern, text: str) -> float | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _extract_account_last4(text: str) -> str | None:
    """Pick a likely last-4 digit account number from the header. Cape Cod Five
    typically prints the full account number; the last 4 of '5975' or '2757'
    matches Red Nun's two operating accounts."""
    # Prefer 5975 or 2757 if either appears in the first 1500 chars (header).
    head = text[:1500]
    for known in ("5975", "2757"):
        if known in head:
            return known
    m = _RE_ACCT_LAST4.search(head)
    if m:
        for g in m.groups():
            if g:
                return g
    return None


def _extract_check_ref(desc: str) -> str:
    """Pull a check number out of descriptions like 'Check 9561'."""
    m = re.match(r"^Check\s+0*(\d+)\b", desc, re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def _classify_tx(desc: str, debit: float, credit: float) -> str:
    d = desc.lower()
    if d.startswith("check ") or "check" in d.split()[:1]:
        return "check"
    if "deposit" in d or "dep " in d or "toast" in d or "doordash" in d:
        return "deposit" if credit > 0 else "other"
    if "ach" in d or "electronic" in d or "bill pay" in d or "online" in d:
        return "ach_debit" if debit > 0 else "ach_credit"
    if "service charge" in d or "fee" in d or "overdraft" in d:
        return "fee"
    if credit > 0:
        return "ach_credit"
    return "other"


# ─── Empty result helper ─────────────────────────────────────────────────────

def _empty_result(bank: str = "unknown", warnings: list[str] | None = None) -> dict:
    return {
        "bank": bank,
        "account_last4": None,
        "period_start": None,
        "period_end": None,
        "beginning_balance": None,
        "ending_balance": None,
        "total_debits": 0.0,
        "total_credits": 0.0,
        "transactions": [],
        "warnings": warnings or [],
    }


# ─── CLI for quick testing ───────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m integrations.bank_statements.processor <statement.pdf>")
        sys.exit(1)

    with open(sys.argv[1], "rb") as f:
        result = parse_bank_statement_pdf(f.read())

    # Trim transactions for terminal readability
    display = dict(result)
    display["transactions_count"] = len(result["transactions"])
    display["transactions_preview"] = result["transactions"][:5]
    display.pop("transactions", None)
    print(json.dumps(display, indent=2, default=str))
