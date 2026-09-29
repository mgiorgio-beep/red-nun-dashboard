"""Seed the weekly key-item count list (count_templates) for both houses.

Top 25 food + top 10 beer + top 8 liquor/wine by confirmed purchase $ over the
last 90 days. Only adds (INSERT OR IGNORE); safe to re-run. After the first
seed, Mike edits the list at /count/list.

    venv/bin/python3 scripts/seed_weekly_count_list.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations.toast.data_store import get_connection
from reports.key_items import seed_weekly, weekly_product_ids, LOCATIONS

conn = get_connection()
for loc in LOCATIONS:
    n = seed_weekly(conn, loc)
    print(f"{loc}: added {n}, list now {len(weekly_product_ids(conn, loc))}")
conn.commit()
conn.close()
