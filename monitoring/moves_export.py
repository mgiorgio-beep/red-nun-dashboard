#!/usr/bin/env python3
"""
Nightly: transfers / waste / intercompany for the Jarvis briefs (brief 8D, 8F).

The Friday open-items and Monday briefs are Cowork tasks that read the nightly
export folder, not the dashboard. This writes moves_status.json there at 3:25 so
jarvis_export's 3:30 Drive copy carries it:

  open_problems      out-of-balance, failed emails, needs-link, unsettled months,
                     returns open > 14 days, deposits not cleared after 10 days
  open_items         who owes whom, per product
  waste_last_week    per house: loss, staff meal, by reason, top 3 items
  transfers_last_week per house: $ in / $ out
"""
import json
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, '.env'))

from integrations.toast.data_store import get_connection  # noqa: E402
from reports import intercompany as IC  # noqa: E402
from reports import move_numbers as MN  # noqa: E402

OUT = os.environ.get('JARVIS_EXPORT_DIR', '/home/rednun/jarvis_exports')


def main():
    today = date.today()
    end = (today - timedelta(days=today.weekday() + 1))            # last Sunday
    start = end - timedelta(days=6)
    conn = get_connection()
    try:
        items = IC.open_items(conn)
        data = {
            'generated': today.isoformat(),
            'open_problems': IC.open_problems(conn),
            'open_balance': IC.owes_sentence(round(sum(i['cost'] for i in items), 2)),
            'open_items': [{'item': i['name'], 'owed_by': i['owed_by'], 'qty': i['abs_qty'], 'unit': i['base_unit'],
                            'cost': i['abs_cost'], 'suggested': i['suggested'], 'last_moved': i['last_date']} for i in items],
            'week': [start.isoformat(), end.isoformat()],
            'waste_last_week': {loc: dict(MN.waste(conn, loc, start.isoformat(), end.isoformat()),
                                          top_items=MN.top_waste_items(conn, loc, start.isoformat(), end.isoformat()))
                                for loc in ('chatham', 'dennis')},
            'transfers_last_week': {loc: MN.transfers(conn, loc, start.isoformat(), end.isoformat())
                                    for loc in ('chatham', 'dennis')},
        }
    finally:
        conn.close()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'moves_status.json')
    with open(path + '.tmp', 'w') as f:
        json.dump(data, f, indent=1, default=str)
    os.replace(path + '.tmp', path)
    print(f"wrote {path}: {len(data['open_problems'])} problem(s), {len(data['open_items'])} open item(s)")


if __name__ == '__main__':
    main()
