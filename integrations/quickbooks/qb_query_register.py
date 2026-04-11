#!/usr/bin/env python3
"""
qb_query_register.py
Pull all transactions hitting a specific QBO account for a date range,
using the TransactionList report API. Writes a CSV ready for matching
against a bank statement workpaper.

Usage:
  python3 qb_query_register.py --account-id 63 --start 2025-02-03 --end 2025-03-02 \
         --out ~/feb_ccf_register.csv

Env vars required (same as qb_push.py):
  QB_CLIENT_ID, QB_CLIENT_SECRET, QB_REALM_ID
  Reads/writes ~/.qb_tokens.json via qb_push helpers.
"""
import argparse, csv, json, sys, os, urllib.parse, urllib.request, urllib.error
from pathlib import Path

# Reuse auth helpers from qb_push.py (same directory)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from qb_push import (
    get_credentials, get_realm_id, get_valid_token, BASE_URL
)

def fetch_transaction_list(realm_id, token, account_id, start, end):
    """Call the GeneralLedger report API filtered by account.
    Returns one row per posting line that hit the filter account, which is
    what we need to tie out to a bank statement."""
    params = {
        "start_date":   start,
        "end_date":     end,
        "account":      str(account_id),
    }
    qs  = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/v3/company/{realm_id}/reports/GeneralLedger?{qs}&minorversion=65"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            report = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {url}\nBody: {body[:1000]}", file=sys.stderr)
        raise

    col_defs = [c.get("ColTitle","") for c in report.get("Columns", {}).get("Column", [])]
    rows = []

    def walk(rowset):
        for row in rowset.get("Row", []):
            if "Rows" in row:
                walk(row["Rows"])
            cols = row.get("ColData")
            if not cols:
                continue
            rec = {}
            for i, cell in enumerate(cols):
                key = (col_defs[i] if i < len(col_defs) else f"c{i}").strip() or f"c{i}"
                rec[key] = cell.get("value", "")
                if "id" in cell:
                    rec[key + "_id"] = cell["id"]
            rows.append(rec)

    rowset = report.get("Rows", {})
    walk(rowset)
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", required=True, help="QBO Account Id (e.g. 63 for Cape Cod Five)")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end",   required=True, help="YYYY-MM-DD")
    ap.add_argument("--out",   required=True, help="Output CSV path")
    args = ap.parse_args()

    client_id, client_secret, _ = get_credentials()
    realm_id = get_realm_id(client_id, client_secret)
    token    = get_valid_token(client_id, client_secret)

    rows = fetch_transaction_list(realm_id, token, args.account_id, args.start, args.end)

    out = Path(os.path.expanduser(args.out))
    if rows:
        # Collect all keys in a stable order
        preferred = ["Date","Transaction Type","Num","Posting","Name","Memo/Description","Split","Amount","Debit","Credit","Balance"]
        keys = []
        for p in preferred:
            for r in rows:
                if p in r and p not in keys:
                    keys.append(p)
        # Add any leftover
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
    else:
        keys = ["Date","Transaction Type","Num","Name","Memo/Description","Split","Amount"]

    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})

    # Summary — GL report uses a single signed Amount column.
    # For a bank (asset): positive = deposit/debit, negative = payment/credit.
    def fnum(v):
        try: return float(str(v).replace(",","")) if v not in ("", None) else 0.0
        except ValueError: return 0.0
    dr = cr = 0.0
    data_rows = 0
    for r in rows:
        # Skip summary rows like "Beginning Balance" / "Total"
        if not r.get("Date") or r.get("Date","").startswith(("Beginning","Total","Ending")):
            continue
        data_rows += 1
        amt = fnum(r.get("Amount"))
        if amt >= 0: dr += amt
        else:        cr += -amt
    print(f"Data rows: {data_rows} (total rows incl summary: {len(rows)})")
    print(f"Deposits (+ into bank):  {dr:,.2f}")
    print(f"Payments (- out of bank): {cr:,.2f}")
    print(f"Net movement:             {dr - cr:,.2f}")
    print(f"Saved -> {out}")

if __name__ == "__main__":
    main()
