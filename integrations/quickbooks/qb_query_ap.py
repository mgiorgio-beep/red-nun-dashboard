#!/usr/bin/env python3
"""
qb_query_ap.py
Pull open A/P bills from QuickBooks Online, optionally filtered by vendor.

Usage:
  python3 qb_query_ap.py --out ~/ap_open.csv
  python3 qb_query_ap.py --vendor "Horizon" --out ~/ap_horizon.csv
  python3 qb_query_ap.py --vendor "Southern Glazer" --out ~/ap_sg.csv

Env vars required (same as qb_push.py):
  QB_CLIENT_ID, QB_CLIENT_SECRET, QB_REALM_ID
"""
import argparse, csv, json, sys, os, urllib.parse, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from qb_push import get_credentials, get_realm_id, get_valid_token, BASE_URL

def qbo_query(realm_id, token, sql):
    url = f"{BASE_URL}/v3/company/{realm_id}/query?query={urllib.parse.quote(sql)}&minorversion=65"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code}: {body[:600]}", file=sys.stderr)
        raise

def fetch_vendors(realm_id, token, name_like=None):
    vendors = {}
    start = 1
    while True:
        sql = f"SELECT Id, DisplayName FROM Vendor WHERE Active=true STARTPOSITION {start} MAXRESULTS 1000"
        r = qbo_query(realm_id, token, sql)
        items = r.get("QueryResponse", {}).get("Vendor", [])
        if not items:
            break
        for v in items:
            vendors[v["Id"]] = v.get("DisplayName","")
        if len(items) < 1000:
            break
        start += 1000
    if name_like:
        needle = name_like.lower()
        vendors = {k:v for k,v in vendors.items() if needle in v.lower()}
    return vendors

def fetch_bills(realm_id, token, vendor_ids=None):
    """Fetch all bills, optionally filtered to a set of vendor IDs."""
    bills = []
    start = 1
    while True:
        sql = f"SELECT * FROM Bill STARTPOSITION {start} MAXRESULTS 500"
        r = qbo_query(realm_id, token, sql)
        items = r.get("QueryResponse", {}).get("Bill", [])
        if not items:
            break
        for b in items:
            vid = b.get("VendorRef", {}).get("value")
            if vendor_ids is not None and vid not in vendor_ids:
                continue
            bills.append(b)
        if len(items) < 500:
            break
        start += 500
    return bills

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", help="Vendor name substring (case-insensitive)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--include-paid", action="store_true", help="Include fully paid bills")
    args = ap.parse_args()

    cid, sec, _ = get_credentials()
    realm = get_realm_id(cid, sec)
    tok   = get_valid_token(cid, sec)

    vendors = fetch_vendors(realm, tok, args.vendor)
    print(f"Matching vendors: {len(vendors)}")
    for vid, name in sorted(vendors.items(), key=lambda x: x[1]):
        print(f"  {vid}: {name}")

    if not vendors and args.vendor:
        print("No vendors matched — aborting.")
        sys.exit(1)

    bills = fetch_bills(realm, tok, set(vendors.keys()) if args.vendor else None)
    print(f"Bills fetched: {len(bills)}")

    rows = []
    for b in bills:
        vid    = b.get("VendorRef", {}).get("value","")
        vname  = b.get("VendorRef", {}).get("name","") or vendors.get(vid,"")
        total  = float(b.get("TotalAmt", 0) or 0)
        bal    = float(b.get("Balance", 0) or 0)
        if not args.include_paid and bal < 0.005:
            continue
        rows.append({
            "Id":         b.get("Id"),
            "DocNumber":  b.get("DocNumber",""),
            "TxnDate":    b.get("TxnDate",""),
            "DueDate":    b.get("DueDate",""),
            "Vendor":     vname,
            "VendorId":   vid,
            "TotalAmt":   f"{total:.2f}",
            "Balance":    f"{bal:.2f}",
            "Memo":       (b.get("PrivateNote","") or "").replace("\n"," ")[:200],
        })

    rows.sort(key=lambda r: (r["Vendor"], r["TxnDate"]))
    keys = ["Vendor","TxnDate","DueDate","DocNumber","TotalAmt","Balance","Memo","Id","VendorId"]
    out = Path(os.path.expanduser(args.out))
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    total_bal = sum(float(r["Balance"]) for r in rows)
    print(f"Open rows: {len(rows)}  Total open balance: {total_bal:,.2f}")
    print(f"Saved -> {out}")

if __name__ == "__main__":
    main()
