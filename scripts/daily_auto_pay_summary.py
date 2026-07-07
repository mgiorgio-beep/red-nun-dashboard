"""
Daily 6 PM email summary of auto-pay activity.

Queries auto_pay_decisions for the current calendar day, formats a short
HTML email, and sends it via the same SMTP config used by morning_report.

Cron:
    0 18 * * *  /opt/red-nun-dashboard/venv/bin/python3 /opt/red-nun-dashboard/scripts/daily_auto_pay_summary.py

Env vars (re-uses morning_report's setup):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
    REPORT_FROM_EMAIL (default dashboard@rednun.com)
    REPORT_TO_EMAIL   (default mgiorgio@rednun.com)
"""
import os
import sys
import smtplib
import logging
from datetime import date, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from integrations.toast.data_store import get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# Skip-reasons we don't bother showing in the email body. They're noise: every
# non-auto-pay vendor produces a 'vendor_not_auto_pay' row, which would drown
# out the interesting events.
BORING_SKIP_REASONS = {"vendor_not_auto_pay", "no_vendor_bill_pay_record"}


def _fetch_today(conn, today_iso):
    paid = conn.execute(
        """
        SELECT id, invoice_id, vendor_name, invoice_total, check_number,
               ap_payment_id, print_job_id, details, created_at
        FROM auto_pay_decisions
        WHERE decision = 'paid' AND DATE(created_at) = ?
        ORDER BY id
        """,
        (today_iso,),
    ).fetchall()
    skipped = conn.execute(
        """
        SELECT id, invoice_id, vendor_name, invoice_total, reason, details,
               created_at
        FROM auto_pay_decisions
        WHERE decision = 'skipped'
          AND DATE(created_at) = ?
          AND reason NOT IN ('vendor_not_auto_pay', 'no_vendor_bill_pay_record')
        ORDER BY id
        """,
        (today_iso,),
    ).fetchall()
    errors = conn.execute(
        """
        SELECT id, invoice_id, vendor_name, invoice_total, reason, details,
               created_at
        FROM auto_pay_decisions
        WHERE decision = 'error' AND DATE(created_at) = ?
        ORDER BY id
        """,
        (today_iso,),
    ).fetchall()
    # Also include print_jobs in 'error' state regardless of when, since
    # they're still pending operator attention.
    stuck_prints = conn.execute(
        """
        SELECT id, payment_id, check_number, attempts, last_error, updated_at
        FROM print_jobs
        WHERE status = 'error'
        ORDER BY updated_at DESC
        LIMIT 20
        """,
    ).fetchall()
    return paid, skipped, errors, stuck_prints


def _fmt_money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _build_html(paid, skipped, errors, stuck_prints, today_iso, recurring_printed=None):
    paid_total = sum((r["invoice_total"] or 0) for r in paid)

    recurring_printed = recurring_printed or []
    rec_total = sum((r.get("amount") or 0) for r in recurring_printed)
    rows_recurring = "".join(
        f"<tr><td>{r['vendor']}</td>"
        f"<td style='text-align:right'>{_fmt_money(r['amount'])}</td>"
        f"<td style='text-align:center'>#{r['check_number']}</td>"
        f"<td>due {r['due_date']} ({r['location']})</td></tr>"
        for r in recurring_printed
    )
    recurring_section = ""
    if recurring_printed:
        recurring_section = f"""
  <h3 style="margin:24px 0 6px 0;color:#16a34a">Recurring bills auto-printed — {len(recurring_printed)} check{'s' if len(recurring_printed)!=1 else ''}, {_fmt_money(rec_total)}</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <tr style="background:#f3f4f6"><th style='text-align:left;padding:4px 8px'>Vendor</th><th style='text-align:right;padding:4px 8px'>Amount</th><th style='padding:4px 8px'>Check #</th><th style='text-align:left;padding:4px 8px'>Due</th></tr>
    {rows_recurring}
  </table>
"""

    rows_paid = "".join(
        f"<tr><td>{r['vendor_name']}</td>"
        f"<td style='text-align:right'>{_fmt_money(r['invoice_total'])}</td>"
        f"<td style='text-align:center'>#{r['check_number'] or '—'}</td>"
        f"<td>invoice #{r['invoice_id']}</td></tr>"
        for r in paid
    ) or "<tr><td colspan=4 style='color:#6b7280'>None.</td></tr>"

    rows_skipped = "".join(
        f"<tr><td>{r['vendor_name']}</td>"
        f"<td style='text-align:right'>{_fmt_money(r['invoice_total'])}</td>"
        f"<td><code>{r['reason']}</code></td>"
        f"<td style='color:#6b7280'>{(r['details'] or '')}</td></tr>"
        for r in skipped
    ) or "<tr><td colspan=4 style='color:#6b7280'>None.</td></tr>"

    rows_errors = "".join(
        f"<tr><td>{r['vendor_name']}</td>"
        f"<td style='text-align:right'>{_fmt_money(r['invoice_total'])}</td>"
        f"<td style='color:#dc2626'>{(r['details'] or r['reason'])}</td></tr>"
        for r in errors
    ) or "<tr><td colspan=3 style='color:#6b7280'>None.</td></tr>"

    rows_stuck = "".join(
        f"<tr><td>job #{r['id']}</td>"
        f"<td>check #{r['check_number'] or '—'}</td>"
        f"<td>attempts {r['attempts']}</td>"
        f"<td style='color:#dc2626'>{(r['last_error'] or '')[:200]}</td></tr>"
        for r in stuck_prints
    ) or "<tr><td colspan=4 style='color:#6b7280'>None.</td></tr>"

    return f"""
<html><body style="font-family:system-ui,sans-serif;font-size:13px;color:#111827;max-width:800px">
  <h2 style="margin:0 0 4px 0">Red Nun — Auto-Pay Summary</h2>
  <div style="color:#6b7280;font-size:12px;margin-bottom:18px">{today_iso}</div>

  <h3 style="margin:8px 0 6px 0;color:#16a34a">Paid today — {len(paid)} check{'s' if len(paid)!=1 else ''}, {_fmt_money(paid_total)}</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <tr style="background:#f3f4f6"><th style='text-align:left;padding:4px 8px'>Vendor</th><th style='text-align:right;padding:4px 8px'>Amount</th><th style='padding:4px 8px'>Check #</th><th style='text-align:left;padding:4px 8px'>Invoice</th></tr>
    {rows_paid}
  </table>
{recurring_section}

  <h3 style="margin:24px 0 6px 0;color:#d97706">Skipped today — {len(skipped)}</h3>
  <div style="font-size:12px;color:#6b7280;margin-bottom:6px">
    Auto-pay-flagged invoices that hit a guardrail. Non-auto-pay vendors are filtered out.
  </div>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <tr style="background:#f3f4f6"><th style='text-align:left;padding:4px 8px'>Vendor</th><th style='text-align:right;padding:4px 8px'>Amount</th><th style='text-align:left;padding:4px 8px'>Reason</th><th style='text-align:left;padding:4px 8px'>Details</th></tr>
    {rows_skipped}
  </table>

  <h3 style="margin:24px 0 6px 0;color:#dc2626">Errors today — {len(errors)}</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <tr style="background:#f3f4f6"><th style='text-align:left;padding:4px 8px'>Vendor</th><th style='text-align:right;padding:4px 8px'>Amount</th><th style='text-align:left;padding:4px 8px'>Error</th></tr>
    {rows_errors}
  </table>

  <h3 style="margin:24px 0 6px 0;color:#dc2626">Print jobs stuck in error — {len(stuck_prints)}</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px">
    <tr style="background:#f3f4f6"><th style='text-align:left;padding:4px 8px'>Job</th><th style='padding:4px 8px'>Check</th><th style='padding:4px 8px'>Attempts</th><th style='text-align:left;padding:4px 8px'>Last error</th></tr>
    {rows_stuck}
  </table>

  <hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
  <div style="color:#6b7280;font-size:11px">
    Generated by scripts/daily_auto_pay_summary.py · sent from the dashboard server in Chatham.
    To stop receiving, remove the cron entry.
  </div>
</body></html>
"""


def _send(html, today_iso, paid_count, skipped_count, error_count):
    from_addr = os.getenv("REPORT_FROM_EMAIL", "dashboard@rednun.com")
    to_addr = os.getenv("REPORT_TO_EMAIL", "mgiorgio@rednun.com")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")

    subject_bits = [f"{paid_count} paid"]
    if skipped_count:
        subject_bits.append(f"{skipped_count} skipped")
    if error_count:
        subject_bits.append(f"{error_count} ERROR")
    subject = f"[Red Nun] Auto-pay {today_iso} — " + ", ".join(subject_bits)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    logger.info(f"Sent auto-pay summary → {to_addr}: {subject}")


def main():
    today = date.today().isoformat()

    # Auto-print any due recurring bills flagged auto_print=1 (rent etc.)
    # BEFORE building the summary so tonight's email includes them.
    try:
        from integrations.billpay.auto_pay import process_recurring_auto_print
        recurring_printed = process_recurring_auto_print()
    except Exception as e:
        logger.error(f"recurring auto-print failed: {e}")
        recurring_printed = []

    conn = get_connection()
    try:
        paid, skipped, errors, stuck_prints = _fetch_today(conn, today)
    finally:
        conn.close()

    # If literally nothing happened and nothing's stuck, skip the email
    # so we don't spam an empty digest every day.
    if not paid and not skipped and not errors and not stuck_prints and not recurring_printed:
        logger.info("No auto-pay activity and no stuck jobs — skipping email.")
        return 0

    html = _build_html(paid, skipped, errors, stuck_prints, today,
                       recurring_printed=recurring_printed)
    _send(html, today, len(paid) + len(recurring_printed), len(skipped), len(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
