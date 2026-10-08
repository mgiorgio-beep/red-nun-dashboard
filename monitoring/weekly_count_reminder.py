"""
Tuesday weekly-count reminder email, with one-tap links to this week's count.

    python monitoring/weekly_count_reminder.py            # the Tuesday morning email
    python monitoring/weekly_count_reminder.py --nudge    # afternoon: only if a house isn't counted yet today
    python monitoring/weekly_count_reminder.py --dry-run  # print the email, send nothing

Cron (Chatham): 0 9 * * 2 and 0 15 * * 2 --nudge. No API spend; SMTP only,
same settings as the morning report.
"""
import os
import sys
import smtplib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from integrations.toast.data_store import get_connection
from reports.key_items import weekly_product_ids, last_weekly_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')
BASE_URL = os.getenv('DASHBOARD_URL', 'https://dashboard.rednun.com')
HOUSES = [('chatham', 'Chatham'), ('dennis', 'Dennis Port')]


def _et(ts):
    """count timestamps are SQLite CURRENT_TIMESTAMP (UTC)."""
    return datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('UTC')).astimezone(ET)


def house_status(conn, loc, now):
    ids = weekly_product_ids(conn, loc)
    noshelf = 0
    if ids:
        noshelf = conn.execute(f"""
            SELECT COUNT(*) FROM products p WHERE p.id IN ({','.join('?' * len(ids))})
              AND NOT EXISTS (SELECT 1 FROM product_storage_locations psl
                              JOIN storage_locations sl ON sl.id = psl.storage_location_id
                              WHERE psl.product_id = p.id AND sl.location = ?)
        """, (*ids, loc)).fetchone()[0]
    last = last_weekly_count(conn, loc)
    last_at = _et(last['created_at']) if last else None
    return {
        'items': len(ids),
        'noshelf': noshelf,
        'last_at': last_at,
        'done_today': bool(last_at and last_at.date() == now.date()),
        # Counted within the last 8 days = last Tuesday's count happened (a day's slack).
        'skipped_last': bool(ids) and not (last_at and (now - last_at) <= timedelta(days=8)),
    }


def waste_html(conn, now):
    """Last week's logged waste per house + top 3 items (brief 8F). Empty when none."""
    from reports.move_numbers import waste, top_waste_items
    end = (now - timedelta(days=1)).strftime('%Y%m%d')
    start = (now - timedelta(days=7)).strftime('%Y%m%d')
    lines = []
    for loc, name in HOUSES:
        w = waste(conn, loc, start, end)
        if not w['entries']:
            continue
        top = top_waste_items(conn, loc, start, end)
        lines.append(f"<p style='margin:4px 0;font-size:14px'><b>{name}</b>: ${w['loss']:,.2f} wasted"
                     + (f" (+ ${w['staff_meal']:,.2f} staff meal)" if w['staff_meal'] else '')
                     + (' — ' + ', '.join(f"{t['item']} ${t['cost']:,.2f}" for t in top) if top else '') + '</p>')
    if not lines:
        return ''
    return ("<h3 style='margin:20px 0 6px;font-size:15px'>Last week's waste</h3>" + ''.join(lines)
            + f"<p style='font-size:12px;color:#94a3b8;margin:4px 0'>Details: <a href='{BASE_URL}/waste'>{BASE_URL.replace('https://', '')}/waste</a></p>")


def build(now, houses, nudge):
    btn = ('display:block;padding:16px 20px;margin:10px 0;border-radius:12px;background:#22c55e;color:#000;'
           'font:800 18px -apple-system,Segoe UI,sans-serif;text-decoration:none;text-align:center')
    rows = []
    for loc, name in HOUSES:
        h = houses[loc]
        if not h['items'] or (nudge and h['done_today']):
            continue
        if h['done_today']:
            rows.append(f'<p style="margin:14px 0;color:#22c55e;font-weight:700">✓ {name} — counted today.</p>')
            continue
        notes = [f"{h['items']} items, about 15 minutes"]
        if h['last_at']:
            notes.append(f"last counted {h['last_at'].strftime('%a %b %-d')}")
        warn = ''
        if h['skipped_last']:
            warn = ('<div style="color:#f59e0b;font-weight:700;margin-top:4px">Last week was skipped — this week matters more.</div>'
                    if h['last_at'] else
                    '<div style="color:#f59e0b;font-weight:700;margin-top:4px">First weekly count here — it also puts items on their shelves, '
                    f"so it runs longer ({h['noshelf']} have no shelf yet).</div>")
        rows.append(f'<a href="{BASE_URL}/week?loc={loc}" style="{btn}">Count {name} →</a>'
                    f'<div style="color:#64748b;font-size:13px;margin:-2px 4px 16px">{" · ".join(notes)}</div>{warn}')
    if not rows:
        return None, None
    if nudge:
        subject = "[Red Nun] Still to do: this week's inventory count"
        intro = "Quick nudge — this week's key-item count isn't in yet."
    else:
        subject = f"[Red Nun] Inventory Tuesday — weekly count ({now.strftime('%b %-d')})"
        intro = "It's count day. Tap a house, walk the shelves, hit Save."
    html = f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:480px;margin:0 auto;padding:8px;color:#0f172a">
<h2 style="margin:0 0 6px">Weekly key-item count</h2>
<p style="margin:0 0 16px;color:#334155">{intro}</p>
{''.join(rows)}
{houses.get('_waste_html', '')}
<p style="margin-top:22px;font-size:12px;color:#94a3b8">Blank means not counted; out of it = 0.
Change what's on the list: <a href="{BASE_URL}/count/list">{BASE_URL.replace('https://', '')}/count/list</a></p>
</div>"""
    return subject, html


def send(subject, html):
    from_addr = os.getenv("REPORT_FROM_EMAIL", "dashboard@rednun.com")
    to_addr = os.getenv("COUNT_REMINDER_TO", os.getenv("REPORT_TO_EMAIL", "mgiorgio@rednun.com"))
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, from_addr, to_addr
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587"))) as server:
        server.ehlo()
        server.starttls()
        if os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"):
            server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
        server.sendmail(from_addr, [a.strip() for a in to_addr.split(',')], msg.as_string())
    logger.info(f"Sent '{subject}' to {to_addr}")


if __name__ == "__main__":
    nudge = '--nudge' in sys.argv
    now = datetime.now(ET)
    conn = get_connection()
    try:
        houses = {loc: house_status(conn, loc, now) for loc, _ in HOUSES}
        houses['_waste_html'] = '' if nudge else waste_html(conn, now)
    finally:
        conn.close()
    subject, html = build(now, houses, nudge)
    if not subject:
        logger.info("Nothing to send (no list, or every house already counted today).")
    elif '--dry-run' in sys.argv:
        print(subject)
        print(html)
    else:
        send(subject, html)
