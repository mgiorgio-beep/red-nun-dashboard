s = open('analytics.py', 'r', encoding='utf-8').read()

# Find the CASE statement for dayparts
old_case_start = s.find("CASE\n                WHEN CAST(strftime('%H'")
if old_case_start < 0:
    # Try alternate spacing
    old_case_start = s.find("CASE\n")
    while old_case_start >= 0 and "daypart" not in s[old_case_start:old_case_start+200]:
        old_case_start = s.find("CASE\n", old_case_start + 1)

if old_case_start < 0:
    print("ERROR: Could not find daypart CASE statement")
    exit()

old_case_end = s.find("END as daypart", old_case_start)
if old_case_end < 0:
    print("ERROR: Could not find END as daypart")
    exit()

old_case = s[old_case_start:old_case_end + len("END as daypart")]
print("Found daypart CASE:", len(old_case), "chars")

new_case = """CASE
                WHEN CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) >= 22
                     OR CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) < 4
                    THEN 'Late Night'
                WHEN CAST(strftime('%H', datetime(substr(opened_at,1,23), '-5 hours')) AS INTEGER) < 16
                    THEN 'Lunch'
                ELSE 'Dinner'
            END as daypart"""

s = s[:old_case_start] + new_case + s[old_case_end + len("END as daypart"):]
open('analytics.py', 'w', encoding='utf-8').write(s)
print('DONE - Dayparts: Lunch <4pm, Dinner 4-10pm, Late Night 10pm-1am')
