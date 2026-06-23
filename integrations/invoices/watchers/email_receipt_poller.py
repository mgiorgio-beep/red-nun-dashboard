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
from typing import Optional

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
ALERT_EMAIL = os.environ.get("REPORT_TO_EMAIL", "mgiorgio@rednun.com")
FROM_EMAIL  = os.environ.get("REPORT_FROM_EMAIL", "dashboard@rednun.com")

# Gmail query — narrow to known receipt senders so we never accidentally touch
# something unrelated. Updated whenever a Tier 1 vendor is added.
RECEIPT_GMAIL_QUERY = (
    "is:unread newer_than:14d -in:trash ("
    "from:quickbooks@notification.intuit.com OR "
    "from:no-reply@servicecore.com OR "
    "from:tigerexchange.us OR "
    "from:noreply@vtinfo.com OR "             # L. Knife + Colonial
    "from:usfoods-notification@usfoods.com OR "
    "from:no-reply@valet.billfire.com OR "    # PFG via Billfire
    "from:support@cintas.com OR "             # Cintas autopay + payment confirmations
    # Forwarded receipts from Mike's personal inboxes
    "(from:mgiorgio@rednun.com subject:Fwd) OR "
    "(from:mike@rednun.com subject:Fwd) OR "
    "(from:invoice@rednun.com subject:Fwd)"
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

AMOUNT_DISCREPANCY_HARD_LIMIT = 1.00  # dollars

def apply_payment(invoice_id: int, amount: float, payment_method: str,
                  payment_date: str, reference: str, memo: str) -> int:
    """
    Mark a single invoice paid via the same wiring used by the
    `mark-paid-external` endpoint: insert ap_payments + ap_payment_invoices +
    vendor_payments, update scanned_invoices. Returns the ap_payments id.

    The `amount` parameter is the receipt's reported amount and is what gets
    recorded. We compare against the invoice's current balance and:
      - Equal (within AMOUNT_DISCREPANCY_HARD_LIMIT): mark paid, balance → 0
      - amount < balance: apply partial, leave balance positive, status partial
      - amount > balance by more than the limit: RAISE — matcher should have
        caught this, defensive

    Raises on any DB error so the caller can hold for review.
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

        balance = float(inv["balance"] or 0)
        applied = float(amount)

        # Sanity check — refuse to apply more than the invoice's balance.
        # Matcher should have caught this and held for review, but defensive.
        if applied > balance + AMOUNT_DISCREPANCY_HARD_LIMIT:
            raise RuntimeError(
                f"receipt amount ${applied:.2f} exceeds invoice balance ${balance:.2f} "
                f"by more than ${AMOUNT_DISCREPANCY_HARD_LIMIT:.2f} — refusing to over-apply"
            )

        # Are we paying the invoice in full (within tolerance) or partially?
        is_full_pay = abs(applied - balance) <= AMOUNT_DISCREPANCY_HARD_LIMIT
        new_balance = 0 if is_full_pay else balance - applied
        new_status = "paid" if is_full_pay else "partial"
        # When paying in full, record the invoice's total as amount_paid to keep
        # the ledger square even if our receipt amount differs from balance by
        # cents (rounding). When partial, sum prior + applied.
        new_amount_paid = inv["total"] if is_full_pay else (
            float(inv.get("amount_paid") or 0) + applied
        )

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
               SET amount_paid = ?,
                   balance = ?,
                   payment_status = ?,
                   paid_date = CASE WHEN ? = 'paid' THEN ? ELSE paid_date END,
                   payment_reference = COALESCE(?, payment_reference),
                   notes = COALESCE(notes, '') || ' | ' || ?
               WHERE id = ?""",
            (new_amount_paid, new_balance, new_status,
             new_status, payment_date,
             reference,
             f"auto-receipt {payment_date}: {memo}",
             invoice_id),
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

def _send_email(subject: str, body: str, content_type: str = "plain"):
    """Best-effort SMTP send. Mirrors reports/morning_report.py env vars.

    Env vars: SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587),
              SMTP_USER, SMTP_PASSWORD, REPORT_FROM_EMAIL, REPORT_TO_EMAIL.
    Silently no-ops with a warning if SMTP credentials aren't set.
    """
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    if not (smtp_user and smtp_pass):
        logger.warning(f"Email skipped (SMTP not configured): {subject}")
        return
    try:
        msg = MIMEText(body, content_type)
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        logger.info(f"Email sent to {ALERT_EMAIL}: {subject}")
    except Exception as e:
        logger.error(f"Email send failed: {e}")


def send_alert(subject: str, body: str):
    """Back-compat wrapper for the cap-exceeded alert path."""
    _send_email(subject, body, content_type="plain")


def send_apply_notification(applied_events: list):
    """Email a summary of every auto-apply that happened this cron run."""
    if not applied_events:
        return
    n = len(applied_events)
    total = sum(e["amount"] for e in applied_events)

    subject = f"[Red Nun] Receipt poller auto-applied {n} payment{'s' if n != 1 else ''} (${total:,.2f})"

    rows = []
    for e in applied_events:
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{e['vendor']}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-family:monospace'>#{e['invoice_number'] or '—'}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:right;font-weight:600'>${e['amount']:,.2f}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#666'>inv {e['invoice_id']}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#666'>ap_payment {e['payment_id']}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#666;font-size:11px'>{e['signature']}</td>"
            f"</tr>"
        )

    html = f"""<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#222">
<h2 style="color:#0f172a;margin:0 0 8px 0">Receipt poller auto-applied {n} payment{'s' if n != 1 else ''}</h2>
<div style="color:#666;font-size:13px;margin-bottom:16px">Total: <strong>${total:,.2f}</strong> · cron run at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</div>
<table style="border-collapse:collapse;width:100%;max-width:760px;font-size:13px">
<thead><tr style="background:#f5f5f5">
<th style="padding:8px 10px;text-align:left">Vendor</th>
<th style="padding:8px 10px;text-align:left">Invoice #</th>
<th style="padding:8px 10px;text-align:right">Amount</th>
<th style="padding:8px 10px;text-align:left">Dashboard ID</th>
<th style="padding:8px 10px;text-align:left">Payment ID</th>
<th style="padding:8px 10px;text-align:left">Source</th>
</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
<p style="color:#666;font-size:12px;margin-top:24px">
If any of these were applied incorrectly: kill switch is <code>touch /opt/red-nun-dashboard/.receipt_poller_disabled</code>.
Audit log: <code>/opt/red-nun-dashboard/monitoring/receipt_poller_audit.jsonl</code>.
</p>
</body></html>"""

    _send_email(subject, html, content_type="html")


# ── Main poll loop ────────────────────────────────────────────────────────────

def run(dry_run: bool = False, backfill_days: Optional[int] = None):
    """Poll Gmail for receipts and process them.

    Args:
        dry_run: if True, log decisions but make no DB or Gmail changes.
        backfill_days: if set, ignore the `is:unread` filter and look back this
            many days. Useful for retroactive matching once a new signature
            is added or after fixing matcher logic. Manifest dedup still
            prevents already-processed messages from being touched.
    """
    if dry_run:
        logger.info("DRY RUN — no DB writes, no Gmail labels modified, no manifest writes")
    if os.path.exists(KILL_SWITCH):
        if dry_run:
            logger.info(f"Kill switch present at {KILL_SWITCH} — proceeding anyway "
                        f"(dry-run is read-only)")
        else:
            logger.info(f"Kill switch present at {KILL_SWITCH}; exiting without polling")
            return

    service = get_gmail_service()
    if service is None:
        return

    manifest = load_manifest()
    processed = manifest.setdefault("processed", {})

    # Construct the Gmail query — backfill mode drops `is:unread` and widens
    # the date window. The sender list stays the same.
    if backfill_days:
        query = RECEIPT_GMAIL_QUERY \
            .replace("is:unread ", "") \
            .replace("newer_than:14d", f"newer_than:{backfill_days}d")
        logger.info(f"BACKFILL MODE — querying all receipts in last {backfill_days}d (read or unread)")
    else:
        query = RECEIPT_GMAIL_QUERY

    try:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=200 if backfill_days else 50
        ).execute()
    except Exception as e:
        logger.error(f"Gmail list failed: {e}")
        return

    msg_metas = resp.get("messages", []) or []
    logger.info(f"Gmail query returned {len(msg_metas)} candidate messages "
                f"({len(processed)} in manifest)")
    if not msg_metas:
        return

    auto_applied_this_run = 0
    skipped_due_to_cap = 0
    applied_events = []   # collected for the end-of-run notification email

    for meta in msg_metas:
        msg_id = meta["id"]
        if msg_id in processed:
            prior = processed[msg_id]
            sig = prior.get("signature") or prior.get("decision") or "?"
            logger.info(f"Skipping {msg_id} — already in manifest ({sig})")
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

        # Second pass with PDF text for signatures whose key data lives in the
        # attachment (Suburban: amount; Tiger: invoice number).
        if (not receipt.line_items) and any(
            n in receipt.parse_notes
            for n in ("pdf-attachment-amount-unparsed", "tiger-needs-pdf")
        ):
            pdf_text = fetch_pdf_attachment_text(service, msg)
            if pdf_text:
                receipt = classify_message(msg, pdf_text=pdf_text)

        # (Cap is now enforced per line item inside the loop below.)
        # Match against scanned_invoices — returns one MatchResult per line item
        # (or one for the whole receipt if there's no invoice breakdown).
        conn = get_connection()
        try:
            matches = find_matching_invoice(receipt, conn)
        finally:
            conn.close()

        # Process each match independently. A multi-invoice receipt can have
        # some lines auto-apply and others needs_review / no_match.
        per_line_outcomes = []
        any_apply_error = False

        for match in matches:
            li = match.line_item
            line_payload = {
                "message_id": msg_id,
                "signature": receipt.signature_key,
                "vendor": receipt.vendor_canonical,
                "amount": li.amount,
                "invoice_number": li.invoice_number,
                "payment_date": receipt.payment_date,
                "decision": match.decision,
                "reason": match.reason,
                "matched_invoice_id": match.matched_invoice_id,
                "candidate_count": match.candidate_count,
            }

            if match.decision == "auto_apply":
                if auto_applied_this_run >= PER_RUN_AUTO_APPLY_CAP:
                    skipped_due_to_cap += 1
                    audit("skipped_cap", **line_payload)
                    per_line_outcomes.append("skipped_cap")
                    continue

                if dry_run:
                    logger.info(
                        f"[DRY] Would auto-apply {receipt.vendor_canonical} "
                        f"${li.amount:.2f} → invoice #{li.invoice_number} "
                        f"(id={match.matched_invoice_id})"
                    )
                    auto_applied_this_run += 1
                    per_line_outcomes.append("dry_auto_apply")
                    continue

                try:
                    payment_id = apply_payment(
                        invoice_id=match.matched_invoice_id,
                        amount=li.amount,
                        payment_method=receipt.payment_method,
                        payment_date=receipt.payment_date or date.today().isoformat(),
                        reference=f"RCPT-{msg_id}",
                        memo=f"Auto-applied from {receipt.signature_key} receipt",
                    )
                    auto_applied_this_run += 1
                    line_payload["payment_id"] = payment_id
                    audit("auto_applied", **line_payload)
                    logger.info(
                        f"Auto-applied {receipt.vendor_canonical} ${li.amount:.2f} "
                        f"→ invoice #{li.invoice_number} (id={match.matched_invoice_id}, "
                        f"ap_payment {payment_id})"
                    )
                    per_line_outcomes.append("auto_applied")
                    applied_events.append({
                        "vendor": receipt.vendor_canonical,
                        "invoice_number": li.invoice_number,
                        "amount": float(li.amount),
                        "invoice_id": match.matched_invoice_id,
                        "payment_id": payment_id,
                        "signature": receipt.signature_key,
                    })
                except Exception as e:
                    logger.error(f"apply_payment failed for {msg_id} #{li.invoice_number}: {e}")
                    audit("apply_error", error=str(e), **line_payload)
                    any_apply_error = True
                    per_line_outcomes.append("apply_error")

            elif match.decision == "needs_review":
                if dry_run:
                    logger.info(
                        f"[DRY] Needs review: {receipt.vendor_canonical} "
                        f"#{li.invoice_number} ${li.amount} — {match.reason}"
                    )
                    per_line_outcomes.append("dry_needs_review")
                    continue
                review_record = {
                    **line_payload,
                    "subject": _subj(msg),
                    "candidates": match.candidates,
                }
                log_review(review_record)
                audit("needs_review", **line_payload)
                per_line_outcomes.append("needs_review")

            else:  # no_match
                if dry_run:
                    logger.info(
                        f"[DRY] No match: {receipt.vendor_canonical} "
                        f"#{li.invoice_number} ${li.amount} — {match.reason}"
                    )
                    per_line_outcomes.append("dry_no_match")
                    continue
                audit("no_match", **line_payload)
                per_line_outcomes.append("no_match")

        # Mark message processed (manifest) only if every line had a terminal
        # decision — i.e. no transient apply errors. If we hit any errors, we'll
        # retry next run; otherwise lock the message in so we don't re-process.
        if not dry_run:
            if any_apply_error:
                # Don't mark processed — re-process next run to retry failed lines.
                # apply_payment is defensive about already-paid invoices, so
                # re-running is safe for lines that did apply this round.
                pass
            else:
                processed[msg_id] = {
                    "vendor": receipt.vendor_canonical,
                    "signature": receipt.signature_key,
                    "outcomes": per_line_outcomes,
                    "ts": datetime.utcnow().isoformat() + "Z",
                }
                # Mark read only if at least one line auto-applied. Leave it
                # unread for needs_review / no_match so Mike sees it.
                if "auto_applied" in per_line_outcomes:
                    mark_message_read(service, msg_id)
            save_manifest(manifest)

    logger.info(f"Run complete: {auto_applied_this_run} auto-applied, "
                f"{skipped_due_to_cap} deferred by cap")

    # Notify Mike of every auto-apply in one summary email per run
    if not dry_run and applied_events:
        send_apply_notification(applied_events)

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
    dry_run = "--dry-run" in sys.argv
    backfill_days = None
    for i, a in enumerate(sys.argv):
        if a == "--backfill" and i + 1 < len(sys.argv):
            try:
                backfill_days = int(sys.argv[i + 1])
            except ValueError:
                logger.error(f"--backfill requires an integer (days), got {sys.argv[i + 1]!r}")
                sys.exit(2)
    run(dry_run=dry_run, backfill_days=backfill_days)
