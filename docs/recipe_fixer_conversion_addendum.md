# Recipe Cost Fixer — Unit Conversion Addendum

Read this alongside `recipe_fixer_brief.md`. Same deliverable, same page, same blueprint — this addendum extends the functionality to handle unit-conversion gaps inline.

## Why this exists

When `cost_ingredient()` returns `source='no_conversion'`, the costing engine couldn't figure out how to convert the recipe unit (e.g. "1 each Coke") to the product's purchase unit (e.g. "5 gal / CS"). The recipe shows zero cost even when both the product link and quantity are correct.

This is common for bottled/canned drinks, portion-packed items, and anything sold in bulk but used in single-serve units. The `product_unit_conversions` table exists to solve this — we need to surface it in the fixer UI so the user can resolve these gaps on the spot.

## Schema (already exists — reference only)

```sql
CREATE TABLE product_unit_conversions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL,
    from_qty        REAL    NOT NULL DEFAULT 1,
    from_unit       TEXT    NOT NULL,
    to_qty          REAL    NOT NULL,
    to_unit         TEXT    NOT NULL DEFAULT 'oz',
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    source          TEXT    DEFAULT NULL,
    UNIQUE(product_id, from_unit)
);
```

Reading: "from_qty from_unit of product X equals to_qty to_unit." Example for Coke: `from_qty=1, from_unit='each', to_qty=12, to_unit='fl_oz'` means one "each" of this product is 12 fluid ounces.

The unique constraint is `(product_id, from_unit)` — one conversion per product per source unit.

## UI changes

### On each ingredient row

When `cost_ingredient()` returns `source='no_conversion'`, replace the "—" in the Line Cost column with a small pill/button labeled **"Set conversion"** (amber background, matching the warning color from CLAUDE.md). Clicking it opens an inline conversion dialog (not a modal — expand in place, below the row, so the user doesn't lose context).

The dialog shows:

- **Heading:** "Convert recipe units to product purchase units"
- **Context block** (read-only, for reference):
  - "Recipe uses: **{quantity} {recipe_unit}** of {ingredient_name}"
  - "Product is: **{active_vendor_item.pack_size} {active_vendor_item.pack_unit}**, priced at {purchase_price}"
- **Input form:**
  - Label: "1 {recipe_unit} of this product ="
  - Two inputs: a number field and a unit dropdown (same canonical unit list from the main brief plus the product's own `pack_unit`, `inventory_unit`, and `contains_unit` if set)
  - Small helper text: "e.g. '1 each = 12 fl_oz' for a 12oz can"
- **Buttons:** "Save conversion" · "Cancel"

After save, the row's cost recomputes live (same mechanism as quantity changes). The "Set conversion" pill disappears and is replaced by the actual cost with source label.

### Edit existing conversions

If a conversion already exists for `(product_id, recipe_unit)` but the cost still comes back wrong, the ingredient row should still expose it — replace "Set conversion" with a small "Edit conversion" link next to the cost. Clicking opens the same dialog, pre-populated with the existing values.

### Don't block the user

The conversion dialog is optional. The user can ignore it and move on. The recipe will just stay at zero cost for that ingredient. Same philosophy as the product-link picker — offer the fix, don't force it.

## API endpoints (add to the existing blueprint)

- `GET /api/recipe-fixer/conversion?product_id={id}&from_unit={unit}` — return the existing conversion if any, else `null`. Include the product's `pack_unit`, `inventory_unit`, `contains_unit`, `pack_size`, and `pack_contains` so the dialog can show the context block.

- `POST /api/recipe-fixer/conversion` — upsert a conversion row. Body:
  ```json
  {
    "product_id": 417,
    "from_qty": 1,
    "from_unit": "each",
    "to_qty": 12,
    "to_unit": "fl_oz"
  }
  ```
  Use `INSERT ... ON CONFLICT(product_id, from_unit) DO UPDATE SET to_qty=excluded.to_qty, to_unit=excluded.to_unit`. Set `source='manual_fixer_ui'` on the row for provenance. Return the saved row.

- `DELETE /api/recipe-fixer/conversion/{id}` — not strictly required, but nice to have. Skip if scope creeps.

Both endpoints behind `@login_required`, use `get_connection()`.

## Affects the existing ingredient-update endpoint

The existing `POST /api/recipe-fixer/ingredient/<ingredient_id>` from the main brief already calls `cost_ingredient()` and returns the live cost. That will continue to work after a conversion is saved — no changes needed. The conversion save just needs to trigger a re-call of that endpoint (or equivalent) from the client so the row updates.

## Detection logic

On each ingredient load (in `GET /api/recipe-fixer/recipe/<recipe_id>`), include in the response:

```json
"ingredients": [
  {
    ...,
    "cost_source": "vendor_item" | "standard_conversion" | "no_conversion" | "no_price",
    "needs_conversion": true   // only when source == "no_conversion"
  }
]
```

The client uses `needs_conversion` to decide whether to show the "Set conversion" pill.

## Out of scope (still)

- Bulk-setting conversions across multiple products
- Inferring conversions from `pack_size`/`pack_contains` automatically — if the costing engine could figure it out from those columns, it wouldn't return `no_conversion` in the first place
- Touching `recipe_costing.py` itself

## Test plan additions

After the base UI works, test:

1. Find a recipe with a drink/bottled item that's at `no_conversion` (try Coke, Sprite, Club Soda, Tonic — anything priced in gallons or cases sold by the bottle/can).
2. Open it in the fixer, see the "Set conversion" pill.
3. Set `1 each = 12 fl_oz` (or whatever's true for that product).
4. Confirm the cost resolves immediately, the pill disappears, and the source label becomes `standard_conversion`.
5. Reload the page, confirm the conversion persisted.
6. Open the same ingredient on a *different* recipe — the conversion should already apply (it's product-level, not recipe-level).

## Deliverable change vs main brief

Same four deliverables. Just extend them:
- `routes/recipe_fixer_routes.py` — add two endpoints, extend one response
- `web/static/recipe_fixer.html` — add the inline conversion dialog
- No sidebar change
- No extra migration (the table already exists)

Commit as part of the same "Add Recipe Cost Fixer UI" commit, or as a follow-up commit — your call.
