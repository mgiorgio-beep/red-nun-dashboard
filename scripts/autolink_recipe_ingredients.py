"""
Auto-link unlinked recipe ingredients to products.

Many recipe_ingredients rows have product_id NULL, 0, or pointing to a missing
product, but their product_name text is an obvious match to an existing product
(e.g. "Kahlua" -> "KAHLUA COFFEE LIQUEUR"). This script does pure local string
matching to link the obvious ones so the Recipe Cost Fixer UI only has to
handle the genuinely ambiguous cases.

When to run:
- After a bulk recipe import or a products-table rebuild.
- Before opening the Recipe Cost Fixer UI, to cut the manual work roughly in half.
- Idempotent: running it twice finds nothing new.

Usage:
    python3 scripts/autolink_recipe_ingredients.py               # dry run (default)
    python3 scripts/autolink_recipe_ingredients.py --commit      # write changes
    python3 scripts/autolink_recipe_ingredients.py --min-confidence 0.90

After --commit, recost recipes:
    python3 -c "from integrations.toast.data_store import get_connection; \\
                from integrations.recipes.recipe_costing import cost_all_recipes; \\
                cost_all_recipes(get_connection())"
"""

import argparse
import os
import re
import shutil
import string
import sys
from datetime import datetime
from difflib import SequenceMatcher

# Allow running as `python scripts/autolink_recipe_ingredients.py` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import get_connection  # noqa: E402


BACKUP_DIR = "/opt/backups"
_PUNCT_RE = re.compile(r"[%s]" % re.escape(string.punctuation))
_WS_RE = re.compile(r"\s+")

# Applied to raw product names before fuzzy matching: peel off pack codes, size
# suffixes, bare trailing numbers, and stray typographic symbols that tank the
# difflib ratio for otherwise-obvious matches (e.g. "Fireball Cinnamon Whisky 750ML").
_STRIP_PATTERNS = [
    re.compile(r"\b\d+/\w+\b", re.IGNORECASE),                                                # 6/CS, 24/CS, 12/750
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:ml|l|oz|lt|ltr|pet|pk|ct|can|btl)\b", re.IGNORECASE),   # 750ml, 13.2oz, 16oz, 1L
    re.compile(r"\s\d+(?:\.\d+)?\s*$"),                                                       # bare trailing numbers
    re.compile(r"[\u00b0\u00ae\u2122]"),                                                      # °, ®, ™
]


def normalize(text):
    """Lowercase, strip punctuation, collapse whitespace. Applied to BOTH sides
    of every match attempt (exact / substring / word-boundary / fuzzy) so case
    is always consistent."""
    if not text:
        return ""
    t = text.lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def strip_size_suffixes(text):
    if not text:
        return text
    t = text
    for _ in range(3):  # iterate — stripping can expose new trailing numbers
        prev = t
        for pat in _STRIP_PATTERNS:
            t = pat.sub(" ", t)
        t = t.strip()
        if t == prev:
            break
    return t


def _contains_word(haystack_n, needle_n):
    """Word-boundary contiguous substring check. Both inputs must already be
    normalized (lowercase, punctuation-stripped). Prevents 'Apple' from
    matching 'Pineapple Juice' or 'Ice' from matching 'Jasmine Rice'."""
    if not haystack_n or not needle_n:
        return False
    pattern = r"\b" + re.escape(needle_n) + r"\b"
    return re.search(pattern, haystack_n) is not None


def _head_noun_ok(ing_n, product_n):
    """Sanity check: reject when the product's first word is strictly longer
    than the ingredient's first word AND the ingredient's first word is a
    substring of it. Blocks the 'Apple' -> 'Pineapple' class of false positive."""
    if not ing_n or not product_n:
        return True
    ing_parts = ing_n.split()
    prod_parts = product_n.split()
    if not ing_parts or not prod_parts:
        return True
    ing_first = ing_parts[0]
    prod_first = prod_parts[0]
    if ing_first == prod_first:
        return True
    if len(prod_first) > len(ing_first) and ing_first in prod_first:
        return False
    return True


def _candidate_ok(ing_n, p):
    """Apply head-noun check against the stripped forms of both name and
    display_name. Keep candidate if at least one non-empty form passes."""
    forms = [f for f in (p["_name_stripped_n"], p["_display_stripped_n"]) if f]
    if not forms:
        # Fall back to unstripped forms (shouldn't normally happen)
        forms = [f for f in (p["_name_n"], p["_display_n"]) if f]
    if not forms:
        return True
    return any(_head_noun_ok(ing_n, f) for f in forms)


def _dedup_filter(ing_n, candidates):
    uniq = {p["id"]: p for p in candidates}
    return [p for p in uniq.values() if _candidate_ok(ing_n, p)]


def has_column(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def load_candidate_products(conn, recipe_location, products_has_location):
    """Active products, scoped to the recipe's location (+ location-agnostic)."""
    if products_has_location and recipe_location in ("chatham", "dennis"):
        rows = conn.execute(
            """
            SELECT id, name, display_name
            FROM products
            WHERE active = 1
              AND (location = ? OR location IS NULL OR location = '')
            """,
            (recipe_location,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, display_name FROM products WHERE active = 1"
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        name = d.get("name") or ""
        display = d.get("display_name") or ""
        d["_name_n"] = normalize(name)
        d["_display_n"] = normalize(display)
        d["_name_stripped_n"] = normalize(strip_size_suffixes(name))
        d["_display_stripped_n"] = normalize(strip_size_suffixes(display))
        out.append(d)
    return out


def _product_label(p):
    return p.get("display_name") or p.get("name") or f"#{p['id']}"


def find_match(ingredient_text, products, min_confidence):
    """
    Returns (status, confidence, candidates) where:
      status == 'match'       -> exactly one candidate, link it
      status == 'ambiguous'   -> multiple candidates tied at top confidence
      status == 'no_match'    -> best confidence below threshold
      status == 'empty'       -> ingredient text is empty after normalization
    """
    ing_n = normalize(ingredient_text)
    if not ing_n:
        return ("empty", 0.0, [])

    # 1) Exact (on normalized name or display_name)
    exact = [p for p in products if p["_name_n"] == ing_n or p["_display_n"] == ing_n]
    exact = _dedup_filter(ing_n, exact)
    if exact:
        return ("match" if len(exact) == 1 else "ambiguous", 1.0, exact)

    # 2) Ingredient text is a contiguous substring of a product name AT A WORD
    #    BOUNDARY. Without the boundary check, "Apple" matched "Pineapple".
    substring = []
    for p in products:
        if _contains_word(p["_name_n"], ing_n) or _contains_word(p["_display_n"], ing_n):
            substring.append(p)
    substring = _dedup_filter(ing_n, substring)
    if substring:
        return ("match" if len(substring) == 1 else "ambiguous", 0.95, substring)

    # 3) Product name starts with the ingredient text at a word boundary
    starts = []
    for p in products:
        for pn in (p["_name_n"], p["_display_n"]):
            if not pn:
                continue
            if pn == ing_n or pn.startswith(ing_n + " "):
                starts.append(p)
                break
    starts = _dedup_filter(ing_n, starts)
    if starts:
        return ("match" if len(starts) == 1 else "ambiguous", 0.90, starts)

    # 4) Fuzzy ratio against the STRIPPED product names (pack/size suffixes
    #    removed) so "Fireball" can match "Fireball Cinnamon Whisky 750ML".
    best_ratio = 0.0
    best = []
    for p in products:
        r = 0.0
        if p["_name_stripped_n"]:
            r = max(r, SequenceMatcher(None, ing_n, p["_name_stripped_n"]).ratio())
        if p["_display_stripped_n"]:
            r = max(r, SequenceMatcher(None, ing_n, p["_display_stripped_n"]).ratio())
        if r > best_ratio + 1e-9:
            best_ratio = r
            best = [p]
        elif abs(r - best_ratio) <= 1e-9 and r > 0:
            best.append(p)

    if best_ratio >= min_confidence and best:
        filtered = _dedup_filter(ing_n, best)
        if filtered:
            return ("match" if len(filtered) == 1 else "ambiguous", best_ratio, filtered)

    return ("no_match", best_ratio, best[:1])


def backup_db():
    """Copy the live DB to /opt/backups/ with an autolink-tagged timestamp."""
    from integrations.toast.data_store import DB_PATH

    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    dest = os.path.join(BACKUP_DIR, f"toast_data_autolink_{stamp}.db")
    shutil.copy2(DB_PATH, dest)
    return dest


def run(min_confidence, commit):
    conn = get_connection()

    products_has_location = has_column(conn, "products", "location")

    # Pre-cache existing product IDs so we can identify broken FKs cheaply.
    existing_ids = {
        r[0] for r in conn.execute("SELECT id FROM products").fetchall()
    }

    # Pull every unlinked ingredient whose recipe isn't already costed.
    rows = conn.execute(
        """
        SELECT ri.id          AS ingredient_id,
               ri.recipe_id   AS recipe_id,
               ri.product_id  AS product_id,
               ri.product_name AS ingredient_name,
               r.name         AS recipe_name,
               r.location     AS recipe_location,
               COALESCE(r.total_cost, 0) AS recipe_total_cost
        FROM recipe_ingredients ri
        JOIN recipes r ON r.id = ri.recipe_id
        WHERE (ri.product_id IS NULL
               OR ri.product_id = 0
               OR ri.product_id NOT IN (SELECT id FROM products))
          AND COALESCE(r.total_cost, 0) = 0
        ORDER BY r.name, ri.id
        """
    ).fetchall()

    # Cache candidate-product lists per (location, products_has_location) key.
    product_cache = {}

    stats = {"processed": 0, "match": 0, "ambiguous": 0, "no_match": 0, "empty": 0}
    to_link = []  # list of (ingredient_id, product_id)

    for row in rows:
        row = dict(row)
        # Defensive: skip if product_id is actually valid — shouldn't happen given
        # the WHERE clause above, but keeps the "never overwrite a valid link" rule explicit.
        pid = row["product_id"]
        if pid and pid in existing_ids:
            continue

        stats["processed"] += 1
        loc = row["recipe_location"]
        cache_key = loc if (products_has_location and loc in ("chatham", "dennis")) else None
        if cache_key not in product_cache:
            product_cache[cache_key] = load_candidate_products(conn, loc, products_has_location)
        products = product_cache[cache_key]

        status, conf, cands = find_match(row["ingredient_name"], products, min_confidence)
        recipe_label = row["recipe_name"] or f"recipe #{row['recipe_id']}"
        ing_text = row["ingredient_name"] or "(blank)"

        if status == "match":
            p = cands[0]
            print(f'[MATCH {conf:.2f}] {recipe_label} / "{ing_text}"')
            print(f'  -> product {p["id"]} "{_product_label(p)}"')
            stats["match"] += 1
            to_link.append((row["ingredient_id"], p["id"]))
        elif status == "ambiguous":
            stats["ambiguous"] += 1
            n = len(cands)
            preview = ", ".join(
                f'product {p["id"]} ({_product_label(p)})' for p in cands[:4]
            )
            if n > 4:
                preview += f", ... +{n - 4} more"
            print(f'[SKIP ambiguous] {recipe_label} / "{ing_text}"')
            print(f"  -> {n} candidates at {conf:.2f}: {preview}")
        elif status == "empty":
            stats["empty"] += 1
            print(f'[SKIP empty] {recipe_label} / ingredient {row["ingredient_id"]} has no product_name')
        else:  # no_match
            stats["no_match"] += 1
            if cands:
                p = cands[0]
                print(f'[SKIP no-match] {recipe_label} / "{ing_text}"')
                print(f'  -> best was {conf:.2f} (product {p["id"]} "{_product_label(p)}")')
            else:
                print(f'[SKIP no-match] {recipe_label} / "{ing_text}"')
                print(f"  -> no candidate products")

    total = stats["processed"]
    would_link = stats["match"]
    pct = f"{(would_link / total * 100):.0f}%" if total else "0%"

    print("-" * 55)
    print(f"Processed:       {total} unlinked ingredients")
    print(f"Would link:      {would_link} ({pct})")
    print(f"Ambiguous:       {stats['ambiguous']}")
    print(f"No match:        {stats['no_match']}")
    if stats["empty"]:
        print(f"Empty names:     {stats['empty']}")
    print("-" * 55)

    if not commit:
        print("DRY RUN - no changes written. Pass --commit to apply.")
        conn.close()
        return 0

    if not to_link:
        print("Nothing to link. No changes written.")
        conn.close()
        return 0

    backup_path = backup_db()
    print(f"Backed up DB to {backup_path}")

    try:
        conn.execute("BEGIN")
        for ingredient_id, product_id in to_link:
            conn.execute(
                "UPDATE recipe_ingredients SET product_id = ? WHERE id = ?",
                (product_id, ingredient_id),
            )
            print(f"Linked ingredient {ingredient_id} -> product {product_id}")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    conn.close()
    print("-" * 55)
    print(f"Committed: {len(to_link)} ingredient(s) linked.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Auto-link unlinked recipe ingredients to products."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write changes. Without this flag, runs as dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode (overrides --commit).",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.85,
        help="Minimum fuzzy-match ratio to accept (default: 0.85).",
    )
    args = parser.parse_args()

    commit = args.commit and not args.dry_run
    if not 0.0 < args.min_confidence <= 1.0:
        print("--min-confidence must be in (0, 1]", file=sys.stderr)
        return 2

    return run(args.min_confidence, commit)


if __name__ == "__main__":
    sys.exit(main())
