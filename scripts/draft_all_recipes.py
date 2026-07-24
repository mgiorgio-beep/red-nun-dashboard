#!/usr/bin/env python3
"""
draft_all_recipes.py — batch-run the Recipe Fixer's AI draft over every
empty-shell recipe (0 ingredients), then recost each one.

Approved by Mike 2026-07-23 ("run all the remaining recipes") — explicit
one-off batch; each recipe = one Claude API call on ANTHROPIC_API_KEY.

Run on the Beelink from the repo root:
    cd /opt/red-nun-dashboard
    nohup python3 scripts/draft_all_recipes.py > /tmp/draft_all.log 2>&1 &
    tail -f /tmp/draft_all.log

Safe to re-run any time: recipes that already have ingredients are skipped.
Optional: --limit N to test on the first N recipes.
Results JSON: data/draft_all_results.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from web.server import app  # reuses the real endpoints; scheduler is file-lock guarded


def main():
    limit = None
    if len(sys.argv) > 2 and sys.argv[1] == "--limit":
        limit = int(sys.argv[2])

    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["role"] = "admin"

    w = c.get("/api/recipe-fixer/worklist?sort=revenue&include_research=1").get_json()
    queue = [q for q in w["queue"] if q["total_ings"] == 0]
    if limit:
        queue = queue[:limit]
    print(f"{len(queue)} empty-shell recipes to draft "
          f"(of {w['remaining']} zero-cost). Starting...", flush=True)

    results, ok, empty, fail = [], 0, 0, 0
    for i, q in enumerate(queue, 1):
        rid, name = q["id"], q["name"]
        try:
            r = c.post(f"/api/recipe-fixer/recipe/{rid}/ai-draft")
            j = r.get_json() or {}
            if r.status_code == 409:
                print(f"[{i}/{len(queue)}] SKIP {name} (already has ingredients)", flush=True)
                continue
            if r.status_code != 200:
                fail += 1
                print(f"[{i}/{len(queue)}] FAIL {name}: {j.get('error', r.status_code)}", flush=True)
                results.append({"id": rid, "name": name, "status": "fail",
                                "error": j.get("error")})
                time.sleep(2)
                continue
            n = len(j.get("ingredients", []))
            if n == 0:
                empty += 1
                print(f"[{i}/{len(queue)}] EMPTY {name} ({j.get('message', '')})", flush=True)
                results.append({"id": rid, "name": name, "status": "no_ingredients"})
                continue
            rc = c.post(f"/api/recipe-fixer/recipe/{rid}/recost").get_json() or {}
            ok += 1
            print(f"[{i}/{len(queue)}] OK {name}: {n} rows ({j.get('matched', 0)} linked) "
                  f"-> ${rc.get('total_cost', 0):.2f} / {rc.get('food_cost_pct', 0):.1f}%",
                  flush=True)
            results.append({"id": rid, "name": name, "status": "ok", "rows": n,
                            "matched": j.get("matched"), "unmatched": j.get("unmatched"),
                            "total_cost": rc.get("total_cost"),
                            "food_cost_pct": rc.get("food_cost_pct"),
                            "revenue_90d": q.get("revenue_90d")})
        except Exception as e:
            fail += 1
            print(f"[{i}/{len(queue)}] ERROR {name}: {e}", flush=True)
            results.append({"id": rid, "name": name, "status": "error", "error": str(e)})
        time.sleep(1)  # gentle API pacing

    out = "data/draft_all_results.json"
    json.dump({"run_finished": time.strftime("%Y-%m-%d %H:%M:%S"),
               "drafted": ok, "empty": empty, "failed": fail,
               "results": results}, open(out, "w"), indent=1)

    still_zero = [r for r in results if r.get("status") == "ok" and not r.get("total_cost")]
    rich = [r for r in results if r.get("status") == "ok"
            and (r.get("food_cost_pct") or 0) > 40]
    print(f"\nDONE: {ok} drafted+costed, {empty} returned no ingredients "
          f"(single-pour beverages etc.), {fail} failed.", flush=True)
    print(f"Still $0 after draft (need manual links/conversions): {len(still_zero)}", flush=True)
    print(f"Food cost > 40% (probably unit bugs, review first): "
          f"{[r['name'] for r in rich][:15]}", flush=True)
    print(f"Full results: {out}", flush=True)


if __name__ == "__main__":
    main()
