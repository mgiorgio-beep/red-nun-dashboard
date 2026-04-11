"""
Backfill Product Setup from Invoice History — Red Nun Analytics

Populates product_inventory_settings fields (purchase_price, ordering_unit,
vendor_name, case_pack_size, contains_qty, contains_unit) from all existing
invoice data:
  - me_invoice_items: direct product_name match (ME names = canonical names)
  - scanned_invoice_items: via product_name_map canonical lookup

Rules:
  - purchase_price: always updated to most recent invoice price
  - All other fields: only filled if currently NULL/empty
  - Most recent invoice date wins when multiple records exist for a product

Safe to re-run (idempotent by design).

Usage:
    python3 backfill_product_setup.py
    python3 backfill_product_setup.py --dry-run   # preview without writing
"""

import sys
import re
from data_store import get_connection
from invoice_processor import parse_pack_size


def extract_pack_from_name(product_name):
    """
    Fallback: extract pack size embedded in US Foods product names.

    US Foods bakes pack size into the product name:
      "KETCHUP, TMTO FCY 33% RED 9QZ 16/20 OZ" → "16/20 OZ"
      "OIL, CNOLA OLIV EX VRGN 75/25 4/1 GA"   → "4/1 GA"
      "ONION, RED,JMB 50 SUP REF BAG 25 LB"    → "25 LB"

    Uses negative lookbehind to avoid matching numbers mid-abbreviation (e.g. VMT2/90).
    Returns None if no pack size found.
    """
    if not product_name:
        return None
    m = re.search(r'(?<![A-Z\d#])([\d#.]+(?:/[\d#.]+)+)\s+([A-Z]{2,3})$', product_name)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    # Single number + unit at end (e.g. "25 LB", "2000 EA")
    m = re.search(r'(?<![A-Z\d#])(\d+)\s+([A-Z]{2,3})$', product_name)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return None


def backfill(dry_run=False):
    conn = get_connection()

    print("=" * 70)
    print("  PRODUCT SETUP BACKFILL")
    print("  Mode:", "DRY RUN (no writes)" if dry_run else "LIVE")
    print("=" * 70)
    print()

    # ── Collect all invoice items from both sources ──────────────────────────

    # ME invoice items: product_name IS the canonical name
    me_rows = conn.execute("""
        SELECT mii.product_name,
               mii.unit_price,
               mii.unit,
               mi.invoice_date,
               mi.vendor_name,
               mi.order_id
        FROM me_invoice_items mii
        JOIN me_invoices mi ON mii.order_id = mi.order_id
        WHERE mii.unit_price > 0 AND mii.product_name IS NOT NULL
        ORDER BY mi.invoice_date DESC
    """).fetchall()

    # Scanned invoice items: look up canonical via product_name_map
    scan_rows = conn.execute("""
        SELECT sii.product_name AS source_name,
               pnm.canonical_name,
               sii.unit_price,
               sii.unit,
               sii.pack_size,
               si.invoice_date,
               si.vendor_name,
               si.invoice_number
        FROM scanned_invoice_items sii
        JOIN scanned_invoices si ON sii.invoice_id = si.id
        JOIN product_name_map pnm
            ON LOWER(pnm.source_name) = LOWER(sii.product_name)
           AND pnm.canonical_name IS NOT NULL
        WHERE si.status = 'confirmed' AND sii.unit_price > 0
        ORDER BY si.invoice_date DESC
    """).fetchall()

    # ── Build unified item list sorted by date (most recent first) ───────────

    all_items = []

    for r in me_rows:
        all_items.append({
            "canonical": r["product_name"],
            "unit_price": r["unit_price"],
            "unit": r["unit"],
            "pack_size": None,   # ME data has no pack_size column
            "vendor": r["vendor_name"],
            "date": r["invoice_date"] or "0000-00-00",
            "label": f"{r['vendor_name']} order {r['order_id']}",
            "source": "ME",
        })

    for r in scan_rows:
        # Use explicitly extracted pack_size, or try to parse from source name
        pack = r["pack_size"] or extract_pack_from_name(r["source_name"])
        all_items.append({
            "canonical": r["canonical_name"],
            "unit_price": r["unit_price"],
            "unit": r["unit"],
            "pack_size": pack,
            "vendor": r["vendor_name"],
            "date": r["invoice_date"] or "0000-00-00",
            "label": f"{r['vendor_name']} #{r['invoice_number']} ({r['source_name']})",
            "source": "SCAN",
        })

    # Most recent date first
    all_items.sort(key=lambda x: x["date"], reverse=True)

    print(f"Items from ME invoices:      {len(me_rows)}")
    print(f"Items from scanned invoices: {len(scan_rows)}")
    print(f"Total to process:            {len(all_items)}")
    print()

    # ── Process (most recent per canonical product) ──────────────────────────

    seen = set()
    updated = 0
    skipped_no_product = 0
    skipped_seen = 0

    for item in all_items:
        canonical = item["canonical"]

        if canonical in seen:
            skipped_seen += 1
            continue
        seen.add(canonical)

        # Find in product_inventory_settings
        prod = conn.execute(
            """SELECT id, purchase_price, ordering_unit, vendor_name,
                      case_pack_size, contains_qty, contains_unit
               FROM product_inventory_settings
               WHERE LOWER(product_name) = LOWER(?)""",
            (canonical,)
        ).fetchone()

        if not prod:
            skipped_no_product += 1
            continue

        case_sz, qty, qty_unit = parse_pack_size(item["pack_size"])
        prev_price = prod["purchase_price"]

        # Log what we're doing
        changes = [f"price=${item['unit_price']:.2f}"
                   + (f" (was ${prev_price:.2f})" if prev_price else " (new)")]
        if item["unit"] and not prod["ordering_unit"]:
            changes.append(f"unit={item['unit']}")
        if item["vendor"] and not prod["vendor_name"]:
            changes.append(f"vendor={item['vendor']}")
        if case_sz is not None and prod["case_pack_size"] is None:
            changes.append(f"case_sz={case_sz}")
        if qty is not None and prod["contains_qty"] is None:
            changes.append(f"contains={qty} {qty_unit or ''}")

        print(f"  {canonical}")
        print(f"    {', '.join(changes)}")
        print(f"    ← {item['label']}, {item['date']}")

        if not dry_run:
            conn.execute("""
                UPDATE product_inventory_settings SET
                    purchase_price = ?,
                    ordering_unit  = CASE WHEN ordering_unit IS NULL OR ordering_unit = ''
                                          THEN ? ELSE ordering_unit END,
                    vendor_name    = CASE WHEN vendor_name IS NULL OR vendor_name = ''
                                          THEN ? ELSE vendor_name END,
                    case_pack_size = CASE WHEN case_pack_size IS NULL THEN ? ELSE case_pack_size END,
                    contains_qty   = CASE WHEN contains_qty IS NULL THEN ? ELSE contains_qty END,
                    contains_unit  = CASE WHEN contains_unit IS NULL OR contains_unit = ''
                                          THEN ? ELSE contains_unit END,
                    updated_at     = datetime('now')
                WHERE id = ?
            """, (
                item["unit_price"],
                item["unit"],
                item["vendor"],
                case_sz,
                qty,
                qty_unit,
                prod["id"],
            ))

        updated += 1

    if not dry_run:
        conn.commit()

    # ── Second pass: fill pack_size from scanned items where still NULL ───────
    # ME data has no pack_size; scanned items may have been skipped above
    # because ME item was more recent. Apply pack_size regardless of date order.
    pack_filled = 0
    print()
    print("--- Pack size fill (scanned items only) ---")
    for r in scan_rows:
        pack = r["pack_size"] or extract_pack_from_name(r["source_name"])
        if not pack:
            continue
        canonical = r["canonical_name"]
        case_sz, qty, qty_unit = parse_pack_size(pack)
        if case_sz is None and qty is None:
            continue
        # Only update if case_pack_size is still NULL
        prod = conn.execute(
            """SELECT id, case_pack_size, contains_qty, contains_unit
               FROM product_inventory_settings
               WHERE LOWER(product_name) = LOWER(?) AND case_pack_size IS NULL""",
            (canonical,)
        ).fetchone()
        if not prod:
            continue
        print(f"  {canonical}: case={case_sz}, contains={qty} {qty_unit}  ← {pack}")
        if not dry_run:
            conn.execute("""
                UPDATE product_inventory_settings SET
                    case_pack_size = ?,
                    contains_qty   = CASE WHEN contains_qty IS NULL THEN ? ELSE contains_qty END,
                    contains_unit  = CASE WHEN contains_unit IS NULL OR contains_unit = ''
                                          THEN ? ELSE contains_unit END,
                    updated_at     = datetime('now')
                WHERE id = ?
            """, (case_sz, qty, qty_unit, prod["id"]))
        pack_filled += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print()
    print("=" * 70)
    print(f"  Products updated (price):   {updated}")
    print(f"  Pack sizes filled:          {pack_filled}")
    print(f"  Skipped (newer seen):       {skipped_seen}")
    print(f"  Skipped (not in catalog):   {skipped_no_product}")
    if dry_run:
        print("  *** DRY RUN — no changes written ***")
    print("=" * 70)


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    backfill(dry_run=dry_run)
