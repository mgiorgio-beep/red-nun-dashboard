# Recipe Cost Fixer — Build Brief

## Problem

Of 341 recipes in `recipes`, only 65 have a real `total_cost`. The other 276 are zero-cost.

Root cause: `recipe_autopopulate.py` parses Toast menu descriptions into ingredient lists. It captures ingredient *names* and even links many to `products`, but menu descriptions don't contain quantities — so every autopopulated ingredient row gets `quantity = 0`.

Breakdown of the 512 ingredient rows in the 276 zero-cost recipes:
- 128 rows: no `product_id` at all
- 155 rows: `product_id = 0`
- 10 rows: `product_id` set but product doesn't exist
- 219 rows: correctly linked to a priced product, but quantity = 0
- 455 of 512 have `quantity = 0` (the dominant blocker)

If quantities were filled in, the 219 already-linked ingredients would immediately produce real costs on the next run of `cost_all_recipes()`. That's the highest-leverage fix.

This task builds a UI to walk through those 276 recipes one at a time, enter quantities, fix broken product links inline, and see live food-cost feedback.

## What to build

A single page at `/recipes/fixer` that presents zero-cost recipes one at a time and lets the user enter ingredient quantities and fix product links. Live cost preview. Save + advance to the next recipe.

### Route structure

New blueprint: `routes/recipe_fixer_routes.py` → `recipe_fixer_bp`, mounted at `/recipes/fixer` (page) and `/api/recipe-fixer/*` (JSON endpoints).

Register it in `web/server.py` alongside the other blueprints. Follow the existing pattern.

### API endpoints

- `GET /api/recipe-fixer/worklist` — return summary of the queue: total zero-cost recipes, how many remain, how many marked "needs research." Also return the next recipe to work on (or `null` if done).
- `GET /api/recipe-fixer/recipe/<recipe_id>` — return full recipe detail: name, menu_price, location, serving_size, yield_qty/unit, and a list of ingredients. Each ingredient: `id`, `product_name` (the text captured by autopopulate), `quantity`, `unit`, `product_id`, linked product info (name, current_price, active vendor item pack/price/unit), and a flag indicating whether the product link is missing/broken.
- `GET /api/recipe-fixer/product-search?q=<query>` — fuzzy search against `products.name` and `products.display_name`. Return top 10 matches with id, name, current_price, active_vendor_item_id, pack_unit, inventory_unit. Used for the inline product picker.
- `POST /api/recipe-fixer/ingredient/<ingredient_id>` — update a single ingredient's `quantity`, `unit`, and/or `product_id`. Return the recomputed ingredient cost using `cost_ingredient()` from `integrations/recipes/recipe_costing.py` so the UI can show live feedback without saving the whole recipe.
- `POST /api/recipe-fixer/recipe/<recipe_id>/recost` — run `cost_recipe(recipe_id, conn)` and return updated `total_cost`, `cost_per_serving`, `food_cost_pct`. Call this when the user hits Save so the recipe totals persist to `recipes.total_cost`, `cost_per_serving`, `food_cost_pct`, and `last_costed_at`.
- `POST /api/recipe-fixer/recipe/<recipe_id>/skip` — mark the recipe as "needs research" so the worklist skips over it. Use a new column `recipes.needs_research INTEGER DEFAULT 0`. Add this column via ALTER TABLE if it doesn't exist (check `PRAGMA table_info(recipes)` first).
- `POST /api/recipe-fixer/recipe/<recipe_id>/unskip` — clear the flag.

All endpoints require `@login_required` (import from `routes/auth_routes.py`). Use `get_connection()` from `integrations/toast/data_store.py` — never `sqlite3.connect()` directly.

### Worklist ordering

Zero-cost recipes ordered by:
1. `needs_research = 0` first
2. Then recipes whose ingredients are mostly already linked to products (highest "linked ingredients / total ingredients" ratio) — these are the closest to being fixed, knock them out first
3. Then alphabetical by name

"Zero-cost" means `total_cost = 0 OR total_cost IS NULL`.

### Page UI (`/recipes/fixer`)

Single SPA-style page. Dark theme matching the existing dashboard (see Design System in `CLAUDE.md`). Layout:

**Top bar:** progress readout — "42 of 276 recipes fixed · 18 marked needs research · 216 remaining." Small reset button to re-query the worklist.

**Main card (per recipe):**
- Recipe name (large, heading)
- Small metadata row: location, category, menu price, serving size
- Ingredients table with one row per ingredient:
  - **Ingredient** column: the `product_name` text from autopopulate (read-only for now)
  - **Linked product** column: shows linked product name + current_price, OR shows a red "Not linked" pill with an inline search input. Clicking the product name opens a small picker to change it. Picker is an input that calls `/api/recipe-fixer/product-search?q=...` on debounce, shows results as a dropdown, clicking a result updates `product_id` for that ingredient.
  - **Quantity** column: number input (step 0.01, min 0)
  - **Unit** column: dropdown with the canonical options: fl_oz, oz, lb, g, kg, ml, l, tsp, tbsp, cup, pt, qt, gal, each, slice, pinch (match what `recipe_costing.py` handles in `WEIGHT_TO_OZ` and `VOLUME_TO_FLOZ`)
  - **Line cost** column: live-calculated, updates as the user types. Shows "—" if still zero or unresolvable. Shows the source (vendor_item / standard_conversion / no_conversion / no_price) as a small label.

- Below the table: **Totals row** — Total cost · Cost per serving · Food cost % (uses `menu_price` if present). All three update live. Food cost % color-coded: green <30%, amber 30-40%, red >40%.

- **Action row** at the bottom:
  - Primary button: **Save & Next** — POSTs any pending ingredient changes, runs recost, advances to next recipe.
  - Secondary: **Save** — same but stays on the current recipe.
  - Secondary: **Skip (needs research)** — marks recipe and advances.
  - Tertiary link: **Previous** — go back one recipe in the queue.

### Live cost preview behavior

Use the `cost_ingredient()` function from `integrations/recipes/recipe_costing.py` to compute line costs server-side. Don't duplicate its logic in JS. When a quantity/unit/product changes, POST to `/api/recipe-fixer/ingredient/<id>` with the new values, return the recomputed cost, and update the row. Debounce quantity changes ~300ms.

Recipe totals (total_cost, cost_per_serving, food_cost_pct) are computed on save via `cost_recipe()` — no need to recompute on every keystroke.

### Handling the orphan ingredients

When `product_id IS NULL` or `product_id = 0` or the product row is missing, the "Linked product" column shows "Not linked" with a search input right there. Typing triggers search; clicking a result saves the `product_id` for that ingredient row. Don't force the user to fix the link to enter a quantity — both fields are editable independently. But if the product is unlinked, the line cost stays "—" regardless of quantity.

### Sidebar nav

Add an entry to `web/static/sidebar.js` — "Recipe Fixer" — under whatever section has Recipes/Inventory. Badge it with the remaining count if easy (reuses the worklist endpoint).

## Critical constraints (from CLAUDE.md)

- Everything through `@login_required`
- Use `get_connection()` from `integrations/toast/data_store.py`
- Don't modify `inventory_routes.py`
- Port is 8080, `sudo systemctl restart rednun` to reload
- No silent API spend — this task doesn't need any Anthropic API calls, keep it that way
- Match existing code style (check how `routes/invoice_routes.py` or `routes/catalog_routes.py` are structured and follow the same patterns)

## Before you start

Back up the DB:
```bash
cp /var/lib/rednun/toast_data.db /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db
```

Then delete older .db backups per the backup policy in CLAUDE.md.

## Testing

After building, test in this order:

1. Schema migration ran cleanly: `sqlite3 /var/lib/rednun/toast_data.db "PRAGMA table_info(recipes)"` should show `needs_research` column.
2. Worklist endpoint returns counts matching: `sqlite3 /var/lib/rednun/toast_data.db "SELECT COUNT(*) FROM recipes WHERE total_cost = 0 OR total_cost IS NULL"` should equal the "remaining" number returned by `/api/recipe-fixer/worklist`.
3. Load `/recipes/fixer` in a browser, confirm the first recipe renders with its ingredients.
4. Change a quantity on an ingredient that's linked to a priced product (e.g. something under "Almond Joy Martini" — Kahlua, Absolut, heavy cream all have prices). Confirm line cost updates.
5. Save & Next. Confirm `recipes.total_cost` for that recipe is now nonzero in the DB.
6. Find an unlinked ingredient, use the product search, link it, set a quantity, confirm it costs correctly.
7. Skip a recipe. Confirm it disappears from the worklist but stays visible if you toggle "show all" (or however you expose that — optional).

## Out of scope

- Editing the ingredient *name* / adding/removing ingredient rows — the autopopulator already created them; this UI only fills in what's missing
- Bulk import of standard cocktail recipes — user said one-at-a-time
- Rerunning `recipe_autopopulate.py` — don't touch it
- Changing the `recipe_costing.py` costing engine itself — it works fine
- Anything outside the `/recipes/fixer` page

## Deliverables

1. `routes/recipe_fixer_routes.py` (blueprint + endpoints)
2. `web/static/recipe_fixer.html` (the page)
3. Changes to `web/server.py` (blueprint registration)
4. Changes to `web/static/sidebar.js` (nav entry)
5. A one-line ALTER TABLE migration for `recipes.needs_research`, run idempotently on blueprint load
6. Commit message: "Add Recipe Cost Fixer UI for zero-cost recipes"

## Context files to read first

- `CLAUDE.md` (root of repo) — full repo context
- `integrations/recipes/recipe_costing.py` — the costing engine you'll call
- `routes/invoice_routes.py` — reference for blueprint + endpoint style
- `web/static/invoices.html` — reference for page style and JS patterns
- `web/static/sidebar.js` — where to add the nav entry
