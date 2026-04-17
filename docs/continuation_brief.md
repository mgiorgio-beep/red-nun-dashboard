# Recipe Costing Work — Continuation Brief

Last session: April 16, 2026. Picked up from here.

## Where we are

**Goal:** Get actual food cost % visibility on Red Nun menu items, starting with highest-revenue recipes.

**Where the work lives:**

* Dashboard: https://dashboard.rednun.com/recipes/fixer
* Costing engine: `integrations/recipes/recipe\_costing.py`
* Fixer UI routes: `routes/recipe\_fixer\_routes.py`
* Fixer UI page: `web/static/recipe\_fixer.html`

**Current recipe state** (as of last session):

* 341 total recipes
* 71 with real total\_cost
* 270 zero-cost — split into three buckets:

  * **\~142 have ingredient rows with quantity=0 or unsaved conversions** → fixer UI's primary job
  * **128 are empty shells** (no rows in `recipe\_ingredients` at all) → can't use fixer as-is
  * **a few are active/valid with other issues** (e.g. IBC Root Beer at 502% fc from a product catalog bug)

**What was fixed in the last session:**

* Autolink script (`scripts/autolink\_recipe\_ingredients.py`) — 29 broken FKs repaired
* Costing engine: liquor "OZ means fl\_oz" interpretation
* Costing engine: blank recipe\_unit now returns `no\_unit` instead of silently multiplying by pack price
* Costing engine: PATH 3(B) unit normalization so saved conversions with `cs` match product pack units like `case`
* Costing engine: count unit equivalence — `slice` / `each` / `ea` / `ct` all treat as interchangeable
* Fixer UI: revenue-sort queue (matches `recipes.name` → `order\_items.item\_name` and sorts by 90-day revenue)
* Fixer UI: sort dropdown (Revenue impact / Alphabetical / Default)
* Fixer UI: high-cost-ratio sub-queue link (food\_cost\_pct > 70%)
* Conversion dialog: direction flipped correctly ("1 {purchase\_unit} = \[qty] {recipe\_unit}")

**Verified end-to-end:** Burger - B\&B went from $17.94/94% (bogus) to $4.90/25.8% (believable) after the fixes. Engine is now trustworthy on the paths it handles.

## Pick up here

### Decision to make at the start of the session

Two viable next moves. Pick one before starting.

**Path A — Grind the 142 "fillable" recipes through the fixer UI**

* Open `/recipes/fixer`, sort by Revenue impact, knock out the top 20-30 highest-revenue recipes
* Expected: 30-60 min to get meaningful food cost data on the menu items that actually matter
* Low risk, high leverage, no new code
* Starts producing real P\&L insight immediately

**Path B — Tackle the 128 empty-shell recipes first**

* Question to answer up front: where do the recipes live today (MarginEdge export, paper binder, chef knowledge, Toast menu engineering)?
* Also: do burger/sandwich variants (Turkey, Veggie versions) share ingredients with their beef counterparts except the protein?
* Likely tooling: (a) "Clone from variant" feature in fixer UI for recipe families, (b) MarginEdge CSV import if the data exists
* Higher complexity, higher reward if a source of truth exists

**Recommendation:** Start with A for 30-60 min to get momentum and real data. Then decide if B is worth the build or if manual entry is fine at that point.

### Known followups (deferred from last session)

1. **Unit test suite for `cost\_ingredient()`.** Four bug-fix commits today, all caught by manual inspection of burger totals. A table-driven pytest file in `tests/test\_recipe\_costing.py` would stop the regression whack-a-mole. The earlier brief specified 10 test cases — they're in chat history if needed, but Claude Code can redesign.
2. **Beverage catalog data quality.** `SODA ROOT BEER RTD` has `pack\_size=24 pack\_unit='oz' price=$30.13 price\_per\_unit=$30.13` — all three are wrong or contradictory. Probably not unique: bottled/canned beverages imported via OCR may have the same issue where "24/12oz" collapsed to `pack\_size=24 pack\_unit=oz`. One-shot SQL audit + fix script would clean the lot.
3. **IBC Root Beer** (`recipe\_id=550`) at 502% food cost — a symptom of #2 above. Fix will happen naturally once the catalog is cleaned.
4. **Autolink brief + fixer briefs** are in the repo root:

   * `autolink\_brief.md`
   * `recipe\_fixer\_brief.md`
   * `recipe\_fixer\_conversion\_addendum.md`
These are historical — don't re-run, but they show what was built and why.

5\. \*\*Email invoice poller is dead on the Beelink.\*\* 

&#x20;  `integrations/invoices/watchers/email\_invoice\_poller.py` hardcodes 

&#x20;  `/opt/rednun` (DigitalOcean path) in a `sys.path.insert` and `os.chdir` 

&#x20;  at line \~30. It has been crashing on every cron tick (every 5 min) 

&#x20;  since the Beelink migration. No invoices have been auto-OCR'd via email 

&#x20;  in months. Fix: replace hardcoded paths with dynamic resolution from 

&#x20;  \_\_file\_\_. BEFORE re-enabling: audit the dashboard@rednun.com inbox — 

&#x20;  on first successful run, the poller will try to process every 

&#x20;  unprocessed message, which could be hundreds of API calls at once. 

&#x20;  Decide whether to archive old mail first.

&#x20;  

&#x20;  Same pattern may exist in other watchers — check 

&#x20;  `integrations/invoices/watchers/drive\_invoice\_watcher.py` and 

&#x20;  `email\_poller.py` (which is actually a separate file per CLAUDE.md) 

&#x20;  before assuming this is isolated.

### Useful diagnostics to run at session start

```bash
# Current state snapshot
sqlite3 /var/lib/rednun/toast\_data.db "
SELECT 'recipes\_total', COUNT(\*) FROM recipes
UNION ALL SELECT 'recipes\_with\_cost', COUNT(\*) FROM recipes WHERE total\_cost > 0
UNION ALL SELECT 'recipes\_zero\_cost', COUNT(\*) FROM recipes WHERE total\_cost = 0 OR total\_cost IS NULL
UNION ALL SELECT 'recipes\_empty\_shell',
  COUNT(\*) FROM (SELECT r.id FROM recipes r LEFT JOIN recipe\_ingredients ri ON ri.recipe\_id=r.id
                 WHERE r.total\_cost = 0 OR r.total\_cost IS NULL
                 GROUP BY r.id HAVING COUNT(ri.id) = 0);
"

# Top 25 menu items by 90-day revenue (for prioritization)
sqlite3 -header -column /var/lib/rednun/toast\_data.db "
SELECT item\_name, COUNT(\*) AS times\_sold, ROUND(SUM(price), 2) AS revenue
FROM order\_items
WHERE business\_date >= strftime('%Y%m%d', 'now', '-90 days')
  AND voided = 0 AND item\_name IS NOT NULL
GROUP BY item\_name
ORDER BY revenue DESC
LIMIT 25;
"
```

### Working agreement

* Don't trust the engine on new classes of product without a spot-check. The pattern this session was: Claude Code reports fix shipped → user opens one recipe → unexpected number surfaces → trace back. Keep that loop.
* Before any engine change, run the per-ingredient dump on at least one representative recipe:

```python
  python3 -c "from integrations.toast.data\_store import get\_connection; from integrations.recipes.recipe\_costing import cost\_ingredient; conn=get\_connection(); \[print(dict(r), cost\_ingredient(dict(r), conn)) for r in conn.execute('SELECT \* FROM recipe\_ingredients WHERE recipe\_id=91')]"
  ```

* Backup policy still applies — any schema change or large migration: `cp /var/lib/rednun/toast\_data.db /opt/backups/toast\_data\_$(date +%Y%m%d\_%H%M).db`, then clean older `.db` backups.

### Environment reminders (see CLAUDE.md for full)

* Repo: `/opt/red-nun-dashboard` on Beelink; `C:\\Users\\giorg\\red-nun-dashboard` on Windows
* DB: `/var/lib/rednun/toast\_data.db`
* Service: `sudo systemctl restart rednun` after Python/template changes
* Flow: edit on Windows → push → pull on Beelink → restart

