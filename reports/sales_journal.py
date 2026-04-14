"""
Sales Journal — Daily Sales Journal module
Generates daily journal entries from Toast data for QuickBooks upload.
"""

import os
import csv
import io
import smtplib
import logging
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

from integrations.toast.data_store import get_connection

load_dotenv()
logger = logging.getLogger(__name__)

LOCATION_NAMES = {"dennis": "Dennis Port", "chatham": "Chatham"}
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "mgiorgio@rednun.com")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://dashboard.rednun.com")

# Same keyword lists as analytics.py for consistent categorization
BEER_KW = ['lager','ipa','ale','stout','pilsner','kolsch','hef','guinness',
           'blue moon','sam adams','bud light','coors','corona','stella',
           'heineken','devils purse','harpoon','cisco','night shift',
           'cape cod beer','draft','seltzer','truly','white claw','bucket']
WINE_KW = ['wine','cab','merlot','pinot','chardonnay','chard','sauvignon',
           'riesling','prosecco','rose','rosé','kim crawford','meiomi',
           'josh','kendall','barefoot','decoy','la crema','glass of','bottle of']
LIQUOR_KW = ['margarita','martini','cocktail','mojito','old fashioned',
             'manhattan','negroni','daiquiri','cosmopolitan','gimlet','sour',
             'mule','spritz','bloody mary','espresso martini','rum','vodka',
             'whiskey','bourbon','tequila','gin','shot','on the rocks',
             'neat','mixed drink']
NA_KW = ['soda','coffee','tea','juice','water','lemonade','sprite','coke',
         'pepsi','ginger ale','tonic','red bull','arnold palmer',
         'shirley temple','mocktail','n/a','non-alc']


# ---------------------------------------------------------------------------
# DB Init
# ---------------------------------------------------------------------------

def init_sales_journal_tables():
    """Create all tables needed for the sales journal module."""
    conn = get_connection()
    # Ensure sales_categories table exists for GUID → label mapping
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_categories (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            guid     TEXT NOT NULL,
            label    TEXT NOT NULL,
            UNIQUE(location, guid)
        )
    """)
    conn.commit()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS qb_journal_entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type      TEXT NOT NULL DEFAULT 'sales_journal',
            location        TEXT NOT NULL,
            entry_date      TEXT NOT NULL,
            je_name         TEXT NOT NULL,
            total_debits    REAL DEFAULT 0,
            total_credits   REAL DEFAULT 0,
            balanced        INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'pending',
            last_sync_attempt TEXT,
            qbo_txn_id      TEXT,
            qbo_error       TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(location, entry_date, entry_type)
        );

        CREATE TABLE IF NOT EXISTS qb_journal_line_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id        INTEGER NOT NULL,
            journal_name    TEXT NOT NULL,
            qbo_account     TEXT,
            memo            TEXT DEFAULT '',
            debit           REAL,
            credit          REAL,
            mapped          INTEGER DEFAULT 0,
            sort_order      INTEGER DEFAULT 0,
            FOREIGN KEY(entry_id) REFERENCES qb_journal_entries(id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS qb_accounts (
            id                  TEXT PRIMARY KEY,
            name                TEXT NOT NULL,
            fully_qualified_name TEXT,
            account_type        TEXT,
            active              INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS qb_line_mapping (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            location        TEXT NOT NULL,
            journal_name    TEXT NOT NULL,
            qbo_account     TEXT NOT NULL,
            UNIQUE(location, journal_name)
        );

        CREATE TABLE IF NOT EXISTS qb_cron_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at          TEXT NOT NULL,
            location        TEXT,
            entries_created INTEGER DEFAULT 0,
            entries_failed  INTEGER DEFAULT 0,
            status          TEXT,
            error_msg       TEXT
        );
    """)
    conn.commit()
    conn.close()
    logger.info("Sales journal tables initialized")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _categorize(item_name: str) -> str:
    """Classify item into Beer/Wine/Liquor/NA Beverage/Food."""
    name = (item_name or "").lower()
    for w in BEER_KW:
        if w in name: return "Beer"
    for w in WINE_KW:
        if w in name: return "Wine"
    for w in LIQUOR_KW:
        if w in name: return "Liquor"
    for w in NA_KW:
        if w in name: return "NA Beverage"
    return "Food"


def _make_je_name(location: str, entry_date: str) -> str:
    """RNDP04012026 (dennis) or RNCH04012026 (chatham)."""
    d = datetime.strptime(entry_date, "%Y-%m-%d")
    prefix = "RNDP" if location == "dennis" else "RNCH"
    return f"{prefix}{d.strftime('%m%d%Y')}"


def _toast_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to YYYYMMDD for Toast DB queries."""
    return iso_date.replace("-", "")


# ---------------------------------------------------------------------------
# Entry Generation Engine
# ---------------------------------------------------------------------------


def _get_sales_categories(location: str) -> dict:
    """Return {guid: category_label} for known sales category GUIDs.
    Stored in DB via sales_categories table; falls back to keyword matching."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT guid, label FROM sales_categories WHERE location=?", (location,)
        ).fetchall()
        return {r["guid"]: r["label"] for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()




def _sync_sales_categories(location: str, client=None):
    """Fetch sales categories from Toast API and cache in DB. Returns {guid: label}."""
    try:
        if client is None:
            from integrations.toast.toast_client import ToastAPIClient
            client = ToastAPIClient()
        import requests
        token = client._get_token()
        guid = client.restaurants[location]
        headers = {"Authorization": f"Bearer {token}", "Toast-Restaurant-External-ID": guid}
        r = requests.get("https://ws-api.toasttab.com/config/v2/salesCategories",
                         headers=headers, timeout=10)
        if r.status_code == 200:
            cats = r.json()
            conn = get_connection()
            for c in cats:
                conn.execute("""
                    INSERT INTO sales_categories (location, guid, label)
                    VALUES (?, ?, ?)
                    ON CONFLICT(location, guid) DO UPDATE SET label=excluded.label
                """, (location, c["guid"], c["name"]))
            conn.commit()
            conn.close()
            return {c["guid"]: c["name"] for c in cats}
    except Exception as e:
        logger.warning(f"Could not sync sales categories: {e}")
    return {}


def _get_declared_cash_tips(location: str, entry_date: str, client=None) -> float:
    """Get employee-declared cash tips from Toast time entries for a business date."""
    try:
        if client is None:
            from integrations.toast.toast_client import ToastAPIClient
            client = ToastAPIClient()
        import requests
        from datetime import datetime, timedelta
        token = client._get_token()
        guid = client.restaurants[location]
        headers = {"Authorization": f"Bearer {token}", "Toast-Restaurant-External-ID": guid}
        # Business date in ET runs 4am–4am UTC+4 offset
        dt = datetime.strptime(entry_date, "%Y-%m-%d")
        start = (dt).strftime("%Y-%m-%dT04:00:00.000+0000")
        end   = (dt + timedelta(days=1)).strftime("%Y-%m-%dT04:00:00.000+0000")
        r = requests.get("https://ws-api.toasttab.com/labor/v1/timeEntries",
                         headers=headers,
                         params={"startDate": start, "endDate": end},
                         timeout=15)
        if r.status_code == 200:
            entries = r.json()
            return round(sum(float(e.get("declaredCashTips", 0) or 0) for e in entries), 2)
    except Exception as e:
        logger.warning(f"Could not fetch declared cash tips: {e}")
    return 0.0

def build_journal_entry(location: str, entry_date: str) -> dict:
    """
    Build a journal entry for the given location and date from Toast data.
    - Category labels come from salesCategory.guid in raw order JSON
    - Amounts use orders.net_amount proportionally split by category
    - Discount = sum(preDiscountPrice - price) across all items
    - Tenders include tips (payments.amount + tip_amount)
    """
    import json as _json
    conn = get_connection()
    td = _toast_date(entry_date)

    # Load sales category GUID → label mapping (sync from API if empty)
    try:
        sc_rows = conn.execute(
            "SELECT guid, label FROM sales_categories WHERE location=?", (location,)
        ).fetchall()
        sc_map = {r["guid"]: r["label"] for r in sc_rows}
    except Exception:
        sc_map = {}
    if not sc_map:
        sc_map = _sync_sales_categories(location)

    # Parse raw order JSON: get category ratios (using price) and discount (preDisc - price)
    cat_price = {}    # label -> sum of price (line totals, already includes qty)
    cat_predisc = {}  # label -> sum of preDiscountPrice (line totals; > price = discount)
    total_price = 0.0
    total_predisc = 0.0

    order_rows = conn.execute(
        "SELECT raw_json FROM orders WHERE location=? AND business_date=?",
        (location, td)
    ).fetchall()

    for row in order_rows:
        if not row[0]:
            continue
        try:
            data = _json.loads(row[0])
        except Exception:
            continue
        for check in data.get("checks", []):
            for sel in check.get("selections", []):
                if sel.get("voided"):
                    continue
                sc = sel.get("salesCategory") or {}
                guid = sc.get("guid")
                label = sc_map.get(guid) if guid else None
                if not label:
                    name = sel.get("displayName", "")
                    # Gift card sales are a liability, not revenue — keep separate
                    if "gift" in name.lower() and not guid:
                        label = "Gift Card Sold"
                    else:
                        label = _categorize(name)

                p  = float(sel.get("price") or 0)
                pd = float(sel.get("preDiscountPrice") or p)
                qty = float(sel.get("quantity") or 1)

                # price = line total (not per-unit); preDiscountPrice = line total or list price
                # for single-qty discounted items. Neither should be multiplied by qty.
                cat_price[label]  = cat_price.get(label, 0)  + p
                cat_predisc[label] = cat_predisc.get(label, 0) + pd
                total_price   += p
                total_predisc += pd

    # Actual discount = preDiscountPrice - price (from Toast's own data)
    actual_discount = round(total_predisc - total_price, 2)

    # Fallback: use order_items if raw JSON empty
    if not cat_price:
        items = conn.execute("""
            SELECT item_name, SUM(price * quantity) AS revenue
            FROM order_items
            WHERE location = ? AND business_date = ?
              AND voided = 0 AND price > 0
            GROUP BY item_name
        """, (location, td)).fetchall()
        for row in items:
            cat = _categorize(row["item_name"])
            cat_price[cat] = cat_price.get(cat, 0) + (row["revenue"] or 0)
            total_price += (row["revenue"] or 0)
        actual_discount = 0.0

    # Order-level net amount (authoritative: what was actually billed, post-discount)
    ord_row = conn.execute("""
        SELECT SUM(total_amount) - SUM(tax_amount) - SUM(tip_amount) AS net,
               SUM(tax_amount) AS tax
        FROM orders WHERE location = ? AND business_date = ?
    """, (location, td)).fetchone()
    net_total = float(ord_row["net"] or 0) if ord_row else 0
    tax       = float(ord_row["tax"] or 0) if ord_row else 0

    # Allocate net_total proportionally by category using price ratios
    net_by_cat = {}
    if total_price > 0:
        for cat, p in cat_price.items():
            net_by_cat[cat] = net_total * p / total_price

    # Rounding correction: assign remainder to largest category
    if net_by_cat:
        allocated = sum(net_by_cat.values())
        remainder = round(net_total - allocated, 2)
        largest = max(net_by_cat, key=net_by_cat.get)
        net_by_cat[largest] = net_by_cat[largest] + remainder

    # Gross-up: add per-category discount (preDiscountPrice - price) to credits
    # This gives us GROSS sales per category (like ME), with discount posted as separate debit
    if actual_discount > 0:
        for cat, pd in cat_predisc.items():
            disc = round(pd - cat_price.get(cat, 0), 2)
            if disc > 0:
                net_by_cat[cat] = net_by_cat.get(cat, 0) + disc

    # CC tips from payments table
    tips_row = conn.execute("""
        SELECT SUM(COALESCE(tip_amount, 0)) AS tips
        FROM payments WHERE location = ? AND business_date = ?
    """, (location, td)).fetchone()
    cc_tips = float(tips_row["tips"] or 0) if tips_row else 0

    # Declared cash tips from Toast time entries (employees declare during shift review)
    declared_cash_tips = _get_declared_cash_tips(location, entry_date)
    tips = round(cc_tips + declared_cash_tips, 2)

    # Tenders: full CC charge (base + tip); cash gets declared cash tips added
    pay_rows = conn.execute("""
        SELECT payment_type, card_type,
               SUM(amount + COALESCE(tip_amount, 0)) AS total
        FROM payments WHERE location = ? AND business_date = ?
        GROUP BY payment_type, card_type
    """, (location, td)).fetchall()

    map_rows = conn.execute(
        "SELECT journal_name, qbo_account FROM qb_line_mapping WHERE location=?",
        (location,)
    ).fetchall()
    mapping = {r["journal_name"]: r["qbo_account"] for r in map_rows}

    conn.close()

    # --- Build line items ---
    line_items = []

    def add_line(jname, debit=None, credit=None):
        qbo = mapping.get(jname)
        line_items.append({
            "journal_name": jname,
            "qbo_account": qbo,
            "memo": "",
            "debit":  round(debit,  2) if debit  is not None and debit  > 0 else None,
            "credit": round(credit, 2) if credit is not None and credit > 0 else None,
            "mapped": qbo is not None,
            "sort_order": len(line_items),
        })

    # CREDITS: Net sales by category (proportionally allocated from net_amount)
    for cat in ["Beer", "Wine", "Liquor", "NA Beverage", "Food"]:
        amt = net_by_cat.get(cat, 0)
        if amt > 0:
            add_line(f"Gross Sales: {cat}", credit=round(amt, 2))

    # CREDIT: Gift card sales (liability, not revenue — maps to Gift Certificates account)
    gc_sold = net_by_cat.get("Gift Card Sold", 0)
    if gc_sold > 0:
        add_line("Summary: Gift Card Sold", credit=round(gc_sold, 2))

    # CREDIT: Tax
    if tax > 0:
        add_line("Summary: Tax", credit=tax)

    # CREDIT: Tips
    if tips > 0:
        add_line("Summary: Tips", credit=tips)

    # DEBITS: Tenders (full amount including tips)
    CARD_LABELS = {
        "VISA": "Visa", "MC": "Mastercard", "MASTERCARD": "Mastercard",
        "AMEX": "Amex", "AMERICAN_EXPRESS": "Amex", "DISCOVER": "Discover",
    }
    TENDER_ORDER = [
        "Tenders: Cash", "Tenders: Credit", "Tenders: Visa",
        "Tenders: Mastercard", "Tenders: Amex", "Tenders: Discover",
        "Tenders: Gift Card", "Tenders: House Account", "Tenders: Other",
    ]

    tender_map = {}
    for row in pay_rows:
        pt = (row["payment_type"] or "").upper()
        ct = (row["card_type"] or "").upper()
        amt = float(row["total"] or 0)
        if pt == "CASH":
            key = "Tenders: Cash"
        elif pt in ("CREDIT", "CREDIT_CARD"):
            card_label = CARD_LABELS.get(ct)
            key = f"Tenders: {card_label}" if card_label else "Tenders: Credit"
        elif pt in ("GIFT_CARD", "GIFTCARD"):
            key = "Tenders: Gift Card"
        elif pt == "HOUSE_ACCOUNT":
            key = "Tenders: House Account"
        else:
            key = "Tenders: Other"
        tender_map[key] = tender_map.get(key, 0) + amt

    # Add declared cash tips to cash tender (employees tip out cash at shift review)
    if declared_cash_tips > 0:
        tender_map["Tenders: Cash"] = round(tender_map.get("Tenders: Cash", 0) + declared_cash_tips, 2)

    for key in TENDER_ORDER:
        if key in tender_map:
            add_line(key, debit=tender_map.pop(key))
    for key, amt in tender_map.items():
        add_line(key, debit=amt)

    # Adjusting entry: balance_gap = credits - tenders (includes actual discounts
    # plus any order/payment mismatches like gift card sales, refunds, comps)
    total_credits_built = sum((li["credit"] or 0) for li in line_items)
    total_tenders       = sum((li["debit"]  or 0) for li in line_items)
    balance_gap = round(total_credits_built - total_tenders, 2)
    if balance_gap > 0.005:
        add_line("Discounts: Total", debit=balance_gap)
    elif balance_gap < -0.005:
        # Payments > revenue (refunds/overpayments) — credit to balance
        add_line("Summary: Other", credit=abs(balance_gap))

    total_debits  = sum((li["debit"]  or 0) for li in line_items)
    total_credits = sum((li["credit"] or 0) for li in line_items)
    balanced = abs(total_debits - total_credits) < 0.005

    any_unmapped = any(not li["mapped"] for li in line_items)
    status = "needs_attention" if (any_unmapped or not balanced) else "ready"

    return {
        "entry_date": entry_date,
        "je_name": _make_je_name(location, entry_date),
        "location": location,
        "entry_type": "sales_journal",
        "line_items": line_items,
        "total_debits":  round(total_debits,  2),
        "total_credits": round(total_credits, 2),
        "balanced": balanced,
        "status": status,
        "last_sync_attempt": None,
        "qbo_error": None,
    }


def persist_journal_entry(entry: dict) -> int:
    """Write (or replace) a journal entry and its line items to DB. Returns entry id."""
    conn = get_connection()
    try:
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("""
            INSERT INTO qb_journal_entries
                (entry_type, location, entry_date, je_name, total_debits,
                 total_credits, balanced, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(location, entry_date, entry_type) DO UPDATE SET
                je_name=excluded.je_name,
                total_debits=excluded.total_debits,
                total_credits=excluded.total_credits,
                balanced=excluded.balanced,
                status=excluded.status,
                updated_at=excluded.updated_at
        """, (
            entry["entry_type"], entry["location"], entry["entry_date"],
            entry["je_name"], entry["total_debits"], entry["total_credits"],
            1 if entry["balanced"] else 0, entry["status"], now, now,
        ))

        row = conn.execute("""
            SELECT id FROM qb_journal_entries
            WHERE location=? AND entry_date=? AND entry_type=?
        """, (entry["location"], entry["entry_date"], entry["entry_type"])).fetchone()
        entry_id = row["id"]

        # Replace line items
        conn.execute("DELETE FROM qb_journal_line_items WHERE entry_id=?", (entry_id,))
        for li in entry["line_items"]:
            conn.execute("""
                INSERT INTO qb_journal_line_items
                    (entry_id, journal_name, qbo_account, memo, debit, credit, mapped, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry_id, li["journal_name"], li.get("qbo_account"),
                li.get("memo", ""), li.get("debit"), li.get("credit"),
                1 if li.get("mapped") else 0, li.get("sort_order", 0),
            ))

        conn.commit()
        return entry_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cron Job — runs nightly at 5 AM ET
# ---------------------------------------------------------------------------

def run_daily_journal(target_date: str = None):
    """
    Generate journal entries for both locations for target_date (YYYY-MM-DD).
    Defaults to yesterday (ET). Called by the APScheduler cron job.
    """
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    if target_date is None:
        yesterday = (datetime.now(ET) - timedelta(days=1)).date()
        target_date = yesterday.strftime("%Y-%m-%d")

    logger.info(f"[sales_journal] Running daily journal for {target_date}")
    conn = get_connection()
    run_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    created = 0
    failed = 0
    error_msg = None

    for location in ("dennis", "chatham"):
        try:
            entry = build_journal_entry(location, target_date)
            persist_journal_entry(entry)
            created += 1
            logger.info(f"[sales_journal] {location} {target_date}: {entry['status']}")

            # Send failure alert if not ready
            if entry["status"] in ("needs_attention", "error"):
                _send_failure_alert(entry)

        except Exception as e:
            failed += 1
            error_msg = str(e)
            logger.error(f"[sales_journal] Error for {location} {target_date}: {e}")

    conn.execute("""
        INSERT INTO qb_cron_log (run_at, entries_created, entries_failed, status, error_msg)
        VALUES (?, ?, ?, ?, ?)
    """, (run_at, created, failed, "ok" if failed == 0 else "partial", error_msg))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# QBO Push (stub — wire up when QBO OAuth is configured)
# ---------------------------------------------------------------------------

def push_to_qbo(entry_id: int) -> dict:
    """
    Push journal entry to QuickBooks Online.
    Returns {"success": True, "txn_id": "..."} or {"success": False, "error": "..."}.
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT * FROM qb_journal_entries WHERE id=?
        """, (entry_id,)).fetchone()

        if not row:
            return {"success": False, "error": "Entry not found"}
        if row["status"] == "posted":
            return {"success": False, "error": "Already posted"}
        if not row["balanced"]:
            return {"success": False, "error": "Entry is not balanced"}

        # Check for unmapped lines
        unmapped = conn.execute("""
            SELECT COUNT(*) as n FROM qb_journal_line_items
            WHERE entry_id=? AND mapped=0
        """, (entry_id,)).fetchone()["n"]
        if unmapped:
            return {"success": False, "error": f"{unmapped} line items are unmapped"}

        # --- Build QBO JournalEntry payload ---
        lines = conn.execute("""
            SELECT journal_name, qbo_account, debit, credit, sort_order
            FROM qb_journal_line_items WHERE entry_id=? ORDER BY sort_order
        """, (entry_id,)).fetchall()

        qbo_lines = []
        for i, li in enumerate(lines, 1):
            amt = li["debit"] or li["credit"]
            posting = "Debit" if li["debit"] else "Credit"
            qbo_lines.append({
                "Id": str(i),
                "Description": li["journal_name"],
                "Amount": round(float(amt), 2),
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {
                    "PostingType": posting,
                    "AccountRef": {"value": str(li["qbo_account"])},
                },
            })

        payload = {
            "TxnDate": row["entry_date"],
            "DocNumber": row["je_name"],
            "PrivateNote": f"Generated by Red Nun Dashboard — {row['location'].title()}",
            "Line": qbo_lines,
        }

        # --- QBO API call ---
        import json as _json, time as _time, base64 as _b64, urllib.request, urllib.parse, urllib.error
        from pathlib import Path

        TOKEN_FILE = Path.home() / ".qb_tokens.json"
        TOKEN_URL  = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
        BASE_URL   = "https://quickbooks.api.intuit.com"

        client_id     = os.getenv("QB_CLIENT_ID")
        client_secret = os.getenv("QB_CLIENT_SECRET")
        realm_id      = os.getenv("QB_REALM_ID")

        if not all([client_id, client_secret, realm_id]):
            return {"success": False, "error": "QB_CLIENT_ID / QB_CLIENT_SECRET / QB_REALM_ID env vars missing"}

        if not TOKEN_FILE.exists():
            return {"success": False, "error": "No QB tokens file. Run qb_push.py --auth first."}

        with open(TOKEN_FILE) as tf:
            tokens = _json.load(tf)

        # Refresh access token if older than 50 minutes
        age = _time.time() - tokens.get("obtained_at", 0)
        if age > 3000:
            creds = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).encode()
            req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
            req.add_header("Authorization", f"Basic {creds}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req) as resp:
                tokens = _json.loads(resp.read())
            tokens["obtained_at"] = _time.time()
            with open(TOKEN_FILE, "w") as tf:
                _json.dump(tokens, tf, indent=2)

        access_token = tokens["access_token"]
        url = f"{BASE_URL}/v3/company/{realm_id}/journalentry?minorversion=65"
        body_bytes = _json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body_bytes, method="POST")
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req) as resp:
                result = _json.loads(resp.read())
            txn_id = result.get("JournalEntry", {}).get("Id")
            conn.execute("""
                UPDATE qb_journal_entries
                SET status='posted', qbo_txn_id=?, qbo_error=NULL, updated_at=datetime('now')
                WHERE id=?
            """, (txn_id, entry_id))
            conn.commit()
            return {"success": True, "txn_id": txn_id}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            logger.error(f"[push_to_qbo] QBO HTTP {e.code}: {err_body[:500]}")
            conn.execute("""
                UPDATE qb_journal_entries SET qbo_error=?, updated_at=datetime('now') WHERE id=?
            """, (err_body[:500], entry_id))
            conn.commit()
            return {"success": False, "error": f"QBO API error {e.code}: {err_body[:300]}"}

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Email Alerts
# ---------------------------------------------------------------------------

def _send_email(subject: str, body: str):
    """Send plain-text email via SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("[sales_journal] SMTP not configured, skipping email")
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = ALERT_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        logger.info(f"[sales_journal] Alert email sent: {subject}")
    except Exception as e:
        logger.error(f"[sales_journal] Email failed: {e}")


def _send_failure_alert(entry: dict):
    """Send immediate failure notification for a single entry."""
    loc_name = LOCATION_NAMES.get(entry["location"], entry["location"])
    entry_date_fmt = datetime.strptime(entry["entry_date"], "%Y-%m-%d").strftime("%m/%d/%Y")
    status_label = "Needs Attention" if entry["status"] == "needs_attention" else "Error"

    conn = get_connection()
    entry_id = conn.execute("""
        SELECT id FROM qb_journal_entries
        WHERE location=? AND entry_date=? AND entry_type='sales_journal'
    """, (entry["location"], entry["entry_date"])).fetchone()
    conn.close()

    eid = entry_id["id"] if entry_id else "unknown"
    total = entry.get("total_credits", 0)

    unmapped_count = sum(1 for li in entry.get("line_items", []) if not li.get("mapped"))
    if entry["status"] == "needs_attention" and unmapped_count:
        issue = f"- {unmapped_count} line item(s) are unmapped"
    elif not entry.get("balanced"):
        diff = abs((entry.get("total_debits") or 0) - (entry.get("total_credits") or 0))
        issue = f"- Entry is not balanced (${diff:.2f} difference)"
    else:
        issue = f"- QBO upload failed: {entry.get('qbo_error', 'unknown error')}"

    body = f"""Location:     {loc_name}
Sales Date:   {entry_date_fmt}
JE Name:      {entry['je_name']}
Total:        ${total:,.2f}
Status:       {status_label}

Issue:
  {issue}

Resolve here: {DASHBOARD_URL}/sales-journal/{eid}
"""
    subject = f"[Red Nun] Sales entry needs attention — {loc_name} {entry_date_fmt}"
    _send_email(subject, body)


def send_weekly_unresolved_summary():
    """
    Send weekly summary of unresolved entries (Monday 7 AM).
    Only sends if there are open items.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT location, entry_date, je_name, total_credits, status
        FROM qb_journal_entries
        WHERE status IN ('needs_attention', 'error')
          AND entry_type = 'sales_journal'
        ORDER BY location, entry_date DESC
    """).fetchall()
    conn.close()

    if not rows:
        logger.info("[sales_journal] Weekly summary: nothing to report")
        return

    by_loc = {}
    for r in rows:
        loc = LOCATION_NAMES.get(r["location"], r["location"])
        by_loc.setdefault(loc, []).append(r)

    lines = ["The following sales entries have not been posted to QuickBooks:\n"]
    for loc_name, entries in by_loc.items():
        lines.append(f"{loc_name}")
        for e in entries:
            d = datetime.strptime(e["entry_date"], "%Y-%m-%d").strftime("%m/%d/%Y")
            status_label = "Needs Attention" if e["status"] == "needs_attention" else "Error"
            lines.append(f"  {d}  {e['je_name']}  ${e['total_credits']:,.2f}  {status_label}")
        lines.append("")

    lines.append(f"Resolve here: {DASHBOARD_URL}/sales-journal")
    body = "\n".join(lines)
    count = len(rows)
    subject = f"[Red Nun] Weekly QBO sync summary — {count} item{'s' if count != 1 else ''} need attention"
    _send_email(subject, body)


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------

def export_entries_csv(location: str, start_date: str, end_date: str) -> str:
    """Return CSV string for entries in date range."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT entry_date, je_name, balanced, total_debits, total_credits,
               total_credits as total_sales, status, last_sync_attempt
        FROM qb_journal_entries
        WHERE location=? AND entry_date >= ? AND entry_date <= ?
          AND entry_type='sales_journal'
        ORDER BY entry_date DESC
    """, (location, start_date, end_date)).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Entry Date", "JE Name", "Balanced",
        "Total Debits", "Total Credits", "Total Sales",
        "Status", "Last Sync Attempt",
    ])
    for r in rows:
        writer.writerow([
            r["entry_date"], r["je_name"],
            "Yes" if r["balanced"] else "No",
            f"{r['total_debits']:.2f}", f"{r['total_credits']:.2f}",
            f"{r['total_sales']:.2f}",
            r["status"].replace("_", " ").title(),
            r["last_sync_attempt"] or "",
        ])
    return output.getvalue()
