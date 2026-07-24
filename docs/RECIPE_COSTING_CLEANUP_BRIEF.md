# Recipe Costing Cleanup — Claude Code Brief

**Context:** `scripts/draft_all_recipes.py` batch-drafted AI ingredient lists for every
empty-shell recipe (results: `data/draft_all_results.json`). Your job is the judgment pass:
make the drafted recipes actually cost out, and sanity-check the numbers. Mike approved this
work 2026-07-23. Work directly on this box (`/opt/red-nun-dashboard`, SQLite via
`integrations.toast.data_store.get_connection()`).

## What to fix, in priority order
1. **Recipes still $0 after drafting** (`status: ok, total_cost: 0` in the results JSON) —
   their rows are unlinked or missing conversions.
2. **Food cost > 40%** — almost always a pack-data bug, not a real number. Also anything
   under ~5% on a food item (suspiciously cheap = usually a catch-weight or pack bug).
3. Rows with `cost_source` of `no_price` / `no_conversion` in otherwise-costed recipes.

## The playbook (proven on 12 recipes 7/23 — see memory `project_rednun_recipe_costing_batch`)
- **Orphan products:** the AI draft often links rows to duplicate products named with raw US Foods
  invoice descriptions that have NO vendor item (`products.active_vendor_item_id IS NULL`) →
  `cost_source = no_price`. Find the twin product that HAS an active vendor item (same
  `current_price` is a strong signal, else name similarity) and repoint
  `recipe_ingredients.product_id`. Canonical picks already established: chicken tenders = product
  **114**, fries = **314**, BBQ sauce = **95**, basket liners = **249**, 2-oz soufflé cups = **92**.
- **Conversions:** the costing engine (`integrations/recipes/recipe_costing.py`) only does
  single-hop lookups in `product_unit_conversions` — store `1 <purchase unit, usually 'cs'> =
  <total> <canonical unit>` (canonical units: fl_oz, oz, lb, g, kg, ml, l, tsp, tbsp, cup, pt,
  qt, gal, each, slice, pinch). Get the real pack contents from the ACTIVE `vendor_items` row
  (`pack_size` strings like "4/1 GA", "12/200 EA", "6/#10 CN" tell you the truth).
- **⚠️ PATH-ORDER TRAP:** if `vendor_items.pack_contains` is WRONG (classic bug: "4/1 GA" case
  recorded as `pack_contains=1, contains_unit='gal'`), the engine's standard volume/weight paths
  fire BEFORE conversions and produce 4× costs. A conversion row cannot override it — you must
  fix the `vendor_items` row itself (`pack_contains`, and `price_per_unit = purchase_price/pack_contains`).
  Sweep for more of these: `SELECT id, vendor_description, pack_size, pack_contains FROM
  vendor_items WHERE pack_size LIKE '%/%' AND pack_contains = 1;` — verify each against the
  pack_size string before fixing.
- **Catch-weight items** (price is per LB, `pack_size` like "3/2/8.55 LBA"): set the product's
  `pack_size=1, pack_unit='LB'` so per-lb pricing computes. For cooked-yield items (pulled pork
  etc.) set `products.yield_pct` as a decimal (0.55 = 55% cooking yield).
- **Single-pour beverages** (wine/beer/spirits recipes, `status: no_ingredients`): out of scope —
  leave them; pour costing is a separate project (wine costing punch list exists).
- IDs: `products.id` ≠ `vendor_items.id`. ALWAYS `SELECT` and eyeball the row before any `UPDATE`.

## After each fix
Recost via `python3 -c` calling `integrations.recipes.recipe_costing.cost_recipe(<id>, conn)`,
or POST the fixer endpoints through `app.test_client()` with a faked session (see
`scripts/draft_all_recipes.py` for the pattern).

## Deliverable
Update `data/draft_all_results.json` sidecar or write `data/cleanup_report.md`: recipes fixed,
recipes still broken and why, vendor_items rows corrected, and a final distribution of food-cost
% across all costed recipes. Leave AI-guessed quantities alone (Mike reviews those in the Fixer —
they're flagged with `recipe_ingredients.notes='ai_draft'`).

## House rules
- Any code changes: commit AND push the same session (dirty tree blocks the auto-deployer).
- Never touch rednun.com DNS. No new API spend without explicit approval — your own reasoning
  is fine, calling the Anthropic API from scripts is not (the drafts are already done).
- SQLite is live under gunicorn (WAL) — keep transactions short.
