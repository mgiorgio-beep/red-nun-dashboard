from data_store import get_connection

c = get_connection()

# Check what the SQL CASE statement actually produces
rows = c.execute("""
    SELECT business_date,
        CASE
            WHEN CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) >= 22
                 OR CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) < 4
                THEN 'Late Night'
            WHEN CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) < 16
                THEN 'Lunch'
            ELSE 'Dinner'
        END as daypart,
        COUNT(*) as orders,
        ROUND(SUM(total_amount - tax_amount - tip_amount), 0) as revenue
    FROM orders
    WHERE location='chatham' AND business_date BETWEEN '20260204' AND '20260208'
    GROUP BY business_date, daypart
    ORDER BY business_date, daypart
""").fetchall()

for r in rows:
    print(f"{r[0]}  {r[1]:12s}  {r[2]:3d} orders  ${r[3]:,.0f}")
