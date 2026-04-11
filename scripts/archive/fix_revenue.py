"""
Fix net_revenue calculation in analytics.py
Run: python fix_revenue.py
"""

import re

with open("analytics.py", "r") as f:
    content = f.read()

# Fix get_daily_revenue: net_revenue should be total - tax - tips
old = "SUM(total_amount) as net_revenue"
new = "SUM(total_amount - tax_amount - tip_amount) as net_revenue"

if old in content:
    content = content.replace(old, new)
    with open("analytics.py", "w") as f:
        f.write(content)
    print("FIXED: net_revenue now = total_amount - tax_amount - tip_amount")
    print("  This matches MarginEdge's 'Net Sales' definition.")
else:
    if new in content:
        print("Already fixed!")
    else:
        print("Could not find the expected SQL pattern. Manual fix needed.")
        print("In analytics.py, find 'SUM(total_amount) as net_revenue'")
        print("Replace with: SUM(total_amount - tax_amount - tip_amount) as net_revenue")
