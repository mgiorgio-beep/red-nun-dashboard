#!/usr/bin/env python3
"""
Push cash deposits to QuickBooks Online as Bank Deposits.
Cape Cod Five (5975) → Daily Sales:Cash Sales
Source: Jan 2025 bank statement for Red Nun Chatham
"""

import json, os, sys, time, base64
import urllib.request, urllib.parse, urllib.error
from pathlib import Path

# ── Config ──
TOKEN_FILE = Path.home() / ".qb_tokens.json"
TOKEN_URL  = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
BASE_URL   = "https://quickbooks.api.intuit.com"
REALM_ID   = os.environ.get("QB_REALM_ID", "123146237986854")

def load_tokens():
    with open(TOKEN_FILE) as f:
        return json.load(f)

def save_tokens(tokens):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    TOKEN_FILE.chmod(0o600)

def refresh_access_token(tokens):
    client_id = os.environ.get("QB_CLIENT_ID")
    client_secret = os.environ.get("QB_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Missing QB_CLIENT_ID / QB_CLIENT_SECRET env vars")
    creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Authorization", f"Basic {creds_b64}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req) as resp:
        new_tokens = json.loads(resp.read())
    new_tokens["obtained_at"] = time.time()
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens["refresh_token"]
    new_tokens["realm_id"] = tokens.get("realm_id", REALM_ID)
    save_tokens(new_tokens)
    return new_tokens

def get_valid_token():
    tokens = load_tokens()
    age = time.time() - tokens.get("obtained_at", 0)
    if age > 3000:
        tokens = refresh_access_token(tokens)
    return tokens["access_token"], tokens

def qbo_post(path, payload, tokens):
    url = f"{BASE_URL}/v3/company/{REALM_ID}/{path}?minorversion=65"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {tokens['access_token']}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()), tokens
    except urllib.error.HTTPError as e:
        if e.code == 401:
            tokens = refresh_access_token(tokens)
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Authorization", f"Bearer {tokens['access_token']}")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read()), tokens
        else:
            body = e.read().decode()
            print(f"  ❌ HTTP {e.code}: {body[:500]}")
            return None, tokens

def qbo_query(sql, tokens):
    url = f"{BASE_URL}/v3/company/{REALM_ID}/query?query={urllib.parse.quote(sql)}&minorversion=65"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {tokens['access_token']}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()), tokens
    except urllib.error.HTTPError as e:
        if e.code == 401:
            tokens = refresh_access_token(tokens)
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {tokens['access_token']}")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read()), tokens
        else:
            body = e.read().decode()
            print(f"  ❌ HTTP {e.code}: {body[:500]}")
            return None, tokens

# ── Cash deposits from Jan 2025 bank statement ──
# NOTE: $1,402 on 1/02 already in QB — excluded
CASH_DEPOSITS = [
    ("2025-01-02", 471.00, "Deposit"),
    ("2025-01-06", 142.00, "Deposit"),
    ("2025-01-06", 205.00, "Deposit"),
    ("2025-01-06", 243.00, "Deposit"),
    ("2025-01-06", 356.00, "Deposit"),
    ("2025-01-06", 803.00, "Deposit"),
    ("2025-01-09", 193.00, "Deposit"),
    ("2025-01-09", 202.00, "Deposit"),
    ("2025-01-09", 296.00, "Deposit"),
    ("2025-01-13", 149.00, "Deposit"),
    ("2025-01-13", 268.00, "Deposit"),
    ("2025-01-13", 404.00, "Deposit"),
    ("2025-01-15", 115.00, "Deposit"),
    ("2025-01-15", 153.00, "Deposit"),
    ("2025-01-15", 279.00, "Deposit"),
    ("2025-01-16", 370.00, "Deposit"),
    ("2025-01-21", 316.00, "Deposit"),
    ("2025-01-21", 353.00, "Deposit"),
    ("2025-01-21", 476.00, "Deposit"),
    ("2025-01-21", 476.00, "Deposit"),
    ("2025-01-21", 872.00, "Deposit"),
    ("2025-01-23", 104.00, "Deposit"),
    ("2025-01-23", 139.00, "Deposit"),
    ("2025-01-23", 261.00, "Deposit"),
    ("2025-01-27", 253.00, "Deposit"),
    ("2025-01-27", 307.00, "Deposit"),
    ("2025-01-27", 336.00, "Deposit"),
    ("2025-01-27", 580.00, "Deposit"),
    ("2025-01-29", 83.00, "Deposit"),
    ("2025-01-29", 400.00, "Deposit"),
    ("2025-01-30", 337.00, "Deposit"),
]

def find_account_id(tokens, name_search):
    sql = "SELECT Id, Name FROM Account WHERE Active=true MAXRESULTS 1000"
    result, tokens = qbo_query(sql, tokens)
    for acct in result.get("QueryResponse", {}).get("Account", []):
        if name_search.lower() in acct["Name"].lower():
            return acct["Id"], acct["Name"], tokens
    return None, None, tokens

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Push cash deposits to QBO")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be posted without posting")
    args = parser.parse_args()

    access_token, tokens = get_valid_token()

    print("Looking up QBO accounts...")
    bank_acct_id = "63"
    bank_acct_name = "Cape Cod Five (5975)"

    cash_sales_id, cash_sales_name, tokens = find_account_id(tokens, "Cash Sales")
    if not cash_sales_id:
        print("❌ Could not find 'Cash Sales' account in QBO")
        sql = "SELECT Id, Name FROM Account WHERE Active=true MAXRESULTS 1000"
        result, tokens = qbo_query(sql, tokens)
        for acct in result.get("QueryResponse", {}).get("Account", []):
            if "daily" in acct["Name"].lower() or "sales" in acct["Name"].lower() or "cash" in acct["Name"].lower():
                print(f"   → {acct['Name']} (ID: {acct['Id']})")
        sys.exit(1)

    print(f"  Bank account:   {bank_acct_name} (ID: {bank_acct_id})")
    print(f"  Income account: {cash_sales_name} (ID: {cash_sales_id})")
    print(f"\n  Deposits to push: {len(CASH_DEPOSITS)}")
    print(f"  Total amount:     ${sum(d[1] for d in CASH_DEPOSITS):,.2f}")

    if args.dry_run:
        print("\n── DRY RUN ──\n")
        for bank_date, amount, description in CASH_DEPOSITS:
            print(f"  {bank_date}  ${amount:>10,.2f}  {description}")
        print(f"\nDRY RUN complete — {len(CASH_DEPOSITS)} deposits would be created")
        return

    print("\n" + "=" * 90)
    success = 0
    failed = 0

    for bank_date, amount, description in CASH_DEPOSITS:
        payload = {
            "DepositToAccountRef": {
                "value": bank_acct_id,
                "name": bank_acct_name,
            },
            "TxnDate": bank_date,
            "Line": [
                {
                    "Amount": round(amount, 2),
                    "DetailType": "DepositLineDetail",
                    "DepositLineDetail": {
                        "AccountRef": {
                            "value": cash_sales_id,
                            "name": cash_sales_name,
                        },
                    },
                    "Description": description,
                }
            ],
        }

        result, tokens = qbo_post("deposit", payload, tokens)
        if result and "Deposit" in result:
            dep = result["Deposit"]
            dep_id = dep.get("Id", "?")
            print(f"  ✅ {bank_date}  ${amount:>10,.2f}  {description}  → Deposit #{dep_id}")
            success += 1
        else:
            print(f"  ❌ {bank_date}  ${amount:>10,.2f}  {description}  → FAILED")
            failed += 1

        time.sleep(0.3)

    print("\n" + "=" * 90)
    print(f"\nDONE: {success} created, {failed} failed out of {len(CASH_DEPOSITS)} deposits")

if __name__ == "__main__":
    main()
