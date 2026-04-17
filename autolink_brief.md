# Auto-Link Recipe Ingredients — Build Brief

## Problem

In the 276 zero-cost recipes, there are ~293 ingredient rows that aren't linked to a product (128 with `product_id IS NULL`, 155 with `product_id = 0`, 10 with a broken FK). Many of these are obvious matches — `ri.product_name` is something like "Kahlua" and there's a product named "KAHLUA COFFEE LIQUEUR" sitting right there.

Before the user spends time clicking through the Recipe Cost Fixer UI, auto-link the obvious ones. This should cut the manual linking work by maybe half, leaving only the genuinely ambiguous cases for human judgment.

## What to build

A one-shot script at `scripts/autolink_recipe_ingredients.py`. Not a blueprint, not a route. A standalone Python script that runs from the command line.

```
python3 scripts/autolink_recipe_ingredients.py [--dry-run] [--min-confidence 0.85]
```

Default behavior is **dry-run**. User has to pass `--commit` to actually write.

## Matching strategy

For each ingredient row where `product_id IS NULL OR product_id = 0 OR` the product doesn't exist:

1. Normalize the ingredient text: lowercase, strip punctuation, collapse whitespace.
2. Normalize all product names the same way (both `products.name` and `products.display_name`).
3. Try matches in this order, stopping at the first hit:
   a. **Exact match** (after normalization) → confidence 1.0
   b. **Ingredient text is a contiguous substring of a product name** (e.g. "Kahlua" in "kahlua coffee liqueur") → confidence 0.95
   c. **Product name starts with the ingredient text as a word boundary** → confidence 0.90
   d. **Fuzzy ratio ≥ min-confidence** using `difflib.SequenceMatcher.ratio()` → confidence = ratio

4. If multiple products tie at the top confidence, **skip that row** — ambiguous, needs human. Log it as ambiguous in the output.

5. If the best match is below `--min-confidence` (default 0.85), **skip**. Log as "no confident match."

## Scoping the match

When searching for a product match for an ingredient, consider the recipe's `location` if set (`chatham` or `dennis`):
- First try products with matching `location`
- Then try products with `location IS NULL` (location-agnostic)
- Ignore products belonging to the *other* location

If recipe has no location, search all products.

Also, **only match against active products** (`products.active = 1`).

## Output

Print a report as it runs. One line per candidate:

```
[MATCH 0.95] Almond Joy Martini / "Amaretto"
  → product 523 "AMARETTO DISARONNO LIQ 750ML"
[MATCH 1.00] Aperol Spritz / "Club Soda"
  → product 781 "Club Soda"
[SKIP ambiguous] Old Fashioned / "Bitters"
  → 3 candidates at 0.88: product 401 (Angostura Bitters), product 402 (Peychaud's Bitters), product 403 (Orange Bitters)
[SKIP no-match] Margarita / "Salt rim"
  → best was 0.62 (product 891 "KOSHER SALT")
```

At the end, print a summary:
```
───────────────────────────────────────
Processed:       293 unlinked ingredients
Would link:      127 (43%)
Ambiguous:        18
No match:        148
───────────────────────────────────────
DRY RUN — no changes written. Pass --commit to apply.
```

## Commit behavior

If `--commit` is passed (and not `--dry-run`), UPDATE `recipe_ingredients` to set `product_id` for each matched row. Wrap in a transaction. Print a single line per commit: `Linked ingredient 4812 → product 523`. After commit, print a final summary confirming the count.

**Do NOT recost recipes in this script.** That's the user's job via the UI or via `cost_all_recipes()` separately. This script only fixes the linkage.

## Safety

- Dry-run by default. Require explicit `--commit`.
- Back up the DB automatically before committing. Write to `/opt/backups/toast_data_autolink_$(date +%Y%m%d_%H%M).db`. Don't clean up old backups here — that's for the CLAUDE.md backup policy to handle separately.
- Only touch rows where `product_id IS NULL OR product_id = 0 OR product_id` points to a missing product. Never overwrite an existing valid link.
- Don't touch ingredients whose recipe already has `total_cost > 0`. Those are already working; leave them alone.

## Constraints (from CLAUDE.md)

- Use `get_connection()` from `integrations/toast/data_store.py`. Never `sqlite3.connect()` directly.
- Database path comes from `TOAST_DB_PATH` env var — which `data_store.py` already handles.
- No Anthropic API calls. Pure local string matching.
- Script should be idempotent — running it twice after commit should find nothing new to link.

## Deliverables

1. `scripts/autolink_recipe_ingredients.py`
2. A short note at the top of the file explaining what it does and when to run it (so future-you understands)
3. Commit message: "Add one-shot autolink script for recipe ingredients"

## How to run after it's built

```bash
cd /opt/red-nun-dashboard
source venv/bin/activate   # or whatever venv path

# Dry run first — eyeball the output
python3 scripts/autolink_recipe_ingredients.py

# If it looks good, commit
python3 scripts/autolink_recipe_ingredients.py --commit

# Then recost all recipes
python3 -c "from integrations.toast.data_store import get_connection; from integrations.recipes.recipe_costing import cost_all_recipes; cost_all_recipes(get_connection())"
```

After that, launch the Recipe Cost Fixer UI to handle the remaining ambiguous/unmatched ingredients and the quantity entry.

## Out of scope

- Fixing quantities (the Recipe Cost Fixer UI handles that)
- Any UI or blueprint — this is a terminal script, not a feature
- Training a smarter matcher — if stdlib `difflib` isn't good enough, raise it with the user before reaching for `rapidfuzz` or anything else
- Handling the legitimately un-pricable items (garnishes, "splash of X", etc.) — those will just show up as "no match" and that's fine
