#!/usr/bin/env python3
"""Load L. Knife & Son invoices from the portal's PDF export.

L. Knife's PDFs carry a text layer (dot-matrix layout: ITEM# QTY DESCRIPTION
PRICE DISC NET DEP EXT, then "Invoice Total"). Those parse to full line items.
Credit memos come as image-only reprints; they are listed and left for the
`--manual` file, e.g. [{"number":"85484","date":"2026-03-31","total":-180.0,
"description":"Sun Cruiser tea returned, ref inv 485484","category":"BEER"}].

Invoices already on file under the number are left alone (a scanned copy may
carry a credit netted in: Chatham 581876 is 1,931.90 on file = 3,363.40 -
credit 81876 of 1,431.50). New ones: source 'lknife_pdf', confirmed on
Mike's word, PDF copied into routes/invoice_images so the invoice viewer
shows it. Item categories by keyword (BEER default; LIQUOR/WINE/NA_BEVERAGES
by description; cooperage and deposit-return lines DEPOSIT, which maps to
Beer COGS so deposits paid and returned net out there).

After loading, prints a settle plan: each unmatched L. Knife bank line of
the location paired with the unlinked invoices (<= 120 days before the
draft, up to 6) that sum to it. Feed that to scripts/settle_bank_lines.py.

    python scripts/lknife_pdf_load.py --dir <pdfs> --location chatham [--manual m.json] [--apply]
"""
import argparse, glob, itertools, json, os, re, shutil, subprocess, sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-23"
VENDOR = "L. Knife & Son, Inc."
IMG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "routes", "invoice_images")
LIQUOR = ("VODKA", "GIN ", "RUM", "WHISK", "BOURBON", "TEQUILA", "SCOTCH", "LIQUEUR", "BRANDY", "COGNAC", "MEZCAL", "SPIRIT")
WINE = ("WINE", "CHARD", "CABERNET", "PINOT", "ROSE", "PROSECCO", "SAUV", "MERLOT", "CHAMPAGNE", "BRUT", "RIESLING", "MALBEC")
NA = ("N/A", " NA ", "NON-ALC", "NON ALC", "ATHLETIC", "SODA", "WATER", "GINGER BEER", "TONIC", "BEST DAY", "RUN WILD", "SELTZER WATER")
DEPOSIT = ("COOPERAGE", "COOPERG", "DEPOSIT", "BBL", "KEG RETURN", "SHELL", "PALLET")


def category(desc):
    u = " " + desc.upper() + " "
    if any(k in u for k in DEPOSIT):
        return "DEPOSIT"
    if any(k in u for k in NA):
        return "NA_BEVERAGES"
    if any(k in u for k in LIQUOR):
        return "LIQUOR"
    if any(k in u for k in WINE):
        return "WINE"
    return "BEER"


def parse(pdf):
    t = subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True).stdout
    m = re.search(r"Invoice#:\s*(\d+)", t)
    if not m:
        return None
    num = m.group(1)
    d = datetime.strptime(re.search(r"^\s*\w{3} (\w{3} \d{2}, \d{4})", t, re.M).group(1), "%b %d, %Y").strftime("%Y-%m-%d")
    total = float(re.search(r"Invoice Total\s+(-?[\d,]+\.\d\d)", t).group(1).replace(",", ""))
    items = []
    lines = t.splitlines()
    for i, ln in enumerate(lines):
        h = re.match(r"^\s*(\d{5,6})\s+(-?\d+)\s+(.+?)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*$", ln)
        if not h:
            continue
        code, qty, desc, price, disc, net, dep, ext = h.groups()
        sub = lines[i + 1].strip() if i + 1 < len(lines) and re.match(r"^\s{10,}\S", lines[i + 1]) and not re.match(r"^\s*\d{5,6}\s", lines[i + 1]) else ""
        name = (desc.strip() + (" " + sub if sub else "")).title()
        items.append({"code": code, "qty": float(qty), "name": name, "price": float(price), "ext": float(ext)})
    assert abs(sum(i["ext"] for i in items) - total) < 0.005, f"{pdf}: items {sum(i['ext'] for i in items)} != total {total}"
    return {"number": num, "date": d, "total": total, "items": items, "pdf": pdf}


def insert_invoice(conn, loc, inv, now, note, image_path=None):
    subtotal = round(sum(i["ext"] for i in inv["items"] if i["ext"] > 0), 2) if inv.get("items") else inv["total"]
    cur = conn.execute(
        """INSERT INTO scanned_invoices (location, vendor_name, invoice_number, invoice_date, subtotal, tax, total, category, status,
               notes, source, confirmed_by, confirmed_at, payment_status, image_path, auto_confirmed, confidence_score, is_low_confidence,
               raw_extraction)
           VALUES (?,?,?,?,?,0,?,'BEER','confirmed',?,'lknife_pdf',?,?,'unpaid',?,0,100,0,?)""",
        (loc, VENDOR, inv["number"], inv["date"], subtotal, inv["total"], note, WHO, now, image_path,
         json.dumps({k: v for k, v in inv.items() if k != "pdf"})))
    iid = cur.lastrowid
    for it in inv["items"]:
        unit = "keg" if re.search(r"\bK-", it["name"], re.I) or "GAL" in it["name"].upper() else "case"
        conn.execute(
            """INSERT INTO scanned_invoice_items (invoice_id, product_name, description, quantity, unit, unit_price, total_price,
                   category_type, vendor_item_code) VALUES (?,?,?,?,?,?,?,?,?)""",
            (iid, it["name"], it["name"], it["qty"], unit, it["price"], it["ext"], it.get("category") or category(it["name"]), it.get("code")))
    return iid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--location", required=True, choices=("chatham", "dennis"))
    ap.add_argument("--manual")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    loc = a.location
    try:
        onfile = {r["invoice_number"]: r["total"] for r in conn.execute(
            "SELECT invoice_number, total FROM scanned_invoices WHERE location=? AND UPPER(vendor_name) LIKE '%KNIFE%'", (loc,))}
        created, image_only, differs = [], [], []
        for pdf in sorted(glob.glob(os.path.join(a.dir, "*.pdf"))):
            inv = parse(pdf)
            if not inv:
                image_only.append(os.path.basename(pdf)); continue
            if inv["number"] in onfile:
                if abs(onfile[inv["number"]] - inv["total"]) > 0.005:
                    row = conn.execute("SELECT id, source, total FROM scanned_invoices WHERE location=? AND invoice_number=? "
                                       "AND UPPER(vendor_name) LIKE '%KNIFE%'", (loc, inv["number"])).fetchone()
                    if row["source"] == "csv":
                        # The VTInfo CSV import dropped lines (Dennis 453266: 3 of 8
                        # items, 459.40 of 593.60). The PDF is the vendor's own
                        # invoice; replace the lines and total from it.
                        conn.execute("DELETE FROM scanned_invoice_items WHERE invoice_id=?", (row["id"],))
                        for it in inv["items"]:
                            unit = "keg" if re.search(r"\bK-", it["name"], re.I) or "GAL" in it["name"].upper() else "case"
                            conn.execute("INSERT INTO scanned_invoice_items (invoice_id, product_name, description, quantity, unit, unit_price, "
                                         "total_price, category_type, vendor_item_code) VALUES (?,?,?,?,?,?,?,?,?)",
                                         (row["id"], it["name"], it["name"], it["qty"], unit, it["price"], it["ext"], category(it["name"]), it["code"]))
                        dest = os.path.join(IMG_DIR, f"{loc}_lknife_{inv['number']}.pdf")
                        if a.apply:
                            shutil.copyfile(pdf, dest)
                        conn.execute("UPDATE scanned_invoices SET subtotal=?, total=?, amount_paid=NULL, balance=NULL, image_path=?, "
                                     "notes=COALESCE(notes,'') || ? WHERE id=?",
                                     (round(sum(i["ext"] for i in inv["items"] if i["ext"] > 0), 2), inv["total"], dest,
                                      f" | lines and total replaced from the L. Knife PDF on 2026-09-23 (CSV copy had {row['total']}, PDF says {inv['total']})",
                                      row["id"]))
                        print(f"FIXED FROM PDF: #{inv['number']} {row['total']} -> {inv['total']} ({len(inv['items'])} items)")
                    else:
                        differs.append((inv["number"], inv["date"], inv["total"], onfile[inv["number"]]))
                continue
            dest = os.path.join(IMG_DIR, f"{loc}_lknife_{inv['number']}.pdf")
            if a.apply:
                shutil.copyfile(pdf, dest)
            insert_invoice(conn, loc, inv, now, "Parsed from the L. Knife PDF export (text layer), 2026-09-23.", dest)
            created.append(inv)
        for m in (json.load(open(a.manual)) if a.manual else []):
            if m["number"] in onfile:
                continue
            inv = {"number": m["number"], "date": m["date"], "total": m["total"],
                   "items": [{"code": None, "qty": 1.0, "name": m["description"], "price": m["total"], "ext": m["total"],
                              "category": m.get("category", "BEER")}]}
            insert_invoice(conn, loc, inv, now, f"Image-only credit memo, keyed by hand from the PDF: {m['description']}.", m.get("pdf"))
            created.append(inv)
        print(f"{loc}: created {len(created)} invoices, {sum(i['total'] for i in created):.2f}")
        for i in sorted(created, key=lambda x: x["date"]):
            print(f"   #{i['number']} {i['date']} {i['total']:>9.2f}  {len(i['items'])} items")
        print("image-only PDFs (not parsed):", image_only)
        for d in differs:
            print(f"ON FILE BUT DIFFERENT: #{d[0]} {d[1]} pdf {d[2]} vs db {d[3]}")

        # settle plan
        inv_rows = conn.execute("SELECT invoice_number, invoice_date, total FROM scanned_invoices WHERE location=? AND status='confirmed' "
                                "AND UPPER(vendor_name) LIKE '%KNIFE%'", (loc,)).fetchall()
        linked = {r[0] for r in conn.execute("SELECT vpi.invoice_number FROM vendor_payment_invoices vpi JOIN vendor_payments v ON v.id=vpi.payment_id "
                                             "WHERE v.cleared=1 AND UPPER(v.vendor) LIKE '%KNIFE%'")}
        pool_all = [dict(r) for r in inv_rows if r["invoice_number"] not in linked]
        lines = conn.execute("""SELECT m.id, m.entry_date, m.amount FROM manual_bank_entries m JOIN bank_accounts b ON b.id=m.bank_account_id
                                WHERE b.location=? AND UPPER(m.payee) LIKE '%KNIFE%' ORDER BY m.entry_date""", (loc,)).fetchall()
        plan, used, unmatched = [], set(), []
        for L in lines:
            amt = round(abs(L["amount"]), 2); ld = date.fromisoformat(L["entry_date"])
            pool = [i for i in pool_all if i["invoice_number"] not in used and 0 <= (ld - date.fromisoformat(i["invoice_date"])).days <= 120]
            hit = next((c for k in range(1, min(6, len(pool)) + 1) for c in itertools.combinations(pool, k)
                        if abs(sum(i["total"] for i in c) - amt) < 0.005), None)
            if hit:
                used.update(i["invoice_number"] for i in hit)
                plan.append({"line": L["id"], "vendor": VENDOR, "invoices": [i["invoice_number"] for i in hit]})
                print(f"PLAN line {L['id']} {L['entry_date']} {amt:>8.2f} = " + " + ".join(f"#{i['invoice_number']}({i['invoice_date'][5:]} {i['total']})" for i in hit))
            else:
                unmatched.append((L["id"], L["entry_date"], amt))
        print("still unmatched:", unmatched)
        out = os.path.join(a.dir, f"settle_plan_{loc}.json")
        json.dump(plan, open(out, "w"), indent=1); print("plan written:", out)
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
