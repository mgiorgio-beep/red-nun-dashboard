#!/usr/bin/env python3
"""
1st of the month, 7 AM ET: who owes whom between the houses (brief 8H2).

  - builds the prior month's transfer entries for both houses (status 'ready';
    Mike pushes them from /transfer/settle — nothing posts from here)
  - runs the tie-out; out of balance goes at the top in red
  - emails Mike: net balance in one line, both directions by category, every open
    item netted (Return / Pay suggestion), each house's waste (FYI, never settled),
    open problems, and the link to Reconcile

No API calls. Uses the same SMTP sender as the per-entry emails.

  venv/bin/python3 monitoring/intercompany_month_email.py              # cron: prior month
  venv/bin/python3 monitoring/intercompany_month_email.py --dry-run    # print, don't send or build
  venv/bin/python3 monitoring/intercompany_month_email.py --month 2026-10
"""
import os
import sys
from datetime import date, timedelta
from html import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, '.env'))

from integrations.toast.data_store import get_connection  # noqa: E402
from reports import intercompany as IC  # noqa: E402
from reports import move_numbers as MN  # noqa: E402

BASE_URL = os.getenv('PUBLIC_BASE_URL', 'https://dashboard.rednun.com')
HOUSE = {'chatham': 'Chatham', 'dennis': 'Dennis'}


def money(x):
    return f'${abs(x or 0):,.2f}'


def build(conn, ym, persist=True):
    import calendar
    st = IC.build_statement(conn, ym)
    entries = IC.build_month_entries(conn, ym, persist=persist) if persist else {}
    tie = IC.tie_out(conn)
    items = IC.open_items(conn)
    open_net = round(sum(i['cost'] for i in items), 2)
    mon = calendar.month_abbr[int(ym[5:])]
    if abs(open_net) < 0.005:
        subject = f"{mon} transfers: the houses are even"
    else:
        debtor, creditor = ('dennis', 'chatham') if open_net > 0 else ('chatham', 'dennis')
        subject = f"{mon} transfers: {IC.ENTITY[debtor]} owes {IC.ENTITY[creditor]} {money(open_net)}"
    y, m = int(ym[:4]), int(ym[5:])
    start, end = f'{ym}-01', f'{ym}-{calendar.monthrange(y, m)[1]:02d}'
    h = ['<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;color:#0f172a">']
    if not tie['ok']:
        h.append(f'<div style="background:#dc2626;color:#fff;padding:12px 14px;border-radius:8px;font-weight:700">'
                 f'{escape(tie["headline"])}<ul style="margin:6px 0 0 18px;font-weight:500">'
                 + ''.join(f'<li>{escape(p)}</li>' for p in tie['problems']) + '</ul></div>')
    h.append(f'<h2 style="margin:12px 0 4px">{escape(subject.split(": ", 1)[1].capitalize())}</h2>')
    h.append(f'<p style="color:#475569;margin:0 0 14px">What is still open between Red Buoy (Chatham) and Red Nun Public House '
             f'(Dennis) after returns and checks. {calendar.month_name[m]} activity: {escape(st["sentence"])}.</p>')
    if st['categories']:
        h.append('<table style="border-collapse:collapse;font-size:14px;width:100%"><tr style="color:#64748b">'
                 '<td>Category</td><td align="right">Chatham → Dennis</td><td align="right">Dennis → Chatham</td></tr>')
        for c in st['categories']:
            h.append(f'<tr><td>{escape(c["category"])}</td><td align="right">{money(c["chatham_to_dennis"])}</td>'
                     f'<td align="right">{money(c["dennis_to_chatham"])}</td></tr>')
        h.append('</table>')
    if items:
        h.append('<h3 style="margin:18px 0 6px;font-size:15px">Open items (netted by item)</h3>'
                 '<table style="border-collapse:collapse;font-size:14px;width:100%">')
        for i in items:
            way = f"{HOUSE[i['owed_to']]} → {HOUSE[i['owed_by']]}"
            sug = 'RETURN' if i['suggested'] == 'return' else 'PAY'
            h.append(f'<tr><td style="padding:3px 0">{escape(i["name"])} — {way} {i["abs_qty"]:g} {escape(i["base_unit"] or "")}'
                     f' ({money(i["abs_cost"])})</td><td align="right" style="color:{"#2563eb" if sug == "RETURN" else "#16a34a"};'
                     f'font-weight:700">{sug}</td></tr>')
        h.append('</table><p style="font-size:12px;color:#64748b">Suggestions only: RETURN = everyday stock both houses carry; '
                 'PAY = power buys (liquor, wine). You decide on the Reconcile page.</p>')
    h.append('<h3 style="margin:18px 0 6px;font-size:15px">Waste (FYI — never settled between the houses)</h3>')
    for loc in ('chatham', 'dennis'):
        w = MN.waste(conn, loc, start, end)
        top = MN.top_waste_items(conn, loc, start, end)
        h.append(f'<p style="margin:2px 0">{HOUSE[loc]}: {money(w["loss"])} lost'
                 + (f' + {money(w["staff_meal"])} staff meal' if w['staff_meal'] else '')
                 + (' — top: ' + ', '.join(f'{escape(t["item"])} {money(t["cost"])}' for t in top) if top else '') + '</p>')
    probs = [p for p in IC.open_problems(conn) if not p['text'].startswith('INTERCOMPANY OUT OF BALANCE')]
    if probs:
        h.append('<h3 style="margin:18px 0 6px;font-size:15px">Needs you</h3><ul style="margin:0 0 0 18px">'
                 + ''.join(f'<li>{escape(p["text"])}</li>' for p in probs) + '</ul>')
    if entries:
        h.append('<p style="font-size:13px;color:#475569">Month-close entries built and waiting for you to push: '
                 + ', '.join(f'{HOUSE[k]} {money(v["total_debits"])} ({v["status"]})' for k, v in entries.items()) + '.</p>')
    h.append(f'<p style="margin:20px 0"><a href="{BASE_URL}/transfer/settle?month={ym}" style="background:#16a34a;color:#fff;'
             f'padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:700">Settle {calendar.month_name[m]}</a>'
             f' &nbsp; <a href="{BASE_URL}/transfer/statement?month={ym}">Statement</a></p></div>')
    return subject, ''.join(h)


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--month' in args:
        ym = args[args.index('--month') + 1]
    else:
        last = date.today().replace(day=1) - timedelta(days=1)
        ym = last.strftime('%Y-%m')
    dry = '--dry-run' in args
    conn = get_connection()
    try:
        subject, html = build(conn, ym, persist=not dry)
    finally:
        conn.close()
    if dry:
        print(subject)
        print(html)
    else:
        from reports.move_notify import smtp_send
        smtp_send(subject, html, to=os.getenv('MOVES_EMAIL_TO', 'mgiorgio@rednun.com'))
        print('sent:', subject)
