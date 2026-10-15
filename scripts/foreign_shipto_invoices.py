"""
Invoices delivered somewhere other than the house they're filed under (READ-ONLY).

job080 (2026-10-08) flagged 12 US Foods invoices, $1,791.81, shipped to CAPT
PARKERS PUB (W. Yarmouth) and LANYARD (Harwich Port). Mike, 2026-10-14: these are
usually OUR orders — when a house can't make the delivery minimum, US Foods drops
it at a nearby restaurant for pickup. The scraper only sees invoices on Red Nun's
own US Foods accounts, so they are billed to us. This list is for reviewing them,
not proof of a problem.

Reads the ship-to the import saved (scanned_invoices.raw_extraction ->
ship_to_address; bill-to and customer name too for US Foods imports since
2026-10-14) and lists every invoice delivered:
  ELSEWHERE   — not either house's address (e.g. a pickup at another restaurant)
  OTHER HOUSE — the other Red Nun house (worth a look: wrong entity, or a
                deliberate drop at the other house)
For each: status, balance, and any payment found — Bill Pay (ap_payment_invoices,
by invoice id) or a portal / imported payment (vendor_payment_invoices, by number).

Nothing is changed. Run on Chatham:
  cd /opt/red-nun-dashboard && venv/bin/python3 scripts/foreign_shipto_invoices.py
  venv/bin/python3 scripts/foreign_shipto_invoices.py --vendor "US Foods" --csv /tmp/foreign.csv
"""
import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.invoices.processor import detect_location_from_address  # noqa: E402
from integrations.toast.data_store import get_connection  # noqa: E402


def saved(raw):
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def placed(address):
    """Which house an address is, by ZIP first. Main St and Route 28 run through half
    the Cape, so a ZIP that isn't 02633/02639 means neither house."""
    a = (address or "").lower()
    if "02633" in a:
        return "chatham"
    if "02639" in a:
        return "dennis"
    if re.search(r"\b\d{5}(?:-\d{4})?\b", a):
        return None
    return detect_location_from_address(address)


def payments(conn, inv):
    out = []
    for r in conn.execute(
        """SELECT p.id, p.payment_date, p.payment_method, p.check_number, p.status, a.amount_applied
           FROM ap_payment_invoices a JOIN ap_payments p ON p.id = a.payment_id
           WHERE a.invoice_id = ?""", (inv["id"],)):
        out.append(f"BillPay #{r['id']} {r['payment_date']} {r['payment_method'] or ''}"
                   f"{' chk ' + str(r['check_number']) if r['check_number'] else ''} "
                   f"${r['amount_applied'] or 0:,.2f} ({r['status']})")
    if inv["invoice_number"]:
        for r in conn.execute(
            """SELECT v.id, v.payment_date, v.payment_method, v.payment_ref, v.location, i.amount_paid
               FROM vendor_payment_invoices i JOIN vendor_payments v ON v.id = i.payment_id
               WHERE i.invoice_number = ?""", (inv["invoice_number"],)):
            out.append(f"Portal/import pmt #{r['id']} {r['payment_date']} {r['payment_method'] or ''} "
                       f"ref {r['payment_ref'] or '-'} [{r['location'] or '?'}] ${r['amount_paid'] or 0:,.2f}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--vendor", default="", help="vendor name contains (default: all vendors)")
    ap.add_argument("--csv", help="also write the list to this CSV file")
    args = ap.parse_args()

    conn = get_connection()
    rows = conn.execute(
        """SELECT id, location, vendor_name, invoice_number, invoice_date, total, balance,
                  status, payment_status, raw_extraction
           FROM scanned_invoices
           WHERE status != 'deleted' AND vendor_name LIKE ?
           ORDER BY invoice_date""", (f"%{args.vendor}%",)).fetchall()

    flagged, no_ship_to = [], 0
    for inv in rows:
        d = saved(inv["raw_extraction"])
        ship = (d.get("ship_to_address") or "").strip()
        if not ship:
            no_ship_to += 1
            continue
        house = placed(ship)
        if house == inv["location"]:
            continue
        kind = "ELSEWHERE" if house is None else "OTHER HOUSE"
        bill = (d.get("bill_to_address") or d.get("customer_name") or "").strip()
        flagged.append((kind, inv, ship, bill, payments(conn, inv)))
    conn.close()

    print(f"Checked {len(rows)} invoices{' for ' + repr(args.vendor) if args.vendor else ''}; "
          f"{no_ship_to} have no ship-to saved (not checked).")
    if not flagged:
        print("Nothing delivered elsewhere.")
        return
    total = 0.0
    for kind in ("ELSEWHERE", "OTHER HOUSE"):
        group = [f for f in flagged if f[0] == kind]
        if not group:
            continue
        print(f"\n== {kind}: {len(group)} invoice(s) ==")
        for _, inv, ship, bill, pays in group:
            total += inv["total"] or 0
            bal = inv["balance"] if inv["balance"] is not None else inv["total"]
            print(f"#{inv['id']:<6} {inv['invoice_date'] or '?':10} {inv['vendor_name'][:22]:22} "
                  f"inv {inv['invoice_number'] or '-':12} filed {inv['location']:8} "
                  f"${inv['total'] or 0:>9,.2f}  bal ${bal or 0:>9,.2f}  {inv['status']}/{inv['payment_status'] or '-'}")
            print(f"        ship-to: {ship}")
            if bill:
                print(f"        bill-to: {bill}")
            for p in pays:
                print(f"        payment: {p}")
    paid = sum(1 for f in flagged if f[4])
    print(f"\n{len(flagged)} invoice(s), ${total:,.2f} total; {paid} with a payment recorded against them.")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["kind", "id", "invoice_date", "vendor", "invoice_number", "filed_location", "total",
                        "balance", "status", "payment_status", "ship_to", "bill_to", "payments"])
            for kind, inv, ship, bill, pays in flagged:
                w.writerow([kind, inv["id"], inv["invoice_date"], inv["vendor_name"], inv["invoice_number"],
                            inv["location"], inv["total"], inv["balance"], inv["status"], inv["payment_status"],
                            ship, bill, " | ".join(pays)])
        print(f"Wrote {args.csv}")


if __name__ == "__main__":
    main()
