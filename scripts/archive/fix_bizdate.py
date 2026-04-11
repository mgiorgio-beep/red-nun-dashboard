s = open('data_store.py', 'r').read()

old = '''            cursor.execute("""
                INSERT OR REPLACE INTO orders
                (guid, location, business_date, opened_at, closed_at,'''

new = '''            # Fix late-night orders: if opened before 5AM ET (10AM UTC), assign to previous day
            opened_str = order.get("openedDate", "")
            if opened_str and business_date:
                hour_utc = int(opened_str[11:13]) if len(opened_str) > 13 else 99
                if hour_utc < 10:  # Before 5AM Eastern
                    from datetime import datetime as dt2, timedelta as td2
                    try:
                        prev = dt2.strptime(business_date, "%Y%m%d") - td2(days=1)
                        business_date = prev.strftime("%Y%m%d")
                    except:
                        pass
            cursor.execute("""
                INSERT OR REPLACE INTO orders
                (guid, location, business_date, opened_at, closed_at,'''

s = s.replace(old, new)
open('data_store.py', 'w').write(s)
print("DONE" if "Fix late-night" in s else "FAILED")
