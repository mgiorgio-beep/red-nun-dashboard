"""
Invoice Processor — Red Nun Analytics
Uses Claude Vision API to extract structured data from invoice photos.
Replaces MarginEdge's core OCR functionality.

Cost: ~$0.05 per invoice vs $363/month for MarginEdge
"""

import os
import json
import base64
import logging
import sqlite3
import re
from datetime import datetime
from dotenv import load_dotenv
from routes.product_name_mapper import get_name_variants

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "toast_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_invoice_tables():
    """Create tables for the invoice scanning system."""
    conn = get_connection()
    conn.executescript("""
        -- Scanned invoices (replaces me_invoices for new data)
        CREATE TABLE IF NOT EXISTS scanned_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            vendor_name TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            subtotal REAL DEFAULT 0,
            tax REAL DEFAULT 0,
            total REAL DEFAULT 0,
            category TEXT,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            image_path TEXT,
            raw_extraction TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            confirmed_at TEXT,
            confirmed_by TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_scanned_inv_date
            ON scanned_invoices(location, invoice_date);
        CREATE INDEX IF NOT EXISTS idx_scanned_inv_vendor
            ON scanned_invoices(vendor_name);
        CREATE INDEX IF NOT EXISTS idx_scanned_inv_status
            ON scanned_invoices(status);

        -- Scanned invoice line items
        CREATE TABLE IF NOT EXISTS scanned_invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            product_name TEXT,
            description TEXT,
            quantity REAL DEFAULT 0,
            unit TEXT,
            unit_price REAL DEFAULT 0,
            total_price REAL DEFAULT 0,
            category_type TEXT,
            FOREIGN KEY (invoice_id) REFERENCES scanned_invoices(id)
        );

        CREATE INDEX IF NOT EXISTS idx_scanned_items_inv
            ON scanned_invoice_items(invoice_id);

        -- Product price history (tracks cost changes over time)
        CREATE TABLE IF NOT EXISTS product_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            vendor_name TEXT,
            location TEXT,
            unit_price REAL,
            unit TEXT,
            invoice_date TEXT,
            invoice_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invoice_id) REFERENCES scanned_invoices(id)
        );

        CREATE INDEX IF NOT EXISTS idx_prod_prices_name
            ON product_prices(product_name);
        CREATE INDEX IF NOT EXISTS idx_prod_prices_vendor
            ON product_prices(vendor_name);
    """)
    conn.commit()
    # Session 13 migration: add pack_size column to line items
    try:
        conn.execute("ALTER TABLE scanned_invoice_items ADD COLUMN pack_size TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists
    # Session 14: product mapping columns
    try:
        conn.execute("ALTER TABLE scanned_invoice_items ADD COLUMN canonical_product_name TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE scanned_invoice_items ADD COLUMN auto_linked INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    # vendor_item_links table for persistent product mapping
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vendor_item_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_item_name TEXT NOT NULL UNIQUE,
            canonical_product_name TEXT NOT NULL,
            confidence REAL,
            auto_linked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    # Session 29: discrepancy tracking columns
    try:
        conn.execute("ALTER TABLE scanned_invoices ADD COLUMN discrepancy REAL DEFAULT 0.0")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE scanned_invoices ADD COLUMN needs_reconciliation INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    # Session 29: backfill discrepancy values for existing invoices
    try:
        conn.execute("""
            UPDATE scanned_invoices
            SET discrepancy = ROUND(total - (
                COALESCE((SELECT SUM(total_price) FROM scanned_invoice_items WHERE invoice_id = scanned_invoices.id), 0)
                + COALESCE(tax, 0)
            ), 2),
            needs_reconciliation = CASE
                WHEN status = 'pending' AND ABS(total - (
                    COALESCE((SELECT SUM(total_price) FROM scanned_invoice_items WHERE invoice_id = scanned_invoices.id), 0)
                    + COALESCE(tax, 0)
                )) >= 0.02 THEN 1
                ELSE 0
            END
            WHERE discrepancy = 0.0
              AND total > 0
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"Discrepancy backfill skipped: {e}")
    conn.close()
    logger.info("Invoice scanner tables initialized")


# Vendor -> category mapping (same as existing ME mapping)
VENDOR_CATEGORIES = {
    # Mixed distributors (beer/wine/liquor) - categorize at product level
    "southern glazer": "LIQUOR_WINE_BEER",
    "southern glazers": "LIQUOR_WINE_BEER",
    # Wine & Liquor
    "martignetti": "LIQUOR_WINE",
    # Beer distributors
    "l. knife": "BEER",
    "l knife": "BEER",
    "atlantic beverage": "BEER",
    "colonial wholesale": "BEER",
    "craft collective": "BEER",
    "cape cod beer": "BEER",
    "horizon beverage": "BEER",
    # Food broadline
    "us foods": "FOOD",
    "reinhart": "FOOD",
    "performance food": "FOOD",
    "chef's warehouse": "FOOD",
    "chefs warehouse": "FOOD",
    "cape fish": "FOOD",
    "sysco": "FOOD",
    # Non-COGS
    "cintas": "NON_COGS",
    "unifirst": "NON_COGS",
    "cozzini": "NON_COGS",
    "rooter": "NON_COGS",
    "dennisport village": "NON_COGS",
    "caron group": "NON_COGS",
    "robert b. our": "NON_COGS",
    "marginedge": "NON_COGS",
}


def categorize_vendor(vendor_name):
    """Auto-categorize a vendor based on known mappings."""
    if not vendor_name:
        return "OTHER"
    lower = vendor_name.lower().strip()
    for hint, cat in VENDOR_CATEGORIES.items():
        if hint in lower:
            return cat
    return "OTHER"


def detect_location_from_address(address):
    """
    Detect which Red Nun location an invoice belongs to based on the ship-to address.

    Locations:
      Chatham:     746 Main Street, Chatham, MA 02633
      Dennis Port: 746 Route 28, Dennis Port, MA 02639

    Matching order (most to least reliable):
      1. ZIP code: 02633 → chatham, 02639 → dennis
      2. Street:   Main St/Street → chatham; Route 28/Rte 28/RT 28 → dennis
      3. Town:     Chatham → chatham; Dennis/Dennisport → dennis

    Returns 'chatham', 'dennis', or None (user picks manually).
    """
    if not address:
        return None
    addr = address.lower()

    # 1. ZIP code — most reliable
    if "02633" in addr:
        return "chatham"
    if "02639" in addr:
        return "dennis"

    # 2. Street name
    if re.search(r'\brt\.?\s*28\b|rte\.?\s*28\b|route\s*28\b', addr):
        return "dennis"
    if re.search(r'\bmain\s+st(reet)?\b', addr):
        return "chatham"

    # 3. Town name
    if "chatham" in addr:
        return "chatham"
    if "dennisport" in addr or "dennis port" in addr or re.search(r'\bdennis\b', addr):
        return "dennis"

    return None


def extract_invoice_data(image_base64, mime_type="image/jpeg", extra_pages=None):
    """
    Use Claude Vision API to extract structured data from an invoice image or PDF.

    Args:
        image_base64: Base64-encoded image or PDF data
        mime_type: MIME type (image/jpeg, image/png, application/pdf, etc.)

    Returns:
        dict with extracted invoice data
    """
    import requests

    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set in .env file")

    prompt = """Analyze this invoice/receipt image and extract the following data in JSON format.
If multiple pages/images are provided, treat them as ONE invoice — combine all line items from all pages.
The image may be rotated — read it in whatever orientation the text is readable.

CRITICAL — HOW TO READ PRICES ON DOT-MATRIX INVOICES:
- US Foods invoices have columns: ITEM # | DESCRIPTION | PACK/SIZE | ORDERED | SHIPPED | UNIT PRICE | EXTENSION
- SHIPPED = number of cases/units delivered (usually 1-5, small number)
- SOUTHERN GLAZER'S / HORIZON BEVERAGE invoices: Columns are LIST PRICE | POST OFF | DEPOSIT | DISCOUNT | NET PRICE PER CASE | NET PRICE PER BOTTLE | EXTENSION. Use EXTENSION as total_price and NET PRICE PER CASE as unit_price. IGNORE any "Prompt Pay Discount" box — do NOT include it as a line item.
- Before extracting line items, read the column headers to identify which column represents the final billed amount — the amount the restaurant actually owes for that line after all discounts. It may be labeled EXTENSION, EXT, TOTAL, AMOUNT, or TOTAL W/O DEPOSITS. Use that column as total_price.
- Use the column labeled UNIT PRICE, NET PRICE PER CASE, or similar as unit_price.
- Read quantity, unit_price, and total_price for each line directly from the invoice.
- CROSS-CHECK: quantity × unit_price should approximately equal total_price. If they don't match within $0.02, re-read the values. If still mismatched, trust the total_price column and set unit_price = null. NEVER compute unit_price = total_price / quantity — a null unit_price is better than a computed wrong price.
- CRAFT COLLECTIVE HOMEGROWN invoices: Columns are ID | QTY | PRODUCT | UPC | PRICE | DEP | DISC | NET | TOTAL. The PRICE column is the base price per keg, DEP is the keg deposit ($30 per keg), NET = PRICE, and TOTAL = PRICE + DEP. Use TOTAL as total_price and PRICE as unit_price. The line "Beer MA 15.50 Gallons" near the bottom is a deposit summary — do NOT include it as a separate line item. Payment history rows at the bottom (showing dates and invoice numbers like "5/9 921533 ($30.00)") are NOT line items — skip them. Keg deposit credits ("Empty Keg" or "Empty Keg Credit") should be included as negative line items with category NON_COGS.
- MARTIGNETTI COMPANIES invoices: IGNORE any "Prompt Pay Discount" line or box — do NOT include it as a line item. Only extract actual products. For the "total" field, use the "Total Amount Due" box — this is the final amount owed. Do NOT use any gross or pre-discount total. Non-Alcoholic Deposit lines are small surcharges (typically $0.50-$5.00 total) — read the actual deposit amount from the totals/deposit section, do NOT copy a product line's price. Include the deposit as a NON_COGS line item with its correct small amount.
- DEPOSIT RETURNS/CREDITS: If the invoice shows empty keg credits, container credits, or deposit returns, include them as negative line items with category NON_COGS.
- FINAL CHECK: After extracting ALL items, sum all total_price values. This should match the printed subtotal or invoice total (before tax). If off by more than $1, you likely missed an item or misread a digit.

Return ONLY valid JSON with this structure (no other text):
{
    "vendor_name": "Full vendor/supplier name",
    "invoice_number": "Invoice or order number",
    "invoice_date": "YYYY-MM-DD format",
    "subtotal": 0.00,
    "tax": 0.00,
    "total": 0.00,
    "ship_to_address": "Full ship-to or delivery address from the invoice (street, city, state, zip) as a single string",
    "line_items": [
        {
            "product_name": "Item name",
            "description": "Brand/size/details if visible",
            "quantity": 1,
            "unit": "case/bottle/lb/each/etc",
            "unit_price": 0.00,
            "total_price": 0.00,
            "pack_size": "4/5 LB or 20/8 OZ or 25 LB or null if not shown",
            "category": "FOOD or LIQUOR or BEER or WINE or NA_BEVERAGES or NON_COGS or TOGO_SUPPLIES or DR_SUPPLIES or KITCHEN_SUPPLIES",
            "vendor_item_code": "Vendor's product/item number or null if not visible"
        }
    ],
    "notes": "Any relevant details (delivery date, PO number, account number, etc.)",
    "total_line_items": null,
    "invoice_subtotal": null,
    "invoice_total": null,
    "page_info": "Page X of Y if visible, e.g. 'Page 01 of 03'. null if not shown."
}

Rules:
- If a field is not visible, use null
- For dates, convert to YYYY-MM-DD format
- Include ALL line items you can read
- IMPORTANT: Read ALL three columns: SHIPPED QTY, UNIT PRICE, and EXTENSION. Set total_price = extension (read directly). Cross-check: qty × unit_price should ≈ total_price. If not, trust extension and set unit_price = total_price / quantity.
- On US Foods invoices, columns are: SHIPPED QTY | UNIT PRICE | EXTENSION. Read all three, use EXTENSION for total_price.
- On Performance Foodservice invoices, columns are: QTY | UNIT PRICE | EXTENSION — same approach.
- On US Foods invoices, the INVOICE NO field (e.g. 1931359) is the correct invoice_number — NOT the ORDER NUMBER, NOT the ACCOUNT NO, NOT the CUSTOMER NO. The invoice number appears in the header row labeled "INVOICE NO."
- IMPORTANT: Look for service charges, fuel surcharges, delivery fees, environmental fees, or any other charges that appear OUTSIDE the main line items table — often near the subtotal or in a separate section. Include each as a line item with category NON_COGS. These fees are part of the invoice total and must not be missed.
- If you can't read a value clearly, make your best guess and add "(unclear)" to notes
- For pack_size, look for a PACK SIZE, PACK, or UNIT SIZE column (e.g., "4/5 LB", "20/8 OZ", "6/24/1 OZ", "25 LB"). Use null if not visible.
- For ship_to_address, look for "Ship To", "Deliver To", "Sold To", or any delivery/recipient address block. Return the full address as one string (e.g., "746 Main St, Chatham, MA 02633"). Use null if not visible.
- NEVER include the invoice subtotal, tax, total, balance due, or any summary row as a line item. Only include actual purchased products and services/fees. If a row's total_price equals the invoice subtotal or total, it is NOT a line item — skip it.
- IMPORTANT: Include negative line items (credits, deposit returns, empty keg returns). These appear as negative quantities or negative EXT values (e.g. "-1 AB COOPERAGE -50.00", "-5 NON AB COOPERG -250.00"). Capture them with negative total_price values. They are part of the invoice total and must be included for the math to balance. Do NOT skip lines just because they have a negative or zero unit price.
- Combine multi-line item descriptions into one product_name
- For category, classify each item individually:
  - LIQUOR: spirits, vodka, whiskey, rum, tequila, gin, brandy, cordials, bitters
  - BEER: beer, ale, IPA, lager, stout, cider, seltzer, kegs of beer
  - WINE: wine, champagne, prosecco, sparkling wine
  - FOOD: all food items, produce, meat, seafood, dairy, dry goods
  - NA_BEVERAGES: soda, juice, coffee, tea, energy drinks, water, non-alcoholic beer/cocktails
  - TOGO_SUPPLIES: to-go containers, plastic bags, takeout utensils, napkin packs, styrofoam boxes, paper bags, straws, lids
  - DR_SUPPLIES: candles, table covers, cleaning supplies, floor mats, light bulbs, bathroom supplies, menus, table tents
  - KITCHEN_SUPPLIES: aluminum foil, plastic wrap, gloves, sanitizer, trash bags, pan liners, parchment paper, kitchen towels
  - NON_COGS: uniforms, equipment, rent, services, maintenance, landscaping, legal, accounting
- For total_line_items: Look for item count indicators like "Total Items", "Line Items", "Qty Lines", or similar text on the invoice. If you see "47 Items" or "Line Items: 47", extract 47. If no explicit count is printed, count the distinct line items and use that number. Use null if uncertain.
- For invoice_subtotal: Extract the subtotal amount (sum of all items BEFORE tax/fees) exactly as printed on the invoice. Look for "Subtotal", "Merchandise Total", "Items Total", or similar. Use null if not visible.
- For invoice_total: Extract the grand total amount (final amount including tax/fees if present) exactly as printed on the invoice. Look for "Total", "Invoice Total", "Amount Due", "Balance Due", or similar. Use null if not visible.

IMPORTANT — US FOODS INVOICES (multi-page):
US Foods invoices span 3-4 pages. The true invoice total appears on the INVOICE SUMMARY page (usually page 3 or the second-to-last page), labeled "PLEASE REMIT THIS AMOUNT BY [date]  $X,XXX.XX". This is the ONLY value to use as the invoice total and invoice_total.
DO NOT use any of these as the invoice total:
- The "DELIVERY SUMMARY TOTALS" extended price (page 2)
- Any weight values shown in lbs (these are NOT dollar amounts)
- Any per-category subtotals (DRY, REFRIGERATED, FROZEN)
- The "AS SHIPPED DELIVERY AMOUNT" or "DELIVERED AMOUNT"
For US Foods, the subtotal is labeled "Product Total" on the summary page. There may be a "FUEL SURCHARGE" line — include it as a NON_COGS line item, not as tax. Sales tax is typically $0.00 on US Foods food invoices. Do NOT include Sales Tax as a line item — it is already captured in the invoice_tax field.

US FOODS LINE ITEMS:
The DESCRIPTION column contains the product name. It is typically the 5th column after ORD, SHP, ADJ, SALES UNIT, PRODUCT NUMBER. The EXTENDED PRICE (rightmost dollar column) is the line item total. The UNIT PRICE is the second-to-last dollar column. Ignore LABEL, PACK SIZE, CODE, and WEIGHT columns for product data. Use DESCRIPTION as product_name and EXTENDED PRICE as total_price.

UNIFIRST INVOICE RULES:
- UniFirst is a SERVICE invoice (linen/uniform rental). There are NO food items.
- Set category = "NON_COGS" for ALL line items, no exceptions.
- The invoice layout has two totals:
    * "Invoice Total" in the table footer = pre-tax subtotal (use this as invoice_subtotal AND as subtotal)
    * "Total Amount Due" or "Amount Due" at the bottom = after-tax total (use this as invoice_total AND as total)
- DO NOT use "Total Amount Due" as invoice_subtotal — it includes tax.
- invoice_tax / tax is shown in the TAX column on each line (usually $0.00 per item)
  and as a summary line. Sum the tax column for tax.
- Common line item types (all NON_COGS):
    * STOCK items (aprons, shirts, pants) — weekly rental charge
    * Automatic Replacement items — replacement garment fee
    * WET MOP / DRY MOP — cleaning equipment rental
    * TERRY CLOTHS / SHOP TOWELS — towel service
    * AIR FRESH / FRESHENER — air freshener service
    * BAG RACK / HANGER RACK — equipment rental
    * UL (4x6H) — mat rental
    * GMP / GSP / GLP — garment protection charges
    * BOWL CLIP / AIRCLIP — dispenser items

CINTAS INVOICE RULES:
- Cintas is a SERVICE invoice (uniform/linen rental, mats, restroom supplies). Set category = "NON_COGS" for ALL line items.
- The invoice layout: columns are EMP#/LOCK# | MATERIAL | DESCRIPTION | FREQ | EXCH | QTY | UNIT PRICE | LINE TOTAL | TAX
- "SUBTOTAL" at the bottom of the items table = pre-tax item total. Use this as subtotal and invoice_subtotal.
- "SERVICE CHARGE" below the subtotal is a separate fee — include it as a NON_COGS line item.
- "SALES TAX" is the tax amount. Use this as tax.
- "TOTAL USD" is the grand total (subtotal + service charge + tax). Use this as total and invoice_total.
- CRITICAL: subtotal must NOT include tax. The "TOTAL USD" includes tax — do NOT use it as subtotal. Use the printed "SUBTOTAL" value.

VENDOR ITEM CODE:
- For vendor_item_code, look for a PRODUCT NUMBER, ITEM #, ITEM CODE, CODE, or SKU column on the invoice. This is the vendor's internal product identifier.
- US Foods: Look for the PRODUCT NUMBER column (typically a 7-digit number like "1234567" or "7654321"). It usually appears before the DESCRIPTION column.
- Performance Foodservice / Reinhart: Look for an ITEM # or CODE column (typically 6-7 digits).
- Southern Glazer's / Horizon Beverage: Look for an ITEM or CODE column.
- If no product code column is visible, set vendor_item_code to null for each item."""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 16384,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document" if mime_type == "application/pdf" else "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_base64,
                            },
                        },
                        *([{
                            "type": "document" if p["mime"] == "application/pdf" else "image",
                            "source": {
                                "type": "base64",
                                "media_type": p["mime"],
                                "data": p["data"],
                            },
                        } for p in (extra_pages or [])]),
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
        },
        timeout=180,
    )

    if resp.status_code != 200:
        logger.error(f"Claude API error {resp.status_code}: {resp.text[:500]}")
        raise Exception(f"Claude API error: {resp.status_code}")

    result = resp.json()
    stop_reason = result.get("stop_reason", "")
    if stop_reason == "max_tokens":
        logger.warning("Claude response truncated (hit max_tokens) — response may be incomplete")

    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block["text"]

    # Parse JSON from response (handle markdown code blocks and preamble text)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse extracted JSON: {e}\nResponse: {text[:500]}")
                raise Exception(f"Could not parse invoice data: {e}")
        else:
            logger.error(f"No JSON found in response:\nResponse: {text[:500]}")
            raise Exception("No JSON found in Claude response")

    # Calculate confidence score based on completeness
    confidence_score = 0
    required_fields = {
        "vendor_name": 30,
        "invoice_number": 20,
        "invoice_date": 20,
        "total": 30
    }

    for field, weight in required_fields.items():
        value = data.get(field)
        if value and str(value).strip() and str(value).lower() not in ["unknown", "n/a", "none", ""]:
            confidence_score += weight

    data["confidence_score"] = confidence_score
    data["is_low_confidence"] = confidence_score < 70

    # Auto-detect location from ship-to address
    data["detected_location"] = detect_location_from_address(data.get("ship_to_address"))

    # Apply category memory — override Claude's guesses with user-confirmed categories
    try:
        conn = get_connection()
        vendor = data.get("vendor_name", "")
        for item in data.get("line_items", []):
            pname = item.get("product_name", "")
            if not pname:
                continue
            # Try exact match first, then fuzzy
            mem = conn.execute("""
                SELECT category FROM product_category_memory
                WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
                AND vendor_name LIKE ?
            """, (pname, f"%{vendor[:15]}%")).fetchone()
            if not mem:
                # Try prefix match
                prefix = pname[:20].lower().strip()
                if len(prefix) > 8:
                    mem = conn.execute("""
                        SELECT category FROM product_category_memory
                        WHERE LOWER(SUBSTR(product_name, 1, 20)) = ?
                        AND vendor_name LIKE ?
                    """, (prefix, f"%{vendor[:15]}%")).fetchone()
            if mem:
                item["category"] = mem[0]
        conn.close()
    except Exception as e:
        logger.warning(f"Category memory lookup failed: {e}")

    return data


def verify_math_errors(image_base64, mime_type, data, extra_pages=None):
    """
    Second-pass OCR: re-read the full invoice when issues are detected.
    Triggers on: per-line math errors OR total mismatch (missing items / wrong prices).
    Returns corrected data dict (modifies in place).
    """
    import requests

    items = data.get("line_items", [])
    items_sum = round(sum(float(it.get("total_price", 0) or 0) for it in items), 2)
    # Use pre-tax subtotal for comparison (line items don't include tax)
    invoice_subtotal = float(data.get("invoice_subtotal") or data.get("subtotal") or 0)
    invoice_total_raw = float(data.get("invoice_total") or data.get("total") or 0)
    invoice_tax = float(data.get("tax") or 0)
    if invoice_subtotal > 0:
        invoice_total = invoice_subtotal
    elif invoice_total_raw > 0 and invoice_tax > 0:
        invoice_total = round(invoice_total_raw - invoice_tax, 2)
    else:
        invoice_total = invoice_total_raw
    total_gap = round(abs(items_sum - invoice_total), 2) if invoice_total > 0 else 0

    # Find per-line math errors
    bad_items = []
    for i, item in enumerate(items):
        qty = float(item.get("quantity", 0) or 0)
        up = float(item.get("unit_price", 0) or 0)
        tp = float(item.get("total_price", 0) or 0)
        expected = round(qty * up, 2)
        if abs(expected - tp) > 0.02 and qty > 0 and up > 0:
            bad_items.append({
                "index": i,
                "product_name": item.get("product_name", "Unknown"),
                "quantity": qty,
                "unit_price": up,
                "total_price": tp,
                "expected": expected,
            })

    if not bad_items and total_gap <= 1.00:
        return data
    if mime_type == "application/pdf" and not bad_items:
        logger.info("Skipping API verification pass for native PDF — no per-line math errors")
        return data

    logger.info(f"Verification pass: {len(bad_items)} math errors, total gap ${total_gap}")

    # Build the extracted items summary so Claude can see what we got
    extracted_summary = "\n".join([
        f"  {i+1}. \"{it.get('product_name', '?')}\" qty={it.get('quantity',0)} total_price={it.get('total_price',0)}"
        for i, it in enumerate(items)
    ])

    # Build specific error notes
    error_notes = ""
    if bad_items:
        error_lines = "\n".join([
            f"- Line {b['index']+1}: \"{b['product_name']}\" — qty={b['quantity']} × unit_price={b['unit_price']} = {b['expected']}, but total_price={b['total_price']}"
            for b in bad_items
        ])
        error_notes += f"\nPer-line math errors:\n{error_lines}\n"

    if total_gap > 1.00:
        error_notes += f"\nTotal mismatch: sum of my extracted line items = ${items_sum}, but the printed invoice total = ${invoice_total} (gap: ${total_gap}). I may have missed line items or misread prices.\n"

    verify_prompt = f"""I previously extracted data from this invoice but found errors. The invoice is always correct — I must have misread something.

Here's what I extracted ({len(items)} items, sum=${items_sum}):
{extracted_summary}

Invoice printed total: ${invoice_total}
{error_notes}
Please re-read the ENTIRE invoice carefully and return a corrected JSON object with:
{{
    "corrected_items": [
        {{
            "index": 0,
            "product_name": "exact name",
            "quantity": 1,
            "unit_price": 0.00,
            "total_price": 0.00
        }}
    ],
    "missing_items": [
        {{
            "product_name": "item I missed",
            "description": "details",
            "quantity": 1,
            "unit": "case",
            "unit_price": 0.00,
            "total_price": 0.00,
            "pack_size": null,
            "category": "FOOD"
        }}
    ]
}}

Rules:
- In corrected_items, include ONLY items where you read a DIFFERENT value than what I extracted. Use the same index numbers.
- Read all three columns: SHIPPED QTY, UNIT PRICE, and EXTENSION. Set total_price = extension (read directly).
- Cross-check: qty × unit_price should ≈ extension. If not, trust extension and set unit_price = total_price / quantity.
- In missing_items, include any line items visible on the invoice that I missed entirely.
- If nothing needs correcting, return {{"corrected_items": [], "missing_items": []}}
- Return ONLY the JSON object, no other text."""

    image_content = [{
        "type": "document" if mime_type == "application/pdf" else "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": image_base64,
        },
    }]
    for p in (extra_pages or []):
        image_content.append({
            "type": "document" if p["mime"] == "application/pdf" else "image",
            "source": {
                "type": "base64",
                "media_type": p["mime"],
                "data": p["data"],
            },
        })
    image_content.append({"type": "text", "text": verify_prompt})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 16384,
                "messages": [{"role": "user", "content": image_content}],
            },
            timeout=180,
        )

        if resp.status_code != 200:
            logger.warning(f"Verify pass API error {resp.status_code}, skipping correction")
            return data

        result = resp.json()
        text = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                text += block["text"]

        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

        fixes = json.loads(text)
        if not isinstance(fixes, dict):
            logger.warning("Verify pass returned non-dict, skipping")
            return data

        # Apply corrected items
        corrected_count = 0
        for fix in fixes.get("corrected_items", []):
            idx = fix.get("index")
            if idx is None or idx < 0 or idx >= len(items):
                continue
            new_tp = float(fix.get("total_price", 0) or 0)
            new_up = float(fix.get("unit_price", 0) or 0)
            new_qty = float(fix.get("quantity", items[idx].get("quantity", 0)) or 0)
            if new_tp > 0:
                old_tp = float(items[idx].get("total_price", 0) or 0)
                items[idx]["total_price"] = new_tp
                items[idx]["unit_price"] = new_up if new_up > 0 else (round(new_tp / new_qty, 2) if new_qty > 0 else 0)
                items[idx]["quantity"] = new_qty
                if fix.get("product_name"):
                    items[idx]["product_name"] = fix["product_name"]
                corrected_count += 1
                logger.info(f"  Corrected line {idx}: {items[idx].get('product_name')} — tp {old_tp} → {new_tp}")

        # Add missing items
        added_count = 0
        for missing in fixes.get("missing_items", []):
            if missing.get("product_name") and float(missing.get("total_price", 0) or 0) > 0:
                items.append(missing)
                added_count += 1
                logger.info(f"  Added missing item: {missing.get('product_name')} ${missing.get('total_price')}")

        data["line_items"] = items
        logger.info(f"Verification: {corrected_count} corrected, {added_count} missing items added")

    except Exception as e:
        logger.warning(f"Math verification pass failed: {e}")

    return data


def fix_subtotal_includes_tax(data):
    """
    Fix cases where OCR sets subtotal = total (including tax) instead of pre-tax subtotal.
    This happens with Cintas and similar service invoices where the OCR confuses the total
    with the subtotal. If subtotal ≈ total and tax > 0, adjust subtotal = total - tax.
    """
    subtotal = float(data.get("subtotal", 0) or 0)
    tax = float(data.get("tax", 0) or 0)
    total = float(data.get("total", 0) or 0)

    if tax > 0 and total > 0 and subtotal > 0 and abs(subtotal - total) < 0.02:
        corrected = round(total - tax, 2)
        logger.info(f"Subtotal fix: subtotal ({subtotal}) == total ({total}) but tax={tax} → corrected subtotal to {corrected}")
        data["subtotal"] = corrected
        if data.get("invoice_subtotal") and abs(float(data["invoice_subtotal"]) - total) < 0.02:
            data["invoice_subtotal"] = corrected

    return data


def validate_invoice_extraction(data):
    """
    Validate extracted invoice data for auto-confirm eligibility.

    Checks:
    1. Item count: Compare total_line_items (from invoice) vs len(line_items) (extracted)
    2. Total match: Compare sum of extracted items vs invoice_subtotal or invoice_total

    Returns:
        dict with validation results and auto_confirm flag
    """
    result = {
        "valid": False,
        "item_count_match": None,
        "item_count_invoice": None,
        "item_count_extracted": None,
        "total_match": None,
        "total_invoice": None,
        "total_extracted": None,
        "total_difference": None,
        "auto_confirm": False,
        "issues": []
    }
    
    line_items = data.get("line_items", [])
    extracted_count = len(line_items)
    result["item_count_extracted"] = extracted_count
    
    # CHECK 1: Item count validation
    invoice_count = data.get("total_line_items")
    if invoice_count is not None:
        result["item_count_invoice"] = invoice_count
        if abs(invoice_count - extracted_count) <= 2:
            result["item_count_match"] = True
        else:
            result["item_count_match"] = False
            diff = abs(invoice_count - extracted_count)
            result["issues"].append(
                f"{extracted_count} of {invoice_count} items extracted — {diff} item(s) may be missing"
            )
    else:
        # No item count on invoice — can't validate, mark as null (pass)
        result["item_count_match"] = None
    
    # CHECK 2: Total validation
    extracted_total = sum(
        float(item.get("total_price", 0) or 0) 
        for item in line_items
    )
    result["total_extracted"] = round(extracted_total, 2)
    
    # Use invoice_total minus tax (line items don't include tax), fall back to subtotal
    invoice_total_raw = float(data.get("invoice_total") or data.get("invoice_subtotal") or 0)
    invoice_tax = float(data.get("invoice_tax") or data.get("tax") or 0)
    invoice_total = (invoice_total_raw - invoice_tax) if invoice_total_raw > 0 else None
    
    if invoice_total is not None:
        invoice_total = float(invoice_total)
        result["total_invoice"] = invoice_total
        difference = abs(extracted_total - invoice_total)
        result["total_difference"] = round(difference, 2)
        
        if difference <= 0.05:  # $0.05 tolerance for rounding
            result["total_match"] = True
        else:
            result["total_match"] = False
            result["issues"].append(
                f"Total mismatch: invoice shows ${invoice_total:.2f}, extracted ${extracted_total:.2f} (difference: ${difference:.2f})"
            )
    else:
        # No total on invoice — can't validate, mark as null (pass)
        result["total_match"] = None
    
    # Determine auto_confirm eligibility
    # Both checks must be True or None (pass), and at least one must be True (actually validated)
    item_check_ok = result["item_count_match"] in [True, None]
    total_check_ok = result["total_match"] in [True, None]
    at_least_one_validated = result["item_count_match"] is True or result["total_match"] is True
    
    if item_check_ok and total_check_ok and at_least_one_validated:
        result["auto_confirm"] = True
        result["valid"] = True
    else:
        result["auto_confirm"] = False
        result["valid"] = False

    # CHECK 3: Multi-page invoice detection — block auto-confirm if page_info says more pages exist
    page_info = data.get("page_info") or ""
    if page_info and data.get("_mime_type") != "application/pdf":
        import re
        m = re.search(r'(\d+)\s*of\s*(\d+)', page_info, re.IGNORECASE)
        if m:
            total_pages = int(m.group(2))
            if total_pages > 1:
                result["auto_confirm"] = False
                result["issues"].append(f"Multi-page invoice ({page_info}) — scan all pages before confirming")

    return result



def generate_thumbnail(image_path, thumbnail_dir='/opt/rednun/invoice_thumbnails', max_width=400):
    """
    Generate a thumbnail from an invoice image or PDF.
    
    Args:
        image_path: Path to the original invoice file
        thumbnail_dir: Directory to save thumbnails
        max_width: Maximum width of thumbnail in pixels
    
    Returns:
        Path to the generated thumbnail, or None if generation failed
    """
    import os
    from PIL import Image
    
    try:
        os.makedirs(thumbnail_dir, exist_ok=True)
        
        base_name = os.path.splitext(os.path.basename(image_path))[0]
        thumb_path = os.path.join(thumbnail_dir, f"{base_name}.jpg")
        
        # Skip if thumbnail already exists
        if os.path.exists(thumb_path):
            return thumb_path
        
        ext = os.path.splitext(image_path)[1].lower()
        
        if ext == '.pdf':
            # Convert first page of PDF to image
            from pdf2image import convert_from_path
            images = convert_from_path(image_path, first_page=1, last_page=1, dpi=300, poppler_path='/usr/bin')
            if images:
                img = images[0]
            else:
                logger.warning(f"Could not convert PDF to image: {image_path}")
                return None
        elif ext in ['.jpg', '.jpeg', '.png', '.webp']:
            # Open image file
            img = Image.open(image_path)
        else:
            logger.warning(f"Unsupported file type for thumbnail: {ext}")
            return None
        
        # Resize maintaining aspect ratio
        width, height = img.size
        if width > max_width:
            new_height = int((max_width / width) * height)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        # Convert to RGB if necessary (for PNG with transparency, etc.)
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        
        # Save as JPEG
        img.save(thumb_path, 'JPEG', quality=85, optimize=True)
        logger.info(f"Generated thumbnail: {thumb_path}")
        return thumb_path
        
    except Exception as e:
        logger.error(f"Failed to generate thumbnail for {image_path}: {e}")
        return None


def generate_csv_thumbnail(invoice_id, vendor_name, invoice_number, invoice_date,
                           total, item_count, source_label='CSV',
                           thumbnail_dir='/opt/rednun/invoice_thumbnails',
                           line_items=None, ship_to_address=None):
    """Generate a professional invoice-style image for CSV-imported invoices.

    Light background mimicking a real PDF invoice with vendor header,
    invoice details, line item table with alternating rows, and totals.
    Returns the thumbnail path and updates the invoice record.
    """
    import os
    from PIL import Image, ImageDraw, ImageFont

    try:
        os.makedirs(thumbnail_dir, exist_ok=True)
        thumb_path = os.path.join(thumbnail_dir, f"csv_{invoice_id}.jpg")

        if os.path.exists(thumb_path):
            return thumb_path

        items = line_items or []

        # ─── Fonts ────────────────────────────────────────────────
        _djv = "/usr/share/fonts/truetype/dejavu"
        try:
            f_vendor = ImageFont.truetype(f"{_djv}/DejaVuSans-Bold.ttf", 22)
            f_title  = ImageFont.truetype(f"{_djv}/DejaVuSans-Bold.ttf", 16)
            f_label  = ImageFont.truetype(f"{_djv}/DejaVuSans.ttf", 10)
            f_val    = ImageFont.truetype(f"{_djv}/DejaVuSans-Bold.ttf", 12)
            f_mono   = ImageFont.truetype(f"{_djv}/DejaVuSansMono.ttf", 10)
            f_mono_b = ImageFont.truetype(f"{_djv}/DejaVuSansMono-Bold.ttf", 10)
            f_total  = ImageFont.truetype(f"{_djv}/DejaVuSans-Bold.ttf", 14)
            f_small  = ImageFont.truetype(f"{_djv}/DejaVuSans.ttf", 9)
        except (OSError, IOError):
            f_vendor = f_title = f_label = f_val = f_mono = f_mono_b = f_total = f_small = ImageFont.load_default()

        # ─── Colors (light invoice theme) ─────────────────────────
        BG        = (255, 255, 255)    # white page
        TEXT      = (33, 37, 41)       # near-black
        MUTED     = (108, 117, 125)    # gray-600
        LIGHT     = (173, 181, 189)    # gray-500
        ACCENT    = (13, 110, 253)     # blue-600
        ACCENT_BG = (232, 240, 254)    # light blue tint
        BORDER    = (206, 212, 218)    # gray-300
        HDR_BG    = (233, 236, 239)    # gray-200
        ROW_ALT   = (248, 249, 250)    # gray-50
        TOTAL_BG  = (25, 135, 84)      # green-600
        TOTAL_TXT = (255, 255, 255)    # white on green
        DARK      = (52, 58, 64)       # gray-700

        # ─── Layout constants ─────────────────────────────────────
        W = 800
        MARGIN = 32
        ROW_H = 20
        HEADER_H = 150
        TABLE_HDR_H = 24
        FOOTER_H = 70

        n_items = len(items)
        table_h = TABLE_HDR_H + (n_items * ROW_H) + 4
        H = HEADER_H + table_h + FOOTER_H + MARGIN
        H = max(H, 280)

        img = Image.new('RGB', (W, H), BG)
        draw = ImageDraw.Draw(img)

        # ─── Top accent bar ───────────────────────────────────────
        draw.rectangle([0, 0, W, 4], fill=ACCENT)

        # ─── Header area ──────────────────────────────────────────
        y = 18

        # Vendor name (left)
        vn = (vendor_name or 'Unknown Vendor')[:45]
        draw.text((MARGIN, y), vn, fill=TEXT, font=f_vendor)

        # "INVOICE" label (right)
        inv_label = "INVOICE"
        lw = draw.textlength(inv_label, font=f_title)
        draw.text((W - MARGIN - lw, y + 4), inv_label, fill=ACCENT, font=f_title)
        y += 36

        # Thin rule under vendor name
        draw.line([(MARGIN, y), (W - MARGIN, y)], fill=BORDER, width=1)
        y += 12

        # Invoice details row
        col1_x = MARGIN
        col2_x = 260
        col3_x = 480

        draw.text((col1_x, y), "INVOICE #", fill=MUTED, font=f_label)
        draw.text((col2_x, y), "DATE", fill=MUTED, font=f_label)
        if ship_to_address:
            draw.text((col3_x, y), "SHIP TO", fill=MUTED, font=f_label)
        y += 14
        draw.text((col1_x, y), str(invoice_number or '—')[:25], fill=TEXT, font=f_val)
        draw.text((col2_x, y), str(invoice_date or '—')[:15], fill=TEXT, font=f_val)
        if ship_to_address:
            addr = str(ship_to_address)[:40]
            draw.text((col3_x, y), addr, fill=TEXT, font=f_val)
        y += 22

        try:
            total_str = f"${float(total):,.2f}"
        except (ValueError, TypeError):
            total_str = str(total or '$0.00')

        # Summary line
        stats = f"{n_items} items"
        draw.text((col1_x, y), stats, fill=MUTED, font=f_label)

        # Source badge (small, subtle)
        badge_text = source_label.upper()
        bw = draw.textlength(badge_text, font=f_small) + 12
        bx = col1_x + draw.textlength(stats, font=f_label) + 16
        draw.rounded_rectangle([bx, y - 1, bx + int(bw), y + 13], radius=3, fill=ACCENT_BG)
        draw.text((bx + 6, y + 1), badge_text, fill=ACCENT, font=f_small)

        # ─── Line items table ─────────────────────────────────────
        ty = HEADER_H

        # Column positions
        c_desc = MARGIN + 4
        c_pack = 440
        c_qty  = 530
        c_up   = 610
        c_ext  = 720

        # Table header
        draw.rectangle([MARGIN - 2, ty, W - MARGIN + 2, ty + TABLE_HDR_H], fill=HDR_BG)
        draw.line([(MARGIN - 2, ty), (W - MARGIN + 2, ty)], fill=BORDER, width=1)
        draw.line([(MARGIN - 2, ty + TABLE_HDR_H), (W - MARGIN + 2, ty + TABLE_HDR_H)], fill=BORDER, width=1)
        draw.text((c_desc, ty + 5), "DESCRIPTION", fill=DARK, font=f_mono_b)
        draw.text((c_pack, ty + 5), "PACK", fill=DARK, font=f_mono_b)
        draw.text((c_qty,  ty + 5), "QTY", fill=DARK, font=f_mono_b)
        draw.text((c_up,   ty + 5), "PRICE", fill=DARK, font=f_mono_b)
        draw.text((c_ext,  ty + 5), "TOTAL", fill=DARK, font=f_mono_b)
        ty += TABLE_HDR_H

        for idx, item in enumerate(items):
            row_bg = ROW_ALT if idx % 2 == 0 else BG
            draw.rectangle([MARGIN - 2, ty, W - MARGIN + 2, ty + ROW_H], fill=row_bg)

            name = str(item.get('product_name', ''))
            max_desc_w = c_pack - c_desc - 8
            if draw.textlength(name, font=f_mono) > max_desc_w:
                while len(name) > 10 and draw.textlength(name + '..', font=f_mono) > max_desc_w:
                    name = name[:-1]
                name = name.rstrip() + '..'

            draw.text((c_desc, ty + 4), name, fill=TEXT, font=f_mono)

            pack = str(item.get('pack_size') or item.get('unit') or '')[:12]
            draw.text((c_pack, ty + 4), pack, fill=MUTED, font=f_mono)

            qty = item.get('quantity', 0)
            try:
                qty_f = float(qty)
                qty_s = f"{qty_f:g}" if qty_f == int(qty_f) else f"{qty_f:.1f}"
            except (ValueError, TypeError):
                qty_s = str(qty)
            draw.text((c_qty, ty + 4), qty_s, fill=TEXT, font=f_mono)

            up = item.get('unit_price', 0)
            try:
                up_s = f"${float(up):,.2f}"
            except (ValueError, TypeError):
                up_s = str(up)
            up_w = draw.textlength(up_s, font=f_mono)
            draw.text((c_ext - 8 - up_w, ty + 4), up_s, fill=MUTED, font=f_mono)

            ext = item.get('total_price', 0)
            try:
                ext_s = f"${float(ext):,.2f}"
            except (ValueError, TypeError):
                ext_s = str(ext)
            ext_w = draw.textlength(ext_s, font=f_mono)
            draw.text((W - MARGIN - 4 - ext_w, ty + 4), ext_s, fill=TEXT, font=f_mono)

            ty += ROW_H

        # Table bottom border
        draw.line([(MARGIN - 2, ty), (W - MARGIN + 2, ty)], fill=BORDER, width=1)

        # ─── Totals footer ────────────────────────────────────────
        ty += 12
        # Total row with green background pill
        total_label = "TOTAL"
        tlw = draw.textlength(total_label, font=f_total)
        ttw = draw.textlength(total_str, font=f_total)
        pill_w = tlw + ttw + 40
        pill_x = W - MARGIN - int(pill_w)
        draw.rounded_rectangle([pill_x, ty - 2, W - MARGIN + 2, ty + 22], radius=4, fill=TOTAL_BG)
        draw.text((pill_x + 10, ty + 2), total_label, fill=TOTAL_TXT, font=f_total)
        draw.text((W - MARGIN - 8 - ttw, ty + 2), total_str, fill=TOTAL_TXT, font=f_total)

        img.save(thumb_path, 'JPEG', quality=92, optimize=True)
        logger.info(f"Generated CSV invoice image: {thumb_path} ({n_items} items, {W}x{H})")

        # Update invoice record with image
        try:
            conn = get_connection()
            conn.execute(
                "UPDATE scanned_invoices SET thumbnail_path = ?, image_path = ? WHERE id = ?",
                (thumb_path, thumb_path, invoice_id),
            )
            conn.commit()
            conn.close()
        except Exception as db_err:
            logger.error(f"Failed to update thumbnail path for invoice #{invoice_id}: {db_err}")

        return thumb_path

    except Exception as e:
        logger.error(f"Failed to generate CSV thumbnail for invoice #{invoice_id}: {e}")
        return None


def parse_iif_invoice(iif_data, location='dennis'):
    """Parse US Foods IIF (QuickBooks Interchange Format) file into standard invoice dict.
    IIF files contain perfect structured data — no OCR needed, zero errors."""
    if isinstance(iif_data, bytes):
        text = iif_data.decode('utf-8', errors='replace')
    else:
        text = iif_data
    lines = text.splitlines()

    # Parse header rows to get column indices
    trns_cols = {}
    spl_cols = {}
    for line in lines:
        parts = line.split('\t')
        if parts[0] == '!TRNS':
            trns_cols = {h: i for i, h in enumerate(parts)}
        elif parts[0] == '!SPL':
            spl_cols = {h: i for i, h in enumerate(parts)}

    if not spl_cols:
        raise ValueError("No !SPL header found — not a valid IIF file")

    invoice_date = None
    invoice_num = None
    total = 0.0
    line_items = []
    seen_trns = False

    for line in lines:
        parts = line.split('\t')
        if not parts:
            continue

        if parts[0] == 'ENDTRNS' and seen_trns:
            break  # Stop after first complete invoice (multi-invoice IIF safety)

        if parts[0] == 'TRNS':
            if seen_trns:
                break  # Second TRNS = second invoice — stop here
            seen_trns = True
            # Invoice header row
            try:
                date_idx = trns_cols.get('DATE', 2)
                date_raw = parts[date_idx] if len(parts) > date_idx else ''
                invoice_date = datetime.strptime(date_raw, '%m/%d/%y').strftime('%Y-%m-%d')
            except Exception:
                invoice_date = None
            num_idx = trns_cols.get('DOCNUM', 7)
            invoice_num = parts[num_idx] if len(parts) > num_idx else None
            amt_idx = trns_cols.get('AMOUNT', 6)
            try:
                total = abs(float(parts[amt_idx])) if len(parts) > amt_idx else 0.0
            except Exception:
                total = 0.0

        elif parts[0] == 'SPL':
            try:
                accnt = parts[spl_cols.get('ACCNT', 5)] if len(parts) > spl_cols.get('ACCNT', 5) else ''
                memo = parts[spl_cols.get('MEMO', 6)] if len(parts) > spl_cols.get('MEMO', 6) else ''
                price_str = parts[spl_cols.get('PRICE', 7)] if len(parts) > spl_cols.get('PRICE', 7) else '0'
                qty_str = parts[spl_cols.get('QNTY', 8)] if len(parts) > spl_cols.get('QNTY', 8) else '1'
                amount_str = parts[spl_cols.get('AMOUNT', 9)] if len(parts) > spl_cols.get('AMOUNT', 9) else '0'

                unit_price = float(price_str) if price_str else 0.0
                quantity = float(qty_str) if qty_str else 1.0
                total_price = float(amount_str) if amount_str else 0.0

                # Skip credits/returns (negative) and zero amounts
                if total_price <= 0:
                    continue

                # Parse product name from MEMO ("CODE:NAME" or just "NAME")
                if ':' in memo:
                    product_code, product_name = memo.split(':', 1)
                    product_code = product_code.strip()
                else:
                    product_code = ''
                    product_name = memo
                product_name = product_name.strip()
                if not product_name:
                    product_name = accnt  # fallback for fees

                # Map storage category to invoice category
                accnt_lower = accnt.lower()
                if any(k in accnt_lower for k in ['dry', 'refrigerat', 'frozen']):
                    category = 'FOOD'
                elif any(k in accnt_lower for k in ['tax', 'charge', 'expense', 'other']):
                    category = 'NON_COGS'
                else:
                    category = 'FOOD'

                # Refine category from product name
                name_lower = product_name.lower()
                if any(k in name_lower for k in ['glove', 'wrap', 'foil', 'liner', 'parchment', 'towel']):
                    category = 'KITCHEN_SUPPLIES'
                elif any(k in name_lower for k in ['container', 'bag ', 'tray', 'styro', 'to-go', 'togo']):
                    category = 'TOGO_SUPPLIES'
                elif any(k in name_lower for k in ['napkin', 'straw', 'cup ', 'candle']):
                    category = 'DR_SUPPLIES'
                elif any(k in name_lower for k in ['vodka', 'whiskey', 'rum', 'tequila', 'gin', 'bourbon', 'scotch', 'brandy', 'cordial', 'liqueur']):
                    category = 'LIQUOR'
                elif any(k in name_lower for k in ['beer', 'ale', 'ipa', 'lager', 'stout', 'cider', 'seltzer']):
                    category = 'BEER'
                elif any(k in name_lower for k in ['wine', 'champagne', 'prosecco']):
                    category = 'WINE'
                elif any(k in name_lower for k in ['soda', 'juice', 'coffee', 'tea ', 'water', 'energy drink']):
                    if 'cranb' not in name_lower:  # cranberry used in food/cocktails
                        category = 'NA_BEVERAGES'

                line_items.append({
                    'product_name': product_name,
                    'description': f'US Foods #{product_code}' if product_code else '',
                    'quantity': quantity,
                    'unit': 'CS',
                    'unit_price': unit_price,
                    'total_price': total_price,
                    'pack_size': None,
                    'category': category,
                })
            except Exception as e:
                logger.warning(f"IIF line parse error: {e}")
                continue

    logger.info(f"IIF parsed: invoice #{invoice_num}, {len(line_items)} items, ${total:.2f}")

    return {
        'vendor_name': 'US Foods, Inc.',
        'invoice_number': invoice_num,
        'invoice_date': invoice_date,
        'subtotal': total,
        'tax': 0.0,
        'total': total,
        'ship_to_address': None,
        'line_items': line_items,
        'notes': f'Parsed from IIF file — {len(line_items)} items, exact data',
        'total_line_items': len(line_items),
        'invoice_subtotal': total,
        'invoice_total': total,
        'page_info': None,
        'source': 'iif',
        'auto_confirmed': True,
        'confidence_score': 100,
    }


def parse_csv_invoice(csv_data, location=None):
    """Parse US Foods CSV Full format into standard invoice dict.
    CSV Full is structured data — no OCR needed, auto-confirmed at 100% confidence.

    Args:
        csv_data: Raw CSV text (string or bytes)
        location: Optional explicit location ('dennis' or 'chatham').
                  If not provided, auto-detected from ship-to address.

    Returns:
        Standard invoice dict compatible with save_invoice().
    """
    import csv
    import io

    if isinstance(csv_data, bytes):
        text = csv_data.decode('utf-8', errors='replace')
    else:
        text = csv_data

    reader = csv.reader(io.StringIO(text))
    header = next(reader)

    # Build column index map — handles duplicate column names (BillToStreet, ShipToStreet)
    col = {}
    for i, name in enumerate(header):
        name = name.strip()
        if name in col:
            col[name + '_2'] = i
        else:
            col[name] = i

    def get(row, name, default=''):
        idx = col.get(name)
        if idx is not None and idx < len(row):
            return row[idx].strip()
        return default

    rows = list(reader)
    if not rows:
        raise ValueError("CSV file has no data rows")

    # Read header-level info from first row
    first = rows[0]
    doc_type = get(first, 'DocumentType')
    doc_number = get(first, 'DocumentNumber')
    doc_date_raw = get(first, 'DocumentDate')
    customer_name = get(first, 'CustomerName')
    credit_memo_number = get(first, 'CreditMemoNumber')
    credit_memo_date = get(first, 'CreditMemoDate')
    net_amount = get(first, 'NetAmountAfter Adjustment')
    delivery_adj_str = get(first, 'DeliveryAdjustment', '0')

    # Determine invoice number based on document type
    is_credit = doc_type == 'CREDIT_MEMO'
    if is_credit and credit_memo_number:
        invoice_number = credit_memo_number
    else:
        invoice_number = doc_number

    # Parse date
    invoice_date = None
    try:
        if is_credit and credit_memo_date:
            invoice_date = datetime.strptime(credit_memo_date, '%Y/%m/%d').strftime('%Y-%m-%d')
        elif doc_date_raw:
            invoice_date = datetime.strptime(doc_date_raw, '%m/%d/%Y').strftime('%Y-%m-%d')
    except Exception:
        pass

    # Build ship-to address for location detection
    ship_to_parts = [
        get(first, 'ShipToName'),
        get(first, 'ShipToStreet'),
        get(first, 'ShipToCity'),
        get(first, 'ShipToState'),
        get(first, 'ShipToZip'),
    ]
    ship_to_address = ', '.join(p for p in ship_to_parts if p)

    # Auto-detect location if not explicitly provided
    if not location:
        location = detect_location_from_address(ship_to_address)
        if not location:
            cn = customer_name.lower()
            if 'chatham' in cn:
                location = 'chatham'
            elif 'dennis' in cn:
                location = 'dennis'
            else:
                location = 'dennis'

    # Parse line items
    line_items = []
    for row in rows:
        product_number = get(row, 'ProductNumber')
        description = get(row, 'ProductDescription')
        brand = get(row, 'Product Label')
        pack_size = get(row, 'PackingSize')
        qty_shipped_str = get(row, 'QtyShip', '0')
        unit_price_str = get(row, 'UnitPrice', '0')
        extended_str = get(row, 'ExtendedPrice', '0')
        pricing_unit = get(row, 'PricingUnit', 'CS')

        try:
            qty_shipped = float(qty_shipped_str) if qty_shipped_str else 0
        except ValueError:
            qty_shipped = 0
        try:
            unit_price = float(unit_price_str) if unit_price_str else 0
        except ValueError:
            unit_price = 0
        try:
            extended_price = float(extended_str) if extended_str else 0
        except ValueError:
            extended_price = 0

        if not product_number and not description:
            continue

        # Build product name: description is the primary name
        product_name = description

        # Categorize based on product name (same logic as IIF parser)
        category = 'FOOD'
        name_lower = (product_name or '').lower()
        if any(k in name_lower for k in ['glove', 'wrap', 'foil', 'liner', 'parchment', 'towel']):
            category = 'KITCHEN_SUPPLIES'
        elif any(k in name_lower for k in ['container', 'bag ', 'tray', 'styro', 'to-go', 'togo', 'box,']):
            category = 'TOGO_SUPPLIES'
        elif any(k in name_lower for k in ['napkin', 'straw', 'cup ', 'candle']):
            category = 'DR_SUPPLIES'
        elif any(k in name_lower for k in ['vodka', 'whiskey', 'rum', 'tequila', 'gin', 'bourbon', 'scotch', 'brandy', 'cordial', 'liqueur']):
            category = 'LIQUOR'
        elif any(k in name_lower for k in ['beer', 'ale', 'ipa', 'lager', 'stout', 'cider', 'seltzer']):
            category = 'BEER'
        elif any(k in name_lower for k in ['wine', 'champagne', 'prosecco']):
            category = 'WINE'
        elif any(k in name_lower for k in ['soda', 'juice', 'coffee', 'tea ', 'water', 'energy drink']):
            if 'cranb' not in name_lower:
                category = 'NA_BEVERAGES'

        line_items.append({
            'product_name': product_name,
            'description': f'US Foods #{product_number}' if product_number else '',
            'quantity': qty_shipped,
            'unit': pricing_unit,
            'unit_price': unit_price,
            'total_price': extended_price,
            'pack_size': pack_size,
            'category': category,
            'vendor_item_code': product_number,
        })

    # Add delivery adjustment as a NON_COGS line item if nonzero
    try:
        delivery_adj = float(delivery_adj_str) if delivery_adj_str else 0
    except ValueError:
        delivery_adj = 0
    if delivery_adj != 0:
        line_items.append({
            'product_name': 'Delivery Adjustment',
            'description': 'US Foods delivery adjustment',
            'quantity': 1,
            'unit': 'charge',
            'unit_price': delivery_adj,
            'total_price': delivery_adj,
            'pack_size': None,
            'category': 'NON_COGS',
            'vendor_item_code': None,
        })

    # Calculate total
    items_total = round(sum(it['total_price'] for it in line_items), 2)
    try:
        header_total = float(net_amount) if net_amount else 0
    except ValueError:
        header_total = 0
    total = header_total if header_total else items_total

    logger.info(f"CSV parsed: {'credit' if is_credit else 'invoice'} #{invoice_number}, "
                f"{len(line_items)} items, ${total:.2f} ({customer_name})")

    return {
        'vendor_name': 'US Foods, Inc.',
        'invoice_number': invoice_number,
        'invoice_date': invoice_date,
        'subtotal': total,
        'tax': 0.0,
        'total': total,
        'ship_to_address': ship_to_address,
        'line_items': line_items,
        'notes': f"Parsed from CSV Full — {len(line_items)} items, {'credit memo' if is_credit else 'invoice'}",
        'total_line_items': len(line_items),
        'invoice_subtotal': total,
        'invoice_total': total,
        'page_info': None,
        'source': 'csv',
        'auto_confirmed': True,
        'confidence_score': 100,
        'is_credit': is_credit,
    }


def parse_pfg_csv_invoice(csv_data, location=None):
    """Parse PFG (Performance Foodservice) CSV export into standard invoice dicts.

    PFG exports a SINGLE CSV containing ALL selected invoices' line items.
    Each row has the full invoice header repeated + one line item.
    Must group rows by Invoice Number and return a LIST of invoice dicts.

    CSV columns (40): Customer OpCo, Customer #, Customer Name, Address, City,
    State, Zip Code, Invoice Date, Invoice Number, Invoice Order Number,
    Invoice Type, PO Number, Route Number, Route Stop Number, Invoice Subtotal,
    Invoice Discount, Invoice Charges Fees, Invoice Total Tax, Invoice Total,
    Total Qty Ordered, Total Qty Shipped, Vendor #, Manufacturer Name,
    Manufacturer Product #, Category/Class, GTIN, Product #,
    Custom Product Number, Product Description, Custom Product Description,
    Brand, Pack Size, UOM, Printed Sequence, Net Price, Qty Ordered,
    Qty Shipped, Weight, Unit Price, Ext. Price

    Args:
        csv_data: Raw CSV text (string or bytes)
        location: Optional explicit location ('dennis' or 'chatham').

    Returns:
        List of standard invoice dicts compatible with save_invoice().
    """
    import csv as csv_mod
    import io

    if isinstance(csv_data, bytes):
        text = csv_data.decode('utf-8', errors='replace')
    else:
        text = csv_data

    reader = csv_mod.reader(io.StringIO(text))
    header = next(reader)

    # Build column index map
    col = {}
    for i, name in enumerate(header):
        col[name.strip()] = i

    def get(row, col_name, default=''):
        idx = col.get(col_name)
        if idx is not None and idx < len(row):
            return row[idx].strip()
        return default

    # Read all rows, group by invoice number
    from collections import defaultdict
    invoice_groups = defaultdict(list)
    for row in reader:
        if not row or len(row) < 10:
            continue
        inv_num = get(row, 'Invoice Number')
        if inv_num:
            invoice_groups[inv_num].append(row)

    results = []
    for inv_num, rows in invoice_groups.items():
        first = rows[0]

        # Invoice header fields (same across all rows in this group)
        customer_name = get(first, 'Customer Name')
        customer_opco = get(first, 'Customer OpCo')
        address = get(first, 'Address')
        city = get(first, 'City')
        state = get(first, 'State')
        zip_code = get(first, 'Zip Code')
        inv_date_str = get(first, 'Invoice Date')
        inv_type = get(first, 'Invoice Type')
        subtotal_str = get(first, 'Invoice Subtotal', '0')
        discount_str = get(first, 'Invoice Discount', '0')
        charges_str = get(first, 'Invoice Charges Fees', '0')
        tax_str = get(first, 'Invoice Total Tax', '0')
        total_str = get(first, 'Invoice Total', '0')

        # Parse date — PFG uses M/D/YYYY
        invoice_date = ''
        if inv_date_str:
            for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
                try:
                    invoice_date = datetime.strptime(inv_date_str.strip(), fmt).strftime('%Y-%m-%d')
                    break
                except ValueError:
                    continue
            if not invoice_date:
                invoice_date = inv_date_str  # Keep raw if unparseable

        is_credit = inv_type.lower() == 'credit' if inv_type else False

        # Parse totals
        def parse_float(s):
            try:
                return float(s.replace(',', '').replace('$', '')) if s else 0.0
            except ValueError:
                return 0.0

        subtotal = parse_float(subtotal_str)
        tax = parse_float(tax_str)
        total = parse_float(total_str)

        # Location detection from customer name (always detect from CSV data,
        # since PFG portal shows all invoices regardless of location filter)
        cn_lower = customer_name.lower()
        if 'chat' in cn_lower or 'chatham' in cn_lower or '02633' in zip_code:
            det_location = 'chatham'
        elif 'dennis' in cn_lower or '02639' in zip_code:
            det_location = 'dennis'
        elif location:
            det_location = location
        else:
            det_location = 'dennis'  # default

        ship_to_address = f"{customer_name}, {address}, {city}, {state} {zip_code}"

        # Parse line items
        line_items = []
        for row in rows:
            product_num = get(row, 'Product #')
            description = get(row, 'Product Description')
            brand = get(row, 'Brand')
            pack_size = get(row, 'Pack Size')
            uom = get(row, 'UOM', 'CS')
            qty_shipped = parse_float(get(row, 'Qty Shipped', '0'))
            unit_price = parse_float(get(row, 'Unit Price', '0'))
            ext_price = parse_float(get(row, 'Ext. Price', '0'))
            category_class = get(row, 'Category/Class')
            vendor_num = get(row, 'Vendor #')
            mfr_name = get(row, 'Manufacturer Name')

            if not product_num and not description:
                continue

            # Categorize based on product name
            category = 'FOOD'
            name_lower = (description or '').lower()
            if any(k in name_lower for k in ['glove', 'wrap', 'foil', 'liner', 'parchment', 'towel']):
                category = 'KITCHEN_SUPPLIES'
            elif any(k in name_lower for k in ['container', 'bag ', 'tray', 'styro', 'to-go', 'togo', 'box,']):
                category = 'TOGO_SUPPLIES'
            elif any(k in name_lower for k in ['napkin', 'straw', 'cup ', 'candle']):
                category = 'DR_SUPPLIES'
            elif any(k in name_lower for k in ['vodka', 'whiskey', 'rum', 'tequila', 'gin', 'bourbon', 'scotch', 'brandy', 'cordial', 'liqueur']):
                category = 'LIQUOR'
            elif any(k in name_lower for k in ['beer', 'ale', 'ipa', 'lager', 'stout', 'cider', 'seltzer']):
                category = 'BEER'
            elif any(k in name_lower for k in ['wine', 'champagne', 'prosecco']):
                category = 'WINE'
            elif any(k in name_lower for k in ['soda', 'juice', 'coffee', 'tea ', 'water', 'energy drink']):
                if 'cranb' not in name_lower:
                    category = 'NA_BEVERAGES'

            line_items.append({
                'product_name': description,
                'description': f'PFG #{product_num}' if product_num else '',
                'quantity': qty_shipped,
                'unit': uom,
                'unit_price': unit_price,
                'total_price': ext_price,
                'pack_size': pack_size,
                'category': category,
                'vendor_item_code': product_num,
            })

        items_total = round(sum(it['total_price'] for it in line_items), 2)
        if not total:
            total = items_total

        logger.info(f"PFG CSV parsed: {'credit' if is_credit else 'invoice'} #{inv_num}, "
                    f"{len(line_items)} items, ${total:.2f} ({customer_name})")

        results.append({
            'vendor_name': 'Performance Foodservice',
            'invoice_number': inv_num,
            'invoice_date': invoice_date,
            'subtotal': subtotal,
            'tax': tax,
            'total': total,
            'ship_to_address': ship_to_address,
            'line_items': line_items,
            'notes': f"Parsed from PFG CSV — {len(line_items)} items, {'credit' if is_credit else 'invoice'}",
            'total_line_items': len(line_items),
            'invoice_subtotal': subtotal,
            'invoice_total': total,
            'page_info': None,
            'source': 'csv',
            'auto_confirmed': True,
            'confidence_score': 100,
            'is_credit': is_credit,
            '_detected_location': det_location,
        })

    return results


def parse_vtinfo_csv_invoice(csv_data, filename=None, location=None):
    """Parse VTInfo (L. Knife / Colonial Wholesale) CSV into standard invoice dict.

    VTInfo CSVs contain ONE invoice per file. Invoice metadata (number, date,
    vendor, location) is encoded in the filename, NOT in the CSV data.

    Filename pattern: vtinfo_{vendor}_{location}_{invoicenum}_{YYYYMMDD}.csv
        vendor: 'lknife' or 'colonial'
        location: 'chatham' or 'dennis'

    CSV columns: ProductId, ItemDescription, UnitOfMeasure, UnitsPerCase,
    SellableUnitsPerCase, QuantityOrdered, QuantityFilled, QuantityOut,
    Price, Discount, Deposit, ExtendedDeposit, ExtendedPrice,
    RetailerUPC, PackageUPC, UnitUPC

    ExtendedPrice = (Price - Discount + Deposit) x QuantityFilled

    Args:
        csv_data: Raw CSV text (string or bytes)
        filename: Original filename for metadata extraction
        location: Optional explicit location override

    Returns:
        Standard invoice dict compatible with save_invoice().
    """
    import csv as csv_mod
    import io
    import re

    if isinstance(csv_data, bytes):
        text = csv_data.decode('utf-8', errors='replace')
    else:
        text = csv_data

    # Extract metadata from filename
    vendor_name = 'Colonial Wholesale'
    invoice_number = ''
    invoice_date = ''
    det_location = location or 'dennis'

    if filename:
        # vtinfo_colonial_chatham_542237_20260323.csv
        m = re.match(r'vtinfo_(lknife|colonial)_(chatham|dennis)_(\d+)_(\d{8})', filename)
        if m:
            vendor_code, loc, inv_num, date_str = m.groups()
            vendor_name = 'L. Knife & Son' if vendor_code == 'lknife' else 'Colonial Wholesale'
            det_location = loc
            invoice_number = inv_num
            try:
                invoice_date = datetime.strptime(date_str, '%Y%m%d').strftime('%Y-%m-%d')
            except ValueError:
                invoice_date = date_str

    reader = csv_mod.reader(io.StringIO(text))
    header = next(reader)

    col = {}
    for i, name in enumerate(header):
        col[name.strip()] = i

    def get(row, col_name, default=''):
        idx = col.get(col_name)
        if idx is not None and idx < len(row):
            return row[idx].strip()
        return default

    def parse_float(s):
        try:
            return float(s.replace(',', '').replace('$', '')) if s else 0.0
        except ValueError:
            return 0.0

    line_items = []
    for row in reader:
        if not row or len(row) < 5:
            continue

        product_id = get(row, 'ProductId')
        description = get(row, 'ItemDescription')
        uom = get(row, 'UnitOfMeasure', 'case')
        units_per_case = get(row, 'UnitsPerCase', '1')
        qty_filled = parse_float(get(row, 'QuantityFilled', '0'))
        price = parse_float(get(row, 'Price', '0'))
        discount = parse_float(get(row, 'Discount', '0'))
        deposit = parse_float(get(row, 'Deposit', '0'))
        ext_price = parse_float(get(row, 'ExtendedPrice', '0'))

        if not description:
            continue

        # Skip zero-quantity rows (unless it's a return with negative qty)
        if qty_filled == 0:
            continue

        # RETURNS / cooperage lines — negative qty, deposit refund only
        desc_upper = description.upper()
        is_return = qty_filled < 0 or 'RETURN' in desc_upper or 'COOPERAGE' in desc_upper or 'COOPERG' in desc_upper

        # Unit price = Price - Discount (product cost before deposit)
        unit_price = price - discount if price > 0 else 0.0

        # Categorize — these are beverage distributors
        name_lower = description.lower()
        if is_return:
            category = 'NON_COGS'
        elif any(k in name_lower for k in ['vodka', 'whiskey', 'rum', 'tequila', 'gin',
                                            'bourbon', 'scotch', 'brandy', 'cordial',
                                            'liqueur', 'vermouth', 'bitters', 'amaro']):
            category = 'LIQUOR'
        elif any(k in name_lower for k in ['wine', 'champagne', 'prosecco', 'pinot',
                                            'cabernet', 'chardonnay', 'sauvignon',
                                            'merlot', 'rosé', 'rose ', 'sangria']):
            category = 'WINE'
        elif any(k in name_lower for k in ['soda', 'juice', 'water', 'tea ', 'coffee',
                                            'energy', 'tonic', 'ginger ale', 'club soda',
                                            'red bull', 'coca cola', 'pepsi', 'sprite',
                                            'non-alc', 'non alc', 'n/a ', '0.0%',
                                            'athletic brew']):
            category = 'NA_BEVERAGES'
        else:
            # Default for beer/cider distributors
            category = 'BEER'

        # Pack size from UOM + UnitsPerCase
        pack_size = f"{units_per_case}/{uom}" if units_per_case != '1' else uom

        line_items.append({
            'product_name': description,
            'description': f'VTInfo #{product_id}' if product_id else '',
            'quantity': qty_filled,
            'unit': uom.upper() if uom else 'CS',
            'unit_price': unit_price,
            'total_price': ext_price,
            'pack_size': pack_size,
            'category': category,
            'vendor_item_code': product_id,
        })

    total = round(sum(it['total_price'] for it in line_items), 2)

    logger.info(f"VTInfo CSV parsed: {vendor_name} #{invoice_number}, "
                f"{len(line_items)} items, ${total:.2f} ({det_location})")

    return {
        'vendor_name': vendor_name,
        'invoice_number': invoice_number,
        'invoice_date': invoice_date,
        'subtotal': total,
        'tax': 0.0,
        'total': total,
        'ship_to_address': f"Red Nun {'Chatham' if det_location == 'chatham' else 'Dennis Port'}",
        'line_items': line_items,
        'notes': f"Parsed from VTInfo CSV — {len(line_items)} items",
        'total_line_items': len(line_items),
        'invoice_subtotal': total,
        'invoice_total': total,
        'page_info': None,
        'source': 'csv',
        'auto_confirmed': True,
        'confidence_score': 100,
        'is_credit': total < 0,
        '_detected_location': det_location,
    }


# Vendor name normalization — maps common OCR variants to canonical names
_VENDOR_ALIASES = {
    'us foods': 'US Foods',
    'us foods inc': 'US Foods',
    'us foods, inc': 'US Foods',
    'usfoods': 'US Foods',
    'performance foodservice': 'Performance Foodservice',
    'performance food service': 'Performance Foodservice',
    'performance foodservice inc': 'Performance Foodservice',
    'performance foodservice, inc': 'Performance Foodservice',
    "southern glazer's": "Southern Glazer's",
    "southern glazers": "Southern Glazer's",
    "southern glazer's wine & spirits": "Southern Glazer's",
    "southern glazer's wine and spirits": "Southern Glazer's",
    "southern glazer's beverage company": "Southern Glazer's Beverage Company",
    "southern glazer's beverage co": "Southern Glazer's Beverage Company",
    "southern glazer's beverage company": "Southern Glazer's Beverage Company",
    "southern glazer's beverage co": "Southern Glazer's Beverage Company",
    'martignetti companies': 'Martignetti Companies',
    'martignetti companies inc': 'Martignetti Companies',
    'artignetti companies': 'Martignetti Companies',
    'artignetti': 'Martignetti Companies',
    'tignetti companies': 'Martignetti Companies',
    'martignetti': 'Martignetti Companies',
    'l. knife & son': 'L. Knife & Son, Inc.',
    'l. knife & son, inc': 'L. Knife & Son, Inc.',
    'l. knife & son, inc.': 'L. Knife & Son, Inc.',
    'l knife & son': 'L. Knife & Son, Inc.',
    'knife & son': 'L. Knife & Son, Inc.',
    'colonial wholesale': 'Colonial Wholesale Beverage',
    'colonial wholesale bev': 'Colonial Wholesale Beverage',
    'colonial wholesale beverage': 'Colonial Wholesale Beverage',
    'unifirst': 'UniFirst',
    'uni first': 'UniFirst',
    'unifirst corporation': 'UniFirst',
    'cintas': 'Cintas',
    'cintas corp': 'Cintas',
    'cintas corporation': 'Cintas',
}

def _normalize_vendor_name(name):
    """Normalize vendor name to canonical form (handles OCR variants like 'US Foods, Inc.' vs 'US Foods')."""
    if not name:
        return 'Unknown'
    key = name.strip().lower().rstrip('.')
    if key in _VENDOR_ALIASES:
        return _VENDOR_ALIASES[key]
    # Also try without trailing punctuation/suffixes
    for suffix in [', inc', ' inc', ', llc', ' llc', ', corp', ' corp']:
        stripped = key.rstrip('.').removesuffix(suffix).strip()
        if stripped in _VENDOR_ALIASES:
            return _VENDOR_ALIASES[stripped]
    return name.strip()


def save_invoice(location, data, image_path=None, raw_json=None, validation_data=None):
    """
    Save extracted invoice data to the database.

    Args:
        location: 'dennis' or 'chatham'
        data: Extracted invoice dict from Claude
        image_path: Path to stored image file
        raw_json: Raw JSON string from Claude for debugging

    Returns:
        invoice_id or dict with {"duplicate": True, ...} if duplicate found
    """
    conn = get_connection()
    cursor = conn.cursor()

    vendor = _normalize_vendor_name(data.get("vendor_name", "Unknown"))
    inv_num = data.get("invoice_number", "")
    inv_date = data.get("invoice_date", "")
    category = categorize_vendor(vendor)

    if vendor == "UniFirst":
        logger.info(f"UniFirst invoice detected — all items will be NON_COGS (invoice #{inv_num})")

    # Check for duplicate invoice
    if vendor and inv_num and inv_date:
        dup_check = cursor.execute("""
            SELECT id, vendor_name, invoice_number, invoice_date, total, status
            FROM scanned_invoices
            WHERE vendor_name = ? AND invoice_number = ? AND invoice_date = ?
            LIMIT 1
        """, (vendor, inv_num, inv_date)).fetchone()

        if dup_check:
            logger.warning(f"Duplicate invoice detected: {vendor} #{inv_num} on {inv_date} (existing ID: {dup_check['id']})")
            conn.close()
            return {
                "duplicate": True,
                "existing_id": dup_check["id"],
                "existing_invoice": dict(dup_check)
            }

    # Generate thumbnail if image provided
    thumbnail_path = None
    if image_path:
        thumbnail_path = generate_thumbnail(image_path)

    # Session 29: calculate discrepancy before save
    line_items = data.get("line_items", [])
    line_items_sum = round(sum(float(it.get("total_price", 0) or 0) for it in line_items), 2)
    stated_total = float(data.get("total", 0) or 0)
    tax_val = float(data.get("tax", 0) or 0)
    calculated_total = round(line_items_sum + tax_val, 2)
    if stated_total:
        discrepancy_val = round(stated_total - calculated_total, 2)
        needs_reconciliation_val = 1 if abs(discrepancy_val) >= 0.02 else 0
    else:
        discrepancy_val = None
        needs_reconciliation_val = 1

    cursor.execute("""
        INSERT INTO scanned_invoices
        (location, vendor_name, invoice_number, invoice_date,
         subtotal, tax, total, category, status, notes,
         image_path, thumbnail_path, raw_extraction, created_at, confidence_score, is_low_confidence,
         validation_json, auto_confirmed, source, discrepancy, needs_reconciliation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        location,
        vendor,
        data.get("invoice_number"),
        data.get("invoice_date"),
        data.get("subtotal", 0) or 0,
        data.get("tax", 0) or 0,
        data.get("total", 0) or 0,
        category,
        data.get("notes"),
        image_path,
        thumbnail_path,
        raw_json or json.dumps(data),
        datetime.now().isoformat(),
        data.get("confidence_score", 100),
        1 if data.get("is_low_confidence", False) else 0,
        data.get("validation_json"),
        1 if data.get("auto_confirmed", False) else 0,
        data.get("source", "scanned"),
        discrepancy_val,
        needs_reconciliation_val,
    ))

    invoice_id = cursor.lastrowid

    # Save line items and check for price spikes
    for item in data.get("line_items", []):
        product_name = item.get("product_name", "")
        unit_price = float(item.get("unit_price", 0) or 0)
        price_change_pct = 0
        is_spike = 0

        # Check for price spike (20%+ increase)
        if product_name and unit_price > 0:
            prev_price_row = cursor.execute("""
                SELECT sii.unit_price
                FROM scanned_invoice_items sii
                JOIN scanned_invoices si ON sii.invoice_id = si.id
                WHERE sii.product_name = ?
                  AND si.vendor_name = ?
                  AND si.invoice_date < ?
                  AND sii.unit_price > 0
                ORDER BY si.invoice_date DESC
                LIMIT 1
            """, (product_name, vendor, inv_date)).fetchone()

            if prev_price_row:
                prev_price = prev_price_row[0]
                if prev_price > 0:
                    price_change_pct = ((unit_price - prev_price) / prev_price) * 100
                    is_spike = 1 if price_change_pct >= 20 else 0

        cursor.execute("""
            INSERT INTO scanned_invoice_items
            (invoice_id, product_name, description, quantity, unit,
             unit_price, total_price, category_type, price_change_pct, is_price_spike,
             pack_size, vendor_item_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            invoice_id,
            product_name,
            item.get("description"),
            item.get("quantity", 0) or 0,
            item.get("unit"),
            unit_price,
            item.get("total_price", 0) or 0,
            item.get("category") or item.get("category_type") or category,  # Item category first, vendor fallback
            round(price_change_pct, 1),
            is_spike,
            item.get("pack_size"),
            item.get("vendor_item_code"),
        ))

    conn.commit()
    conn.close()

    logger.info(f"Saved invoice #{invoice_id}: {vendor} ${data.get('total', 0)} "
                f"({len(data.get('line_items', []))} items)")
    return invoice_id


def confirm_invoice(invoice_id, updated_data=None):
    """
    Confirm an invoice after review. Optionally update with corrected data.
    Also updates product price history.
    """
    conn = get_connection()
    cursor = conn.cursor()

    if updated_data:
        # Update the invoice with corrections
        cursor.execute("""
            UPDATE scanned_invoices
            SET vendor_name = ?,
                invoice_number = ?,
                invoice_date = ?,
                subtotal = ?,
                tax = ?,
                total = ?,
                category = ?,
                notes = ?,
                location = COALESCE(?, location),
                status = 'confirmed',
                confirmed_at = ?,
                needs_reconciliation = 0,
                discrepancy = 0.0
            WHERE id = ?
        """, (
            updated_data.get("vendor_name"),
            updated_data.get("invoice_number"),
            updated_data.get("invoice_date"),
            updated_data.get("subtotal", 0),
            updated_data.get("tax", 0),
            updated_data.get("total", 0),
            categorize_vendor(updated_data.get("vendor_name")),
            updated_data.get("notes"),
            updated_data.get("location"),
            datetime.now().isoformat(),
            invoice_id,
        ))

        # Update line items if provided
        if "line_items" in updated_data:
            cursor.execute("DELETE FROM scanned_invoice_items WHERE invoice_id = ?",
                           (invoice_id,))
            for item in updated_data["line_items"]:
                cursor.execute("""
                    INSERT INTO scanned_invoice_items
                    (invoice_id, product_name, description, quantity, unit,
                     unit_price, total_price, category_type, pack_size, vendor_item_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    invoice_id,
                    item.get("product_name"),
                    item.get("description"),
                    item.get("quantity", 0),
                    item.get("unit"),
                    item.get("unit_price", 0),
                    item.get("total_price", 0),
                    item.get("category") or item.get("category_type") or categorize_vendor(updated_data.get("vendor_name")),
                    item.get("pack_size"),
                    item.get("vendor_item_code"),
                ))
    else:
        cursor.execute("""
            UPDATE scanned_invoices
            SET status = 'confirmed', confirmed_at = ?,
                needs_reconciliation = 0, discrepancy = 0.0
            WHERE id = ?
        """, (datetime.now().isoformat(), invoice_id))

    # Update product price history
    inv = cursor.execute(
        "SELECT vendor_name, location, invoice_date FROM scanned_invoices WHERE id = ?",
        (invoice_id,)
    ).fetchone()

    items = cursor.execute(
        "SELECT * FROM scanned_invoice_items WHERE invoice_id = ?",
        (invoice_id,)
    ).fetchall()

    for item in items:
        if item["product_name"] and item["unit_price"] and item["unit_price"] > 0:
            cursor.execute("""
                INSERT INTO product_prices
                (product_name, vendor_name, location, unit_price, unit,
                 invoice_date, invoice_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                item["product_name"],
                inv["vendor_name"],
                inv["location"],
                item["unit_price"],
                item["unit"],
                inv["invoice_date"],
                invoice_id,
            ))

    conn.commit()

    # Auto-populate Product Setup fields from confirmed line items
    try:
        populate_product_setup_from_items(invoice_id, conn)
        conn.commit()
    except Exception as e:
        logger.warning(f"Product Setup auto-populate failed for invoice #{invoice_id}: {e}")

    # Legacy: upsert_product_costing_from_items() disabled — Session 35 consolidation.
    # Product prices now flow through: process_invoice_items() → vendor_items → products
    # Old product_costing table preserved but no longer written to on confirm.
    # try:
    #     upsert_product_costing_from_items(invoice_id, conn)
    #     conn.commit()
    # except Exception as e:
    #     logger.warning(f"Product costing upsert failed for invoice #{invoice_id}: {e}")

    # Auto-link vendor items using existing mappings, then fuzzy-match new ones
    try:
        from routes.product_mapping_routes import apply_existing_links, auto_match_vendor_items
        apply_existing_links(invoice_id, conn)
        conn.commit()
        auto_match_vendor_items(invoice_id=invoice_id)
    except Exception as e:
        logger.warning(f"Vendor item auto-link failed for invoice #{invoice_id}: {e}")

    # ── Bill Pay: set due_date, balance, payment_status on confirm ──
    try:
        inv_refresh = cursor.execute(
            "SELECT vendor_name, invoice_date, total FROM scanned_invoices WHERE id = ?",
            (invoice_id,)
        ).fetchone()
        if inv_refresh and inv_refresh["total"]:
            total = float(inv_refresh["total"])
            inv_date = inv_refresh["invoice_date"]
            vendor = inv_refresh["vendor_name"]

            # Look up vendor payment terms
            due_date = None
            bp_row = cursor.execute(
                "SELECT payment_term_type, payment_term_days, payment_term_day_of_month "
                "FROM vendor_bill_pay WHERE vendor_name = ?",
                (vendor,)
            ).fetchone()

            if inv_date:
                from datetime import timedelta
                try:
                    inv_dt = datetime.strptime(inv_date, "%Y-%m-%d")
                except ValueError:
                    inv_dt = None

                if inv_dt:
                    if bp_row and bp_row["payment_term_type"] == "net_days" and bp_row["payment_term_days"]:
                        due_date = (inv_dt + timedelta(days=bp_row["payment_term_days"])).strftime("%Y-%m-%d")
                    elif bp_row and bp_row["payment_term_type"] == "day_of_month" and bp_row["payment_term_day_of_month"]:
                        dom = bp_row["payment_term_day_of_month"]
                        import calendar
                        year, month = inv_dt.year, inv_dt.month
                        if inv_dt.day >= dom:
                            month += 1
                            if month > 12:
                                month = 1
                                year += 1
                        max_day = calendar.monthrange(year, month)[1]
                        due_date = f"{year}-{month:02d}-{min(dom, max_day):02d}"
                    elif bp_row and bp_row["payment_term_type"] == "not_specified":
                        due_date = inv_date  # due on receipt
                    else:
                        due_date = (inv_dt + timedelta(days=30)).strftime("%Y-%m-%d")  # default net 30

            cursor.execute("""
                UPDATE scanned_invoices
                SET due_date = COALESCE(due_date, ?),
                    balance = COALESCE(balance, ?),
                    amount_paid = COALESCE(amount_paid, 0),
                    payment_status = COALESCE(
                        CASE WHEN payment_status IN ('paid') THEN payment_status ELSE 'unpaid' END,
                        'unpaid'
                    )
                WHERE id = ?
            """, (due_date, total, invoice_id))
            conn.commit()
    except Exception as e:
        logger.warning(f"Bill pay fields update failed for invoice #{invoice_id}: {e}")

    conn.close()
    logger.info(f"Invoice #{invoice_id} confirmed with {len(items)} items")
    return True


def parse_pack_size(raw):
    """
    Parse a pack size string into (case_pack_size, contains_qty, contains_unit).

    Examples:
      "4/5 LB"    → (4,   5.0,  'LB')   — 4 bags × 5 lb each
      "20/8 OZ"   → (20,  8.0,  'OZ')   — 20 portions × 8 oz
      "6/24/1 OZ" → (144, 1.0,  'OZ')   — 6×24=144 pieces × 1 oz
      "25 LB"     → (1,   25.0, 'LB')   — 1 unit of 25 lb
      "2000 EA"   → (1,   2000, 'EA')
      "4/1 GA"    → (4,   1.0,  'GA')
      "6/#10 CN"  → (6,   10.0, 'CN')   — strip # prefix

    Returns (None, None, None) on parse failure.
    """
    if not raw:
        return None, None, None
    raw = raw.strip().upper()

    # Match: numbers (possibly /#10 style) followed by optional 2-4 char unit
    m = re.match(r'^([\d#.]+(?:/[\d#.]+)*)\s*([A-Z]{2,4})?$', raw)
    if not m:
        return None, None, None

    nums_str = m.group(1)
    unit = (m.group(2) or '').strip() or None

    def _to_float(s):
        return float(s.lstrip('#'))

    try:
        parts = [_to_float(n) for n in nums_str.split('/')]
    except ValueError:
        return None, None, None

    if len(parts) == 1:
        # "25 LB" → 1 unit of 25
        return 1, parts[0], unit
    elif len(parts) == 2:
        # "4/5 LB" → 4 × 5
        return int(parts[0]), parts[1], unit
    elif len(parts) == 3:
        # "6/24/1 OZ" → 6×24=144 × 1
        return int(parts[0] * parts[1]), parts[2], unit
    else:
        return None, None, None


def populate_product_setup_from_items(invoice_id, conn):
    """
    Create or update products from a just-confirmed invoice's line items.

    Rules:
      - If product already exists: update current_price always; fill other fields only
        if currently NULL/empty (don't overwrite manually-set values).
      - If product does NOT exist: INSERT a new row with all available data.
      - product_name_map: used when present; if absent, the raw product name IS the
        canonical name and a new mapping entry is recorded for future invoices.

    This handles both:
      - Fresh databases (clean slate): creates products from scratch
      - Existing databases: updates prices and fills gaps
    """
    inv = conn.execute(
        "SELECT vendor_name, invoice_date, category, location FROM scanned_invoices WHERE id = ?",
        (invoice_id,)
    ).fetchone()
    if not inv:
        return

    vendor = inv["vendor_name"]
    vendor_category = inv["category"] or "OTHER"
    location = inv["location"] or "dennis"
    
    # Look up vendor ID
    vendor_row = conn.execute(
        "SELECT id FROM vendors WHERE LOWER(name) = LOWER(?) LIMIT 1",
        (vendor,)
    ).fetchone()
    vendor_id = vendor_row["id"] if vendor_row else None
    
    items = conn.execute(
        """SELECT product_name, unit, unit_price, pack_size, category_type
           FROM scanned_invoice_items WHERE invoice_id = ?""",
        (invoice_id,)
    ).fetchall()

    created = 0
    updated = 0

    for item in items:
        if not item["product_name"] or not item["unit_price"] or item["unit_price"] <= 0:
            continue

        raw_name = item["product_name"].strip()

        # Resolve canonical name via product_name_map; fall back to raw name
        mapping = conn.execute(
            """SELECT canonical_name FROM product_name_map
               WHERE LOWER(source_name) = LOWER(?) AND canonical_name IS NOT NULL LIMIT 1""",
            (raw_name,)
        ).fetchone()
        canonical = mapping["canonical_name"] if mapping else raw_name

        # Determine category: item-level first, vendor-level fallback
        item_cat = item["category_type"] or vendor_category

        case_sz, qty, qty_unit = parse_pack_size(item["pack_size"])

        # Check if product already exists in products table
        prod = conn.execute(
            "SELECT id FROM products WHERE LOWER(name) = LOWER(?)",
            (canonical,)
        ).fetchone()

        if prod:
            # UPDATE — always refresh price; fill other fields only if currently empty
            conn.execute("""
                UPDATE products SET
                    current_price = ?,
                    unit = CASE WHEN unit IS NULL OR unit = '' THEN ? ELSE unit END,
                    preferred_vendor_id = CASE WHEN preferred_vendor_id IS NULL AND ? IS NOT NULL
                                               THEN ? ELSE preferred_vendor_id END,
                    pack_size = CASE WHEN pack_size IS NULL THEN ? ELSE pack_size END,
                    pack_unit = CASE WHEN pack_unit IS NULL OR pack_unit = '' THEN ? ELSE pack_unit END,
                    category = CASE WHEN category IS NULL OR category = '' THEN ? ELSE category END,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (
                item["unit_price"],
                item["unit"],
                vendor_id, vendor_id,
                case_sz,
                qty_unit,
                item_cat,
                prod["id"],
            ))
            updated += 1
        else:
            # INSERT — create new product from invoice data
            try:
                conn.execute("""
                    INSERT INTO products
                    (name, category, unit, pack_size, pack_unit, preferred_vendor_id,
                     current_price, location, active, setup_complete, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, datetime('now'), datetime('now'))
                """, (
                    canonical,
                    item_cat,
                    item["unit"],
                    case_sz,
                    qty_unit,
                    vendor_id,
                    item["unit_price"],
                    location,
                ))
                # Register in product_name_map so future invoices find this product
                if not mapping:
                    conn.execute("""
                        INSERT OR IGNORE INTO product_name_map
                        (source_name, source_table, canonical_name, confidence, verified)
                        VALUES (?, 'scanned_invoice_items', ?, 1.0, 0)
                    """, (raw_name, canonical))
                created += 1
            except Exception as e:
                logger.warning(f"Could not insert product '{canonical}': {e}")

        logger.info(
            f"Product Setup: {'created' if not prod else 'updated'} '{canonical}': "
            f"price=${item['unit_price']:.2f}, unit={item['unit']}, pack={item['pack_size']}"
        )

    logger.info(f"Invoice #{invoice_id}: {created} products created, {updated} updated in Product Setup")


def upsert_product_costing_from_items(invoice_id, conn):
    """
    For each confirmed invoice line-item, upsert into product_costing.
    - New products: insert with case_price and vendor_name
    - Existing products: update case_price; if units_per_case is set, recalculate cost_per_recipe_unit
    - Follow vendor_item_links to also update the linked canonical product
    """
    inv = conn.execute(
        "SELECT vendor_name, invoice_date FROM scanned_invoices WHERE id = ?",
        (invoice_id,)
    ).fetchone()
    if not inv:
        return

    vendor = inv["vendor_name"]
    inv_date = inv["invoice_date"] or ""
    items = conn.execute(
        "SELECT product_name, unit_price FROM scanned_invoice_items WHERE invoice_id = ?",
        (invoice_id,)
    ).fetchall()

    upserted = 0
    canon_updated = 0
    for item in items:
        if not item["product_name"] or not item["unit_price"] or item["unit_price"] <= 0:
            continue

        raw_name = item["product_name"].strip()

        # Resolve canonical name via product_name_map
        mapping = conn.execute(
            "SELECT canonical_name FROM product_name_map WHERE LOWER(source_name) = LOWER(?) AND canonical_name IS NOT NULL LIMIT 1",
            (raw_name,)
        ).fetchone()
        canonical = mapping["canonical_name"] if mapping else raw_name

        conn.execute("""
            INSERT INTO product_costing (product_name, vendor_name, case_price, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(product_name) DO UPDATE SET
                case_price = excluded.case_price,
                vendor_name = COALESCE(excluded.vendor_name, product_costing.vendor_name),
                cost_per_recipe_unit = CASE
                    WHEN product_costing.units_per_case IS NOT NULL AND product_costing.units_per_case > 0
                    THEN ROUND(excluded.case_price / product_costing.units_per_case, 4)
                    ELSE product_costing.cost_per_recipe_unit
                END,
                updated_at = datetime('now')
        """, (canonical, vendor, item["unit_price"]))
        upserted += 1

        # Follow vendor_item_links to update the linked canonical product
        link = conn.execute(
            "SELECT canonical_product_name FROM vendor_item_links "
            "WHERE LOWER(TRIM(vendor_item_name)) = LOWER(TRIM(?)) LIMIT 1",
            (raw_name,)
        ).fetchone()
        if not link:
            continue
        linked_canon = link["canonical_product_name"]
        # Skip if the link points to itself (self-mapped) or same as already upserted
        if linked_canon.lower().strip() == raw_name.lower().strip():
            continue
        if linked_canon.lower().strip() == canonical.lower().strip():
            continue

        # Only update if this invoice is the newest for this vendor item
        newest = conn.execute("""
            SELECT MAX(si.invoice_date) as max_date
            FROM scanned_invoice_items sii
            JOIN scanned_invoices si ON sii.invoice_id = si.id
            WHERE LOWER(TRIM(sii.product_name)) = LOWER(TRIM(?))
            AND si.status = 'confirmed'
        """, (raw_name,)).fetchone()
        if newest and newest["max_date"] and newest["max_date"] > inv_date:
            continue  # A newer invoice exists, don't overwrite

        conn.execute("""
            UPDATE product_costing SET
                case_price = ?,
                vendor_name = COALESCE(?, vendor_name),
                cost_per_recipe_unit = CASE
                    WHEN units_per_case IS NOT NULL AND units_per_case > 0
                    THEN ROUND(? / units_per_case, 4)
                    ELSE cost_per_recipe_unit
                END,
                updated_at = datetime('now')
            WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))
        """, (item["unit_price"], vendor, item["unit_price"], linked_canon))
        canon_updated += 1

    logger.info(f"Invoice #{invoice_id}: {upserted} products upserted, {canon_updated} canonicals updated via vendor_item_links")


def delete_invoice(invoice_id):
    """Delete a pending invoice."""
    conn = get_connection()
    conn.execute("DELETE FROM scanned_invoice_items WHERE invoice_id = ?", (invoice_id,))
    conn.execute("DELETE FROM scanned_invoices WHERE id = ?", (invoice_id,))
    conn.commit()
    conn.close()


def get_invoice(invoice_id):
    """Get a single invoice with its line items."""
    conn = get_connection()
    inv = conn.execute("SELECT * FROM scanned_invoices WHERE id = ?",
                       (invoice_id,)).fetchone()
    if not inv:
        conn.close()
        return None

    items = conn.execute(
        "SELECT * FROM scanned_invoice_items WHERE invoice_id = ? ORDER BY id",
        (invoice_id,)
    ).fetchall()
    conn.close()

    result = dict(inv)
    line_items = [dict(i) for i in items]
    # Compute per-line math validation on the fly
    vendor_name = (result.get("vendor_name") or "").lower()
    is_beer_vendor = any(v in vendor_name for v in ["colonial", "l. knife", "craft collective"])
    line_item_errors = 0
    for li in line_items:
        qty = float(li.get("quantity", 0) or 0)
        up = float(li.get("unit_price", 0) or 0)
        tp = float(li.get("total_price", 0) or 0)
        expected = round(qty * up, 2)
        diff = abs(expected - tp)
        if diff > 0.02 and qty > 0 and up > 0:
            # Beer distributors include deposits in total_price (kegs=$30, bottles/cans=$0.05-$0.10 each)
            # Don't flag as math error if total > expected (deposit added on top)
            if is_beer_vendor and tp > expected:
                li["math_error"] = False
            else:
                li["math_error"] = True
                line_item_errors += 1
        else:
            li["math_error"] = False
    result["line_items"] = line_items
    result["line_item_errors"] = line_item_errors
    return result


def get_invoices(location=None, status=None, start_date=None, end_date=None, limit=50):
    """Get scanned invoices."""
    conn = get_connection()
    where_s = []
    params_s = []
    if location:
        where_s.append("location = ?")
        params_s.append(location)
    if status:
        where_s.append("status = ?")
        params_s.append(status)
    if start_date:
        where_s.append("invoice_date >= ?")
        params_s.append(start_date)
    if end_date:
        where_s.append("invoice_date <= ?")
        params_s.append(end_date)
    ws = "WHERE " + " AND ".join(where_s) if where_s else ""
    sql = f"""
        SELECT id, NULL as order_id, location, vendor_name, invoice_number, invoice_date,
               total, category, status, created_at, confirmed_at, COALESCE(source, 'scanned') as source,
               COALESCE(payment_status, 'unpaid') as payment_status,
               paid_date, payment_method, image_path, auto_confirmed,
               COALESCE(discrepancy, 0.0) as discrepancy,
               COALESCE(needs_reconciliation, 0) as needs_reconciliation
        FROM scanned_invoices {ws}
        ORDER BY invoice_date DESC
        LIMIT ?
    """
    rows = conn.execute(sql, params_s + [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_invoice_paid(invoice_id, paid_date, payment_method, payment_reference=None):
    """Mark a confirmed invoice as paid."""
    conn = get_connection()
    inv = conn.execute(
        "SELECT id, status FROM scanned_invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if not inv:
        conn.close()
        raise ValueError("Invoice not found")
    if inv["status"] != "confirmed":
        conn.close()
        raise ValueError("Only confirmed invoices can be marked as paid")
    conn.execute(
        """UPDATE scanned_invoices
           SET payment_status = 'paid', paid_date = ?,
               payment_method = ?, payment_reference = ?
           WHERE id = ?""",
        (paid_date, payment_method, payment_reference, invoice_id),
    )
    conn.commit()
    conn.close()


def get_outstanding_invoices(location=None):
    """Return unpaid confirmed scanned invoices grouped by vendor."""
    conn = get_connection()
    sql = """
        SELECT id, vendor_name, invoice_number, invoice_date, total, location
        FROM scanned_invoices
        WHERE status = 'confirmed'
          AND (payment_status = 'unpaid' OR payment_status IS NULL)
    """
    params = []
    if location:
        sql += " AND LOWER(location) = LOWER(?)"
        params.append(location)
    sql += " ORDER BY vendor_name, invoice_date"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    vendors = {}
    for row in rows:
        vendor = row["vendor_name"] or "Unknown"
        if vendor not in vendors:
            vendors[vendor] = {
                "vendor": vendor,
                "invoice_count": 0,
                "total_outstanding": 0.0,
                "invoices": [],
            }
        vendors[vendor]["invoice_count"] += 1
        vendors[vendor]["total_outstanding"] += row["total"] or 0.0
        vendors[vendor]["invoices"].append({
            "id": row["id"],
            "invoice_number": row["invoice_number"],
            "date": row["invoice_date"],
            "total": row["total"],
            "location": row["location"],
        })

    result = sorted(vendors.values(), key=lambda v: v["total_outstanding"], reverse=True)
    for v in result:
        v["total_outstanding"] = round(v["total_outstanding"], 2)
    return result


def get_payment_summary(location=None):
    """Return payment summary: total outstanding, paid last 30d, by vendor."""
    conn = get_connection()
    loc_clause = ""
    params = []
    if location:
        loc_clause = " AND LOWER(location) = LOWER(?)"
        params = [location]
    outstanding = conn.execute(f"""
        SELECT COALESCE(SUM(total), 0) as total
        FROM scanned_invoices
        WHERE status = 'confirmed'
          AND (payment_status = 'unpaid' OR payment_status IS NULL)
          {loc_clause}
    """, params).fetchone()
    paid_30d = conn.execute(f"""
        SELECT COALESCE(SUM(total), 0) as total
        FROM scanned_invoices
        WHERE status = 'confirmed' AND payment_status = 'paid'
          AND paid_date >= date('now', '-30 days')
          {loc_clause}
    """, params).fetchone()
    by_vendor = conn.execute(f"""
        SELECT vendor_name, COUNT(*) as invoice_count,
               COALESCE(SUM(total), 0) as total_outstanding
        FROM scanned_invoices
        WHERE status = 'confirmed'
          AND (payment_status = 'unpaid' OR payment_status IS NULL)
          {loc_clause}
        GROUP BY vendor_name
        ORDER BY total_outstanding DESC
    """, params).fetchall()
    conn.close()
    return {
        "total_outstanding": round(outstanding["total"], 2),
        "total_paid_30d": round(paid_30d["total"], 2),
        "by_vendor": [dict(r) for r in by_vendor],
    }


def get_price_changes(days=30):
    """Find products with price changes in the last N days."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            p1.product_name,
            p1.vendor_name,
            p1.unit_price as new_price,
            p2.unit_price as old_price,
            ROUND((p1.unit_price - p2.unit_price) / p2.unit_price * 100, 1) as pct_change,
            p1.invoice_date as new_date,
            p2.invoice_date as old_date
        FROM product_prices p1
        JOIN product_prices p2 ON p1.product_name = p2.product_name
            AND p1.vendor_name = p2.vendor_name
            AND p1.id > p2.id
        WHERE p1.unit_price != p2.unit_price
            AND p1.created_at >= datetime('now', ?)
        GROUP BY p1.product_name, p1.vendor_name
        HAVING p1.id = MAX(p1.id)
        ORDER BY ABS(pct_change) DESC
        LIMIT 20
    """, (f"-{days} days",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_price_alerts_for_invoice(invoice_id):
    """Compare each line item in an invoice against the most recent historical price.

    Returns a list of items where the price changed >= 5% vs the last known price
    from confirmed scanned invoices.
    """
    conn = get_connection()
    items = conn.execute(
        "SELECT product_name, unit_price FROM scanned_invoice_items WHERE invoice_id = ?",
        (invoice_id,)
    ).fetchall()

    alerts = []
    for item in items:
        product_name = item["product_name"]
        current_price = item["unit_price"]
        if not current_price or current_price <= 0:
            continue

        # Expand to all name variants via product_name_map (enables cross-source matching)
        variants = get_name_variants(product_name, conn)
        lower_variants = [v.lower() for v in variants]
        placeholders = ",".join("?" * len(lower_variants))

        row = conn.execute(f"""
            WITH history AS (
                SELECT sii.unit_price, si.invoice_date
                FROM scanned_invoice_items sii
                JOIN scanned_invoices si ON sii.invoice_id = si.id
                WHERE LOWER(sii.product_name) IN ({placeholders})
                  AND sii.invoice_id != ?
                  AND sii.unit_price > 0
                  AND si.status = 'confirmed'
                UNION ALL
                SELECT mii.unit_price, mi.invoice_date
                FROM me_invoice_items mii
                JOIN me_invoices mi ON mii.order_id = mi.order_id
                WHERE LOWER(mii.product_name) IN ({placeholders})
                  AND mii.unit_price > 0
            )
            SELECT unit_price, invoice_date
            FROM history
            ORDER BY invoice_date DESC
            LIMIT 1
        """, (*lower_variants, invoice_id, *lower_variants)).fetchone()

        if not row:
            continue

        prev_price = row["unit_price"]
        prev_date = row["invoice_date"]
        if not prev_price or prev_price <= 0:
            continue

        pct_change = (current_price - prev_price) / prev_price * 100
        if abs(pct_change) >= 5.0:
            alerts.append({
                "product_name": product_name,
                "current_price": round(current_price, 4),
                "previous_price": round(prev_price, 4),
                "previous_date": prev_date,
                "pct_change": round(pct_change, 1),
            })

    conn.close()
    return alerts


def get_spending_summary(location=None, start_date=None, end_date=None):
    """Get spending summary from scanned invoices (mirrors ME cogs/summary)."""
    conn = get_connection()
    where = ["status = 'confirmed'"]
    params = []

    if location:
        where.append("location = ?")
        params.append(location)
    if start_date:
        where.append("invoice_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("invoice_date <= ?")
        params.append(end_date)

    where_sql = "WHERE " + " AND ".join(where)

    rows = conn.execute(f"""
        SELECT category, COUNT(*) as invoice_count,
               SUM(total) as total_cost
        FROM scanned_invoices
        {where_sql}
        GROUP BY category
        ORDER BY total_cost DESC
    """, params).fetchall()
    conn.close()

    total = sum(r["total_cost"] for r in rows) if rows else 0
    return {
        "total_cost": round(total, 2),
        "categories": [
            {
                "category_type": r["category"],
                "total_cost": round(r["total_cost"], 2),
                "invoice_count": r["invoice_count"],
                "pct_of_total": round(r["total_cost"] / total * 100, 1) if total > 0 else 0,
            }
            for r in rows
        ],
    }
