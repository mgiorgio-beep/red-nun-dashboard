#!/usr/bin/env python3
"""
fall_lunch_check.py — Mon-Thu lunch vs dinner, Oct 1 - Nov 30 2025.

Uses real US/Eastern time (DST ends Nov 2, 2025), so the Oct half and the Nov
half are both classified correctly. A single hardcoded offset cannot do this.

Run:  python3 fall_lunch_check.py
"""
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
START, END = "20251001", "20251130"

def _usable(p):
    try:
        if not p or not os.path.exists(p) or os.path.getsize(p) == 0:
            return False
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ok = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
        c.close()
        return ok is not None
    except sqlite3.Error:
        return False


ROOTS = ["/opt/red-nun-dashboard", "/opt/rednun", "/home/rednun"]
_cands = [os.environ.get("JARVIS_DB_PATH", ""), os.environ.get("DB_PATH", "")]
for _d in ROOTS:
    _f = os.path.join(_d, ".env")
    if os.path.exists(_f):
        for _l in open(_f):
            if _l.strip().startswith("DB_PATH="):
                _v = _l.strip().split("=", 1)[1].strip().strip('"\'')
                _cands.append(_v if os.path.isabs(_v) else os.path.join(_d, _v))
_cands += [os.path.join(_d, "toast_data.db") for _d in ROOTS] + ["toast_data.db"]
DB = next((p for p in _cands if _usable(p)), None)
if not DB:
    raise SystemExit("No usable toast DB (non-empty, has 'orders'). Set JARVIS_DB_PATH=/full/path")

# ---- labor model: edit these four numbers, everything else follows ----
COOK_RATE, COOK_HRS = 23.00, 6.0     # <-- unconfirmed, confirm with Mike
TIPPED_RATE, TIPPED_HRS = 6.75, 5.0  # MA service rate, bartender + server
MA_MIN_WAGE = 15.00                  # make-up floor if tips fall short
BURDEN = 0.205   # measured: ER taxes 18.4% + workers comp ~2.1% (JARVIS_KNOWLEDGE 7/22/26)
CONTRIB = 0.67   # 1 - 28% food target - ~3% cards

_cook = COOK_RATE * COOK_HRS
LABOR_PER_DAY = round((_cook + 2 * TIPPED_RATE * TIPPED_HRS) * (1 + BURDEN))
LABOR_MAKEUP  = round((_cook + 2 * MA_MIN_WAGE * TIPPED_HRS) * (1 + BURDEN))
BE_BASE   = round(LABOR_PER_DAY / CONTRIB)
BE_MAKEUP = round(LABOR_MAKEUP / CONTRIB)

# Dennis Port is CLOSED Mon + Tue (Mike, 7/22/26) - zeros there are normal, not gaps
CLOSED = {("dennis", "Mon"), ("dennis", "Tue")}


def daypart(h):
    if h >= 22 or h < 4:
        return "Late"
    return "Lunch" if h < 16 else "Dinner"


conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
agg = defaultdict(lambda: {"checks": 0, "net": 0.0, "days": set()})

for loc, bd, opened, total, tax, tip in conn.execute(
    "SELECT location, business_date, opened_at, total_amount, tax_amount, tip_amount "
    "FROM orders WHERE business_date BETWEEN ? AND ?", (START, END)
):
    try:
        utc = datetime.strptime(str(opened).replace("T", " ")[:19],
                                "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        continue
    date = datetime.strptime(str(bd), "%Y%m%d")
    if date.weekday() > 3:          # Mon=0 .. Thu=3 only
        continue
    k = (loc, date.strftime("%a"), daypart(utc.astimezone(EASTERN).hour))
    a = agg[k]
    a["checks"] += 1
    a["net"] += float(total or 0) - float(tax or 0) - float(tip or 0)
    a["days"].add(bd)

# Calendar Mon-Thu days in range, for an honest denominator
cal = defaultdict(int)
d = datetime.strptime(START, "%Y%m%d")
end = datetime.strptime(END, "%Y%m%d")
while d <= end:
    if d.weekday() <= 3:
        cal[d.strftime("%a")] += 1
    d += timedelta(days=1)

order = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3}
print(f"{'loc':8s} {'dow':4s} {'part':7s} {'ran':>7s} {'checks':>7s} "
      f"{'net':>9s} {'per day':>9s} {'verdict':>10s}")
print("-" * 70)
for (loc, dow, part) in sorted(agg, key=lambda k: (k[0], order[k[1]], k[2])):
    a = agg[(loc, dow, part)]
    ran, total_days = len(a["days"]), cal[dow]
    per = a["net"] / ran if ran else 0
    verdict = ""
    if (loc, dow) in CLOSED:
        verdict = "CLOSED"
    elif part == "Lunch":
        if ran <= 1:
            verdict = "NO SAMPLE"
        elif per >= BE_MAKEUP:
            verdict = f"+${per * CONTRIB - LABOR_PER_DAY:,.0f}/day"
        elif per >= BE_BASE:
            verdict = "MARGINAL"
        else:
            verdict = "LOSES"
    print(f"{loc:8s} {dow:4s} {part:7s} {ran:>3d}/{total_days:<3d} {a['checks']:>7d} "
          f"${a['net']:>8,.0f} ${per:>8,.0f} {verdict:>10s}")

print(f"\nLabor/day: ${LABOR_PER_DAY} (${LABOR_MAKEUP} if tips need make-up), burden {BURDEN:.1%}")
print(f"Lunch break-even: ${BE_BASE}/day (no make-up), ${BE_MAKEUP}/day (with make-up)")
print(f"Cook assumed ${COOK_RATE:.2f}/hr x {COOK_HRS}h - CONFIRM, every verdict keys off it.")
print("'ran' = days that daypart actually had orders vs calendar days available.")
