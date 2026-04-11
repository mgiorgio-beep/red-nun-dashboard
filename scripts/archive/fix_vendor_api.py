s = open('server.py', 'r').read()

old_vendors_start = s.find('@app.route("/api/cogs/vendors")')
old_vendors_end = s.find('@app.route("/api/cogs/invoices")')

new_vendors = """@app.route("/api/cogs/vendors")
def api_cogs_vendors():
    location = request.args.get("location", "dennis")
    category = request.args.get("category", "")
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
    food_hints = ["us foods", "reinhart", "performance food", "chefs warehouse", "cape fish", "sysco"]
    bev_hints = ["southern glazer", "l. knife", "martignetti", "atlantic beverage", "horizon beverage", "colonial wholesale", "craft collective", "cape cod beer"]
    conn = get_connection()
    rows = conn.execute(\"\"\"
        SELECT vendor_name, COUNT(*) as invoice_count,
               SUM(order_total) as total_spent,
               MIN(invoice_date) as first_invoice,
               MAX(invoice_date) as last_invoice
        FROM me_invoices
        WHERE location = ? AND invoice_date >= ? AND invoice_date <= ?
        GROUP BY vendor_name
        ORDER BY total_spent DESC
    \"\"\", (location, start_date, end_date)).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    if category == "food":
        result = [r for r in result if any(h in r["vendor_name"].lower() for h in food_hints)]
    elif category == "bev":
        result = [r for r in result if any(h in r["vendor_name"].lower() for h in bev_hints)]
    return jsonify(result)

"""

s = s[:old_vendors_start] + new_vendors + s[old_vendors_end:]
open('server.py', 'w').write(s)
print("DONE - vendors API updated")
