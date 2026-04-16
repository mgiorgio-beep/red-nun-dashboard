"""
morning_report.py
-----------------
Generates and emails a daily sales summary from the local Toast SQLite database.
Shows this week vs last week vs last year, PTD, and YTD for each location.

Usage (standalone):
    cd /opt/red-nun-dashboard && venv/bin/python -m reports.morning_report
    cd /opt/red-nun-dashboard && venv/bin/python -m reports.morning_report 2026-04-15

Also wired as route: GET /staff/api/reports/morning?date=YYYY-MM-DD&preview=1

Cron (7:30 AM daily):
    30 7 * * * cd /opt/red-nun-dashboard && /opt/red-nun-dashboard/venv/bin/python -m reports.morning_report >> /var/log/morning_report.log 2>&1
"""

import os
import sys
import sqlite3
import smtplib
import logging
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "toast_data.db")

LOCATIONS = {
    "chatham": "Red Nun Bar & Grill - Chatham, MA",
    "dennis":  "Red Nun Bar & Grill - Dennis Port, MA",
}

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------------------
# Data Layer
# ------------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def daily_sales(location, start, end):
    conn = get_conn()
    rows = conn.execute("""
        SELECT business_date,
               SUM(net_amount) AS sales
        FROM   orders
        WHERE  location      = ?
          AND  business_date >= ?
          AND  business_date <= ?
        GROUP  BY business_date
    """, (location, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))).fetchall()
    conn.close()
    return {datetime.strptime(r["business_date"], "%Y%m%d").date(): r["sales"] or 0
            for r in rows}


def ytd_sales(location, year):
    conn = get_conn()
    row = conn.execute("""
        SELECT SUM(net_amount)
        FROM   orders
        WHERE  location      = ?
          AND  business_date >= ?
          AND  business_date <= ?
    """, (location, f"{year}0101", date.today().strftime("%Y%m%d"))).fetchone()
    conn.close()
    return row[0] or 0


def ptd_sales(location, period_start):
    conn = get_conn()
    row = conn.execute("""
        SELECT SUM(net_amount)
        FROM   orders
        WHERE  location      = ?
          AND  business_date >= ?
          AND  business_date <= ?
    """, (location, period_start.strftime("%Y%m%d"), date.today().strftime("%Y%m%d"))).fetchone()
    conn.close()
    return row[0] or 0


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

    tw_t = total(this_sales)
    lw_t = total(last_sales)
    ly_t = total(ly_sales)

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

    ps = date(today.year, today.month, 1)
    ptd_this = ptd_sales(loc_key, ps)
    ptd_last = ptd_sales(loc_key, date(today.year - 1, today.month, 1))
    ytd_this = ytd_sales(loc_key, today.year)
    ytd_last = ytd_sales(loc_key, today.year - 1)

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

    ps = date(today.year, today.month, 1)
    ptd_this = sum(ptd_sales(loc, ps) for loc in LOCATIONS)
    ptd_last = sum(ptd_sales(loc, date(today.year - 1, today.month, 1)) for loc in LOCATIONS)
    ytd_this = sum(ytd_sales(loc, today.year) for loc in LOCATIONS)
    ytd_last = sum(ytd_sales(loc, today.year - 1) for loc in LOCATIONS)

    return f"""
    <div class="loc-name">Company Wide</div>
    <div class="week-of">Week of {this_mon.strftime('%m/%d/%Y')}</div>
    {render_sales_table(this_s, last_s, ly_s, visible)}
    {render_summary(ptd_this, ptd_last, ytd_this, ytd_last)}"""


def build_html(report_date):
    date_str = report_date.strftime("%B %d, %Y")
    body = "".join(location_block(k, v, report_date) for k, v in LOCATIONS.items())
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

def send_email(html_body, report_date):
    from_addr = os.getenv("REPORT_FROM_EMAIL", "dashboard@rednun.com")
    to_addr   = os.getenv("REPORT_TO_EMAIL",   "mgiorgio@rednun.com")
    smtp_host = os.getenv("SMTP_HOST",         "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT",     "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")

    subject = f"[Red Nun] Morning Sales Report for {report_date.strftime('%m/%d/%Y')}"
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
    html = build_html(report_date)

    if os.getenv("SAVE_HTML"):
        out = f"/tmp/morning_report_{report_date}.html"
        with open(out, "w") as f:
            f.write(html)
        logger.info(f"HTML saved to {out}")

    send_email(html, report_date)
