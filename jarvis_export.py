#!/usr/bin/env python3
"""
jarvis_export.py — nightly rollup export for Jarvis.

Writes small CSVs to the Drive-synced folder so any Claude/Jarvis session can
answer sales + daypart questions without SSH'ing into the Beelink.

Key fix vs. the dashboard's analytics.py: dayparts are classified using real
US/Eastern local time via zoneinfo, NOT a hardcoded '-5 hours'. The hardcoded
offset is EST and silently misclassifies every EDT date (mid-Mar to early Nov),
pushing 4-5pm orders into Lunch.

Usage:
    python3 jarvis_export.py                 # export everything
    python3 jarvis_export.py --days 400      # limit lookback
    JARVIS_EXPORT_DIR=/path/to/drive python3 jarvis_export.py

Cron (3:30am nightly):
    30 3 * * * cd /opt/red-nun-dashboard && /opt/red-nun-dashboard/venv/bin/python \
        jarvis_export.py >> /var/log/jarvis_export.log 2>&1
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    sys.exit("Need Python 3.9+ for zoneinfo. Check: python3 --version")

EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- configuration

# Where the CSVs land. Must be inside the folder that syncs to Google Drive.
# Override with the JARVIS_EXPORT_DIR env var.
DEFAULT_OUT_DIR = os.environ.get(
    "JARVIS_EXPORT_DIR", "/home/rednun/cowork/red-nun-dashboard/jarvis_exports"
)

# DB discovery. The app uses DB_PATH (relative by default), so a bare
# "toast_data.db" in the wrong cwd resolves to an empty placeholder. We validate:
# non-empty AND has an `orders` table. See find_db().
SEARCH_ROOTS = ["/opt/red-nun-dashboard", "/opt/rednun", "/home/rednun", "/var/lib/rednun"]

# Daypart boundaries in Eastern local hours. Matches the dashboard's existing
# definition so these numbers reconcile with what you see on screen.
#   Late Night: >= 22 or < 4
#   Lunch:      4 <= h < 16
#   Dinner:     16 <= h < 22
LUNCH_END_HOUR = 16
LATE_START_HOUR = 22
LATE_END_HOUR = 4


def daypart_for(hour: int) -> str:
    if hour >= LATE_START_HOUR or hour < LATE_END_HOUR:
        return "Late"
    if hour < LUNCH_END_HOUR:
        return "Lunch"
    return "Dinner"


# ---------------------------------------------------------------- db plumbing


def _usable(path):
    """A DB is usable only if it exists, is non-empty, and has an orders table."""
    try:
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ok = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
        ).fetchone() is not None
        c.close()
        return ok
    except sqlite3.Error:
        return False


def _from_env_file():
    """The app sets DB_PATH in its .env; read it rather than guessing."""
    for d in SEARCH_ROOTS:
        f = os.path.join(d, ".env")
        if not os.path.exists(f):
            continue
        try:
            for line in open(f):
                line = line.strip()
                if line.startswith("DB_PATH="):
                    v = line.split("=", 1)[1].strip().strip('"\'')
                    yield v if os.path.isabs(v) else os.path.join(d, v)
        except OSError:
            continue


def find_db() -> str:
    tried = []
    candidates = [os.environ.get("JARVIS_DB_PATH", ""), os.environ.get("DB_PATH", "")]
    candidates += list(_from_env_file())
    candidates += [os.path.join(d, "toast_data.db") for d in SEARCH_ROOTS]
    candidates.append("toast_data.db")

    for path in candidates:
        if not path:
            continue
        tried.append(path)
        if _usable(path):
            return os.path.abspath(path)

    # Last resort: walk the search roots for any real toast DB.
    for root in SEARCH_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "venv", "backups", "_backups")]
            for fn in filenames:
                if fn.endswith(".db"):
                    p = os.path.join(dirpath, fn)
                    if _usable(p):
                        return os.path.abspath(p)

    sys.exit(
        "No usable toast DB found (needs a non-empty file with an 'orders' table).\n"
        "Tried:\n  " + "\n  ".join(tried) + "\n"
        "Fix: JARVIS_DB_PATH=/full/path/to/real.db python3 jarvis_export.py"
    )


def parse_utc(raw: str):
    """Toast opened_at is an ISO-ish UTC string. Be forgiving about the tail."""
    if not raw:
        return None
    s = str(raw).strip().replace("T", " ")
    s = s[:19]  # YYYY-MM-DD HH:MM:SS
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def bd_to_iso(bd: str) -> str:
    """business_date is YYYYMMDD text."""
    bd = str(bd)
    return f"{bd[0:4]}-{bd[4:6]}-{bd[6:8]}" if len(bd) == 8 else bd


# ---------------------------------------------------------------- the export


def export_dayparts(conn, out_dir, since_bd):
    cur = conn.execute(
        """
        SELECT location, business_date, opened_at,
               total_amount, tax_amount, tip_amount
        FROM orders
        WHERE business_date >= ?
        """,
        (since_bd,),
    )

    # (date, location, daypart) -> aggregates
    agg = defaultdict(lambda: {"checks": 0, "net": 0.0, "gross": 0.0,
                               "tax": 0.0, "tips": 0.0})
    skipped = 0

    for location, bd, opened_at, total, tax, tip in cur:
        dt = parse_utc(opened_at)
        if dt is None:
            skipped += 1
            continue
        local_hour = dt.astimezone(EASTERN).hour
        total = float(total or 0)
        tax = float(tax or 0)
        tip = float(tip or 0)

        key = (bd_to_iso(bd), location, daypart_for(local_hour))
        a = agg[key]
        a["checks"] += 1
        a["gross"] += total
        a["tax"] += tax
        a["tips"] += tip
        a["net"] += total - tax - tip

    path = os.path.join(out_dir, "jarvis_daypart_rollup.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "dow", "location", "daypart", "checks",
                    "net_sales", "gross_sales", "tax", "tips", "avg_check"])
        for (date, location, daypart) in sorted(agg):
            a = agg[(date, location, daypart)]
            dow = datetime.strptime(date, "%Y-%m-%d").strftime("%a")
            avg = a["net"] / a["checks"] if a["checks"] else 0
            w.writerow([
                date, dow, location, daypart, a["checks"],
                round(a["net"], 2), round(a["gross"], 2),
                round(a["tax"], 2), round(a["tips"], 2), round(avg, 2),
            ])

    return path, len(agg), skipped


def export_schema(conn, out_dir):
    """Dump table + column names so Jarvis can extend this without guessing."""
    schema = {}
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    for t in tables:
        try:
            cols = [(r[1], r[2]) for r in conn.execute(f"PRAGMA table_info('{t}')")]
            n = conn.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
            schema[t] = {"rows": n, "columns": [{"name": c, "type": ty} for c, ty in cols]}
        except sqlite3.Error as e:
            schema[t] = {"error": str(e)}

    path = os.path.join(out_dir, "jarvis_schema.json")
    with open(path, "w") as f:
        json.dump(schema, f, indent=2)
    return path, len(schema)


def _hour_span(start, end):
    """Yield (hour_of_day, minutes) for each clock hour a shift touches."""
    if end <= start:
        return
    cur = start
    while cur < end:
        nxt = (cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        seg_end = min(nxt, end)
        yield cur.hour, (seg_end - cur).total_seconds() / 60.0
        cur = seg_end


def export_labor(conn, out_dir, since_bd):
    """Allocate each punch's hours and pay across dayparts by clock overlap.

    Replaces the assumed labor model with what was actually paid. Cost uses
    total_pay when present, else regular + overtime at 1.5x. Burden is reported
    as a separate column, never baked in.
    """
    try:
        cur = conn.execute(
            """
            SELECT location, business_date, job_title, clock_in, clock_out,
                   regular_hours, overtime_hours, hourly_wage, total_pay
            FROM time_entries WHERE business_date >= ?
            """, (since_bd,))
    except sqlite3.Error as e:
        return None, 0, {"error": str(e)}

    agg = defaultdict(lambda: defaultdict(lambda: {"hours": 0.0, "cost": 0.0}))
    hours_hist = defaultdict(int)
    bad = 0

    for loc, bd, job, cin, cout, reg, ot, wage, pay in cur:
        a = parse_utc(cin)
        b = parse_utc(cout)
        if a is None or b is None:
            bad += 1
            continue
        a = a.astimezone(EASTERN)
        b = b.astimezone(EASTERN)
        if b <= a:
            bad += 1
            continue
        hours_hist[a.hour] += 1

        reg = float(reg or 0); ot = float(ot or 0); wage = float(wage or 0)
        cost = float(pay) if pay not in (None, "") else (reg * wage + ot * wage * 1.5)
        total_min = (b - a).total_seconds() / 60.0
        if total_min <= 0:
            bad += 1
            continue

        date = bd_to_iso(bd)
        job = (job or "Unknown").strip()
        for hr, mins in _hour_span(a, b):
            dp = daypart_for(hr)
            share = mins / total_min
            cell = agg[(date, loc, dp)][job]
            cell["hours"] += mins / 60.0
            cell["cost"] += cost * share

    path = os.path.join(out_dir, "jarvis_labor_rollup.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "dow", "location", "daypart", "job_title",
                    "hours", "labor_cost", "loaded_cost"])
        for (date, loc, dp) in sorted(agg):
            dow = datetime.strptime(date, "%Y-%m-%d").strftime("%a")
            for job, c in sorted(agg[(date, loc, dp)].items()):
                if c["hours"] < 0.01:
                    continue
                w.writerow([date, dow, loc, dp, job,
                            round(c["hours"], 2), round(c["cost"], 2),
                            round(c["cost"] * (1 + BURDEN), 2)])

    # Sanity: a restaurant's clock-ins should cluster in daytime/evening.
    tot = sum(hours_hist.values()) or 1
    peak = sorted(hours_hist.items(), key=lambda kv: -kv[1])[:5]
    sanity = {
        "punches_allocated": tot,
        "unparseable_or_zero_length": bad,
        "most_common_clock_in_hours_eastern": [{"hour": h, "punches": n} for h, n in peak],
        "note": "If these cluster at 2-6am the clock_in timezone assumption is wrong.",
    }
    return path, len(agg), sanity



BURDEN = 0.205  # ER taxes 18.4% + workers comp ~2.1% (measured)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0,
                    help="Lookback window in days (0 = everything)")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)

    db_path = find_db()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    if args.days:
        since = (datetime.now(EASTERN) - timedelta(days=args.days)).strftime("%Y%m%d")
    else:
        since = "00000000"

    dp_path, dp_rows, skipped = export_dayparts(conn, out_dir, since)
    sc_path, sc_tables = export_schema(conn, out_dir)
    lb_path, lb_rows, lb_sanity = export_labor(conn, out_dir, since)

    meta = {
        "generated_at": datetime.now(EASTERN).isoformat(),
        "db_path": db_path,
        "since_business_date": since,
        "daypart_rows": dp_rows,
        "orders_with_unparseable_timestamp": skipped,
        "tables_described": sc_tables,
        "timezone": "America/New_York (true DST, not hardcoded offset)",
        "daypart_definition": {
            "Lunch": f"{LATE_END_HOUR}:00-{LUNCH_END_HOUR-1}:59 Eastern",
            "Dinner": f"{LUNCH_END_HOUR}:00-{LATE_START_HOUR-1}:59 Eastern",
            "Late": f"{LATE_START_HOUR}:00-{LATE_END_HOUR-1}:59 Eastern",
        },
        "net_sales_definition": "total_amount - tax_amount - tip_amount",
        "labor_rows": lb_rows,
        "labor_burden_applied": BURDEN,
        "labor_sanity": lb_sanity,
    }
    meta_path = os.path.join(out_dir, "jarvis_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    conn.close()

    print(f"DB:      {db_path}")
    print(f"Out:     {out_dir}")
    print(f"Wrote:   {os.path.basename(dp_path)}  ({dp_rows} rows)")
    print(f"         {os.path.basename(sc_path)}  ({sc_tables} tables)")
    if lb_path:
        print(f"         {os.path.basename(lb_path)}  ({lb_rows} day/part cells)")
    else:
        print(f"         labor export SKIPPED: {lb_sanity.get('error')}")
    print(f"         {os.path.basename(meta_path)}")
    if skipped:
        print(f"WARNING: {skipped} orders had unparseable opened_at and were dropped")


if __name__ == "__main__":
    main()
