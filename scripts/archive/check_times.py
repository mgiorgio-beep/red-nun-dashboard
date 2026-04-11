from data_store import get_connection
from datetime import datetime

c = get_connection()
rows = c.execute("""
    SELECT opened_at, round(total_amount-tax_amount-tip_amount,0) 
    FROM orders 
    WHERE location='chatham' AND business_date='20260205' 
    ORDER BY opened_at LIMIT 10
""").fetchall()

for r in rows:
    utc = r[0][:19]
    # Manual UTC to Eastern (-5)
    dt = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%S")
    eastern_hour = (dt.hour - 5) % 24
    print(f"{eastern_hour}:{dt.minute:02d}  ${r[1]}")
