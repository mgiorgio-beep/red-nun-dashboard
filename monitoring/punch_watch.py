#!/usr/bin/env python3
"""
punch_watch.py — daily bad-punch detector for Red Nun.

Reads Toast time entries from the local SQLite store and reports punches that
will overpay (or underpay) if they reach payroll untouched:

  1. AUTO CLOCK-OUT  — Toast auto-punches anyone who forgets, landing ~4:00 AM.
  2. MISSING OUT     — no clock-out recorded at all (still "on the clock").
  3. LONG SHIFT      — worked hours above a threshold, a softer version of #1.

One message per location, sent to that location's manager. Silent when clean.

Why this exists: corrections made in Toast do not reliably reach 7shifts, so a
bad punch found at payroll time has usually already gone stale. Catching it the
next morning means the manager still remembers the shift.

Run from the repo root so `integrations` is importable:
    python3 monitoring/punch_watch.py

Env (from .env):
    TOAST_DB_PATH             default /var/lib/rednun/toast_data.db
    PUNCH_TZ                  default America/New_York
    PUNCH_STORED_TZ           utc | local   (how naive timestamps are stored; default utc)
    TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID
    PUNCH_ALERT_EMAIL_<LOC>   e.g. PUNCH_ALERT_EMAIL_CHATHAM=gm@...  (comma-separated ok)
    PUNCH_ALERT_EMAIL         fallback recipients for every location
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
"""

import os
import re
import sys
import json
import smtplib
import sqlite3
import argparse
from email.message import EmailMessage
from datetime import datetime, date, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

DB_PATH = os.getenv("TOAST_DB_PATH", "/var/lib/rednun/toast_data.db")
LOCAL_TZ_NAME = os.getenv("PUNCH_TZ", "America/New_York")
STORED_TZ = os.getenv("PUNCH_STORED_TZ", "utc").strip().lower()

# An auto clock-out lands at 4:00 AM. Allow slack for clock drift / rounding.
AUTO_WINDOW = (3, 40, 4, 20)   # from 03:40 to 04:20 local
LONG_SHIFT_HOURS = 12.0

# Pseudo-employees that never clock out by design (Toast register "people").
# Matched on the SET of name tokens, so "Bar PM" == "PM, Bar" == "pm bar".
# Override with env PUNCH_IGNORE="bar pm,host pm,..." (comma-separated).
_IGNORE_RAW = os.getenv(
    "PUNCH_IGNORE", "bar pm,bar am,host pm,host am")
IGNORE_SETS = [frozenset(re.sub(r"[^a-z ]", " ", n.lower()).split())
               for n in _IGNORE_RAW.split(",") if n.strip()]


def is_pseudo(name):
    toks = frozenset(re.sub(r"[^a-z ]", " ", (name or "").lower()).split())
    return toks in IGNORE_SETS


def local_tz():
    if ZoneInfo:
        try:
            return ZoneInfo(LOCAL_TZ_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=-4))  # EDT fallback; only affects display


def parse_ts(raw):
    """Parse a Toast timestamp into an aware datetime in local time.

    Handles ISO strings with or without an offset, with or without millis,
    and the 'Z' suffix. Returns None if unparseable rather than guessing.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # Toast sends +0000; fromisoformat wants +00:00
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if STORED_TZ == "utc" else local_tz())
    return dt.astimezone(local_tz())



def bdate_str(d):
    """DB business_date format: yyyyMMdd (confirmed via job 002, 2026-08-18)."""
    return d.strftime("%Y%m%d")


def bdate_parse(raw):
    """Parse a DB business_date ('20260818' or ISO) into a date."""
    t = str(raw).strip()[:10].replace("-", "")[:8]
    try:
        return datetime.strptime(t, "%Y%m%d").date()
    except ValueError:
        return None

def in_auto_window(dt):
    h1, m1, h2, m2 = AUTO_WINDOW
    mins = dt.hour * 60 + dt.minute
    return h1 * 60 + m1 <= mins <= h2 * 60 + m2


def sync_gaps(conn, start_date, end_date):
    """Locations/dates with no completed labor sync in sync_log for the window.

    A scan that runs before the Toast sync would report a clean day that
    isn't. Defensive: if sync_log is missing or shaped differently, return
    None and let the caller say 'could not verify' instead of guessing.
    """
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT location, business_date FROM sync_log
            WHERE data_type = 'labor' AND status = 'complete'
              AND business_date >= ? AND business_date <= ?
            """,
            (bdate_str(start_date), bdate_str(end_date)),
        )
        synced = {(r[0], r[1]) for r in cur.fetchall()}
        locs = [r[0] for r in conn.execute(
            "SELECT DISTINCT location FROM time_entries").fetchall()]
        gaps = []
        d = start_date
        while d <= end_date:
            for loc in locs:
                if (loc, bdate_str(d)) not in synced:
                    gaps.append((loc, d.isoformat()))
            d += timedelta(days=1)
        return gaps
    except Exception:
        return None


def fetch(conn, start_date, end_date):
    cur = conn.execute(
        """
        SELECT guid, location, employee_name, job_title, business_date,
               clock_in, clock_out, regular_hours, overtime_hours, hourly_wage
        FROM time_entries
        WHERE business_date >= ? AND business_date <= ?
        ORDER BY location, business_date, employee_name
        """,
        (bdate_str(start_date), bdate_str(end_date)),
    )
    return [dict(r) for r in cur.fetchall()]


def typical_out(conn, employee_name, job_title, before_date, lookback=45):
    """Median clock-out time for this person+job on comparable prior shifts.

    Returned only as a SUGGESTION. The real out-time is a human call.
    """
    since = bdate_str(before_date - timedelta(days=lookback))
    cur = conn.execute(
        """
        SELECT clock_out FROM time_entries
        WHERE employee_name = ? AND IFNULL(job_title,'') = IFNULL(?,'')
          AND business_date >= ? AND business_date < ?
          AND clock_out IS NOT NULL AND clock_out != ''
        """,
        (employee_name, job_title, since, bdate_str(before_date)),
    )
    mins = []
    for (raw,) in cur.fetchall():
        dt = parse_ts(raw)
        if not dt or in_auto_window(dt):
            continue          # never learn from another bad punch
        m = dt.hour * 60 + dt.minute
        if m < 240:           # after-midnight outs: treat as 24h+ for averaging
            m += 1440
        mins.append(m)
    if len(mins) < 3:
        return None
    mins.sort()
    med = mins[len(mins) // 2] % 1440
    h24, mm = (med // 60) % 24, med % 60
    ampm = "AM" if h24 < 12 else "PM"
    h12 = h24 % 12 or 12
    return f"{h12}:{mm:02d} {ampm}"


def _hm(label):
    """'10:00 PM' -> (22, 0)"""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", label)
    h, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return h, mm


def classify(rows, conn):
    findings = []
    for r in rows:
        if is_pseudo(r["employee_name"]):
            continue
        cin, cout = parse_ts(r["clock_in"]), parse_ts(r["clock_out"])
        hours = (r["regular_hours"] or 0) + (r["overtime_hours"] or 0)
        wage = r["hourly_wage"] or 0
        bdate = bdate_parse(r["business_date"]) or date.today()

        kind = None
        if not cout:
            kind = "MISSING OUT"
        elif in_auto_window(cout):
            kind = "AUTO CLOCK-OUT"
        elif hours > LONG_SHIFT_HOURS:
            kind = "LONG SHIFT"
        if not kind:
            continue

        suggestion = typical_out(conn, r["employee_name"], r["job_title"], bdate)
        exposure = None
        if suggestion and cin and hours:
            sh, sm = _hm(suggestion)
            end = cin.replace(hour=sh, minute=sm, second=0, microsecond=0)
            if end <= cin:
                end += timedelta(days=1)
            likely = (end - cin).total_seconds() / 3600
            if hours - likely > 0.25:
                exposure = round((hours - likely) * wage, 2)

        findings.append({
            "location": r["location"], "employee": r["employee_name"],
            "job": r["job_title"], "date": bdate.isoformat(), "kind": kind,
            "in": cin.strftime("%-I:%M %p") if cin else "?",
            "out": cout.strftime("%-I:%M %p") if cout else "— none —",
            "hours": round(hours, 2), "wage": wage,
            "suggestion": suggestion, "exposure": exposure,
        })
    return findings


def history_by_location(conn, days, end_date):
    """Per-location missing-clock-out counts over the trailing window.

    NOTE: historical rows are frozen at end-of-night (the sync never revisits
    old dates), so many of these were later fixed in Toast. That makes this a
    coaching list — who forgets — not a list of unpaid/overpaid shifts.
    """
    start = end_date - timedelta(days=days - 1)
    cur = conn.execute(
        """
        SELECT location, employee_name, COUNT(*) AS n, MAX(business_date) AS last_d
        FROM time_entries
        WHERE business_date >= ? AND business_date <= ?
          AND (clock_out IS NULL OR clock_out = '')
        GROUP BY location, employee_name
        ORDER BY location, n DESC, employee_name
        """,
        (bdate_str(start), bdate_str(end_date)),
    )
    out = {}
    for loc, name, n, last_d in cur.fetchall():
        if is_pseudo(name):
            continue
        d = bdate_parse(last_d)
        out.setdefault(loc, []).append(
            (" ".join((name or "?").split()), n, d.strftime("%-m/%-d") if d else "?"))
    return out


INTRO = """This is the new automatic punch check for {loc}, set up by Mike.

WHAT THIS IS
Every morning at 8:00 AM this address emails you IF someone didn't clock
out properly the night before. No forgotten punches = no email.

WHY IT MATTERS
A missed clock-out turns into a 4:00 AM auto-punch in Toast. If nobody
catches it, payroll pays hours nobody worked - last pay period that was
44 phantom hours across the two restaurants before we caught them by hand.

WHAT TO DO WHEN ONE ARRIVES
1. Open Toast -> Time entry management
2. Fix the clock-out to the real time (each alert suggests the person's
   usual out-time to jog your memory)
3. That's it. Do it the same day - payroll picks up the correction.

Below: what last night looked like, then the 60-day list of who forgets.
Questions go to Mike - replies to this address go nowhere.

================================================================
"""


def render_history(entries, days):
    lines = ["", f"WHO FORGETS TO CLOCK OUT — last {days} days", ""]
    repeat = [e for e in entries if e[1] >= 2]
    once = [e for e in entries if e[1] == 1]
    for name, n, last_d in repeat:
        lines.append(f"   {name:<24} {n:>2}x   (last: {last_d})")
    if once:
        w = "person" if len(once) == 1 else "people"
        lines.append(f"   ...plus {len(once)} {w} once each")
    lines.append("")
    lines.append("Many of these were fixed in Toast after the fact — this list is")
    lines.append("about the habit, not money owed. Every miss becomes a 4:00 AM")
    lines.append("auto-punch someone has to catch. Worth a word with the repeats.")
    return "\n".join(lines)


def render(location, items):
    lines = [f"PUNCH CHECK — {location}", ""]
    total = 0.0
    for f in items:
        lines.append(f"{f['kind']}  {f['employee']} ({f['job'] or 'no role'})")
        lines.append(f"   {f['date']}   {f['in']} - {f['out']}   {f['hours']} hrs")
        if f["suggestion"]:
            lines.append(f"   usually clocks out around {f['suggestion']}")
        if f["exposure"]:
            total += f["exposure"]
            lines.append(f"   ~${f['exposure']:.2f} of extra wages if it stands")
        lines.append("")
    if total:
        lines.append(f"Total exposure if untouched: ~${total:.2f}")
        lines.append("")
    lines.append("Fix these in TOAST today. A missing clock-out becomes a 4:00 AM")
    lines.append("auto-punch that overpays if it reaches payroll uncorrected.")
    return "\n".join(lines)


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        if resp.status_code == 200:
            print("[OK] Telegram sent")
            return True
        print(f"[WARN] Telegram {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[WARN] Telegram failed: {e}")
    return False


def loc_key(location):
    """Map messy Toast location strings to a stable env-key suffix.

    'Red Nun Chatham' -> CHATHAM, 'Red Nun Bar & Grill - Dennis Port' -> DENNIS.
    Falls back to the stripped uppercase string for anything unrecognized.
    """
    low = (location or "").lower()
    if "chatham" in low:
        return "CHATHAM"
    if "dennis" in low:
        return "DENNIS"
    return re.sub(r"[^A-Z0-9]", "", (location or "").upper()) or "UNKNOWN"


def recipients_for(location):
    key = "PUNCH_ALERT_EMAIL_" + loc_key(location)
    raw = os.getenv(key) or os.getenv("PUNCH_ALERT_EMAIL", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def send_email(location, subject, body):
    to = recipients_for(location)
    host = os.getenv("SMTP_HOST", "").strip()
    if not to or not host:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "dashboard@rednun.com"))
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            # morning_report.py uses SMTP_PASSWORD; accept both names
            user = os.getenv("SMTP_USER")
            pw = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD")
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        print(f"[OK] Emailed {', '.join(to)}")
        return True
    except Exception as e:
        print(f"[WARN] Email failed: {e}")
    return False


def main():
    ap = argparse.ArgumentParser(description="Daily bad-punch check")
    ap.add_argument("--days", type=int, default=1, help="business days back to scan (default 1 = yesterday)")
    ap.add_argument("--date", help="scan a single business date (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="print only, send nothing")
    ap.add_argument("--always-notify", action="store_true", help="send even when clean")
    ap.add_argument("--intro", action="store_true",
                    help="prepend the one-time introduction explaining the system")
    ap.add_argument("--history-days", type=int, default=0,
                    help="append a per-location repeat-offender list covering the last N days (intro email)")
    ap.add_argument("--sample", action="store_true", help="dump raw vs parsed timestamps and exit")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    if args.date:
        start = end = date.fromisoformat(args.date)
    else:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=max(args.days, 1) - 1)

    rows = fetch(conn, start, end)

    if args.sample:
        print(f"DB={DB_PATH}  stored_tz={STORED_TZ}  display_tz={LOCAL_TZ_NAME}")
        print(f"{len(rows)} rows for {start}..{end}\n")
        for r in rows[:12]:
            print(f"  {r['employee_name']:<22} raw_out={str(r['clock_out'])[:32]:<34} "
                  f"parsed={parse_ts(r['clock_out'])}")
        print("\nIf 'parsed' times do not match what actually happened on the floor, "
              "flip PUNCH_STORED_TZ between utc and local.")
        return 0

    findings = classify(rows, conn)
    print(f"Scanned {len(rows)} punches for {start}..{end} — {len(findings)} flagged")

    gaps = sync_gaps(conn, start, end)
    stale_note = ""
    if gaps is None:
        stale_note = ("NOTE: could not verify Toast sync status (sync_log "
                      "unreadable) — treat a clean result with suspicion.")
    elif gaps:
        pieces = ", ".join(f"{l} {d}" for l, d in gaps[:6])
        stale_note = (f"WARNING: Toast labor sync has NOT completed for: {pieces}."
                      " Punches for those days are missing or stale —"
                      " this scan cannot clear them.")
    if stale_note:
        print("\n" + stale_note)
        if not args.dry_run:
            send_telegram("PUNCH CHECK — " + stale_note)

    by_loc = {}
    for f in findings:
        by_loc.setdefault(f["location"], []).append(f)

    hist = {}
    if args.history_days > 0:
        hist = history_by_location(conn, args.history_days, end)

    if not findings and not hist:
        if args.always_notify and not args.dry_run:
            send_telegram(f"PUNCH CHECK {start}..{end} — all clean, {len(rows)} punches.")
        return 0

    all_locs = sorted(set(by_loc) | set(hist))
    for loc in all_locs:
        items = by_loc.get(loc, [])
        if items:
            body = render(loc, items)
        else:
            body = f"PUNCH CHECK — {loc}\n\nNo punch issues yesterday."
        if hist.get(loc):
            body += "\n" + render_history(hist[loc], args.history_days)
        if args.intro:
            body = INTRO.format(loc=loc) + body
        if stale_note:
            body = stale_note + "\n\n" + body
        print("\n" + body)
        if args.dry_run:
            continue
        n_issues = len(items)
        if args.intro:
            subject = f"[Red Nun] NEW: daily punch check for {loc} — how it works"
        elif n_issues:
            subject = f"[Red Nun] {n_issues} punch issue(s) — {loc} — {start}"
        else:
            subject = f"[Red Nun] Punch check — {loc} — all clear"
        sent = send_email(loc, subject, body)
        send_telegram(body)
        if not sent and not recipients_for(loc):
            print(f"[NOTE] no email recipients configured for {loc} "
                  f"(set PUNCH_ALERT_EMAIL_{re.sub(r'[^A-Z0-9]', '', loc.upper())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
