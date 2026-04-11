from data_store import get_connection
from datetime import datetime

c = get_connection()

for bd in ['20260204','20260205','20260206','20260207','20260208']:
    rows = c.execute("""
        SELECT opened_at, total_amount-tax_amount-tip_amount
        FROM orders 
        WHERE location='chatham' AND business_date=?
        ORDER BY opened_at
    """, (bd,)).fetchall()
    
    lunch = 0
    dinner = 0
    late = 0
    lunch_ct = 0
    dinner_ct = 0
    for r in rows:
        utc = r[0][:19]
        dt = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%S")
        eastern_hour = (dt.hour - 5) % 24
        rev = r[1] or 0
        if eastern_hour >= 22 or eastern_hour < 4:
            late += rev
        elif eastern_hour < 16:
            lunch += rev
            lunch_ct += 1
        else:
            dinner += rev
            dinner_ct += 1
    
    dt_obj = datetime.strptime(bd, "%Y%m%d")
    day = dt_obj.strftime("%a")
    print(f"{day} {bd}: Lunch ${lunch:,.0f} ({lunch_ct}) | Dinner ${dinner:,.0f} ({dinner_ct}) | Late ${late:,.0f}")
