#!/usr/bin/env python3
"""
Push coded bank statement payments to QBO as Expense transactions.
Reads the coded CSV, checks for existing transactions, and only pushes missing ones.
Cape Cod Five (5975) as the bank account.
"""

import csv, json, os, sys, time, base64
import urllib.request, urllib.parse, urllib.error
from pathlib import Path

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
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).encode()
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
            print(f"  HTTP {e.code}: {body[:400]}")
            return None, tokens

def get_accounts(tokens):
    """Fetch all active accounts, return name→id mapping."""
    sql = "SELECT Id, Name FROM Account WHERE Active=true MAXRESULTS 1000"
    result, tokens = qbo_query(sql, tokens)
    accts = {}
    for a in result.get("QueryResponse", {}).get("Account", []):
        accts[a["Name"]] = a["Id"]
    return accts, tokens

def build_existing_amounts(tokens):
    """Pull all QBO transactions in Jan 2025 and build a set of (date, amount) tuples."""
    existing = set()
    for txn_type in ["Purchase", "BillPayment", "JournalEntry", "Bill"]:
        sql = f"SELECT * FROM {txn_type} WHERE TxnDate >= '2024-12-15' AND TxnDate <= '2025-02-15' MAXRESULTS 1000"
        result, tokens = qbo_query(sql, tokens)
        if result:
            items = result.get("QueryResponse", {}).get(txn_type, [])
            print(f"  {txn_type}: {len(items)} records")
            for item in items:
                total = item.get("TotalAmt", item.get("Amount", 0))
                txn_date = item.get("TxnDate", "")
                existing.add((txn_date, round(total, 2)))
                # Also add line-level amounts
                for line in item.get("Line", []):
                    line_amt = line.get("Amount", 0)
                    if line_amt > 0:
                        existing.add((txn_date, round(line_amt, 2)))
    return existing, tokens

def read_coded_csv(csv_path):
    """Read the coded payment CSV."""
    payments = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_val = row.get("Date", "").strip()
            amount_str = row.get("Amount", "0").replace(",", "").replace("$", "").strip()
            desc = row.get("Bank Description", "").strip()
            acct = row.get("QB Account", "").strip()

            if not date_val or date_val == "TOTAL" or not acct:
                continue

            try:
                amount = float(amount_str)
            except ValueError:
                continue

            payments.append({
                "date": date_val,
                "amount": amount,
                "description": desc,
                "account": acct,
            })
    return payments

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Push coded payments to QBO")
    parser.add_argument("--csv", required=True, help="Path to coded CSV")
    parser.add_argument("--dry-run", action="store_true", help="Check for dupes but don't push")
    parser.add_argument("--skip-dupe-check", action="store_true", help="Skip duplicate checking")
    args = parser.parse_args()

    access_token, tokens = get_valid_token()

    # Read CSV
    payments = read_coded_csv(args.csv)
    print(f"Read {len(payments)} coded payments from {args.csv}")
    print(f"Total: ${sum(p['amount'] for p in payments):,.2f}")

    # Get account IDs
    print("\nFetching QBO accounts...")
    accts, tokens = get_accounts(tokens)
    print(f"  {len(accts)} accounts loaded")

    # Bank account
    bank_acct_id = "63"
    bank_acct_name = "Cape Cod Five (5975)"

    # Validate all accounts exist in QBO
    missing_accts = set()
    for p in payments:
        if p["account"] not in accts:
            missing_accts.add(p["account"])
    if missing_accts:
        print(f"\n❌ These accounts from the CSV are NOT in QBO:")
        for a in sorted(missing_accts):
            print(f"   → '{a}'")
        print("\nFix the CSV and re-run.")
        sys.exit(1)
    print("  ✅ All account names validated")

    # Check for existing transactions
    to_push = payments
    if not args.skip_dupe_check:
        print("\nChecking for existing transactions in QBO...")
        existing, tokens = build_existing_amounts(tokens)
        print(f"  {len(existing)} existing (date, amount) pairs loaded")

        to_push = []
        skipped = []
        for p in payments:
            key = (p["date"], round(p["amount"], 2))
            if key in existing:
                skipped.append(p)
            else:
                to_push.append(p)

        if skipped:
            print(f"\n  ⚠ Skipping {len(skipped)} payments already in QBO:")
            for s in skipped:
                print(f"    {s['date']}  ${s['amount']:>10,.2f}  {s['description']}")

    print(f"\n  Payments to push: {len(to_push)}")
    print(f"  Total to push:    ${sum(p['amount'] for p in to_push):,.2f}")

    if args.dry_run:
        print("\n── DRY RUN — nothing pushed ──")
        for p in to_push:
            print(f"  {p['date']}  ${p['amount']:>10,.2f}  {p['description']:<40} → {p['account']}")
        return

    if len(to_push) == 0:
        print("\nNothing to push — all payments already in QBO!")
        return

    # Push each payment as a Purchase (Expense) from Cape Cod Five
    print("\n" + "=" * 100)
    success = 0
    failed = 0

    for p in to_push:
        payload = {
            "PaymentType": "Cash",
            "AccountRef": {
                "value": bank_acct_id,
                "name": bank_acct_name,
            },
            "TxnDate": p["date"],
            "Line": [
                {
                    "Amount": round(p["amount"], 2),
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {
                            "value": accts[p["account"]],
                            "name": p["account"],
                        },
                    },
                    "Description": p["description"],
                }
            ],
        }

        result, tokens = qbo_post("purchase", payload, tokens)
        if result and "Purchase" in result:
            purch = result["Purchase"]
            purch_id = purch.get("Id", "?")
            print(f"  ✅ {p['date']}  ${p['amount']:>10,.2f}  {p['description']:<40} → {p['account']}  (#{purch_id})")
            success += 1
        else:
            print(f"  ❌ {p['date']}  ${p['amount']:>10,.2f}  {p['description']:<40} → FAILED")
            failed += 1

        time.sleep(0.3)

    print("\n" + "=" * 100)
    print(f"\nDONE: {success} created, {failed} failed out of {len(to_push)} payments")
    print(f"Total pushed: ${sum(p['amount'] for p in to_push):,.2f}")

if __name__ == "__main__":
    main()
