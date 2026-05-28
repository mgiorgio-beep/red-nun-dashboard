#!/usr/bin/env python3
"""
Email Receipt Poller — auto-applies payment receipts to open invoices.

Polls dashboard@rednun.com for payment receipts from the Tier 1 vendors
defined in `integrations/invoices/receipt_classifier.RECEIPT_SIGNATURES`,
matches each receipt to an open invoice in `scanned_invoices`, and marks
it paid using the same SQL pattern as the `/api/billpay/invoices/<id>/mark-paid-external`
route in `routes/billpay_routes.py`.

Hard-coded safety nets (Mike opted out of the shadow week):
  - Manifest dedup — each Gmail message ID is processed exactly once
  - Per-run cap — at most 5 auto-applies per cron run; exceeding it halts
    the run and emails alerts@rednun.com (Mike). Catches runaway bugs.
  - Full audit log at /opt/red-nun-dashboard/monitoring/receipt_poller_audit.jsonl
    (one JSON line per decision, append-only).

Cron (add to crontab):
  */5 * * * * /opt/red-nun-dashboard/venv/bin/python3 \
      /opt/red-nun-dashboard/integrations/invoices/watchers/email_receipt_poller.py \
      >> /opt/red-nun-dashboard/monitoring/receipt_poller.log 2>&1

Disable in an emergency:
  touch /opt/red-nun-dashboard/.receipt_poller_disabled
"""
import os
import sys
import json
import base64
import pickle
import logging
import smtplib
from datetime import datetime, date
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

sys.path.insert(0, "/opt/red-nun-dashboard")

from googleapiclient.discovery import build
from google.auth.transport.requests import Request

from integrations.toast.data_store import get_connection
from integrations.invoices.receipt_classifier import (
    classify_message,
    find_matching_invoice,
    RECEIPT_SIGNATURES,
)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_TOKEN_PATH = "/opt/red-nun-dashboard/integrations/google/gmail_token.pickle"
MANIFEST_PATH    = "/opt/red-nun-dashboard/.email_receipt_poller_manifest.json"
AUDIT_LOG_PATH   = "/opt/red-nun-dashboard/monitoring/receipt_poller_audit.jsonl"
REVIEW_LOG_PATH  = "/opt/red-nun-dashboard/monitoring/receipt_reviews.jsonl"
KILL_SWITCH      = "/opt/red-nun-dashboard/.receipt_poller_disabled"

PER_RUN_AUTO_APPLY_CAP = 5
ALERT_EMAIL = "mike@rednun.com"

# Gmail query — narrow to known receipt senders so we never accidentally touch
# something unrelated. Updated whenever a Tier 1 vendor is added.
RECEIPT_GMAIL_QUERY = (
    "is:unread newer_than:14d -in:trash ("
    "from:quickbooks@notification.intuit.com OR "
    "from:no-reply@servicecore.com OR "
    "from:tigerexchange.us OR "
    # Forwarded receipts from Mike
    "(from:mgiorgio@rednun.com subject:Fwd) OR "
    "(from:mike@rednun.com subject:Fwd)"
    ")"
)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ── Manifest ──────────────────────────────────────────────────────────────────

def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Manifest read failed ({e}); starting fresh")
    return {"processed": {}}


def save_manifest(manifest):
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, MANIFEST_PATH)


# ── Audit log (append-only JSONL) ─────────────────────────────────────────────

def audit(event_type: str, **fields):
    """Append a single JSON line to the audit log."""
    record = {"ts": datetime.utcnow().isoformat() + "Z", "event": event_type, **fields}
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def log_review(record: dict):
    """Append a needs-review receipt to the reviews log (for payments-page UI later)."""
    record = {"ts": datetime.utcnow().isoformat() + "Z", **record}
    os.makedirs(os.path.dirname(REVIEW_LOG_PATH), exist_ok=True)
    with open(REVIEW_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    if not os.path.exists(GMAIL_TOKEN_PATH):
        logger.error(f"Gmail token missing at {GMAIL_TOKEN_PATH}")
        return None
    with open(GMAIL_TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    if not creds.valid:
        logger.error("Gmail credentials invalid; re-run gmail_auth.py")
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def mark_message_read(service, msg_id: str):
    """Remove UNREAD label so the next poll skips this message."""
    try:
        service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as e:
        logger.warning(f"Could not mark {msg_id} read: {e}")


def fetch_pdf_attachment_text(service, msg: dict) -> str:
    """
    Find the first PDF attachment in a Gmail message and return its text.
    Used by signatures with parser='pdf_attachment' (Suburban Supply).
    """
    payload = msg.get("payload", {}) or {}

    def find_pdf_part(part):
        filename = (part.get("filename") or "").lower()
        mime = part.get("mimeType", "")
        if filename.endswith(".pdf") or mime == "application/pdf":
            return part
        for sub in part.get("parts", []) or []:
            r = find_pdf_part(sub)
            if r:
                return r
        return None

    pdf_part = find_pdf_part(payload)
    if not pdf_part:
        return ""

    body = pdf_part.get("body", {}) or {}
    att_id = body.get("attachmentId")
    if not att_id:
        return ""

    try:
        att = service.users().messages().attachments().get(
            userId="me", messageId=msg["id"], id=att_id
        ).execute()
        pdf_bytes = base64.urlsafe_b64decode(att["data"])
    except Exception as e:
        logger.warning(f"Failed to fetch PDF attachment: {e}")
        return ""

    # Extract text with pypdf (already a project dependency)
    try:
        import pypdf
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text
    except Exception as e:
        logger.warning(f"pypdf extraction failed: {e}")
        return ""


# ── DB write — mirrors mark_invoice_paid_external in routes/billpay_routes.py ─

def apply_payment(invoice_id: int, amount: float, payment_method: str,
                  payment_date: str, reference: str, memo: str) -> int:
    """
    Mark a single invoice paid via the same wiring used by the
    `mark-paid-external` endpoint: insert ap_payments + ap_payment_invoices +
    vendor_payments, update scanned_invoices. Returns the ap_payments id.

    Raises on any DB error so the caller can roll back.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        inv = cur.execute(
            "SELECT id, vendor_name, total, COALESCE(balance, total) AS balance, "
            "payment_status, location, invoice_number, invoice_date, due_date "
            "FROM scanned_invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        if not inv:
            raise RuntimeError(f"invoice {invoice_id} not found")
        if inv["payment_status"] == "paid":
            raise RuntimeError(f"invoice {invoice_id} already paid (defensive)")

        applied = float(inv["balance"] or 0)
        cur.execute(
            """INSERT INTO ap_payments
               (vendor_name, payment_date, amount, payment_method,
                reference_number, memo, status)
               VALUES (?, ?, ?, ?, ?, ?, 'cleared')""",
            (inv["vendor_name"], payment_date, applied, payment_method, reference, memo),
        )
        payment_id = cur.lastrowid

        cur.execute(
            "INSERT INTO ap_payment_invoices (payment_id, invoice_id, amount_applied) "
            "VALUES (?, ?, ?)",
            (payment_id, invoice_id, applied),
        )

        cur.execute(
            """UPDATE scanned_invoices
               SET amount_paid = COALESCE(total, 0),
                   balance = 0,
                   payment_status = 'paid',
                   paid_date = ?,
                   payment_reference = COALESCE(?, payment_reference),
                   notes = COALESCE(notes, '') || ' | ' || ?
               WHERE id = ?""",
            (payment_date, reference, f"auto-receipt {payment_date}: {memo}", invoice_id),
        )

        ref = reference or f"EXT-AP{payment_id}"
        cur.execute(
            """INSERT INTO vendor_payments
               (vendor, location, payment_date, payment_ref, payment_method,
                payment_total, memo, status, source, ap_payment_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'cleared', 'auto_receipt', ?)""",
            (inv["vendor_name"], inv["location"], payment_date, ref,
             payment_method, applied, memo, payment_id),
        )
        vp_id = cur.lastrowid

        cur.execute(
            """INSERT INTO vendor_payment_invoices
               (payment_id, invoice_number, invoice_date, due_date, amount_paid)
               VALUES (?, ?, ?, ?, ?)""",
            (vp_id, inv["invoice_number"], inv["invoice_date"],
             inv["due_date"], applied),
        )

        conn.commit()
        return payment_id
    finally:
        conn.close()


# ── Alerting ──────────────────────────────────────────────────────────────────

def send_alert(subject: str, body: str):
    """Best-effort SMTP alert. Silently no-ops if SMTP isn't configured."""
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    if not (smtp_host and smtp_user and smtp_pass):
        logger.warning(f"Alert (SMTP not configured): {subject}")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(smtp_host, 587) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    except Exception as e:
        logger.error(f"Alert send failed: {e}")


# ── Main poll loop ────────────────────────────────────────────────────────────

def run(dry_run: bool = False):
    if dry_run:
        logger.info("DRY RUN — no DB writes, no Gmail labels modified, no manifest writes")
    if os.path.exists(KILL_SWITCH):
        logger.info(f"Kill switch present at {KILL_SWITCH}; exiting without polling")
        return

    service = get_gmail_service()
    if service is None:
        return

    manifest = load_manifest()
    processed = manifest.setdefault("processed", {})

    try:
        resp = service.users().messages().list(
            userId="me", q=RECEIPT_GMAIL_QUERY, maxResults=50
        ).execute()
    except Exception as e:
        logger.error(f"Gmail list failed: {e}")
        return

    msg_metas = resp.get("messages", []) or []
    if not msg_metas:
        return

    auto_applied_this_run = 0
    skipped_due_to_cap = 0

    for meta in msg_metas:
        msg_id = meta["id"]
        if msg_id in processed:
            continue

        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
        except Exception as e:
            logger.warning(f"Could not fetch {msg_id}: {e}")
            continue

        # First-pass classify (no PDF text yet)
        receipt = classify_message(msg)
        if receipt is None:
            audit("not_a_receipt", message_id=msg_id, subject=_subj(msg))
            processed[msg_id] = {"decision": "not_a_receipt"}
            save_manifest(manifest)
            # Don't mark read — the user may have other reasons to want it unread.
            continue

        # For Suburban (PDF-attached amount), do a second pass with PDF text
        if receipt.amount is None and "pdf-attachment-amount-unparsed" in receipt.parse_notes:
            pdf_text = fetch_pdf_attachment_text(service, msg)
            if pdf_text:
                receipt = classify_message(msg, pdf_text=pdf_text)

        # Cap check before any DB write
        if auto_applied_this_run >= PER_RUN_AUTO_APPLY_CAP:
            skipped_due_to_cap += 1
            audit("skipped_cap", message_id=msg_id, signature=receipt.signature_key,
                  vendor=receipt.vendor_canonical, amount=receipt.amount)
            # Don't mark processed — try again next run
            continue

        # Match against scanned_invoices
        conn = get_connection()
        try:
            match = find_matching_invoice(receipt, conn)
        finally:
            conn.close()

        audit_payload = {
            "message_id": msg_id,
            "signature": receipt.signature_key,
            "vendor": receipt.vendor_canonical,
            "amount": receipt.amount,
            "invoice_number": receipt.invoice_number,
            "payment_date": receipt.payment_date,
            "decision": match.decision,
            "reason": match.reason,
            "matched_invoice_id": match.matched_invoice_id,
            "candidate_count": match.candidate_count,
        }

        if match.decision == "auto_apply":
            if dry_run:
                logger.info(
                    f"[DRY] Would auto-apply {receipt.vendor_canonical} "
                    f"${receipt.amount:.2f} → invoice {match.matched_invoice_id}"
                )
                auto_applied_this_run += 1
                continue
            try:
                payment_id = apply_payment(
                    invoice_id=match.matched_invoice_id,
                    amount=receipt.amount,
                    payment_method=receipt.payment_method,
                    payment_date=receipt.payment_date or date.today().isoformat(),
                    reference=f"RCPT-{msg_id}",
                    memo=f"Auto-applied from {receipt.signature_key} receipt",
                )
                auto_applied_this_run += 1
                audit_payload["payment_id"] = payment_id
                audit("auto_applied", **audit_payload)
                logger.info(
                    f"Auto-applied {receipt.vendor_canonical} ${receipt.amount:.2f} "
                    f"→ invoice {match.matched_invoice_id} (ap_payment {payment_id})"
                )
                mark_message_read(service, msg_id)
                processed[msg_id] = audit_payload
            except Exception as e:
                logger.error(f"apply_payment failed for {msg_id}: {e}")
                audit("apply_error", message_id=msg_id, error=str(e), **audit_payload)
                # Don't mark processed — retry next run
        elif match.decision == "needs_review":
            if dry_run:
                logger.info(
                    f"[DRY] Needs review: {receipt.vendor_canonical} "
                    f"${receipt.amount} — {match.reason}"
                )
                continue
            review_record = {
                **audit_payload,
                "subject": _subj(msg),
                "candidates": match.candidates,
            }
            log_review(review_record)
            audit("needs_review", **audit_payload)
            processed[msg_id] = audit_payload
            # Leave message unread so Mike sees it in the inbox too
        else:  # no_match
            if dry_run:
                logger.info(
                    f"[DRY] No match: {receipt.vendor_canonical} ${receipt.amount} "
                    f"({receipt.signature_key}) — {match.reason}"
                )
                continue
            audit("no_match", **audit_payload)
            processed[msg_id] = audit_payload

        if not dry_run:
            save_manifest(manifest)

    if auto_applied_this_run > 0:
        logger.info(f"Run complete: {auto_applied_this_run} auto-applied, "
                    f"{skipped_due_to_cap} deferred by cap")

    if skipped_due_to_cap > 0:
        send_alert(
            subject=f"[Red Nun] Receipt poller hit the {PER_RUN_AUTO_APPLY_CAP}-apply cap",
            body=(
                f"The receipt poller auto-applied {PER_RUN_AUTO_APPLY_CAP} invoices this run "
                f"and held back {skipped_due_to_cap} more for safety.\n\n"
                f"They'll be picked up on the next run (every 5 min). If you see this "
                f"more than once or twice, something may be matching incorrectly — "
                f"check {AUDIT_LOG_PATH} and consider creating the kill switch:\n\n"
                f"  touch {KILL_SWITCH}\n"
            ),
        )


def _subj(msg):
    for h in (msg.get("payload", {}) or {}).get("headers", []) or []:
        if h.get("name", "").lower() == "subject":
            return h.get("value", "")
    return ""


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
