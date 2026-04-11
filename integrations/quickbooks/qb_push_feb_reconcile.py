#!/usr/bin/env python3
"""
Push Feb 2025 Red Buoy / Red Nun Chatham bank reconciliation entries to QuickBooks Online.
Reads feb_push_queue.csv (exported from Feb2025_Reconcile_Workpaper.xlsx Coding Plan sheet).

Entry types:
  EXP    → Purchase (Cash type) debiting gl_account, crediting Cape Cod Five (5975)
  CCP    → Purchase (Cash type) debiting credit card liability, crediting bank
  XFR    → Purchase or Deposit depending on direction, against Loan to Red Nun Dennisport
  DEP    → Deposit crediting Cash Sales, depositing to bank
  TOAST  → Deposit crediting Credit Card Sales, depositing to bank

Reuses OAuth helpers from qb_push.py (same directory).
"""
import os, sys, csv, json, time, base64, argparse
import urllib.request, urllib.parse, urllib.error
from pathlib import Path

# Reuse auth from qb_push.py
sys.path.insert(0, str(Path(__file__).parent))
from qb_push import (
    get_credentials, get_valid_token, get_realm_id,
    BASE_URL, TOKEN_URL,
)

def qbo_post(path, payload, realm_id, token):
    url = f"{BASE_URL}/v3/company/{realm_id}/{path}?minorversion=65"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return None, f"HTTP {e.code}: {body[:500]}"

BANK_ACCT_ID = "63"
BANK_ACCT_NAME = "Cape Cod Five (5975)"

def build_purchase(row, bank_id, bank_name):
    """EXP/CCP/XFR(debit) → cash-type Purchase debiting the GL account."""
    gl_id = str(row["gl_acct_id"]).strip()
    gl_name = row["gl_account"]
    payload = {
        "AccountRef": {"value": bank_id, "name": bank_name},
        "PaymentType": "Cash",
        "TxnDate": row["date"],
        "PrivateNote": (row["notes"] or row["description"])[:200],
        "Line": [{
            "Amount": round(float(row["debit"]), 2),
            "DetailType": "AccountBasedExpenseLineDetail",
            "Description": row["description"][:4000],
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": gl_id, "name": gl_name},
            },
        }],
    }
    return payload

def build_deposit(row, bank_id, bank_name):
    """DEP/TOAST/XFR(credit) → Deposit crediting the GL account."""
    gl_id = str(row["gl_acct_id"]).strip()
    gl_name = row["gl_account"]
    payload = {
        "DepositToAccountRef": {"value": bank_id, "name": bank_name},
        "TxnDate": row["date"],
        "PrivateNote": (row["notes"] or row["description"])[:200],
        "Line": [{
            "Amount": round(float(row["credit"]), 2),
            "DetailType": "DepositLineDetail",
            "Description": row["description"][:4000],
            "DepositLineDetail": {
                "AccountRef": {"value": gl_id, "name": gl_name},
            },
        }],
    }
    return payload

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="feb_push_queue.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N successful posts")
    ap.add_argument("--only", default="", help="only push these actions (comma-separated e.g. EXP,DEP)")
    ap.add_argument("--out", default="feb_push_results.csv")
    args = ap.parse_args()

    client_id, client_secret, _ = get_credentials()
    token = get_valid_token(client_id, client_secret)
    realm_id = get_realm_id(client_id, client_secret)

    only = set(a.strip().upper() for a in args.only.split(",") if a.strip())

    rows = []
    with open(args.csv, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    print(f"Loaded {len(rows)} rows from {args.csv}")
    if only:
        rows = [r for r in rows if r["action"].upper() in only]
        print(f"Filtered to {len(rows)} rows (--only {','.join(sorted(only))})")

    results = []
    ok = 0
    fail = 0
    skip_missing = 0

    for r in rows:
        act = r["action"].upper()
        gl_id = str(r["gl_acct_id"]).strip()
        # Skip rows with no GL account id (shouldn't happen post-classification)
        if not gl_id or gl_id == "0" or gl_id == "":
            print(f"  ⏭  row {r['stmt_row']:>3} {r['date']} {act:5s} ${float(r['debit'] or 0)+float(r['credit'] or 0):>8.2f} — NO GL ID, skipping")
            skip_missing += 1
            results.append({**r, "status": "SKIPPED_NO_GL", "qb_id": "", "error": "missing GL acct id"})
            continue

        if act in ("EXP", "CCP"):
            payload = build_purchase(r, BANK_ACCT_ID, BANK_ACCT_NAME)
            endpoint = "purchase"
        elif act == "XFR":
            if float(r["debit"] or 0) > 0:
                payload = build_purchase(r, BANK_ACCT_ID, BANK_ACCT_NAME)
                endpoint = "purchase"
            else:
                payload = build_deposit(r, BANK_ACCT_ID, BANK_ACCT_NAME)
                endpoint = "deposit"
        elif act in ("DEP", "TOAST"):
            payload = build_deposit(r, BANK_ACCT_ID, BANK_ACCT_NAME)
            endpoint = "deposit"
        else:
            print(f"  ⏭  row {r['stmt_row']} unknown action {act}, skipping")
            skip_missing += 1
            continue

        if args.dry_run:
            print(f"  DRY row {r['stmt_row']:>3} {r['date']} {act:5s} ${float(r['debit'] or 0)+float(r['credit'] or 0):>8.2f} → {endpoint} / {r['gl_account']} (#{gl_id})")
            results.append({**r, "status": "DRY", "qb_id": "", "error": ""})
            ok += 1
            if args.limit and ok >= args.limit:
                break
            continue

        result, err = qbo_post(endpoint, payload, realm_id, token)
        if result and (endpoint.capitalize() in result or "Purchase" in result or "Deposit" in result):
            key = "Purchase" if "Purchase" in result else "Deposit"
            txn = result[key]
            qb_id = txn.get("Id", "?")
            print(f"  ✅ row {r['stmt_row']:>3} {r['date']} {act:5s} ${float(r['debit'] or 0)+float(r['credit'] or 0):>8.2f} → {endpoint} #{qb_id} ({r['vendor'] or r['gl_account']})")
            ok += 1
            results.append({**r, "status": "OK", "qb_id": qb_id, "error": ""})
        else:
            print(f"  ❌ row {r['stmt_row']:>3} {r['date']} {act:5s} ${float(r['debit'] or 0)+float(r['credit'] or 0):>8.2f} → {err}")
            fail += 1
            results.append({**r, "status": "FAIL", "qb_id": "", "error": err or "unknown"})

        time.sleep(0.3)
        if args.limit and ok >= args.limit:
            print(f"  Reached --limit {args.limit}, stopping")
            break

    # Write results CSV
    if results:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        print(f"\nWrote results to {args.out}")

    print(f"\nDONE: ok={ok}  fail={fail}  skipped={skip_missing}")

if __name__ == "__main__":
    main()
