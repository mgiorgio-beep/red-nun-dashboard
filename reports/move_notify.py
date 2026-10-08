"""
Email Mike on every confirmed transfer / waste entry and every void (brief 8I).

- Sent AFTER the DB commit, on a background thread, so a slow mail server never
  delays the "Done." in the walk-in.
- Every send is a row in move_notifications (entry, event, attempts, error).
  A failed send retries 3 times; still failing -> status 'failed', which the
  dashboard banner and the Friday open-items list show. Nothing is lost: the
  entry is already saved, and `retry_failed()` (cron) tries again.
- Setting email_mode on /transfer/admin: per_entry (default) | daily_digest | off.
  In digest mode entries are logged 'digest' and send_digest() mails them once a day.
No API calls; plain SMTP like monitoring/weekly_count_reminder.py.
"""
import json
import logging
import os
import smtplib
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from integrations.toast.data_store import get_connection

logger = logging.getLogger(__name__)
TO = os.getenv('MOVES_EMAIL_TO', 'mgiorgio@rednun.com')
BASE_URL = os.getenv('PUBLIC_BASE_URL', 'https://dashboard.rednun.com')
RETRY_WAITS = (0, 20, 90)       # three tries
HOUSE = {'chatham': 'Chatham', 'dennis': 'Dennis'}


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS move_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,                -- transfer | waste
            entry_id INTEGER NOT NULL,
            event TEXT NOT NULL,               -- logged | voided
            status TEXT NOT NULL DEFAULT 'queued',   -- queued | sent | failed | digest | digested | off
            attempts INTEGER NOT NULL DEFAULT 0,
            subject TEXT,
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT,
            UNIQUE(kind, entry_id, event)
        )
    """)


def smtp_send(subject, html, to=TO):
    from_addr = os.getenv('REPORT_FROM_EMAIL', 'dashboard@rednun.com')
    msg = MIMEMultipart('alternative')
    msg['Subject'], msg['From'], msg['To'] = subject, from_addr, to
    msg.attach(MIMEText(html, 'html'))
    with smtplib.SMTP(os.getenv('SMTP_HOST', 'smtp.gmail.com'), int(os.getenv('SMTP_PORT', '587')), timeout=30) as s:
        s.ehlo()
        s.starttls()
        if os.getenv('SMTP_USER') and os.getenv('SMTP_PASSWORD'):
            s.login(os.getenv('SMTP_USER'), os.getenv('SMTP_PASSWORD'))
        s.sendmail(from_addr, [a.strip() for a in to.split(',')], msg.as_string())


def queue(kind, entry_id, event):
    """Record the notification and send it in the background. Never raises."""
    try:
        from reports.item_recognition import get_setting
        conn = get_connection()
        try:
            ensure_table(conn)
            mode = get_setting(conn, 'email_mode')
            status = {'off': 'off', 'daily_digest': 'digest'}.get(mode, 'queued')
            conn.execute("""INSERT OR IGNORE INTO move_notifications (kind, entry_id, event, status)
                            VALUES (?, ?, ?, ?)""", (kind, entry_id, event, status))
            conn.commit()
            nid = conn.execute("SELECT id FROM move_notifications WHERE kind = ? AND entry_id = ? AND event = ?",
                               (kind, entry_id, event)).fetchone()['id']
        finally:
            conn.close()
        if status == 'queued':
            threading.Thread(target=_send_with_retries, args=(nid,), daemon=True).start()
    except Exception as e:
        logger.exception(f'move_notify.queue failed for {kind} {entry_id} {event}: {e}')


def _send_with_retries(nid, waits=RETRY_WAITS):
    for wait in waits:
        time.sleep(wait)
        if send_one(nid):
            return True
    conn = get_connection()
    try:
        conn.execute("UPDATE move_notifications SET status = 'failed' WHERE id = ? AND status <> 'sent'", (nid,))
        conn.commit()
    finally:
        conn.close()
    logger.error(f'move notification {nid} failed after {len(waits)} tries')
    return False


def send_one(nid, sender=None):
    """One attempt. True when sent."""
    conn = get_connection()
    try:
        n = conn.execute("SELECT * FROM move_notifications WHERE id = ?", (nid,)).fetchone()
        if not n or n['status'] == 'sent':
            return True
        subject, html = build(conn, n['kind'], n['entry_id'], n['event'])
        try:
            (sender or smtp_send)(subject, html)
        except Exception as e:
            conn.execute("UPDATE move_notifications SET attempts = attempts + 1, error = ?, subject = ? WHERE id = ?",
                         (f'{type(e).__name__}: {e}'[:500], subject, nid))
            conn.commit()
            return False
        conn.execute("""UPDATE move_notifications SET status = 'sent', attempts = attempts + 1, subject = ?, error = NULL,
                        sent_at = CURRENT_TIMESTAMP WHERE id = ?""", (subject, nid))
        conn.commit()
        return True
    finally:
        conn.close()


def retry_failed():
    """Cron: try every failed / stuck-queued notification again."""
    conn = get_connection()
    try:
        ensure_table(conn)
        ids = [r[0] for r in conn.execute("""SELECT id FROM move_notifications WHERE status = 'failed'
                   OR (status = 'queued' AND created_at < datetime('now', '-15 minutes'))""")]
    finally:
        conn.close()
    ok = 0
    for nid in ids:
        if send_one(nid):
            ok += 1
        else:
            c = get_connection()
            try:
                c.execute("UPDATE move_notifications SET status = 'failed' WHERE id = ?", (nid,))
                c.commit()
            finally:
                c.close()
    return ok, len(ids)


def failures(conn):
    """For the banner / Friday list."""
    ensure_table(conn)
    return [dict(r) for r in conn.execute("""SELECT id, kind, entry_id, event, attempts, error, created_at
                                             FROM move_notifications WHERE status = 'failed' ORDER BY id DESC""")]


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def _money(x):
    return '—' if x is None else f'${x:,.2f}'


def _row(conn, kind, entry_id):
    from reports import item_recognition as R
    if kind == 'transfer':
        r = conn.execute("SELECT * FROM inventory_transfers WHERE id = ?", (entry_id,)).fetchone()
        pid = r['from_product_id']
    else:
        r = conn.execute("SELECT * FROM waste_log WHERE id = ?", (entry_id,)).fetchone()
        pid = r['product_id']
    p = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    return r, (R.card_name(conn, p) if p else f'product {pid}')


def build(conn, kind, entry_id, event):
    from reports import house_moves as H
    from reports.moves import business_date
    r, name = _row(conn, kind, entry_id)
    qty = f"{H.fmt_qty(r['qty_entered'])} {H.plural(r['qty_entered'], r['unit_entered'])}"
    short_unit = {'case': 'cs', 'cases': 'cs', 'bottle': 'btl', 'bottles': 'btl'}.get(
        H.plural(r['qty_entered'], r['unit_entered']), H.plural(r['qty_entered'], r['unit_entered']))
    short = f"{H.fmt_qty(r['qty_entered'])} {short_unit}"
    pre = 'VOIDED: ' if event == 'voided' else ''
    if kind == 'transfer':
        subject = (f"{pre}Transfer: {short} {name}, {HOUSE[r['from_location']]} -> {HOUSE[r['to_location']]} "
                   f"({_money(r['total_cost'])}) — {r['entered_by']}")
        where = f"{HOUSE[r['from_location']]} → {HOUSE[r['to_location']]}"
        open_link = f"{BASE_URL}/transfer?highlight={entry_id}"
    else:
        subject = (f"{pre}Waste: {short} {name} at {HOUSE[r['location']]}, "
                   f"{H.REASONS.get(r['reason_code'], 'no reason')} ({_money(r['total_cost'])}) — {r['entered_by']}")
        where = f"{HOUSE[r['location']]} — reason: {H.REASONS.get(r['reason_code'], 'none given')}"
        open_link = f"{BASE_URL}/waste?highlight={entry_id}"
    flags = []
    if kind == 'transfer' and r['needs_link']:
        flags.append('needs a product link at the receiving house')
    if kind == 'waste' and r['flagged']:
        flags.append('over the review threshold')
    if r['total_cost'] is None:
        flags.append('no price found')
    if kind == 'transfer' and r['is_settlement']:
        flags.append('return in kind (settles an open line)')
    fixes = json.loads(r['fixes']) if r['fixes'] else []
    rows = [('Item', escape(name)), ('Quantity', escape(qty) + (f" = {r['qty_base']:g} {escape(r['base_unit'] or '')}"
                                                                   if r['qty_base'] is not None else '')),
            ('Where', escape(where)),
            ('Cost', f"{_money(r['total_cost'])} ({escape(r['cost_source'] or '')}"
                     + (f": {escape(r['cost_detail'])}" if r['cost_detail'] else '') + ')'),
            ('Logged by', f"{escape(r['entered_by'] or '')} via {escape(r['entered_via'] or '')}"),
            ('When', escape((r['transferred_at'] if kind == 'transfer' else r['logged_at'])[:16].replace('T', ' '))),
            ('They said', f"“{escape(r['raw_text'] or '')}”"),
            ('Matched by', escape(r['match_trace'] or ''))]
    if fixes:
        rows.append(('Fixed', '<br>'.join(escape(f) for f in fixes)))
    if flags:
        rows.append(('Flags', '<b style="color:#b45309">' + escape('; '.join(flags)) + '</b>'))
    if event == 'voided':
        rows.insert(0, ('VOIDED', f"by {escape(r['voided_by'] or '')} {escape((r['voided_at'] or '')[:16])}"
                                  + (f" — {escape(r['void_reason'])}" if r['void_reason'] else '')))
    # running totals
    if kind == 'transfer':
        from reports.intercompany import month_net
        mn = month_net(conn, business_date()[:6])
        total_line = f"{mn['label']} so far: {mn['sentence']}"
    else:
        wk = conn.execute("""SELECT COALESCE(SUM(total_cost), 0) FROM waste_log WHERE location = ? AND status = 'logged'
                             AND business_date >= strftime('%Y%m%d', 'now', 'localtime', '-6 days')""",
                          (r['location'],)).fetchone()[0]
        total_line = f"{HOUSE[r['location']]} waste, last 7 days: {_money(wk)}"
    trs = ''.join(f'<tr><td style="padding:4px 10px 4px 0;color:#64748b;vertical-align:top">{k}</td>'
                  f'<td style="padding:4px 0">{v}</td></tr>' for k, v in rows)
    html = f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:560px;color:#0f172a">
<table style="border-collapse:collapse;font-size:14px">{trs}</table>
<p style="margin:14px 0 6px;font-size:14px"><b>{escape(total_line)}</b></p>
<p style="margin:12px 0"><a href="{open_link}">Open in dashboard</a> &nbsp;·&nbsp;
<a href="{open_link}&void=1">Void</a></p></div>"""
    return subject, html


def send_digest(day=None):
    """daily_digest mode: one email with everything logged as 'digest'."""
    conn = get_connection()
    try:
        ensure_table(conn)
        rows = conn.execute("SELECT * FROM move_notifications WHERE status = 'digest' ORDER BY id").fetchall()
        if not rows:
            return 0
        parts, subjects = [], []
        for n in rows:
            s, h = build(conn, n['kind'], n['entry_id'], n['event'])
            subjects.append(s)
            parts.append(f'<h3 style="font-size:15px;margin:18px 0 6px">{escape(s)}</h3>{h}')
        smtp_send(f'Transfers & waste: {len(rows)} entr{"y" if len(rows) == 1 else "ies"}', ''.join(parts))
        conn.executemany("UPDATE move_notifications SET status = 'digested', sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                         [(n['id'],) for n in rows])
        conn.commit()
        return len(rows)
    finally:
        conn.close()


if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
    logging.basicConfig(level=logging.INFO)
    if '--digest' in sys.argv:
        print('digest entries sent:', send_digest())
    else:
        print('retried (ok, of):', retry_failed())
