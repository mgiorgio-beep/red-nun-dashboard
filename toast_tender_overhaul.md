# Toast Tender Overhaul — Dennis Port + Chatham

Complete migration to replace MarginEdge's sales journal with a direct Toast → Dashboard → QBO pipeline. Captures house accounts per customer, all alternate payment types (DoorDash, Walkout, Barter, Trivia Prizes, etc.), gift card sales + redemptions, and credit card tenders.

Based on confirmed Toast payload structure from live data in `/var/lib/rednun/toast_data.db` (pulled 04/17/2026).

---

## Key numbers the tender split needs to handle

**Dennis Port (historical volume):**
- DoorDash: $89,324 / 1,686 payments (HUGE — must split out)
- House accounts: $5,532 / 50 payments across 7 customers
- Other alt tenders (Walkout, Trvia Prizes, Bar Comps, Barter, GC Donation, etc.): ~$5,500
- Gift card redemptions: $15,541 / 412 payments
- Gift card sales: ~$9,890 (225 units at Dennis)

**Chatham (historical volume):**
- House accounts: $606 / 14 payments (single generic account)
- Alt tenders (Walk out, Barter, Square, GC Donation, etc.): ~$4,889
- Gift card redemptions: $7,964 / 195 payments
- Gift card sales: ~$16,010 (238 units at Chatham — higher than Dennis)
- No DoorDash

---

## 1. Schema Migration

Run once against `/var/lib/rednun/toast_data.db`:

```sql
-- Add lookup columns to existing payments table
ALTER TABLE payments ADD COLUMN house_account_guid TEXT;
ALTER TABLE payments ADD COLUMN alt_payment_guid TEXT;
ALTER TABLE payments ADD COLUMN alt_payment_name TEXT;

CREATE INDEX IF NOT EXISTS idx_payments_house_account
    ON payments(house_account_guid);
CREATE INDEX IF NOT EXISTS idx_payments_alt_payment
    ON payments(alt_payment_guid);

-- House account lookup (GUID → customer name + QBO mapping)
CREATE TABLE IF NOT EXISTS house_accounts (
    guid TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    qbo_customer_id TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Alternate payment type lookup (GUID → DoorDash/Walkout/etc.)
CREATE TABLE IF NOT EXISTS alt_payment_types (
    guid TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    name TEXT NOT NULL,
    qbo_account TEXT,
    je_category TEXT,
    synced_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

Gift card menu items are identified by `item_name` match in `order_items` — no separate table needed. Names at both locations: `Gift Card`, `E-Gift Card`.

---

## 2. Seed Data — Dennis Port

```sql
-- House accounts (7 customers, names cross-referenced from ME)
INSERT OR REPLACE INTO house_accounts (guid, location, customer_name) VALUES
('e17e8749-0e6e-451c-8cb2-f65fa1689ddd', 'dennis', 'Michael Silvester HA'),
('39891d96-bac8-4462-9b05-2c54862746db', 'dennis', 'Richard Farley HA'),
('02706c72-9d80-46fd-a239-94c0402df6cc', 'dennis', 'Michael Giorgio HA'),
('31e9819e-130e-49cb-bea5-921306733820', 'dennis', 'House Account (unassigned)'),
('c25cfbe1-a356-4f7b-9013-f3c807415b8c', 'dennis', 'Peter Giorgio HA'),
('8fceb531-d683-403b-8e8f-632004aa6daa', 'dennis', 'Francine Osenton HA'),
('9283834a-edee-4ed1-bf61-9545396b56e7', 'dennis', 'Pam Larrabee HA');

-- Alt payment types — Dennis (10 types)
INSERT OR REPLACE INTO alt_payment_types (guid, location, name, qbo_account, je_category) VALUES
('938467c1-adaf-423e-a5a6-ce4864f7d9db', 'dennis', 'DoorDash',                 'DoorDash Clearing',       'clearing'),
('4d91f154-ba83-42d3-b81f-2bdc352e4c78', 'dennis', 'Trvia Prizes',             'Marketing - Comps',       'expense'),
('ac930a19-7e91-4165-8da5-b037e8a629ec', 'dennis', 'Walkout',                  'Walkouts',                'expense'),
('385f131f-e51b-48d4-bb99-d25ae56f3f52', 'dennis', 'GC Donation',              'Charitable Contributions','expense'),
('322cacac-1e5e-47d5-aa97-5fc374b6c559', 'dennis', 'Toast Cash Stored Value',  'Gift Certificates',       'liability'),
('2ce72c34-b110-4da2-93e6-2c5168ce3f04', 'dennis', 'Toast Cash',               'Cash',                    'cash'),
('e50e1bc2-01d9-40de-b14c-8ebf6d9b6890', 'dennis', 'House Account Adjustment', 'Accounts Receivable',     'clearing'),
('4dfe3e3c-fe5b-4498-8257-4be66d81dbd4', 'dennis', 'Barter',                   'Repairs & Maintenance',   'expense'),
('a40e2fa3-03e6-4972-b4de-29da8e0e4805', 'dennis', 'Square',                   'Square Clearing',         'clearing'),
('8f342f70-8b55-4547-b9b8-27c2b0227903', 'dennis', 'Bar Comps',                'Marketing - Comps',       'expense');
```

---

## 3. Seed Data — Chatham

```sql
-- House accounts (single generic at Chatham)
INSERT OR REPLACE INTO house_accounts (guid, location, customer_name) VALUES
('b7f8bc03-9c3f-4c84-97d3-b60aa975ff65', 'chatham', 'House Account');

-- Alt payment types — Chatham (7 types, no DoorDash)
INSERT OR REPLACE INTO alt_payment_types (guid, location, name, qbo_account, je_category) VALUES
('1558822a-9544-43c0-97a3-e4f003344fc8', 'chatham', 'Check',                   'Undeposited Funds',       'cash'),
('3049e8fc-bab1-4ed5-a494-275ea3633fbe', 'chatham', 'Barter',                  'Repairs & Maintenance',   'expense'),
('3937c5a7-9396-4239-8548-75a48face91c', 'chatham', 'Toast Cash Stored Value', 'Gift Certificates',       'liability'),
('3e67ddb1-9c95-45f5-aebe-47a3e5d4a7ea', 'chatham', 'Walk out',                'Walkouts',                'expense'),
('47976048-f61c-42d9-bc3b-86949027dece', 'chatham', 'Square',                  'Square Clearing',         'clearing'),
('b56b3767-a674-47f8-9969-01ac8ffef543', 'chatham', 'Toast Cash',              'Cash',                    'cash'),
('e9d39975-b988-4c7a-a1e4-92dbf2f4c4e2', 'chatham', 'GC Donation',             'Charitable Contributions','expense');
```

**Note:** Chatham uses "Walk out" (with space), Dennis uses "Walkout" (no space). Match Toast config exactly — don't normalize.

---

## 4. Parser Updates — `integrations/toast/data_store.py`

In `store_orders()`, inside the payments loop:

```python
# Existing dedup + status checks unchanged...

# Extract house account + alternate payment refs
ha_ref = payment.get("houseAccount") or {}
ha_guid = ha_ref.get("guid") if isinstance(ha_ref, dict) else None

alt_ref = payment.get("otherPayment") or {}
alt_guid = alt_ref.get("guid") if isinstance(alt_ref, dict) else None

payments_to_insert.append((
    pay_guid, guid, location, business_date,
    payment.get("type", ""),
    payment.get("cardType", ""),
    payment.get("amount", 0) or 0,
    payment.get("tipAmount", 0) or 0,
    payment.get("refundAmount", 0) or 0,
    ha_guid,          # NEW
    alt_guid,         # NEW
    None,             # alt_payment_name — joined at query time
))
```

Update the INSERT to 12 columns and extend the `pay_data_fixed` tuple rebuild in the business_date adjustment block to preserve positions 9, 10, 11.

---

## 5. Sync Alt Payment Types — `integrations/toast/toast_client.py`

```python
def get_alternate_payment_types(self, location="dennis"):
    """Get custom tender types (DoorDash, Walkout, Barter, etc.)"""
    guid = self.restaurants[location]
    return self._get("/config/v2/alternatePaymentTypes", guid)
```

Add `sync_alt_payment_types(location)` to `sync.py` — call daily so new Toast tender types flow through. On upsert, preserve `qbo_account` and `je_category` (they're manually set once and shouldn't be overwritten).

---

## 6. Backfill Script (one-time, no API hits)

```python
# backfill_payment_refs.py
import sqlite3, json

DB = '/var/lib/rednun/toast_data.db'
conn = sqlite3.connect(DB)
cur = conn.cursor()

rows = cur.execute("SELECT guid, location, raw_json FROM orders").fetchall()
updated = 0

for order_guid, location, raw_json in rows:
    try:
        o = json.loads(raw_json)
    except Exception:
        continue
    for check in o.get('checks', []) or []:
        for pay in check.get('payments', []) or []:
            pay_guid = pay.get('guid')
            if not pay_guid:
                continue
            ha = pay.get('houseAccount') or {}
            ha_guid = ha.get('guid') if isinstance(ha, dict) else None
            alt = pay.get('otherPayment') or {}
            alt_guid = alt.get('guid') if isinstance(alt, dict) else None
            cur.execute("""
                UPDATE payments 
                SET house_account_guid = ?, alt_payment_guid = ?
                WHERE guid = ?
            """, (ha_guid, alt_guid, pay_guid))
            updated += cur.rowcount

conn.commit()
conn.close()
print(f"Updated {updated} payment rows")
```

---

## 7. Sales Journal — Query Logic

### 7a. Tender lines (debits)

```sql
SELECT 
    CASE
        WHEN p.payment_type = 'HOUSE_ACCOUNT' 
            THEN 'Tender: ' || COALESCE(ha.customer_name, 'House Account ' || substr(p.house_account_guid,1,8))
        WHEN p.payment_type = 'OTHER' 
            THEN 'Tender: ' || COALESCE(apt.name, 'Other - Unknown')
        WHEN p.payment_type = 'GIFTCARD' 
            THEN 'Tender: Gift Card'
        WHEN p.payment_type = 'CREDIT' 
            THEN 'Tender: ' || COALESCE(p.card_type, 'Credit')
        WHEN p.payment_type = 'CASH' 
            THEN 'Tender: Cash'
        ELSE 'Tender: ' || p.payment_type
    END AS journal_name,
    CASE
        WHEN p.payment_type = 'HOUSE_ACCOUNT' THEN 'Accounts Receivable'
        WHEN p.payment_type = 'OTHER' THEN COALESCE(apt.qbo_account, 'Other Clearing')
        WHEN p.payment_type = 'GIFTCARD' THEN 'Gift Certificates'
        WHEN p.payment_type = 'CREDIT' THEN 'Credit Card Clearing'
        WHEN p.payment_type = 'CASH' THEN 'Cash'
    END AS qbo_account,
    ROUND(SUM(p.amount + p.tip_amount), 2) AS debit_amount
FROM payments p
LEFT JOIN house_accounts ha ON p.house_account_guid = ha.guid
LEFT JOIN alt_payment_types apt ON p.alt_payment_guid = apt.guid
WHERE p.location = :location
  AND p.business_date = :business_date
GROUP BY journal_name, qbo_account
HAVING debit_amount <> 0
ORDER BY debit_amount DESC;
```

### 7b. Revenue lines (credits) — gift card sales split out

```sql
-- Gift cards sold (credit Gift Certificates liability)
SELECT 
    'GC: Gross' AS journal_name,
    'Gift Certificates' AS qbo_account,
    ROUND(SUM(price * quantity), 2) AS credit_amount
FROM order_items
WHERE location = :location
  AND business_date = :business_date
  AND voided = 0
  AND (item_name = 'Gift Card' OR item_name = 'E-Gift Card')

UNION ALL

-- Regular revenue by sales category (gift cards excluded)
SELECT 
    'Gross Sales: ' || cm.pour_category AS journal_name,
    CASE cm.pour_category
        WHEN 'food' THEN 'Food Sales'
        WHEN 'draft_beer' THEN 'Beer Sales'
        WHEN 'bottled_beer' THEN 'Beer Sales'
        WHEN 'wine' THEN 'Wine Sales'
        WHEN 'well_liquor' THEN 'Liquor Sales'
        WHEN 'premium_liquor' THEN 'Liquor Sales'
        WHEN 'non_alcoholic' THEN 'NA Beverage Sales'
        ELSE 'Food Sales'
    END AS qbo_account,
    ROUND(SUM(oi.price * oi.quantity - oi.discount), 2) AS credit_amount
FROM order_items oi
LEFT JOIN menu_items mi ON oi.item_guid = mi.guid
LEFT JOIN category_map cm ON mi.menu_group_name = cm.menu_group_name
WHERE oi.location = :location
  AND oi.business_date = :business_date
  AND oi.voided = 0
  AND oi.item_name NOT IN ('Gift Card', 'E-Gift Card')
GROUP BY cm.pour_category;
```

### 7c. Standard lines (tax, tips, discounts)

```sql
-- Tax (credit Sales Tax Payable)
SELECT ROUND(SUM(tax_amount), 2) FROM orders
WHERE location = :location AND business_date = :business_date;

-- Tips (credit Tips Payable — liability, NOT income)
SELECT ROUND(SUM(tip_amount), 2) FROM payments
WHERE location = :location AND business_date = :business_date;

-- Discounts (single bucket, debit Discounts/Refunds Given)
SELECT ROUND(SUM(discount_amount), 2) FROM orders
WHERE location = :location AND business_date = :business_date;
```

---

## 8. Validation — Does It Balance?

For any given day, `SUM(debits) = SUM(credits)`.

**Debits:** All tender lines + Discount Total
**Credits:** All revenue categories + Tax + Tips + GC: Gross

**Test days:**
1. 04/16/2026 Chatham — small day, minimal complexity (should total $2,102.69 matching Toast)
2. 04/16/2026 Dennis Port — must include DoorDash split
3. Pick a day with a house account tab at Dennis
4. Pick a day with gift card sales at Chatham

Out of balance by ≥ $0.01 → check:
- Tip dedup across multiple checks (same payment GUID)
- Voided checks/orders included incorrectly
- Gift card line items leaking into Gross Sales
- Alt payment with unresolved GUID (add to `alt_payment_types`)

---

## 9. Rollout Order

1. Run Section 1 migration against live DB (non-destructive, adds columns/tables)
2. Run Section 2 + 3 seed data (Dennis + Chatham)
3. Update `data_store.py` parser (Section 4)
4. Update `toast_client.py` + `sync.py` (Section 5)
5. Run Section 6 backfill against ~14 months of historical `raw_json`
6. Wire Section 7 queries into dashboard Sales Journal builder
7. Test 04/16/2026 Chatham → confirm $2,102.69 total still matches
8. Test 04/16/2026 Dennis → confirm journal balances + alt payment lines appear
9. Parallel-run dashboard vs manual QBO entry for 1 week
10. Cut over, deprecate ME references

---

## 10. Things to Watch

- **Chatham uses "Walk out" (space), Dennis uses "Walkout" (no space)** — both seeded. Don't normalize.
- **"Trvia Prizes" is misspelled in Dennis Toast config** — leave it. If fixed in Toast, update seed same day.
- **DoorDash at Dennis = $89K/yr** — biggest single split. If it doesn't separate cleanly, CC deposit reconciliation is broken every day.
- **New house accounts** — when an unrecognized GUID appears in `payments`, journal falls back to "House Account [guid-prefix]". Daily check: `SELECT DISTINCT house_account_guid FROM payments WHERE house_account_guid IS NOT NULL AND house_account_guid NOT IN (SELECT guid FROM house_accounts);`
- **New alt payment types** — same problem. Daily check: `SELECT DISTINCT alt_payment_guid FROM payments WHERE alt_payment_guid IS NOT NULL AND alt_payment_guid NOT IN (SELECT guid FROM alt_payment_types);`
- **Gift card item names are hardcoded** ('Gift Card', 'E-Gift Card') — if someone renames in Toast, query fails silently. Add a test that alerts if gift card sales drop to zero unexpectedly.
- **DB cleanup** — after this is working, delete the 5 empty stub `toast_data.db` files scattered around `/opt/red-nun-dashboard/*` and `/home/rednun/`. Only `/var/lib/rednun/toast_data.db` should exist.
