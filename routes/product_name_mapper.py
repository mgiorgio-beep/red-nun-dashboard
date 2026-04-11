"""
Product Name Mapper — Red Nun Analytics

Bridges scanned invoice product names (US Foods ALL-CAPS format) to
MarginEdge product names (Title Case abbreviated format) using fuzzy matching.

Usage:
    python3 product_name_mapper.py                 # generate mappings + print report
    python3 product_name_mapper.py --report-only   # print report from existing data
    python3 product_name_mapper.py --reset         # clear and regenerate all mappings

Table: product_name_map
  source_name   — name as it appears in the invoice table
  source_table  — 'scanned_invoice_items' or 'me_invoice_items'
  canonical_name — normalized/ME canonical name (NULL if no match found)
  confidence    — rapidfuzz WRatio score
  verified      — 1=human confirmed, 0=auto-generated
"""

import re
import sys
import logging
from data_store import get_connection

logger = logging.getLogger(__name__)

SCORE_AUTO = 80    # >= this: auto-map with confidence
SCORE_REVIEW = 60  # >= this: map but flag for review (verified=0)
               # < SCORE_REVIEW: canonical_name stays NULL


# ─────────────────────────────────────────────────────────────────
# Table init
# ─────────────────────────────────────────────────────────────────

def init_name_map_table():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_name_map (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name    TEXT    NOT NULL,
            source_table   TEXT    NOT NULL,
            canonical_name TEXT,
            confidence     REAL,
            verified       INTEGER DEFAULT 0,
            created_at     TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pnm_source_unique
        ON product_name_map(source_name, source_table)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pnm_source
        ON product_name_map(source_name, source_table)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pnm_canonical
        ON product_name_map(canonical_name)
    """)
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────
# Normalization
# ─────────────────────────────────────────────────────────────────

def normalize(name):
    """Normalize a product name for fuzzy comparison.

    Handles both US Foods format (CHICKEN, BRST SNGL 7 2 OHLS 2/5 LB)
    and ME format (Chix Brst Sng Pln 7 2 Ohl).
    """
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"[,/\\|]", " ", name)   # punctuation → space
    name = re.sub(r"\s+", " ", name)        # collapse whitespace
    return name.strip()


def shares_key_token(name1, name2, min_len=4, stopwords=None):
    """Sanity check: do two product names share at least one significant word?

    WRatio can give false-positive high scores on heavily abbreviated strings
    (e.g., BEEF→beer, PICKLE→crayon). Requiring at least one shared token
    of 3+ chars prevents garbage matches.
    """
    tokens1 = set(t for t in normalize(name1).split() if len(t) >= min_len and (not stopwords or t.lower() not in stopwords))
    tokens2 = set(t for t in normalize(name2).split() if len(t) >= min_len and (not stopwords or t.lower() not in stopwords))
    return bool(tokens1 & tokens2)


# ─────────────────────────────────────────────────────────────────
# Generate mappings
# ─────────────────────────────────────────────────────────────────

def generate_mappings(reset=False):
    """
    Build product_name_map entries for all distinct scanned invoice product names.

    For each scanned name:
      - Find the best matching ME name via rapidfuzz WRatio
      - Score >= SCORE_AUTO (80): auto-map (canonical_name = ME name)
      - Score >= SCORE_REVIEW (60): map but leave verified=0 (needs human review)
      - Score < SCORE_REVIEW: canonical_name=NULL (no good match found)

    Idempotent — uses INSERT OR IGNORE so re-running won't overwrite
    existing (possibly human-verified) entries unless --reset is used.
    """
    try:
        from rapidfuzz import process, fuzz
    except ImportError:
        print("ERROR: rapidfuzz not installed. Run: pip install rapidfuzz")
        sys.exit(1)

    conn = get_connection()

    if reset:
        conn.execute("DELETE FROM product_name_map WHERE source_table = 'scanned_invoice_items'")
        conn.commit()
        print("Cleared existing scanned name mappings.")

    # Load all distinct scanned product names
    scanned_names = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT product_name FROM scanned_invoice_items WHERE product_name IS NOT NULL"
        ).fetchall()
    ]

    # Load all distinct ME product names (these are the canonical targets)
    me_names = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT product_name FROM me_invoice_items WHERE product_name IS NOT NULL"
        ).fetchall()
    ]

    # Check how many are already mapped
    already_mapped = set(
        r[0] for r in conn.execute(
            "SELECT source_name FROM product_name_map WHERE source_table = 'scanned_invoice_items'"
        ).fetchall()
    )

    # Normalized ME names for matching
    me_normalized = [normalize(n) for n in me_names]

    print(f"Scanned names: {len(scanned_names)}")
    print(f"ME names: {len(me_names)}")
    print(f"Already mapped: {len(already_mapped)}")
    print()

    stats = {"auto": 0, "review": 0, "no_match": 0, "skipped": 0}
    rows_to_insert = []

    for scanned_name in scanned_names:
        if scanned_name in already_mapped:
            stats["skipped"] += 1
            continue

        norm_scanned = normalize(scanned_name)
        if not norm_scanned:
            rows_to_insert.append((scanned_name, "scanned_invoice_items", None, 0.0, 0))
            stats["no_match"] += 1
            continue

        # Find best ME name match
        result = process.extractOne(
            norm_scanned,
            me_normalized,
            scorer=fuzz.WRatio,
            score_cutoff=0,
        )

        if result is None:
            canonical = None
            score = 0.0
            stats["no_match"] += 1
        else:
            matched_norm, score, idx = result
            me_name = me_names[idx]

            # Sanity check: require at least one shared token of 3+ chars.
            # This prevents WRatio false-positives on abbreviated strings
            # (e.g., BEEF→High Life 1/2bbl, PICKLE→crayon, etc.)
            if score >= SCORE_REVIEW and not shares_key_token(scanned_name, me_name):
                canonical = None
                score = 0.0
                stats["no_match"] += 1
            elif score >= SCORE_AUTO:
                canonical = me_name
                stats["auto"] += 1
            elif score >= SCORE_REVIEW:
                canonical = me_name
                stats["review"] += 1
            else:
                canonical = None
                stats["no_match"] += 1

        rows_to_insert.append((scanned_name, "scanned_invoice_items", canonical, round(score, 1), 0))

    # Bulk insert
    conn.executemany(
        """INSERT OR IGNORE INTO product_name_map
           (source_name, source_table, canonical_name, confidence, verified)
           VALUES (?, ?, ?, ?, ?)""",
        rows_to_insert,
    )
    conn.commit()
    conn.close()

    total_new = len(rows_to_insert)
    print(f"Inserted {total_new} new mappings:")
    print(f"  Auto-mapped (>={SCORE_AUTO}):    {stats['auto']}")
    print(f"  Needs review ({SCORE_REVIEW}-{SCORE_AUTO-1}): {stats['review']}")
    print(f"  No match (<{SCORE_REVIEW}):      {stats['no_match']}")
    print(f"  Skipped (existing):   {stats['skipped']}")
    return stats


# ─────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────

def print_mapping_report():
    """Print a human-readable report of current mappings."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT source_name, canonical_name, confidence, verified
        FROM product_name_map
        WHERE source_table = 'scanned_invoice_items'
        ORDER BY confidence DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("No mappings found. Run generate_mappings() first.")
        return

    auto = [r for r in rows if r["canonical_name"] and r["confidence"] >= SCORE_AUTO]
    review = [r for r in rows if r["canonical_name"] and r["confidence"] < SCORE_AUTO]
    no_match = [r for r in rows if not r["canonical_name"]]
    verified = [r for r in rows if r["verified"] == 1]

    print("=" * 70)
    print("  PRODUCT NAME MAPPING REPORT")
    print("=" * 70)
    print(f"  Total scanned names:  {len(rows)}")
    print(f"  Auto-mapped:          {len(auto)}")
    print(f"  Needs review:         {len(review)}")
    print(f"  No match:             {len(no_match)}")
    print(f"  Human verified:       {len(verified)}")
    print()

    if auto:
        print(f"--- AUTO-MAPPED ({len(auto)} items, confidence >= {SCORE_AUTO}) ---")
        for r in sorted(auto, key=lambda x: -x["confidence"]):
            print(f"  [{r['confidence']:.0f}] {r['source_name']}")
            print(f"       → {r['canonical_name']}")
        print()

    if review:
        print(f"--- NEEDS REVIEW ({len(review)} items, confidence {SCORE_REVIEW}-{SCORE_AUTO-1}) ---")
        print("  (Proposed match shown — verify or correct before relying on alerts)")
        for r in sorted(review, key=lambda x: -x["confidence"]):
            print(f"  [{r['confidence']:.0f}] {r['source_name']}")
            print(f"       → {r['canonical_name']}")
        print()

    if no_match:
        print(f"--- NO MATCH ({len(no_match)} items, confidence < {SCORE_REVIEW}) ---")
        print("  (Non-food items, services, maintenance — expected)")
        for r in no_match:
            print(f"  {r['source_name']}")
        print()


# ─────────────────────────────────────────────────────────────────
# Lookup helpers (used by invoice_processor.py + analytics.py)
# ─────────────────────────────────────────────────────────────────

def get_name_variants(product_name, conn=None):
    """
    Return all product names that map to the same canonical as product_name.

    If product_name has a canonical mapping, returns all source_names
    that share that canonical (enabling cross-source price comparison).
    If no mapping exists, returns [product_name] (just itself).

    The conn argument is optional — if provided, reuses it (no close).
    """
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        # Step 1: find canonical for this name
        row = conn.execute(
            """SELECT canonical_name FROM product_name_map
               WHERE LOWER(source_name) = LOWER(?) AND canonical_name IS NOT NULL
               LIMIT 1""",
            (product_name,),
        ).fetchone()

        if not row:
            return [product_name]

        canonical = row["canonical_name"]

        # Step 2: find all source names that share this canonical
        variants = conn.execute(
            """SELECT DISTINCT source_name FROM product_name_map
               WHERE canonical_name = ?""",
            (canonical,),
        ).fetchall()

        names = [r["source_name"] for r in variants]
        # Also include the canonical itself (ME name)
        if canonical not in names:
            names.append(canonical)
        return names
    finally:
        if close_conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)

    reset = "--reset" in sys.argv
    report_only = "--report-only" in sys.argv

    print("Initializing product_name_map table...")
    init_name_map_table()

    if not report_only:
        print("Generating mappings...\n")
        generate_mappings(reset=reset)
        print()

    print_mapping_report()
