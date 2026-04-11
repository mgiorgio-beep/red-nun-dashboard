# Net Sales Calculation Investigation

## Current Status (Feb 15, 2026)
Chatham Feb 13 comparison:
- Our formula: `total_amount - tax_amount - tip_amount` = **$4,241.99**
- Toast reports: **$4,061.00**
- **Difference: $180.99**

With discounts subtracted: $4,160.32 (closer, off by $99)

## Root Cause
Toast's raw JSON has a check-level `amount` field that represents net sales BEFORE tax/tip are added. We're calculating backwards from `total_amount`, which may introduce rounding errors or miss edge cases.

## Fix Required
Extract and store the check-level `amount` field from Toast API response (inside checks[] array). This is the authoritative net sales figure.

## Workaround
Current formula is close enough for analytics. $181 variance on $4k = 4.5% error.
