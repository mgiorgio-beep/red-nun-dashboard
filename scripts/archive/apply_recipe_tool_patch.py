#!/usr/bin/env python3
"""
apply_recipe_tool_patch.py — adds the get_recipe_costing_status tool to the
Telegram bot (bot.py in this folder).

What it does (2026-07-23, Jarvis):
  1. import re
  2. Recipe helpers after _bd(): _recipe_search_key, _recipe_revenue_map
  3. System-prompt line: recipe costing IS in the dashboard DB
  4. Tool definition: get_recipe_costing_status
  5. Tool implementation before get_food_cost

Safe: verifies every anchor exists first, backs up bot.py to bot.py.bak,
py_compiles the result before writing, and is a no-op if already applied.

Run:  cd /opt/red-nun-dashboard/bot && python3 apply_recipe_tool_patch.py
Then: sudo systemctl restart rednun-agent
"""

import py_compile
import shutil
import sys
import tempfile

BOT = "bot.py"

HELPERS = '''

# ---- Recipe costing helpers (schema per recipe_fixer_routes.py) ----

_RECIPE_VARIANT_RE = re.compile(r"\\s*\\(([^)]+)\\)\\s*$")


def _recipe_search_key(name):
    """'Burger - Nun (Turkey)' -> 'turkey nun burger' (same logic as the
    Recipe Cost Fixer's revenue matcher)."""
    if not name:
        return ""
    s = name.strip()
    variant = ""
    m = _RECIPE_VARIANT_RE.search(s)
    if m:
        variant = m.group(1).strip()
        s = s[: m.start()].strip()
    if " - " in s:
        prefix, core = [p.strip() for p in s.split(" - ", 1)]
        target = f"{variant} {core} {prefix}".strip() if variant else f"{core} {prefix}"
    else:
        target = f"{s} {variant}".strip() if variant else s
    return target.lower()


def _recipe_revenue_map(conn, days=90):
    """recipe_id -> {revenue, qty} over the last N days of order_items."""
    cutoff = int((datetime.now() - timedelta(days=days)).strftime("%Y%m%d"))
    item_rows = conn.execute(
        """SELECT LOWER(item_name) AS item_name,
                  SUM(COALESCE(quantity,0)) AS qty,
                  SUM(COALESCE(price,0)) AS rev
           FROM order_items
           WHERE COALESCE(voided,0)=0 AND business_date >= ?
             AND item_name IS NOT NULL
           GROUP BY LOWER(item_name)""",
        (cutoff,),
    ).fetchall()
    keyed = [(r["id"], _recipe_search_key(r["name"])) for r in conn.execute(
        "SELECT id, name FROM recipes WHERE active = 1").fetchall()]
    keyed = [(rid, k) for rid, k in keyed if k]
    keyed.sort(key=lambda x: len(x[1]), reverse=True)  # longest key wins
    out = {}
    for row in item_rows:
        for rid, key in keyed:
            if key in row["item_name"]:
                agg = out.setdefault(rid, {"revenue": 0.0, "qty": 0.0})
                agg["revenue"] += float(row["rev"] or 0)
                agg["qty"] += float(row["qty"] or 0)
                break
    return out
'''

PROMPT_ADD = '''

Recipe costing ALSO lives in the dashboard DB (recipes / recipe_ingredients
tables, maintained via the Recipe Cost Fixer at /recipes/fixer). Use
get_recipe_costing_status for costing progress and what to cost next —
never say recipe data isn't connected.'''

TOOL_DEF = '''    {
        "name": "get_recipe_costing_status",
        "description": "Recipe costing progress from the dashboard DB: active recipes fully costed vs zero-cost (split into empty shells with no ingredients vs recipes missing quantities), needs_research count, suspect high-cost recipes (>70% — usually unit bugs), and the top uncosted recipes by 90-day revenue. Use for 'how many recipes are costed', 'recipe costing status', 'what should I cost next'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "How many top uncosted-by-revenue recipes to list (default 5)"},
            },
        },
    },
'''

TOOL_IMPL = '''    elif name == "get_recipe_costing_status":
        try:
            top_n = int(args.get("top") or 5)
            conn = _dash_db()
            counts = conn.execute("""
                SELECT
                  SUM(CASE WHEN COALESCE(total_cost,0) > 0 THEN 1 ELSE 0 END) AS costed,
                  SUM(CASE WHEN COALESCE(total_cost,0) = 0 THEN 1 ELSE 0 END) AS zero_cost,
                  SUM(CASE WHEN COALESCE(total_cost,0) = 0
                            AND COALESCE(needs_research,0) = 1 THEN 1 ELSE 0 END) AS needs_research,
                  SUM(CASE WHEN COALESCE(food_cost_pct,0) > 70 THEN 1 ELSE 0 END) AS high_cost_suspect,
                  COUNT(*) AS total
                FROM recipes WHERE active = 1
            """).fetchone()
            shells = conn.execute("""
                SELECT COUNT(*) FROM recipes r
                WHERE r.active = 1 AND COALESCE(r.total_cost,0) = 0
                  AND NOT EXISTS (SELECT 1 FROM recipe_ingredients ri
                                  WHERE ri.recipe_id = r.id)
            """).fetchone()[0]
            revmap = _recipe_revenue_map(conn)
            uncosted = conn.execute("""
                SELECT id, name FROM recipes
                WHERE active = 1 AND COALESCE(total_cost,0) = 0
            """).fetchall()
            ranked = sorted(
                uncosted,
                key=lambda r: -revmap.get(r["id"], {}).get("revenue", 0.0),
            )[:top_n]
            conn.close()
            zero = counts["zero_cost"] or 0
            return json.dumps({
                "source": "dashboard DB recipes/recipe_ingredients + 90-day order_items revenue",
                "active_recipes": counts["total"],
                "fully_costed": counts["costed"],
                "pct_costed": round(100.0 * (counts["costed"] or 0) / counts["total"], 1) if counts["total"] else 0,
                "zero_cost": zero,
                "empty_shells": shells,
                "missing_quantities": zero - shells,
                "needs_research": counts["needs_research"],
                "high_cost_suspect_over_70pct": counts["high_cost_suspect"],
                "top_uncosted_by_90d_revenue": [
                    {"name": r["name"],
                     "revenue_90d": round(revmap.get(r["id"], {}).get("revenue", 0.0), 2),
                     "qty_sold_90d": int(revmap.get(r["id"], {}).get("qty", 0) or 0)}
                    for r in ranked
                ],
                "note": "Fill these in via the Recipe Cost Fixer (/recipes/fixer) or the printed gap worksheet.",
            })
        except Exception as e:
            return json.dumps({"error": f"dashboard DB: {e}"})

'''

EDITS = [
    # (anchor, mode, payload) — all string ops, no regex
    ("import os\nimport json", "REPLACE", "import os\nimport re\nimport json"),
    ('    return day, int(str(day).replace("-", ""))', "AFTER", HELPERS),
    ("(1 + burden) / net sales; burden covers employer taxes + workers comp.",
     "AFTER", PROMPT_ADD),
    ('    {\n        "name": "check_sync_status",', "BEFORE", TOOL_DEF),
    ('    elif name == "get_food_cost":', "BEFORE", TOOL_IMPL),
]


def main():
    src = open(BOT, encoding="utf-8").read()
    if "get_recipe_costing_status" in src:
        print("Already applied — nothing to do.")
        return

    for anchor, _, _ in EDITS:
        if src.count(anchor) != 1:
            sys.exit(f"ABORT: anchor not found exactly once, bot.py has changed:\n{anchor[:70]}")

    for anchor, mode, payload in EDITS:
        if mode == "REPLACE":
            src = src.replace(anchor, payload)
        elif mode == "AFTER":
            src = src.replace(anchor, anchor + payload)
        else:  # BEFORE
            src = src.replace(anchor, payload + anchor)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(src)
        tmp = tf.name
    py_compile.compile(tmp, doraise=True)  # raises (and leaves bot.py alone) on syntax error

    shutil.copy2(BOT, BOT + ".bak")
    open(BOT, "w", encoding="utf-8").write(src)
    print("Patched bot.py (backup at bot.py.bak). Now run:")
    print("  sudo systemctl restart rednun-agent")


if __name__ == "__main__":
    main()
