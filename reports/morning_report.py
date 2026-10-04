"""
morning_report.py
-----------------
Generates and emails a daily sales summary from the local Toast SQLite database.
Leads with a "Yesterday" flash per location (net sales vs same weekday last
week / last year, orders, avg check, labor $ and %, discounts, voids) and a red
banner when a location that normally trades has no sales or no labor rows.
Then this week vs last week vs last year, PTD, and YTD for each location.

Every sales figure here excludes voided/deleted orders, same as the dashboard.
PTD/YTD run through YESTERDAY and compare to the same calendar span last year.

Usage (standalone):
    cd /opt/red-nun-dashboard && venv/bin/python -m reports.morning_report
    cd /opt/red-nun-dashboard && venv/bin/python -m reports.morning_report 2026-04-15

Also wired as route: GET /staff/api/reports/morning?date=YYYY-MM-DD&preview=1

Cron (7:30 AM daily):
    30 7 * * * cd /opt/red-nun-dashboard && /opt/red-nun-dashboard/venv/bin/python -m reports.morning_report >> /var/log/morning_report.log 2>&1
"""

import os
import sys
import smtplib
import logging
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Same filter as reports/analytics.py — keeps these numbers tied to the dashboard.
VALID = ("COALESCE(json_extract(raw_json, '$.deleted'), 0) != 1 "
         "AND COALESCE(json_extract(raw_json, '$.voided'), 0) != 1")

LOCATIONS = {
    "chatham": "Red Nun Bar & Grill - Chatham, MA",
    "dennis":  "Red Nun Bar & Grill - Dennis Port, MA",
}

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------------------
# Data Layer
# ------------------------------------------------------------------------------

def get_conn():
    from integrations.toast.data_store import get_connection
    return get_connection()


def daily_sales(location, start, end):
    conn = get_conn()
    rows = conn.execute("""
        SELECT business_date,
               SUM(net_amount) AS sales
        FROM   orders
        WHERE  location      = ?
          AND  business_date >= ?
          AND  business_date <= ?
          AND  {VALID}
        GROUP  BY business_date
    """.format(VALID=VALID), (location, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))).fetchall()
    conn.close()
    return {datetime.strptime(r["business_date"], "%Y%m%d").date(): r["sales"] or 0
            for r in rows}


def range_sales(location, start, end):
    """Net sales for start..end inclusive (dates)."""
    conn = get_conn()
    row = conn.execute("""
        SELECT SUM(net_amount)
        FROM   orders
        WHERE  location      = ?
          AND  business_date >= ?
          AND  business_date <= ?
          AND  {VALID}
    """.format(VALID=VALID),
        (location, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))).fetchone()
    conn.close()
    return row[0] or 0


def same_day_last_year(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:          # Feb 29
        return d.replace(year=d.year - 1, day=28)


def to_date_pairs(report_date):
    """(ptd_this, ptd_last, ytd_this, ytd_last) spans, all ending YESTERDAY.

    The old version ran last year's PTD/YTD through *today's* date, so it
    compared ~3 days of this month to 13 months of last year (PTD showed
    -99%, YTD -53%). Last year now stops on the same calendar day.
    """
    end = report_date - timedelta(days=1)
    end_ly = same_day_last_year(end)
    return (
        (date(end.year, end.month, 1), end),
        (date(end_ly.year, end_ly.month, 1), end_ly),
        (date(end.year, 1, 1), end),
        (date(end_ly.year, 1, 1), end_ly),
    )


def ptd_ytd(location, report_date):
    return tuple(range_sales(location, s, e) for s, e in to_date_pairs(report_date))


# ------------------------------------------------------------------------------
# Yesterday flash — the numbers to look at before anything else
# ------------------------------------------------------------------------------

# Salaried managers aren't in 7shifts time entries. Same daily rates the
# dashboard's labor summary uses (reports/analytics.get_labor_summary).
SALARIED_DAILY = {"dennis": 880.0 / 7, "chatham": 1375.0 / 7}

LABOR_WARN_PCT = float(os.getenv("FLASH_LABOR_WARN_PCT", "30"))


def day_stats(location, d):
    """One location, one business date. All sales exclude voided/deleted orders."""
    bd = d.strftime("%Y%m%d")
    conn = get_conn()
    try:
        o = conn.execute(f"""
            SELECT COUNT(*) AS orders, SUM(net_amount) AS net,
                   SUM(total_amount) AS total, SUM(discount_amount) AS chk_disc
            FROM orders
            WHERE location = ? AND business_date = ? AND {VALID}
        """, (location, bd)).fetchone()
        item = conn.execute(f"""
            SELECT SUM(CASE WHEN i.voided = 1 THEN i.price ELSE 0 END)  AS void_amt,
                   SUM(CASE WHEN i.voided = 1 THEN 1 ELSE 0 END)        AS void_cnt,
                   SUM(CASE WHEN i.voided = 0 THEN i.discount ELSE 0 END) AS item_disc
            FROM order_items i
            JOIN orders o ON o.guid = i.order_guid
            WHERE i.location = ? AND i.business_date = ? AND {VALID.replace('raw_json', 'o.raw_json')}
        """, (location, bd)).fetchone()
        vchk = conn.execute("""
            SELECT COUNT(*) AS n, SUM(total_amount) AS amt
            FROM orders
            WHERE location = ? AND business_date = ?
              AND (COALESCE(json_extract(raw_json, '$.voided'), 0) = 1
                   OR COALESCE(json_extract(raw_json, '$.deleted'), 0) = 1)
        """, (location, bd)).fetchone()
        top_void = conn.execute(f"""
            SELECT COALESCE(NULLIF(o.server_name, ''), 'Unknown') AS who,
                   SUM(i.price) AS amt
            FROM order_items i
            JOIN orders o ON o.guid = i.order_guid
            WHERE i.location = ? AND i.business_date = ? AND i.voided = 1
            GROUP BY who ORDER BY amt DESC LIMIT 1
        """, (location, bd)).fetchone()
        lab = conn.execute("""
            SELECT COUNT(*) AS punches, SUM(total_pay) AS pay,
                   SUM(regular_hours + overtime_hours) AS hrs,
                   SUM(overtime_hours) AS ot
            FROM time_entries
            WHERE location = ? AND business_date = ?
        """, (location, bd)).fetchone()
    finally:
        conn.close()

    orders = o["orders"] or 0
    net = o["net"] or 0
    hourly = lab["pay"] or 0
    punches = lab["punches"] or 0
    labor = hourly + (SALARIED_DAILY.get(location, 0) if orders else 0)
    return {
        "orders": orders,
        "net": net,
        "avg_check": (o["total"] or 0) / orders if orders else 0,
        "discounts": (o["chk_disc"] or 0) + ((item["item_disc"] or 0) if item else 0),
        "item_void_amt": (item["void_amt"] or 0) if item else 0,
        "item_void_cnt": (item["void_cnt"] or 0) if item else 0,
        "void_checks": vchk["n"] or 0,
        "void_check_amt": vchk["amt"] or 0,
        "top_voider": (top_void["who"], top_void["amt"] or 0) if top_void else None,
        "punches": punches,
        "labor": labor,
        "labor_pct": labor / net * 100 if net else None,
        "hours": lab["hrs"] or 0,
        "ot_hours": lab["ot"] or 0,
    }


def normally_open(location, d):
    """True if this location traded on the same weekday last week or last year.
    Dennis is dark Mon/Tue off-season — no alarm for a day it never opens."""
    for prior in (d - timedelta(days=7), d - timedelta(days=364)):
        conn = get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE location = ? AND business_date = ?",
                (location, prior.strftime("%Y%m%d"))).fetchone()[0]
        finally:
            conn.close()
        if n:
            return True
    return False


def flash_data(report_date):
    y = report_date - timedelta(days=1)
    out = {"date": y, "locs": {}, "alarms": []}
    for loc in LOCATIONS:
        t = day_stats(loc, y)
        t["lw_net"] = day_stats(loc, y - timedelta(days=7))["net"]
        t["ly_net"] = day_stats(loc, y - timedelta(days=364))["net"]
        name = LOCATION_SHORT[loc]
        if t["orders"] == 0 and normally_open(loc, y):
            out["alarms"].append(
                f"{name}: NO SALES recorded for {y:%a %m/%d}. Toast sync may be down "
                f"— these numbers are incomplete until it's fixed.")
        elif t["orders"] and t["punches"] == 0:
            out["alarms"].append(
                f"{name}: sales recorded but NO labor punches for {y:%a %m/%d}. "
                f"Labor % below is salaried only — check the 7shifts sync.")
        out["locs"][loc] = t
    return out


LOCATION_SHORT = {"chatham": "Chatham", "dennis": "Dennis"}


def _pct_span(new, old):
    p, d = pct_change(new, old)
    return f'<span class="{d}">{p}</span>' if p else "&#8212;"


def render_flash(f):
    alarms = "".join(f'<div class="alarm">&#9888; {a}</div>' for a in f["alarms"])
    cols = ""
    for loc, t in f["locs"].items():
        name = LOCATION_SHORT[loc]
        if t["orders"] == 0:
            cols += f'<td class="fcol"><div class="fname">{name}</div><div class="fclosed">No sales</div></td>'
            continue
        lp = t["labor_pct"]
        lp_cls = "down" if lp is not None and lp >= LABOR_WARN_PCT else ""
        voids = f'{t["item_void_cnt"]} items / ${t["item_void_amt"]:,.0f}'
        if t["void_checks"]:
            voids += f'<br>+ {t["void_checks"]} whole checks / ${t["void_check_amt"]:,.0f}'
        tv = t["top_voider"]
        top = (f'<div class="fnote">Most voids: {tv[0]} (${tv[1]:,.0f})</div>'
               if tv and tv[1] >= 50 else "")
        ot = (f'<div class="fnote" style="color:#cb4335">OT: {t["ot_hours"]:.1f} hrs</div>'
              if t["ot_hours"] > 0 else "")
        cols += f"""<td class="fcol">
          <div class="fname">{name}</div>
          <div class="fbig">${t['net']:,.0f}</div>
          <table class="fk">
            <tr><td>vs last wk</td><td>{_pct_span(t['net'], t['lw_net'])}</td></tr>
            <tr><td>vs last yr</td><td>{_pct_span(t['net'], t['ly_net'])}</td></tr>
            <tr><td>Orders / avg</td><td>{t['orders']} / ${t['avg_check']:,.2f}</td></tr>
            <tr><td>Labor</td><td>${t['labor']:,.0f} &middot; <span class="{lp_cls}">{lp:.1f}%</span></td></tr>
            <tr><td>Hours</td><td>{t['hours']:.1f}</td></tr>
            <tr><td>Discounts</td><td>${t['discounts']:,.0f}</td></tr>
            <tr><td>Voids</td><td>{voids}</td></tr>
          </table>{top}{ot}
        </td>"""
    return f"""
    <div class="loc-name">Yesterday &middot; {f['date']:%A %m/%d}</div>
    {alarms}
    <table class="flash"><tr>{cols}</tr></table>
    <div class="fnote">Labor = 7shifts wages + salaried managers, before payroll taxes.
      Red labor % = at or over {LABOR_WARN_PCT:.0f}%.</div>
    <hr class="div">"""


def flash_subject(f):
    if not f["locs"]:
        return ""
    bits = []
    for loc, t in f["locs"].items():
        if t["orders"]:
            lp = f' L{t["labor_pct"]:.0f}%' if t["labor_pct"] is not None else ""
            bits.append(f'{LOCATION_SHORT[loc]} ${t["net"]:,.0f}{lp}')
    s = " · ".join(bits)
    if f["alarms"]:
        s = "⚠ DATA MISSING · " + s
    return s


# ------------------------------------------------------------------------------
# Date Math
# ------------------------------------------------------------------------------

def week_start(d):
    return d - timedelta(days=d.weekday())

def build_week(monday):
    return [monday + timedelta(days=i) for i in range(7)]


# ------------------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------------------

def fmt_dollars(v):
    if v is None or v == 0:
        return ""
    return f"${v:,.0f}"

def pct_change(new_val, old_val):
    if not old_val or not new_val:
        return None, None
    p = round((new_val - old_val) / old_val * 100)
    arrow = "&#8593;" if p >= 0 else "&#8595;"   # ↑ ↓
    direction = "up" if p >= 0 else "down"
    return f"{abs(p)} % {arrow}", direction


# ------------------------------------------------------------------------------
# HTML Pieces
# ------------------------------------------------------------------------------

CSS = """<style>
  body { margin:0; padding:0; background:#f0f0f0;
         font-family:Arial,Helvetica,sans-serif; font-size:13px; color:#333; }
  .outer { max-width:660px; margin:16px auto; background:#fff;
           border:1px solid #d8d8d8; }

  /* Header */
  .hdr { padding:14px 22px; border-bottom:3px solid #8B0000;
         background:#fff; overflow:hidden; }
  .logo-box { float:left; background:#8B0000; color:#fff;
              padding:6px 14px; border-radius:3px;
              font-size:13px; font-weight:bold; line-height:1.4; }
  .logo-sub  { font-size:9px; font-weight:normal; letter-spacing:1.5px;
               display:block; }
  .hdr-date  { float:right; font-size:21px; color:#6faacc;
               font-style:italic; line-height:42px; }

  /* Body */
  .body { padding:18px 22px 28px; }
  .greeting { margin-bottom:22px; line-height:1.7; }

  /* Section heading */
  .loc-name { color:#154360; font-size:14px; font-weight:bold;
              margin:26px 0 2px; }
  .week-of  { font-size:11px; font-weight:bold; color:#555; margin-bottom:7px; }

  /* Weekly sales table */
  table.st { width:100%; border-collapse:collapse; margin-bottom:10px; }
  table.st th.grp { font-size:10px; color:#aaa; font-weight:normal;
                    text-align:center; padding:4px 8px 0; border:none; }
  table.st th.grp.left { text-align:left; }
  table.st th.sub { font-size:11px; font-weight:bold; color:#555;
                    text-align:right; padding:3px 8px 5px;
                    border-bottom:1px solid #ddd; }
  table.st th.sub.left { text-align:left; }
  table.st td { padding:5px 8px; text-align:right;
                border-top:1px solid #f2f2f2; font-size:13px; }
  table.st td.d { text-align:left; font-weight:bold; }
  table.st tr.tot td { border-top:2px solid #154360; font-weight:bold; }
  .up   { color:#229954; }
  .down { color:#cb4335; }

  /* PTD / YTD */
  table.ptd-wrap { width:100%; border-collapse:separate;
                   border-spacing:8px 0; margin:4px -8px 0; }
  table.box { width:100%; border-collapse:collapse;
              border:1px solid #ddd; }
  table.box tr.boxtitle td { background:#f5f5f5; text-align:center;
    font-size:10px; font-weight:bold; color:#666; padding:5px 8px;
    border-bottom:1px solid #ddd; letter-spacing:.4px; }
  table.box tr.boxhdr th { font-size:11px; font-weight:bold; color:#555;
    text-align:center; padding:4px 8px;
    border-bottom:1px solid #e8e8e8; background:#fafafa; }
  table.box tr.boxdata td { text-align:center; padding:7px 8px;
                            font-size:13px; }
  table.box td.up   { color:#229954; font-weight:bold; }
  table.box td.down { color:#cb4335; font-weight:bold; }

  /* Yesterday flash */
  .alarm { background:#fdecea; border:1px solid #cb4335; color:#922b21;
           padding:8px 10px; margin:6px 0; font-weight:bold; }
  table.flash { width:100%; border-collapse:separate; border-spacing:8px 0;
                margin:4px -8px 6px; }
  td.fcol { vertical-align:top; border:1px solid #ddd; padding:10px; width:50%; }
  .fname { font-size:11px; font-weight:bold; color:#555; letter-spacing:.4px; }
  .fbig { font-size:24px; font-weight:bold; color:#154360; margin:2px 0 6px; }
  .fclosed { color:#999; margin-top:8px; }
  table.fk { width:100%; border-collapse:collapse; }
  table.fk td { padding:2px 0; font-size:12px; border-top:1px solid #f2f2f2; }
  table.fk td + td { text-align:right; }
  .fnote { font-size:11px; color:#888; margin-top:4px; }

  hr.div { border:none; border-top:1px solid #ebebeb; margin:20px 0 0; }

  /* Footer */
  .footer { padding:12px 22px; border-top:1px solid #e8e8e8;
            font-size:11px; color:#bbb; }
</style>"""


def st_pct_cell(pct, direction):
    if pct is None:
        return "<td></td>"
    return f'<td class="{direction}">{pct}</td>'


def render_sales_table(this_sales, last_sales, ly_sales, visible):
    def total(arr):
        vals = [v for v in arr if v is not None]
        return sum(vals) if vals else None

    # Compare like-for-like: last week / last year totals only over the days
    # this week has sales so far (a Sunday-morning report otherwise shows a
    # 6-day week "down 11%" against a full 7-day week).
    have = [i for i in range(7) if this_sales[i] is not None]
    tw_t = total(this_sales)
    lw_t = total([last_sales[i] for i in have])
    ly_t = total([ly_sales[i] for i in have])

    rows = ""
    for i in visible:
        tw = this_sales[i]; lw = last_sales[i]; ly = ly_sales[i]
        p_lw, d_lw = pct_change(tw, lw)
        p_ly, d_ly = pct_change(tw, ly)
        rows += f"""
        <tr>
          <td class="d">{DAYS[i]}</td>
          <td>{fmt_dollars(tw)}</td>
          <td>{fmt_dollars(lw)}</td>
          {st_pct_cell(p_lw, d_lw)}
          <td>{fmt_dollars(ly)}</td>
          {st_pct_cell(p_ly, d_ly)}
        </tr>"""

    p_lw_t, d_lw_t = pct_change(tw_t, lw_t)
    p_ly_t, d_ly_t = pct_change(tw_t, ly_t)
    rows += f"""
        <tr class="tot">
          <td class="d"></td>
          <td>{fmt_dollars(tw_t)}</td>
          <td>{fmt_dollars(lw_t)}</td>
          {st_pct_cell(p_lw_t, d_lw_t)}
          <td>{fmt_dollars(ly_t)}</td>
          {st_pct_cell(p_ly_t, d_ly_t)}
        </tr>"""

    return f"""
    <table class="st">
      <thead>
        <tr>
          <th class="grp left"></th>
          <th class="grp">This Week</th>
          <th class="grp" colspan="2">Last Week</th>
          <th class="grp" colspan="2">Last Year</th>
        </tr>
        <tr>
          <th class="sub left"></th>
          <th class="sub">Sales</th>
          <th class="sub">Sales</th><th class="sub">%</th>
          <th class="sub">Sales</th><th class="sub">%</th>
        </tr>
      </thead>
      <tbody>{rows}
      </tbody>
    </table>"""


def render_summary(ptd_this, ptd_last, ytd_this, ytd_last):
    def box(title, this_yr, last_yr):
        pct, direction = pct_change(this_yr, last_yr)
        chng = f'<td class="{direction}">{pct}</td>' if pct else "<td>&#8212;</td>"
        return f"""<table class="box">
          <tr class="boxtitle"><td colspan="3">{title}</td></tr>
          <tr class="boxhdr"><th>This Yr</th><th>Last Yr</th><th>Chng</th></tr>
          <tr class="boxdata">
            <td>{fmt_dollars(this_yr) or "&#8212;"}</td>
            <td>{fmt_dollars(last_yr) or "&#8212;"}</td>
            {chng}
          </tr>
        </table>"""

    return f"""
    <table class="ptd-wrap">
      <tr>
        <td>{box("Period To Date", ptd_this, ptd_last)}</td>
        <td>{box("Year To Date",   ytd_this, ytd_last)}</td>
      </tr>
    </table>"""


def location_block(loc_key, loc_name, report_date):
    today    = report_date
    this_mon = week_start(today)
    last_mon = this_mon - timedelta(weeks=1)
    ly_mon   = this_mon - timedelta(weeks=52)

    tw_days = build_week(this_mon)
    lw_days = build_week(last_mon)
    ly_days = build_week(ly_mon)

    all_start = min(ly_days[0], lw_days[0], tw_days[0])
    all_end   = max(ly_days[-1], lw_days[-1], tw_days[-1])
    sm = daily_sales(loc_key, all_start, all_end)

    this_s = [sm.get(d, None) for d in tw_days]
    last_s = [sm.get(d, None) for d in lw_days]
    ly_s   = [sm.get(d, None) for d in ly_days]

    visible = [i for i in range(7)
               if any(x is not None for x in [this_s[i], last_s[i], ly_s[i]])]

    ptd_this, ptd_last, ytd_this, ytd_last = ptd_ytd(loc_key, report_date)

    return f"""
    <div class="loc-name">{loc_name}</div>
    <div class="week-of">Week of {this_mon.strftime('%m/%d/%Y')}</div>
    {render_sales_table(this_s, last_s, ly_s, visible)}
    {render_summary(ptd_this, ptd_last, ytd_this, ytd_last)}
    <hr class="div">"""


def company_wide_block(report_date):
    today    = report_date
    this_mon = week_start(today)
    last_mon = this_mon - timedelta(weeks=1)
    ly_mon   = this_mon - timedelta(weeks=52)

    tw_days = build_week(this_mon)
    lw_days = build_week(last_mon)
    ly_days = build_week(ly_mon)

    all_start = min(ly_days[0], lw_days[0], tw_days[0])
    all_end   = max(ly_days[-1], lw_days[-1], tw_days[-1])

    def combined(week_days):
        totals = [0.0] * 7
        for loc in LOCATIONS:
            m = daily_sales(loc, all_start, all_end)
            for i, d in enumerate(week_days):
                totals[i] += m.get(d, 0) or 0
        return [v if v > 0 else None for v in totals]

    this_s = combined(tw_days)
    last_s = combined(lw_days)
    ly_s   = combined(ly_days)

    visible = [i for i in range(7)
               if any(x is not None for x in [this_s[i], last_s[i], ly_s[i]])]

    per_loc = [ptd_ytd(loc, report_date) for loc in LOCATIONS]
    ptd_this, ptd_last, ytd_this, ytd_last = (sum(x[i] for x in per_loc) for i in range(4))

    return f"""
    <div class="loc-name">Company Wide</div>
    <div class="week-of">Week of {this_mon.strftime('%m/%d/%Y')}</div>
    {render_sales_table(this_s, last_s, ly_s, visible)}
    {render_summary(ptd_this, ptd_last, ytd_this, ytd_last)}"""


def build_html(report_date, flash=None):
    date_str = report_date.strftime("%B %d, %Y")
    if flash is None:
        flash = flash_data(report_date)
    body = render_flash(flash)
    body += "".join(location_block(k, v, report_date) for k, v in LOCATIONS.items())
    body += company_wide_block(report_date)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{CSS}
</head>
<body>
<div class="outer">
  <div class="hdr">
    <div class="logo-box">RED NUN<span class="logo-sub">BAR &amp; GRILL</span></div>
    <div class="hdr-date">{date_str}</div>
    <div style="clear:both"></div>
  </div>
  <div class="body">
    <div class="greeting">Dear <strong>Mike,</strong><br><br>Here is your morning sales report.</div>
    {body}
  </div>
  <div class="footer">
    Generated by dashboard.rednun.com &nbsp;&middot;&nbsp; Toast POS data
    &nbsp;&middot;&nbsp; {datetime.now().strftime("%Y-%m-%d %H:%M")}
  </div>
</div>
</body>
</html>"""


# ------------------------------------------------------------------------------
# Email
# ------------------------------------------------------------------------------

def send_email(html_body, report_date, headline=""):
    from_addr = os.getenv("REPORT_FROM_EMAIL", "dashboard@rednun.com")
    to_addr   = os.getenv("REPORT_TO_EMAIL",   "mgiorgio@rednun.com")
    smtp_host = os.getenv("SMTP_HOST",         "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT",     "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")

    subject = f"[Red Nun] Morning Sales Report for {report_date.strftime('%m/%d/%Y')}"
    if headline:
        subject = f"[Red Nun] {headline}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.sendmail(from_addr, [to_addr], msg.as_string())

    logger.info(f"Report sent to {to_addr}")


# ------------------------------------------------------------------------------
# Entry Point
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        report_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        report_date = date.today()

    logger.info(f"Building report for {report_date}")
    flash = flash_data(report_date)
    html = build_html(report_date, flash)
    for a in flash["alarms"]:
        logger.warning(f"FLASH ALARM: {a}")

    if os.getenv("SAVE_HTML"):
        out = f"/tmp/morning_report_{report_date}.html"
        with open(out, "w") as f:
            f.write(html)
        logger.info(f"HTML saved to {out}")

    yday = report_date - timedelta(days=1)
    send_email(html, report_date, f"{yday:%a %m/%d}: {flash_subject(flash)}")
