s = open('analytics.py', 'r', encoding='utf-8').read()

old = '''    total_revenue = rev.get("total_revenue") or 0
    total_labor = labor.get("total_labor_cost") or 0
    labor["total_revenue"] = total_revenue
    labor["labor_pct"] = round(
        (total_labor / total_revenue * 100) if total_revenue > 0 else 0, 1
    )
    conn.close()
    return labor'''

new = '''    total_revenue = rev.get("total_revenue") or 0
    total_labor = labor.get("total_labor_cost") or 0

    # Add salaried employees (not tracked in 7shifts time entries)
    SALARIES = {
        "dennis": 880.0 / 7,   # $880/week = $125.71/day
        "chatham": 1375.0 / 7, # $1375/week = $196.43/day
    }
    # Count business days with orders in the range to determine salary days
    day_query = f"""
        SELECT location, COUNT(DISTINCT business_date) as days
        FROM orders
        {where_sql}
        GROUP BY location
    """
    day_rows = conn.execute(day_query, params).fetchall()
    salary_cost = 0
    for row in day_rows:
        loc = row["location"] if isinstance(row, dict) else row[0]
        days = row["days"] if isinstance(row, dict) else row[1]
        daily_rate = SALARIES.get(loc, 0)
        salary_cost += daily_rate * days
    total_labor += salary_cost
    labor["total_labor_cost"] = round(total_labor, 2)
    labor["salary_cost"] = round(salary_cost, 2)

    labor["total_revenue"] = total_revenue
    labor["labor_pct"] = round(
        (total_labor / total_revenue * 100) if total_revenue > 0 else 0, 1
    )
    conn.close()
    return labor'''

if old in s:
    s = s.replace(old, new)
    open('analytics.py', 'w', encoding='utf-8').write(s)
    print('DONE - Salary added: Dennis $880/wk, Chatham $1375/wk')
else:
    print('ERROR - pattern not found')
