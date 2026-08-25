#!/usr/bin/env python3
"""Regenerate tests/fixtures/pl_snapshot.json from the live database.

RUN THIS DELIBERATELY, AND ONLY WHEN THE ENGINE CHANGED.

The snapshot is where exact-value assertions live. The live database is not a
fixture: every time Mike codes a bank row in the register, labor and operating
expense move, and that is the system working rather than a regression. Pinning
live figures made the suite fail on ordinary bookkeeping.

So the split is:

  snapshot tests  — exact figures, frozen. They prove the ENGINE computes the
                    same answer from the same input.
  live-DB tests   — invariants only. They prove the BOOKS still hold: entries
                    balance, clearing accounts stay out of revenue, book minus
                    bank equals outstanding, no settlement sits on a P&L
                    account, and every drill sums to its line.

If a snapshot test goes red, the engine changed. Work out whether that change
was intended BEFORE regenerating — running this script to make a red suite
green throws away the only record of what the engine used to produce.

    venv/bin/python tests/fixtures/regenerate_pl_snapshot.py
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from integrations.toast.data_store import get_connection  # noqa: E402
from reports.profit_loss import build_profit_loss, drill  # noqa: E402

PERIODS = [
    ("dennis", "2026-03-01", "2026-03-31"),
    ("chatham", "2026-03-01", "2026-03-31"),
]

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pl_snapshot.json")


def main():
    conn = get_connection()
    snap = {"_meta": {
        "frozen_on": date.today().isoformat(),
        "why": ("Exact-value assertions live here, NOT against the live DB. Live "
                "figures move every time a bank row is coded in the register, "
                "which is the system working, not a regression. Regenerate "
                "deliberately with tests/fixtures/regenerate_pl_snapshot.py when "
                "the ENGINE changes, never to make a red suite green."),
        "periods": [],
    }}
    for loc, start, end in PERIODS:
        pl = build_profit_loss(loc, start, end, conn=conn)
        key = f"{loc}_{start[:7]}"
        snap["_meta"]["periods"].append(key)
        snap[key] = {"pl": pl, "drills": {}}

        def add(d):
            snap[key]["drills"][f"{d['source']}:{d['key']}"] = \
                drill(conn, loc, start, end, d["source"], d["key"])

        for line in pl["revenue"]["lines"]:
            add(line["drill"])
        for line in pl["cogs"]["lines"]:
            add(line["drill"])
        for line in pl["operating_expenses"]["invoiced"]:
            add(line["drill"])
        for line in pl["operating_expenses"]["banked"]:
            add(line["drill"])
        for acct in pl["labor"].get("accounts", []):
            add(acct["drill"])
        if pl["labor"].get("tip_channel_rows_on_labor"):
            add(pl["labor"]["tip_drill"])

    with open(OUT, "w") as fh:
        json.dump(snap, fh, indent=1, sort_keys=True)
    conn.close()
    print(f"wrote {OUT}")
    for key in snap["_meta"]["periods"]:
        pl = snap[key]["pl"]
        print(f"  {key}: revenue {pl['revenue']['net_revenue']:,.2f}  "
              f"food cost {pl['cogs']['food_cost_pct']}%  "
              f"{len(snap[key]['drills'])} drills")


if __name__ == "__main__":
    main()
