#!/usr/bin/env python3
"""
Push remaining non-Toast, non-cash deposits to QuickBooks Online as Bank Deposits.
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

# ── Remaining deposits from Jan 2025 bank statement ──
# "cc" = Daily Sales:Credit Card Sales, "cash" = Daily Sales:Cash Sales, "loan" = Loan to Red Nun Dennisport
OTHER_DEPOSITS = [
    ("2025-01-03", 4000.00, "Trsf from Nun Dport", "loan"),
    ("2025-01-06", 72.52, "SQ250106 Square Inc", "cc"),
    ("2025-01-10", 1500.00, "Trsf from Nun Dport", "loan"),
    ("2025-01-13", 3.49, "DAVO TECHNOLOGIE credit", "cash"),
    ("2025-01-24", 983.85, "Real Time ACH Credit VENMO", "cc"),
    ("2025-01-24", 1300.00, "Trsf from FMT Holdings", "cash"),
    ("2025-01-28", 2000.00, "Trsf from Nun Dport", "loan"),
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
    parser = argparse.ArgumentParser(description="Push other deposits to QBO")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be posted without posting")
    args = parser.parse_args()

    access_token, tokens = get_valid_token()

    print("Looking up QBO accounts...")
    bank_acct_id = "63"
    bank_acct_name = "Cape Cod Five (5975)"

    cash_sales_id, cash_sales_name, tokens = find_account_id(tokens, "Cash Sales")
    if not cash_sales_id:
        print("❌ Could not find 'Cash Sales' account in QBO")
        sys.exit(1)

    cc_sales_id, cc_sales_name, tokens = find_account_id(tokens, "Credit Card Sales")
    if not cc_sales_id:
        print("❌ Could not find 'Credit Card Sales' account in QBO")
        sys.exit(1)

    loan_id, loan_name, tokens = find_account_id(tokens, "Loan to Red Nun Dennisport")
    if not loan_id:
        # Try shorter search
        loan_id, loan_name, tokens = find_account_id(tokens, "Red Nun Dennisport")
    if not loan_id:
        print("❌ Could not find 'Loan to Red Nun Dennisport' account in QBO")
        sys.exit(1)

    acct_map = {
        "cash": (cash_sales_id, cash_sales_name),
        "cc":   (cc_sales_id, cc_sales_name),
        "loan": (loan_id, loan_name),
    }

    print(f"  Bank account:     {bank_acct_name} (ID: {bank_acct_id})")
    print(f"  Cash Sales acct:  {cash_sales_name} (ID: {cash_sales_id})")
    print(f"  CC Sales acct:    {cc_sales_name} (ID: {cc_sales_id})")
    print(f"  Loan acct:        {loan_name} (ID: {loan_id})")
    print(f"\n  Deposits to push: {len(OTHER_DEPOSITS)}")
    print(f"  Total amount:     ${sum(d[1] for d in OTHER_DEPOSITS):,.2f}")

    if args.dry_run:
        print("\n── DRY RUN ──\n")
        for bank_date, amount, description, acct_type in OTHER_DEPOSITS:
            acct_label = acct_map[acct_type][1]
            print(f"  {bank_date}  ${amount:>10,.2f}  {description:<35} → {acct_label}")
        print(f"\nDRY RUN complete — {len(OTHER_DEPOSITS)} deposits would be created")
        return

    print("\n" + "=" * 90)
    success = 0
    failed = 0

    for bank_date, amount, description, acct_type in OTHER_DEPOSITS:
        acct_id, acct_name = acct_map[acct_type]
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
                            "value": acct_id,
                            "name": acct_name,
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
    print(f"\nDONE: {success} created, {failed} failed out of {len(OTHER_DEPOSITS)} deposits")

if __name__ == "__main__":
    main()
