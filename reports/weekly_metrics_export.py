#!/usr/bin/env python3
"""
Weekly business-metrics export for the Cowork "numbers digest".

Why this exists: Cowork (the desktop assistant) can read files in the Drive
folder but cannot reach the live SQLite DB on the Beelink. This script runs on
the Beelink, computes the weekly metrics from the existing analytics functions,
and writes a JSON snapshot to the Drive-synced mirror so Cowork can pick it up
and produce the weekly digest.

It REUSES existing query logic (reports.analytics, recipes table) — it does not
reinvent any calculations.

Covers: revenue, labor cost %, pour/food cost % by category, sales mix,
recipe margins (worst offenders), and vendor price creep — for both locations.

Run manually:
    cd /opt/red-nun-dashboard && venv/bin/python3 -m reports.weekly_metrics_export

Cron (weekly, Monday 6:00 AM — after the prior week closes):
    0 6 * * 1  cd /opt/red-nun-dashboard && venv/bin/python3 -m reports.weekly_metrics_export >> /opt/red-nun-dashboard/logs/weekly_metrics.log 2>&1

Env: TOAST_DB_PATH, METRICS_OUTPUT (override output path), METRICS_DAYS (window, default 7).
"""
import os
import sys
import json
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Load .env so TOAST_DB_PATH etc. are available when run from cron.
try:
    from dotenv import load_dotenv
    env_path = os.path.join(_REPO_ROOT, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
except Exception:
    pass

from reports.analytics import (
    get_daily_revenue,
    get_labor_summary,
    get_daily_labor,
    get_labor_by_role,
    get_pour_cost_by_category,
    get_sales_mix,
    get_price_movers,
)
from integrations.toast.data_store import get_connection

# Default output: the diagnostics/ folder, which the rclone bisync filter already carries
# to Drive (same channel as scraper_health.md). reports/ is NOT in the bisync filter, so
# writing there never reaches G:\My Drive\Red NUn Dashboard. Lands at
# G:\My Drive\Red NUn Dashboard\diagnostics\weekly_metrics.json.
DEFAULT_OUTPUT = os.path.expanduser("~/cowork/red-nun-dashboard/diagnostics/weekly_metrics.json")
LOCATIONS = ["chatham", "dennis"]
WINDOW_DAYS = int(os.getenv("METRICS_DAYS", "7"))


def _safe(label, fn, *args, **kwargs):
    """Run a metric function, capturing errors instead of aborting the whole export."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[WARN] {label} failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return {"_error": str(e)}


def _recipe_margins(limit=15):
    """Worst-margin active recipes (highest food-cost %). Reads the recipes table directly."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT name, category, menu_price, cost_per_serving, food_cost_pct
            FROM recipes
            WHERE active = 1 AND menu_price > 0 AND food_cost_pct IS NOT NULL
            ORDER BY food_cost_pct DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "name": r["name"],
                "category": r["category"],
                "menu_price": r["menu_price"],
                "cost_per_serving": r["cost_per_serving"],
                "food_cost_pct": r["food_cost_pct"],
                "margin": round((r["menu_price"] or 0) - (r["cost_per_serving"] or 0), 2),
            }
            for r in rows
        ]
    finally:
        conn.close()


def _theoretical_food_cost(location, start_date, end_date):
    """
    Food cost % from recipe costs via the pmix mapping (mirrors /api/pmix/report).
    Only menu items mapped to a recipe with cost > 0 are counted; coverage_pct says
    how much of the window's revenue that represents, so the number is trustworthy.
    Revenue uses SUM(price) to match the existing PMIX report convention.
    """
    conn = get_connection()
    try:
        total_rev = conn.execute(
            "SELECT COALESCE(SUM(price), 0) FROM order_items "
            "WHERE voided = 0 AND location = ? AND business_date BETWEEN ? AND ?",
            (location, start_date, end_date),
        ).fetchone()[0] or 0

        rows = conn.execute(
            """
            SELECT r.category AS category,
                   COALESCE(SUM(oi.price), 0) AS revenue,
                   COALESCE(SUM(oi.quantity * r.cost_per_serving * COALESCE(pm.multiplier, 1)), 0) AS cost
            FROM order_items oi
            JOIN pmix_mapping pm ON pm.menu_item_name = oi.item_name
            JOIN recipes r ON pm.recipe_id = r.id
            WHERE oi.voided = 0 AND oi.location = ?
              AND oi.business_date BETWEEN ? AND ?
              AND r.cost_per_serving > 0
            GROUP BY r.category
            ORDER BY revenue DESC
            """,
            (location, start_date, end_date),
        ).fetchall()

        by_cat, costed_rev, total_cost = [], 0.0, 0.0
        for r in rows:
            rev, cost = (r["revenue"] or 0), (r["cost"] or 0)
            costed_rev += rev
            total_cost += cost
            by_cat.append({
                "category": r["category"],
                "revenue": round(rev, 2),
                "cost": round(cost, 2),
                "food_cost_pct": round(cost / rev * 100, 1) if rev > 0 else None,
            })

        return {
            "overall_food_cost_pct": round(total_cost / costed_rev * 100, 1) if costed_rev > 0 else None,
            "by_category": by_cat,
            "costed_revenue": round(costed_rev, 2),
            "window_revenue": round(total_rev, 2),
            "coverage_pct": round(costed_rev / total_rev * 100, 1) if total_rev > 0 else 0,
        }
    finally:
        conn.close()


# Non-product line items (fees/taxes/surcharges) that pollute the price-creep list.
_FEE_TERMS = (
    "sales tax", "delivery adjustment", "adjustment", "defe charge",
    "energy, insurance", "compliance", "surcharge", "deposit", "freight",
    "fuel", "gratuity", "service charge",
)


def _is_fee(name):
    n = (name or "").lower()
    return any(term in n for term in _FEE_TERMS)


def _clean_price_creep(creep, real_limit=12, review_limit=10):
    """Strip non-product fees + same-day noise; split likely unit-mismatches from real moves."""
    if not isinstance(creep, dict) or "increases" not in creep:
        return creep  # error payload or unexpected shape — pass through

    def split(items, is_mismatch):
        real, review = [], []
        for it in items or []:
            if _is_fee(it.get("product_name")):
                continue
            if it.get("current_date") and it.get("current_date") == it.get("previous_date"):
                continue  # same-day comparison = noise
            (review if is_mismatch(it.get("change_pct")) else real).append(it)
        return real[:real_limit], review[:review_limit]

    inc_real, inc_review = split(creep.get("increases"), lambda p: p is not None and p > 100)
    dec_real, dec_review = split(creep.get("decreases"), lambda p: p is not None and p < -60)
    return {
        "increases_real": inc_real,
        "increases_review_unit_mismatch": inc_review,
        "decreases_real": dec_real,
        "decreases_review_unit_mismatch": dec_review,
    }


def build_window():
    """Trailing WINDOW_DAYS full business days ending yesterday (YYYYMMDD)."""
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    end = today_et - timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def export():
    start_date, end_date = build_window()
    payload = {
        "exported_at": datetime.now().isoformat(),
        "window": {"start": start_date, "end": end_date, "days": WINDOW_DAYS},
        "locations": {},
        # Price creep is vendor-wide; filtered to drop fees + flag unit-mismatch noise.
        "price_creep": _clean_price_creep(_safe("price_movers", get_price_movers, limit=60)),
        "recipe_margins": _safe("recipe_margins", _recipe_margins),
    }

    for loc in LOCATIONS:
        payload["locations"][loc] = {
            "revenue_daily": _safe("daily_revenue", get_daily_revenue, loc, start_date, end_date),
            "labor_summary": _safe("labor_summary", get_labor_summary, loc, start_date, end_date),
            "labor_daily": _safe("daily_labor", get_daily_labor, loc, start_date, end_date),
            "labor_by_role": _safe("labor_by_role", get_labor_by_role, loc, start_date, end_date),
            "pour_cost": _safe("pour_cost", get_pour_cost_by_category, loc, start_date, end_date),
            "food_cost_theoretical": _safe("food_cost", _theoretical_food_cost, loc, start_date, end_date),
            "sales_mix": _safe("sales_mix", get_sales_mix, loc, start_date, end_date),
        }

    output = os.getenv("METRICS_OUTPUT", DEFAULT_OUTPUT)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    tmp = output + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, output)

    # Also drop a dated copy so we keep history Cowork can compare week-over-week.
    dated = os.path.join(os.path.dirname(output), f"weekly_metrics_{end_date}.json")
    try:
        with open(dated, "w") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception as e:
        print(f"[WARN] dated copy failed: {e}", file=sys.stderr)

    print(f"[OK] Wrote weekly metrics ({start_date}-{end_date}) to {output}")
    return output


if __name__ == "__main__":
    export()
