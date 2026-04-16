# Net Sales Calculation

## Formula (Fixed Apr 16, 2026)
**Toast Net Sales = SUM of `check.amount` for non-voided, non-deleted checks.**

`check.amount` is Toast's authoritative net sales figure: gross sales minus discounts
and refunds, excluding tax and tip. This matches the "Net Sales" line on Toast's
Sales Summary screen exactly.

Stored in `orders.net_amount`. All reports (`morning_report`, `analytics`, `forecast`)
now use `SUM(net_amount)` instead of the old `total_amount - tax_amount - tip_amount`.

## Previous Issue
The old formula `total_amount - tax_amount - tip_amount` overcounted because it
included voided check amounts still present in the order-level totals.

- Dennis 4/15: old formula = $5,390.49, Toast = $5,333.94, difference = $56.55
- Chatham 2/13: old formula = $4,241.99, Toast = $4,061.00, difference = $180.99

## Backfill
All 66,491 existing orders backfilled from `raw_json` on 2026-04-16.
New orders populated correctly via `data_store.store_orders()`.
