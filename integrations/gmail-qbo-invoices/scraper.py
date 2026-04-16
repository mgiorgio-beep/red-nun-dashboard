#!/usr/bin/env python3
"""
Scraper A — QBO Invoice Intake

Searches Gmail for invoice emails from quickbooks@notification.intuit.com,
downloads the PDF attachment, detects Chatham vs Dennis from the PDF text,
and POSTs to the dashboard's /api/invoices/scan endpoint.
"""
import argparse
import importlib.util
import io
import logging
import os
import sys
from datetime import datetime, timedelta

import requests

try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader

# Load shared modules directly from the sibling folder.
# The folder name uses hyphens ("gmail-qbo-shared") which Python can't
# import as a package name, so we load each .py file by path.
SHARED_DIR = os.path.expanduser("/opt/red-nun-dashboard/integrations/gmail-qbo-shared")


def _load(mod_name, filename):
    path = os.path.join(SHARED_DIR, filename)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gmail_client = _load("gmail_client", "gmail_client.py")
state = _load("state", "state.py")
vendor_normalize = _load("vendor_normalize", "vendor_normalize.py")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qbo_invoices")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8080")
SCAN_ENDPOINT = f"{DASHBOARD_URL}/api/invoices/scan"
SESSION_ENDPOINT = f"{DASHBOARD_URL}/api/vendor-sessions/update"
VENDOR_SESSION_NAME = "QBO Email Invoices"


def detect_location(pdf_bytes):
    """Extract text from PDF and look for Chatham/Dennis indicators.
    Returns 'chatham', 'dennis', or None if ambiguous.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages[:3]:
            text += page.extract_text() or ""
    except Exception as e:
        log.warning(f"  PDF text extraction failed: {e}")
        return None

    lower = text.lower()
    has_chatham = "chatham" in lower or "02633" in lower
    has_dennis = "dennis" in lower or "02639" in lower or "02660" in lower

    if has_chatham and has_dennis:
        ship_idx = lower.find("ship")
        if ship_idx > 0:
            window = lower[ship_idx : ship_idx + 300]
            if "chatham" in window:
                return "chatham"
            if "dennis" in window:
                return "dennis"
        return None
    if has_chatham:
        return "chatham"
    if has_dennis:
        return "dennis"
    return None


def is_invoice_subject(subject):
    """Filter: only process emails whose subject looks like a new invoice."""
    if not subject:
        return False
    s = subject.lower().strip()
    if s.startswith("reminder"):
        return False
    if "receipt" in s or "payment received" in s or "thank you for your payment" in s:
        return False
    if "invoice" in s and "from" in s:
        return True
    return False


def process_message(service, msg_meta, dry_run=False):
    """Process one message. Returns outcome string."""
    mid = msg_meta["id"]

    if state.is_processed(mid):
        return "already_processed"

    message = gmail_client.get_message(service, mid)
    headers = gmail_client.get_headers(message)
    subject = headers.get("subject", "")
    from_addr = headers.get("from", "")

    log.info(f"[{mid[:12]}] {subject}")

    if not is_invoice_subject(subject):
        log.info(f"  SKIP: subject doesn't match invoice pattern")
        if not dry_run:
            state.mark_processed(mid, "invoices", "skipped", {"reason": "not_invoice_subject", "subject": subject})
        return "skipped"

    vendor_hint = vendor_normalize.extract_vendor_from_subject(subject) or from_addr
    if vendor_normalize.is_native_scraper_vendor(vendor_hint):
        log.info(f"  SKIP: native scraper handles '{vendor_hint}'")
        if not dry_run:
            state.mark_processed(mid, "invoices", "skipped", {"reason": "native_scraper_vendor", "vendor": vendor_hint})
        return "skipped_native"

    attachments = gmail_client.get_pdf_attachments(service, mid, message=message)
    if not attachments:
        log.info(f"  SKIP: no PDF attachments")
        if not dry_run:
            state.mark_processed(mid, "invoices", "skipped", {"reason": "no_pdf"})
        return "skipped_no_pdf"

    filename, pdf_bytes = attachments[0]
    log.info(f"  PDF: {filename} ({len(pdf_bytes)} bytes)")

    location = detect_location(pdf_bytes)
    log.info(f"  Location: {location or 'ambiguous → will leave to dashboard'}")

    if dry_run:
        log.info(f"  DRY-RUN: would POST to {SCAN_ENDPOINT}")
        return "dry_run"

    url = SCAN_ENDPOINT
    if location:
        url += f"?location={location}"

    try:
        files = {"file": (filename, pdf_bytes, "application/pdf")}
        resp = requests.post(url, files=files, timeout=180)
    except requests.exceptions.RequestException as e:
        log.error(f"  POST failed: {e}")
        state.mark_processed(mid, "invoices", "error", {"error": str(e)})
        return "error"

    if resp.status_code == 200:
        data = resp.json()
        status = data.get("status", "unknown")
        inv_id = data.get("invoice_id")
        log.info(f"  OK: {status} (invoice #{inv_id})")
        state.mark_processed(mid, "invoices", status, {
            "invoice_id": inv_id,
            "vendor": vendor_hint,
            "subject": subject,
            "location": location,
        })
        return status
    elif resp.status_code == 409:
        data = resp.json()
        existing_id = data.get("existing_id")
        log.info(f"  DUPLICATE: already have invoice #{existing_id}")
        state.mark_processed(mid, "invoices", "duplicate", {
            "existing_id": existing_id,
            "vendor": vendor_hint,
            "subject": subject,
        })
        return "duplicate"
    else:
        err = resp.text[:300]
        log.error(f"  HTTP {resp.status_code}: {err}")
        state.mark_processed(mid, "invoices", "error", {
            "http_status": resp.status_code,
            "error": err,
        })
        return "error"


def report_session_status(counts, failure_reason=None):
    status = "healthy" if not failure_reason else "expired"
    try:
        requests.post(
            SESSION_ENDPOINT,
            json={
                "vendor_name": VENDOR_SESSION_NAME,
                "status": status,
                "failure_reason": failure_reason,
                "invoices_scraped_last_run": counts.get("auto_confirmed", 0) + counts.get("needs_review", 0),
            },
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Failed to report session status: {e}")


def main():
    p = argparse.ArgumentParser(description="Scrape QBO invoice emails from Gmail")
    p.add_argument("--since", help="Start date YYYY-MM-DD (default: 7 days ago)")
    p.add_argument("--dry-run", action="store_true", help="Don't POST, just report")
    p.add_argument("--limit", type=int, help="Max messages to process")
    args = p.parse_args()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d")
    else:
        since = datetime.now() - timedelta(days=7)

    query = f"from:{gmail_client.QBO_SENDER} after:{gmail_client.format_date_query(since)} has:attachment"
    log.info(f"Query: {query}")
    log.info(f"Dry run: {args.dry_run}")

    try:
        service = gmail_client.get_service()
    except Exception as e:
        log.error(f"Auth failed: {e}")
        report_session_status({}, failure_reason=f"auth_error: {e}")
        return 1

    messages = gmail_client.search_messages(service, query, max_results=args.limit)
    log.info(f"Found {len(messages)} messages")

    counts = {}
    for i, msg_meta in enumerate(messages, 1):
        try:
            outcome = process_message(service, msg_meta, dry_run=args.dry_run)
        except Exception as e:
            log.error(f"  Unexpected error: {e}", exc_info=True)
            outcome = "error"
            if not args.dry_run:
                state.mark_processed(msg_meta["id"], "invoices", "error", {"error": str(e)})
        counts[outcome] = counts.get(outcome, 0) + 1
        if i % 25 == 0:
            log.info(f"Progress: {i}/{len(messages)}")

    log.info("=" * 60)
    log.info("Summary:")
    for outcome, n in sorted(counts.items(), key=lambda x: -x[1]):
        log.info(f"  {outcome}: {n}")

    if not args.dry_run:
        report_session_status(counts)

    return 0


if __name__ == "__main__":
    sys.exit(main())
