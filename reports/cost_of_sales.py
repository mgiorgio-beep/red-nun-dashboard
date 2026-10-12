"""
Food cost and bev cost for the Analytics tabs (rebuilt 2026-10-12).

The tabs lost their /api/cogs/* endpoints in the MarginEdge removal (2026-04-16).
This rebuilds them on the same two sources the P&L uses, so the numbers agree:

  sales — the daily sales journal's "Gross Sales: <category>" lines (Toast,
          before discounts; discounts aren't split by category in the journal)
  cost  — confirmed invoice lines at invoice date, resolved to COGS accounts,
          plus transfers between the houses (reports.profit_loss.cogs)

Food cost % = FOOD cost / food sales. Bev cost % = BEER+LIQUOR+WINE cost /
beer+liquor+wine sales. NA beverages, takeout supplies and other COGS are listed,
not folded into either percentage.
"""
from integrations.toast.data_store import get_connection
from reports.profit_loss import cogs, _INVOICE_LINES_SQL, COGS_TYPES, _r2

LOCATIONS = ("chatham", "dennis")
# Sales journal line -> invoice category_type
SALES_LINES = {
    "Gross Sales: Food": "FOOD",
    "Gross Sales: Beer": "BEER",
    "Gross Sales: Liquor": "LIQUOR",
    "Gross Sales: Wine": "WINE",
    "Gross Sales: NA Beverage": "NA_BEVERAGES",
}
BEV = ("BEER", "LIQUOR", "WINE")


def _iso(d):
    """YYYYMMDD (the Analytics page) or YYYY-MM-DD -> YYYY-MM-DD."""
    d = (d or "").strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def _pct(cost, sales):
    return round(cost / sales * 100, 1) if sales > 0 else None


def _sales(conn, location, start, end):
    names = list(SALES_LINES)
    rows = conn.execute(
        f"""
        SELECT li.journal_name AS name,
               ROUND(SUM(COALESCE(li.credit, 0) - COALESCE(li.debit, 0)), 2) AS amount
        FROM qb_journal_line_items li
        JOIN qb_journal_entries e ON e.id = li.entry_id
        WHERE e.location = ? AND e.entry_date BETWEEN ? AND ?
          AND e.entry_type = 'sales_journal'
          AND li.journal_name IN ({','.join('?' * len(names))})
        GROUP BY li.journal_name
        """,
        (location, start, end, *names),
    ).fetchall()
    out = {cat: 0.0 for cat in SALES_LINES.values()}
    for r in rows:
        out[SALES_LINES[r["name"]]] += r["amount"] or 0
    days = conn.execute(
        "SELECT COUNT(DISTINCT entry_date) FROM qb_journal_entries "
        "WHERE location = ? AND entry_date BETWEEN ? AND ? AND entry_type = 'sales_journal'",
        (location, start, end),
    ).fetchone()[0]
    return out, days or 0


def _vendors(conn, location, start, end, categories):
    types = sorted(COGS_TYPES)
    rows = conn.execute(
        f"""
        SELECT b.vendor AS vendor_name, ROUND(SUM(b.amount), 2) AS total_spent,
               COUNT(DISTINCT b.reference) AS invoice_count
        FROM ({_INVOICE_LINES_SQL}) b
        WHERE b.category IN ({','.join('?' * len(categories))})
          AND b.account_type IN ({','.join('?' * len(types))})
        GROUP BY b.vendor
        """,
        (location, start, end, *categories, *types),
    ).fetchall()
    return [dict(r) for r in rows]


def cost_of_sales(location, start, end, conn=None):
    """Food and bev cost for one house, or both (location None/'' = both)."""
    start, end = _iso(start), _iso(end)
    locs = [location] if location in LOCATIONS else list(LOCATIONS)
    own = conn is None
    if own:
        conn = get_connection()
    try:
        sales, cost, vendors = {}, {}, {}
        journal_days = {}
        for loc in locs:
            s, days = _sales(conn, loc, start, end)
            journal_days[loc] = days
            for cat, amt in s.items():
                sales[cat] = sales.get(cat, 0) + amt
            for line in cogs(conn, loc, start, end)["lines"]:
                cost[line["category"]] = cost.get(line["category"], 0) + line["amount"]
            for group, cats in (("food", ("FOOD",)), ("bev", BEV)):
                for v in _vendors(conn, loc, start, end, cats):
                    key = (group, v["vendor_name"])
                    cur = vendors.setdefault(key, {"vendor_name": v["vendor_name"], "total_spent": 0, "invoice_count": 0})
                    cur["total_spent"] += v["total_spent"] or 0
                    cur["invoice_count"] += v["invoice_count"] or 0
    finally:
        if own:
            conn.close()

    def top(group):
        rows = [dict(v, total_spent=_r2(v["total_spent"])) for (g, _), v in vendors.items() if g == group]
        return sorted(rows, key=lambda v: -v["total_spent"])

    food_sales, food_cost = _r2(sales.get("FOOD", 0)), _r2(cost.get("FOOD", 0))
    by_type = [{"type": t, "sales": _r2(sales.get(t, 0)), "cost": _r2(cost.get(t, 0)),
                "pct": _pct(cost.get(t, 0), sales.get(t, 0))} for t in BEV]
    bev_sales = _r2(sum(t["sales"] for t in by_type))
    bev_cost = _r2(sum(t["cost"] for t in by_type))
    other = sorted(({"category": c, "cost": _r2(v)} for c, v in cost.items()
                    if c not in ("FOOD", "NA_BEVERAGES") + BEV and round(v, 2)), key=lambda o: -o["cost"])
    return {
        "period": {"start": start, "end": end},
        "locations": locs,
        "journal_days": journal_days,   # sales journal days found per house
        "food": {"sales": food_sales, "cost": food_cost, "pct": _pct(food_cost, food_sales),
                 "vendors": top("food")},
        "bev": {"sales": bev_sales, "cost": bev_cost, "pct": _pct(bev_cost, bev_sales),
                "by_type": by_type, "vendors": top("bev")},
        "na_bev": {"sales": _r2(sales.get("NA_BEVERAGES", 0)), "cost": _r2(cost.get("NA_BEVERAGES", 0))},
        "other_cogs": other,
    }
