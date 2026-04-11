s = open('server.py', 'r').read()
old_start = s.find('@app.route("/api/cogs/summary")')
old_end = s.find('@app.route("/api/cogs/products")')

new_func = """@app.route("/api/cogs/summary")
def api_cogs_summary():
    location = request.args.get("location")
    start = request.args.get("start")
    end = request.args.get("end")
    if start and len(start) == 8:
        start = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    if end and len(end) == 8:
        end = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    if not start or not end:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    else:
        start_date = start
        end_date = end
    conn = get_connection()
    where = "WHERE invoice_date >= ? AND invoice_date <= ?"
    params = [start_date, end_date]
    if location:
        where += " AND location = ?"
        params.append(location)
    rows = conn.execute("SELECT vendor_name, SUM(order_total) as total, COUNT(*) as cnt FROM me_invoices " + where + " GROUP BY vendor_name ORDER BY total DESC", params).fetchall()
    hints = {"southern glazer": "LIQUOR", "l. knife": "LIQUOR", "martignetti": "LIQUOR", "atlantic beverage": "LIQUOR", "horizon beverage": "LIQUOR", "colonial wholesale": "BEER", "craft collective": "BEER", "cape cod beer": "BEER", "us foods": "FOOD", "reinhart": "FOOD", "performance food": "FOOD", "chefs warehouse": "FOOD", "cape fish": "FOOD", "sysco": "FOOD", "cintas": "NON_COGS", "unifirst": "NON_COGS", "cozzini": "NON_COGS", "rooter": "NON_COGS", "dennisport village": "NON_COGS", "caron group": "NON_COGS", "robert b. our": "NON_COGS", "marginedge": "NON_COGS"}
    cats = {}
    for row in rows:
        vname = (row["vendor_name"] or "").lower()
        matched = "OTHER"
        for hint, cat in hints.items():
            if hint in vname:
                matched = cat
                break
        if matched not in cats:
            cats[matched] = {"total": 0, "invoices": 0}
        cats[matched]["total"] += row["total"]
        cats[matched]["invoices"] += row["cnt"]
    conn.close()
    total = sum(c["total"] for c in cats.values())
    result = []
    for cat, data in sorted(cats.items(), key=lambda x: -x[1]["total"]):
        result.append({"category_type": cat, "total_cost": round(data["total"], 2), "invoice_count": data["invoices"], "pct_of_total": round((data["total"] / total * 100), 1) if total > 0 else 0})
    return jsonify({"period_start": start_date, "period_end": end_date, "total_cost": round(total, 2), "categories": result})


"""

s = s[:old_start] + new_func + s[old_end:]
open('server.py', 'w').write(s)
print("DONE")
