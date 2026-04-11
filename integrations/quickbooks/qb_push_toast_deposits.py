#!/usr/bin/env python3
"""
Push Toast daily deposits to QuickBooks Online as Bank Deposits.
Cape Cod Five (5975) → Daily Sales:Credit Card Sales
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

# ── Token management (same as qb_push.py) ──
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
    """POST to QBO API with auto-refresh on 401."""
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
    """Run a QBO query."""
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

# ── Toast deposits from Jan 2025 bank statement ──
# (bank_post_date, amount, description)
TOAST_DEPOSITS = [
    ("2025-01-02", 2742.03, "DEP DEC 31 TOAST CCD"),
    ("2025-01-02", 9492.15, "DEP JAN 01 TOAST CCD"),
    ("2025-01-03", 2830.99, "DEP JAN 02 TOAST CCD"),
    ("2025-01-06", 1567.09, "DEP JAN 03 TOAST CCD"),
    ("2025-01-06", 2993.55, "DEP JAN 04 TOAST CCD"),
    ("2025-01-06", 2336.01, "DEP JAN 05 TOAST CCD"),
    ("2025-01-07", 1309.57, "DEP JAN 06 TOAST CCD"),
    ("2025-01-08", 328.62,  "DEP JAN 07 TOAST CCD"),
    ("2025-01-09", 2293.76, "DEP JAN 08 TOAST CCD"),
    ("2025-01-10", 1741.99, "DEP JAN 09 TOAST CCD"),
    ("2025-01-13", 2533.91, "DEP JAN 10 TOAST CCD"),
    ("2025-01-13", 1918.10, "DEP JAN 11 TOAST CCD"),
    ("2025-01-13", 3250.86, "DEP JAN 12 TOAST CCD"),
    ("2025-01-14", 2320.59, "DEP JAN 13 TOAST CCD"),
    ("2025-01-15", 781.83,  "DEP JAN 14 TOAST CCD"),
    ("2025-01-16", 1518.08, "DEP JAN 15 TOAST CCD"),
    ("2025-01-17", 1564.07, "DEP JAN 16 TOAST CCD"),
    ("2025-01-21", 1489.55, "DEP JAN 17 TOAST CCD"),
    ("2025-01-21", 2599.00, "DEP JAN 18 TOAST CCD"),
    ("2025-01-21", 2967.76, "DEP JAN 19 TOAST CCD"),
    ("2025-01-21", 2229.49, "DEP JAN 20 TOAST CCD"),
    ("2025-01-22", 802.36,  "DEP JAN 21 TOAST CCD"),
    ("2025-01-23", 1433.92, "DEP JAN 22 TOAST CCD"),
    ("2025-01-24", 2073.07, "DEP JAN 23 TOAST CCD"),
    ("2025-01-27", 1406.04, "DEP JAN 24 TOAST CCD"),
    ("2025-01-27", 3026.62, "DEP JAN 25 TOAST CCD"),
    ("2025-01-27", 2176.50, "DEP JAN 26 TOAST CCD"),
    ("2025-01-28", 2496.15, "DEP JAN 27 TOAST CCD"),
    ("2025-01-29", 533.93,  "DEP JAN 28 TOAST CCD"),
    ("2025-01-30", 1276.70, "DEP JAN 29 TOAST CCD"),
    ("2025-01-31", 1914.73, "DEP JAN 30 TOAST CCD"),
]

def find_account_id(tokens, name_search):
    """Find account ID by name substring."""
    sql = f"SELECT Id, Name FROM Account WHERE Active=true MAXRESULTS 1000"
    result, tokens = qbo_query(sql, tokens)
    for acct in result.get("QueryResponse", {}).get("Account", []):
        if name_search.lower() in acct["Name"].lower():
            return acct["Id"], acct["Name"], tokens
    return None, None, tokens

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Push Toast deposits to QBO")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be posted without posting")
    args = parser.parse_args()

    access_token, tokens = get_valid_token()

    # Look up account IDs
    print("Looking up QBO accounts...")

    # Bank account: Cape Cod Five (5975) - known ID 63
    bank_acct_id = "63"
    bank_acct_name = "Cape Cod Five (5975)"

    # Income account: Daily Sales:Credit Card Sales
    cc_sales_id, cc_sales_name, tokens = find_account_id(tokens, "Credit Card Sales")
    if not cc_sales_id:
        print("❌ Could not find 'Credit Card Sales' account in QBO")
        print("   Searching for all accounts with 'Daily' or 'Sales'...")
        sql = "SELECT Id, Name FROM Account WHERE Active=true MAXRESULTS 1000"
        result, tokens = qbo_query(sql, tokens)
        for acct in result.get("QueryResponse", {}).get("Account", []):
            if "daily" in acct["Name"].lower() or "sales" in acct["Name"].lower():
                print(f"   → {acct['Name']} (ID: {acct['Id']})")
        sys.exit(1)

    print(f"  Bank account:   {bank_acct_name} (ID: {bank_acct_id})")
    print(f"  Income account: {cc_sales_name} (ID: {cc_sales_id})")
    print(f"\n  Deposits to push: {len(TOAST_DEPOSITS)}")
    print(f"  Total amount:     ${sum(d[1] for d in TOAST_DEPOSITS):,.2f}")

    if args.dry_run:
        print("\n── DRY RUN — showing payloads ──\n")

    print("\n" + "=" * 90)
    success = 0
    failed = 0

    for bank_date, amount, description in TOAST_DEPOSITS:
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
                            "value": cc_sales_id,
                            "name": cc_sales_name,
                        },
                    },
                    "Description": description,
                }
            ],
        }

        if args.dry_run:
            print(f"  {bank_date}  ${amount:>10,.2f}  {description}")
            print(f"    {json.dumps(payload, indent=2)[:200]}...")
            continue

        result, tokens = qbo_post("deposit", payload, tokens)
        if result and "Deposit" in result:
            dep = result["Deposit"]
            dep_id = dep.get("Id", "?")
            print(f"  ✅ {bank_date}  ${amount:>10,.2f}  {description}  → Deposit #{dep_id}")
            success += 1
        else:
            print(f"  ❌ {bank_date}  ${amount:>10,.2f}  {description}  → FAILED")
            failed += 1

        # Small delay to avoid rate limiting
        time.sleep(0.3)

    print("\n" + "=" * 90)
    if not args.dry_run:
        print(f"\nDONE: {success} created, {failed} failed out of {len(TOAST_DEPOSITS)} deposits")
        print(f"Total pushed: ${sum(d[1] for d in TOAST_DEPOSITS if True):,.2f}")
    else:
        print(f"\nDRY RUN complete — {len(TOAST_DEPOSITS)} deposits would be created")

if __name__ == "__main__":
    main()
