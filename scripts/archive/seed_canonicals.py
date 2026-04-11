#!/usr/bin/env python3
"""
Seed canonical products into product_costing and remap vendor items.
v2 — Rollback bad WRatio matches, use smarter 5-rule algorithm.
Run: cd /opt/rednun && source venv/bin/activate && python3 seed_canonicals.py
"""
import sys
import os
import re
sys.path.insert(0, os.path.dirname(__file__))

from data_store import get_connection

try:
    from rapidfuzz import fuzz
except ImportError:
    print("ERROR: rapidfuzz not installed. pip install rapidfuzz")
    sys.exit(1)

# ════════════════════════════════════════════════════════════════
# Canonical list
# ════════════════════════════════════════════════════════════════
CANONICALS = [
    ("Beef Patty", "FOOD"), ("Beef, Steak Tips", "FOOD"),
    ("Beef, Pub Steak 12oz", "FOOD"), ("Pulled Pork", "FOOD"),
    ("Chicken Cutlet", "FOOD"), ("Chicken Breast, Grilled", "FOOD"),
    ("Cod Fillet", "FOOD"), ("Scallops, Dayboat", "FOOD"),
    ("Chicken Wings", "FOOD"), ("Turkey Burger Patty", "FOOD"),
    ("Veggie Burger Patty", "FOOD"),
    ("Bun, English Muffin", "FOOD"), ("Bun, Brioche", "FOOD"),
    ("Tortilla, Flour 10\"", "FOOD"), ("Tortilla Chips", "FOOD"),
    ("Naan Bread", "FOOD"), ("Pretzel, Bavarian", "FOOD"),
    ("Lettuce, Iceberg", "FOOD"), ("Lettuce, Romaine", "FOOD"),
    ("Lettuce, Kale", "FOOD"), ("Tomato, Sliced", "FOOD"),
    ("Onion, Red", "FOOD"), ("Onion, Yellow", "FOOD"),
    ("Mushrooms", "FOOD"), ("Jalapenos, Pickled", "FOOD"),
    ("Avocado", "FOOD"), ("Cucumber", "FOOD"),
    ("Cabbage, Shredded", "FOOD"), ("Carrot, Shredded", "FOOD"),
    ("Brussels Sprouts", "FOOD"), ("Lemon", "FOOD"),
    ("Cranberries, Dried", "FOOD"), ("Walnuts", "FOOD"),
    ("Cheese, American", "FOOD"), ("Cheese, Swiss", "FOOD"),
    ("Cheese, Cheddar Sharp", "FOOD"), ("Cheese, Blue Crumbled", "FOOD"),
    ("Cheese, Ghost Pepper Cheddar", "FOOD"), ("Cheese, Jack Cheddar", "FOOD"),
    ("Cheese, Parmesan", "FOOD"), ("Cheese, Mozzarella Sticks", "FOOD"),
    ("Butter", "FOOD"), ("Sour Cream", "FOOD"),
    ("Ranch Dressing", "FOOD"), ("Blue Cheese Dressing", "FOOD"),
    ("Buffalo Sauce", "FOOD"), ("BBQ Sauce", "FOOD"),
    ("Sriracha", "FOOD"), ("Tartar Sauce", "FOOD"),
    ("Basil Pesto", "FOOD"), ("Salsa", "FOOD"),
    ("Refried Beans", "FOOD"), ("Pickle Chips", "FOOD"),
    ("Croutons", "FOOD"), ("Breadcrumbs", "FOOD"),
    ("Canola Oil", "FOOD"), ("Olive Oil", "FOOD"),
    ("Jasmine Rice", "FOOD"), ("Black Beans", "FOOD"),
    ("Pasta, Macaroni", "FOOD"), ("Bacon, Sliced", "FOOD"),
    ("Bacon, Bits", "FOOD"),
]

CANONICAL_NAMES = [c[0] for c in CANONICALS]
CANONICAL_SET = set(n.lower().strip() for n in CANONICAL_NAMES)

# Stopwords to ignore in token overlap check
STOPWORDS = {'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'and',
             'or', 'not', 'with', 'w', 'ct', 'oz', 'lb', 'gal', 'pk', 'bag',
             'box', 'btl', 'can', 'jug', 'jar', 'tub', 'ref', 'fzn', 'frz',
             'frs', 'shlf', 'plst', 'fresh', 'frozen'}


def tokenize(s):
    """Split into lowercase alpha tokens, strip punctuation."""
    return re.findall(r'[a-z]{2,}', s.lower())


def significant_tokens(s):
    """Get tokens with 4+ chars that aren't stopwords."""
    return [t for t in tokenize(s) if len(t) >= 4 and t not in STOPWORDS]


def smart_match(vendor_name, canonical_name):
    """
    5-rule matching algorithm. Returns (score, passes) tuple.
    score = token_set_ratio (0-100)
    passes = True if all rules pass
    """
    vn_lower = vendor_name.lower().strip()
    cn_lower = canonical_name.lower().strip()

    # RULE 1 — token_set_ratio
    score = fuzz.token_set_ratio(vn_lower, cn_lower)

    # RULE 2 — length ratio penalty
    if len(cn_lower) / max(len(vn_lower), 1) < 0.4:
        return (score, False)

    # RULE 3 — key token overlap
    canon_sig = significant_tokens(canonical_name)
    vendor_tokens = set(tokenize(vendor_name))
    if canon_sig:
        overlap = sum(1 for t in canon_sig if t in vendor_tokens)
        if overlap == 0:
            return (score, False)

    return (score, True)


def main():
    conn = get_connection()

    # ══════════════════════════════════════════════════════════════
    # STEP 1 — ROLLBACK
    # ══════════════════════════════════════════════════════════════
    print("=" * 60)
    print("STEP 1 — ROLLBACK BAD SEED REMAPS")
    print("=" * 60)

    ph = ",".join("?" * len(CANONICAL_NAMES))

    # Count what we're rolling back
    bad_auto = conn.execute(f"""
        SELECT COUNT(*) FROM vendor_item_links
        WHERE canonical_product_name IN ({ph}) AND auto_linked = 1
    """, CANONICAL_NAMES).fetchone()[0]

    bad_sugg = conn.execute(f"""
        SELECT COUNT(*) FROM vendor_item_links
        WHERE canonical_product_name IN ({ph}) AND auto_linked = 0
    """, CANONICAL_NAMES).fetchone()[0]

    print(f"  Auto-linked to canonical names (to rollback): {bad_auto}")
    print(f"  Suggested to canonical names (to rollback): {bad_sugg}")

    # Rollback: reset canonical_product_name = vendor_item_name (self-map)
    # These canonical names didn't exist before seeding, so ALL mappings
    # to them were created by the bad seed script.
    conn.execute(f"""
        UPDATE vendor_item_links
        SET canonical_product_name = vendor_item_name,
            confidence = NULL,
            auto_linked = 1
        WHERE canonical_product_name IN ({ph})
    """, CANONICAL_NAMES)

    # Also reset scanned_invoice_items that were pointed at canonical names
    conn.execute(f"""
        UPDATE scanned_invoice_items
        SET canonical_product_name = product_name,
            auto_linked = 1
        WHERE canonical_product_name IN ({ph})
    """, CANONICAL_NAMES)

    conn.commit()

    remaining_to_canons = conn.execute(f"""
        SELECT COUNT(*) FROM vendor_item_links
        WHERE canonical_product_name IN ({ph})
    """, CANONICAL_NAMES).fetchone()[0]
    print(f"  After rollback, links pointing to canonicals: {remaining_to_canons}")
    print(f"  Rollback complete.")

    # ══════════════════════════════════════════════════════════════
    # Ensure canonicals are seeded (idempotent)
    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 60)
    print("SEEDING CANONICAL PRODUCTS (idempotent)")
    print("=" * 60)
    inserted = 0
    for name, category in CANONICALS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO product_costing (product_name, category, reviewed, updated_at) "
            "VALUES (?, ?, 0, datetime('now'))", (name, category)
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    count = conn.execute(f"SELECT COUNT(*) FROM product_costing WHERE product_name IN ({ph})",
                         CANONICAL_NAMES).fetchone()[0]
    print(f"  Inserted {inserted} new, {count} total in product_costing")

    # ══════════════════════════════════════════════════════════════
    # STEP 2+3 — SMART REMAP WITH 5-RULE ALGORITHM
    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 60)
    print("STEP 2 — SMART REMAPPING (5-rule algorithm)")
    print("=" * 60)
    print("  Rule 1: token_set_ratio (not WRatio)")
    print("  Rule 2: length ratio >= 0.4 (disqualify short canonical vs long vendor)")
    print("  Rule 3: key token overlap (4+ char token from canonical in vendor)")
    print("  Rule 4: category gate (FOOD only, skip BEER/WINE/LIQUOR)")
    print("  Rule 5: auto=90%, suggested=75-89%, below 75%=skip")
    print()

    # RULE 4 — Only get FOOD vendor items (category gate)
    # Get vendor items and their categories from scanned_invoice_items
    food_vendor_items = set()
    food_rows = conn.execute("""
        SELECT DISTINCT sii.product_name, sii.category_type
        FROM scanned_invoice_items sii
        WHERE sii.category_type IN ('FOOD', 'NA_BEVERAGES')
    """).fetchall()
    for r in food_rows:
        food_vendor_items.add(r['product_name'].lower().strip())

    all_links = conn.execute(
        "SELECT vendor_item_name, canonical_product_name, confidence, auto_linked "
        "FROM vendor_item_links"
    ).fetchall()
    print(f"Total vendor_item_links: {len(all_links)}")
    print(f"FOOD/NA_BEVERAGES vendor items: {len(food_vendor_items)}")

    remapped = 0
    suggested_new = 0
    skipped_category = 0
    skipped_rules = 0
    unchanged = 0
    remap_details = []
    suggest_details = []

    for row in all_links:
        vi_name = row["vendor_item_name"]
        current_canon = row["canonical_product_name"]

        # Skip if already mapped to a clean canonical
        if current_canon and current_canon.lower().strip() in CANONICAL_SET:
            unchanged += 1
            continue

        # RULE 4 — Category gate: skip non-FOOD items
        if vi_name.lower().strip() not in food_vendor_items:
            skipped_category += 1
            continue

        # Find best match against canonical list using smart_match
        best_score = 0
        best_canon = None
        for cn in CANONICAL_NAMES:
            score, passes = smart_match(vi_name, cn)
            if passes and score > best_score:
                best_score = score
                best_canon = cn

        # RULE 5 — Thresholds: 90% auto, 75-89% suggested
        if best_score >= 90 and best_canon:
            conn.execute(
                "UPDATE vendor_item_links SET canonical_product_name = ?, "
                "confidence = ?, auto_linked = 1 WHERE vendor_item_name = ?",
                (best_canon, best_score, vi_name)
            )
            conn.execute(
                "UPDATE scanned_invoice_items SET canonical_product_name = ?, auto_linked = 1 "
                "WHERE LOWER(TRIM(product_name)) = LOWER(TRIM(?))",
                (best_canon, vi_name)
            )
            remap_details.append((vi_name, current_canon, best_canon, best_score))
            remapped += 1
        elif best_score >= 75 and best_canon:
            conn.execute(
                "UPDATE vendor_item_links SET canonical_product_name = ?, "
                "confidence = ?, auto_linked = 0 WHERE vendor_item_name = ?",
                (best_canon, best_score, vi_name)
            )
            suggest_details.append((vi_name, best_canon, best_score))
            suggested_new += 1
        else:
            skipped_rules += 1

    conn.commit()

    print(f"\nAuto-remapped to clean canonicals (>=90%): {remapped}")
    print(f"Flagged as suggested (75-89%):              {suggested_new}")
    print(f"Skipped — non-FOOD category:                {skipped_category}")
    print(f"Skipped — failed rules or below 75%:        {skipped_rules}")
    print(f"Already on a clean canonical:               {unchanged}")

    if remap_details:
        print(f"\n{'─' * 60}")
        print("AUTO-REMAPPED (for Mike to verify):")
        print(f"{'─' * 60}")
        for vi, old, new, score in sorted(remap_details, key=lambda x: x[2]):
            print(f"  {vi}")
            print(f"    → {new} ({score:.0f}%)")
            print()

    if suggest_details:
        print(f"\n{'─' * 60}")
        print("SUGGESTED (needs review in UI):")
        print(f"{'─' * 60}")
        for vi, canon, score in sorted(suggest_details, key=lambda x: x[1]):
            print(f"  {vi}")
            print(f"    → {canon} ({score:.0f}%)")

    # ══════════════════════════════════════════════════════════════
    # STEP 3 — VERIFY
    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 60)
    print("STEP 3 — VERIFICATION")
    print("=" * 60)

    rows = conn.execute(f"""
        SELECT canonical_product_name, COUNT(*) as vendor_items
        FROM vendor_item_links
        WHERE canonical_product_name IN ({ph})
        GROUP BY canonical_product_name
        ORDER BY vendor_items DESC
    """, CANONICAL_NAMES).fetchall()

    if rows:
        print(f"\n{'Canonical Product':<35} {'Vendor Items':>12}")
        print(f"{'─' * 35} {'─' * 12}")
        total_mapped = 0
        for r in rows:
            print(f"  {r['canonical_product_name']:<33} {r['vendor_items']:>10}")
            total_mapped += r["vendor_items"]
        print(f"{'─' * 35} {'─' * 12}")
        print(f"  {'TOTAL':<33} {total_mapped:>10}")
    else:
        print("  No vendor items mapped to clean canonicals.")

    # Spot checks
    print()
    print("=" * 60)
    print("SPOT CHECKS")
    print("=" * 60)

    checks = [
        ("BEEF, PTY GRND CHUK BRSKT", True, "Beef Patty"),
        ("BACON, SLICED", True, "Bacon, Sliced"),
        ("MUFFIN ENGLISH SNDWCH 4\"", True, "Bun, English Muffin"),
        ("LETTUCE ICEBERG", True, "Lettuce, Iceberg"),
        ("BUD LIGHT", False, None),
        ("CAPE COD BEER", False, None),
        ("MUSHROOM SLICED", False, "Tomato, Sliced"),
        ("BAR MIX SOUR", False, "Sour Cream"),
    ]

    for vi_search, should_match, expected in checks:
        row = conn.execute(
            "SELECT canonical_product_name FROM vendor_item_links WHERE vendor_item_name LIKE ?",
            (f"%{vi_search}%",)
        ).fetchone()
        if row:
            canon = row["canonical_product_name"]
            is_clean = canon.lower().strip() in CANONICAL_SET
            if should_match:
                ok = is_clean
                sym = "PASS" if ok else "FAIL"
                print(f"  {sym}: '{vi_search}' → '{canon}' {'(clean)' if is_clean else '(NOT clean)'}")
            else:
                # Should NOT match a clean canonical
                ok = not is_clean
                sym = "PASS" if ok else "FAIL"
                print(f"  {sym}: '{vi_search}' → '{canon}' {'(self-map, good)' if ok else '(BAD: matched clean canonical!)'}")
        else:
            if should_match:
                print(f"  SKIP: '{vi_search}' — not in vendor_item_links")
            else:
                print(f"  PASS: '{vi_search}' — not in vendor_item_links")

    # Summary stats
    print()
    total_links = conn.execute("SELECT COUNT(*) FROM vendor_item_links").fetchone()[0]
    auto = conn.execute("SELECT COUNT(*) FROM vendor_item_links WHERE auto_linked = 1").fetchone()[0]
    sugg = conn.execute(
        "SELECT COUNT(*) FROM vendor_item_links WHERE auto_linked = 0 AND confidence IS NOT NULL AND confidence >= 75 AND confidence < 90"
    ).fetchone()[0]
    print(f"Total vendor_item_links: {total_links}")
    print(f"Auto-linked:             {auto}")
    print(f"Suggested (75-89%):      {sugg}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
