#!/usr/bin/env python3
"""
QuickBooks Online — Journal Entry Push
Red Buoy Inc / Red Nun - Chatham
Reads a QB import CSV (produced by 7shifts_to_qb.py or manually) and
POSTs it to QuickBooks Online as a JournalEntry via the QBO API.
────────────────────────────────────────────────────────────
FIRST-TIME SETUP  (one-time, ~10 minutes)
────────────────────────────────────────────────────────────
1. Go to https://developer.intuit.com and sign in with your QBO account.
2. Create an app:  + Create an App → QuickBooks Online → name it "Red Buoy Payroll"
3. On the app's Keys & OAuth page, copy:
     Client ID     → QB_CLIENT_ID
     Client Secret → QB_CLIENT_SECRET
4. Add a redirect URI:  http://localhost:8080/callback
5. Run this script once with --auth to open a browser and complete the OAuth flow:
     python3 qb_push.py --auth
   It will save tokens to ~/.qb_tokens.json (keep that file private).
6. After that, just run normally:
     python3 qb_push.py --csv QB_import_11282025.csv
────────────────────────────────────────────────────────────
ENVIRONMENT VARIABLES
────────────────────────────────────────────────────────────
export QB_CLIENT_ID=your_client_id
export QB_CLIENT_SECRET=your_client_secret
export QB_REALM_ID=your_realm_id   # found in QBO URL: /app/homepage?... realmId=XXXXXXX
"""
import os, sys, csv, json, argparse, webbrowser, time, base64
import urllib.request, urllib.parse, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path
# ── Config ────────────────────────────────────────────────────────────────────
TOKEN_FILE  = Path.home() / ".qb_tokens.json"
AUTH_URL    = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL   = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
BASE_URL    = "https://quickbooks.api.intuit.com"
REDIRECT    = "http://localhost:9876/callback"
SCOPE       = "com.intuit.quickbooks.accounting"
# ── Token storage ─────────────────────────────────────────────────────────────
def save_tokens(tokens):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    TOKEN_FILE.chmod(0o600)
    print(f"✓ Tokens saved to {TOKEN_FILE}")
def load_tokens():
    if not TOKEN_FILE.exists():
        sys.exit("❌ No tokens found. Run:  python3 qb_push.py --auth")
    with open(TOKEN_FILE) as f:
        return json.load(f)
# ── OAuth helpers ─────────────────────────────────────────────────────────────
def get_credentials():
    client_id     = os.environ.get("QB_CLIENT_ID")
    client_secret = os.environ.get("QB_CLIENT_SECRET")
    if not all([client_id, client_secret]):
        sys.exit(
            "❌ Missing environment variables. Set:\n"
            "   export QB_CLIENT_ID=...\n"
            "   export QB_CLIENT_SECRET=..."
        )
    # realm_id can come from env var OR saved tokens
    realm_id = os.environ.get("QB_REALM_ID")
    return client_id, client_secret, realm_id
def get_realm_id(client_id, client_secret):
    """Get realm_id from env var or saved tokens."""
    realm_id = os.environ.get("QB_REALM_ID")
    if realm_id:
        return realm_id
    tokens = load_tokens()
    realm_id = tokens.get("realm_id")
    if not realm_id:
        sys.exit(
            "❌ No Realm ID found. Either:\n"
            "   export QB_REALM_ID=your_realm_id\n"
            "   OR re-run:  python3 qb_push.py --auth"
        )
    return realm_id
def do_auth_flow(client_id, client_secret):
    """Open browser, capture callback, exchange code for tokens."""
    import secrets
    state = secrets.token_hex(8)
    auth_params = urllib.parse.urlencode({
        "client_id":     client_id,
        "response_type": "code",
        "scope":         SCOPE,
        "redirect_uri":  REDIRECT,
        "state":         state,
    })
    url = f"{AUTH_URL}?{auth_params}"
    # Start server FIRST, then open browser
    captured = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            captured["code"]    = params.get("code", [None])[0]
            captured["state"]   = params.get("state", [None])[0]
            captured["realmId"] = params.get("realmId", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful! You can close this tab.</h2>")
        def log_message(self, *args): pass
    server = HTTPServer(("localhost", 9876), Handler)
    print(f"\nOpening browser for QuickBooks authorization...")
    print(f"If it doesn't open automatically, visit:\n  {url}\n")
    webbrowser.open(url)
    print("Waiting for QuickBooks callback...")
    server.handle_request()
    if captured.get("state") != state:
        sys.exit("❌ State mismatch — possible CSRF. Try again.")
    code = captured.get("code")
    if not code:
        sys.exit("❌ No authorization code received.")
    # Exchange code for tokens
    creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": REDIRECT,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Authorization", f"Basic {creds_b64}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())
    tokens["obtained_at"] = time.time()
    tokens["realm_id"]    = captured.get("realmId")
    if tokens["realm_id"]:
        print(f"✓ Realm ID captured: {tokens['realm_id']}")
    return tokens
def refresh_access_token(tokens, client_id, client_secret):
    """Refresh the access token using the refresh token."""
    creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Authorization", f"Basic {creds_b64}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req) as resp:
        new_tokens = json.loads(resp.read())
    new_tokens["obtained_at"] = time.time()
    # Preserve refresh token if not returned
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens["refresh_token"]
    save_tokens(new_tokens)
    print("✓ Access token refreshed")
    return new_tokens
def get_valid_token(client_id, client_secret):
    """Load tokens, refresh if expired."""
    tokens = load_tokens()
    age = time.time() - tokens.get("obtained_at", 0)
    # Access tokens expire after 3600s — refresh at 3000s to be safe
    if age > 3000:
        tokens = refresh_access_token(tokens, client_id, client_secret)
    return tokens["access_token"]
# ── QBO API ───────────────────────────────────────────────────────────────────
def qbo_request(method, path, realm_id, access_token, payload=None):
    url = f"{BASE_URL}/v3/company/{realm_id}/{path}?minorversion=65"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  HTTP {e.code}: {body[:400]}")
        return None
def get_accounts(realm_id, access_token):
    """Fetch all active accounts and return name→Id mapping."""
    url = f"{BASE_URL}/v3/company/{realm_id}/query?query={urllib.parse.quote('SELECT Id, Name, AccountType FROM Account WHERE Active=true MAXRESULTS 1000')}&minorversion=65"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req) as resp:
        r = json.loads(resp.read())
    accounts = {}
    for acct in r.get("QueryResponse", {}).get("Account", []):
        accounts[acct["Name"]] = acct["Id"]
    return accounts
def get_names(realm_id, access_token):
    """Fetch all customers, vendors, and employees. Return DisplayName→(Id, Type) mapping.
    QBO journal entry Entity requires type: Customer, Vendor, or Employee."""
    name_map = {}
    # Fetch customers (includes sub-customers, jobs, etc.)
    for entity_type, query in [
        ("Customer", "SELECT Id, DisplayName FROM Customer WHERE Active=true MAXRESULTS 1000"),
        ("Vendor",   "SELECT Id, DisplayName FROM Vendor WHERE Active=true MAXRESULTS 1000"),
        ("Employee", "SELECT Id, DisplayName FROM Employee WHERE Active=true MAXRESULTS 1000"),
    ]:
        url = f"{BASE_URL}/v3/company/{realm_id}/query?query={urllib.parse.quote(query)}&minorversion=65"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                r = json.loads(resp.read())
            for item in r.get("QueryResponse", {}).get(entity_type, []):
                display = item.get("DisplayName", "")
                name_map[display] = {"id": item["Id"], "type": entity_type}
        except urllib.error.HTTPError:
            # Some QBO plans don't have Employee — skip silently
            pass
    return name_map
# ── CSV reader ─────────────────────────────────────────────────────────────────
def read_csv(csv_path):
    """Read QB import CSV into list of line dicts."""
    lines = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            lines.append({
                "journal_no":  row["JournalNo"],
                "date":        row["JournalDate"],
                "account":     row["AccountName"],
                "debit":       float(row["Debits"])  if row["Debits"]  else 0.0,
                "credit":      float(row["Credits"]) if row["Credits"] else 0.0,
                "description": row["Description"],
                "name":        row.get("Name", ""),
            })
    return lines
# ── Journal entry builder ──────────────────────────────────────────────────────
def build_journal_entry(lines, accounts, name_map):
    """Convert CSV lines to QBO JournalEntry payload."""
    # Derive date and doc number from first line
    date_str   = lines[0]["date"]        # MM/DD/YYYY
    journal_no = lines[0]["journal_no"]  # MMDDYYYY
    dt = datetime.strptime(date_str, "%m/%d/%Y")
    txn_date = dt.strftime("%Y-%m-%d")
    missing = [l["account"] for l in lines if l["account"] not in accounts]
    if missing:
        print(f"\n⚠  These account names were not found in QuickBooks:")
        for m in set(missing):
            print(f"   → '{m}'")
        print("\n  Make sure the account names in the CSV exactly match QBO.")
        print("  Run with --list-accounts to see all available accounts.\n")
        sys.exit(1)
    line_items = []
    missing_names = []
    for i, l in enumerate(lines, 1):
        posting_type = "Debit" if l["debit"] > 0 else "Credit"
        amount       = l["debit"] if l["debit"] > 0 else l["credit"]
        line_item = {
            "Id": str(i),
            "Description": l["description"],
            "Amount": round(amount, 2),
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": posting_type,
                "AccountRef": {
                    "value": accounts[l["account"]],
                    "name":  l["account"],
                },
            },
        }
        # If a Name is provided, look it up and set the Entity reference
        if l["name"].strip():
            name_key = l["name"].strip()
            if name_key in name_map:
                entity = name_map[name_key]
                line_item["JournalEntryLineDetail"]["Entity"] = {
                    "Type": entity["type"],
                    "EntityRef": {
                        "value": entity["id"],
                        "name":  name_key,
                    },
                }
            else:
                missing_names.append(name_key)
        line_items.append(line_item)
    if missing_names:
        print(f"\n⚠  These names were not found in QuickBooks (Customer/Vendor/Employee):")
        for m in sorted(set(missing_names)):
            print(f"   → '{m}'")
        print("\n  The journal entry will be created WITHOUT name references for these lines.")
        print("  Make sure the names in the CSV match QBO DisplayName exactly.\n")
    return {
        "DocNumber": journal_no,
        "TxnDate":   txn_date,
        "Line":      line_items,
    }
# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Push QB import CSV to QuickBooks Online")
    parser.add_argument("--auth",          action="store_true", help="Run OAuth authorization flow")
    parser.add_argument("--csv",           help="Path to QB import CSV")
    parser.add_argument("--list-accounts", action="store_true", help="Print all QBO account names")
    parser.add_argument("--list-names",    action="store_true", help="Print all QBO names (customers/vendors/employees)")
    parser.add_argument("--dry-run",       action="store_true", help="Build payload but don't post")
    args = parser.parse_args()
    client_id, client_secret, _ = get_credentials()
    # ── Auth flow ──
    if args.auth:
        tokens = do_auth_flow(client_id, client_secret)
        save_tokens(tokens)
        print("\n✓ Authorization complete. You can now run:")
        print("   python3 qb_push.py --csv QB_import_MMDDYYYY.csv")
        return
    access_token = get_valid_token(client_id, client_secret)
    realm_id     = get_realm_id(client_id, client_secret)
    # ── List accounts ──
    if args.list_accounts:
        print("\nFetching accounts from QuickBooks...")
        accounts = get_accounts(realm_id, access_token)
        print(f"\n{'Account Name':<50} {'ID'}")
        print("-" * 60)
        for name, aid in sorted(accounts.items()):
            print(f"{name:<50} {aid}")
        return
    # ── List names ──
    if args.list_names:
        print("\nFetching names from QuickBooks...")
        name_map = get_names(realm_id, access_token)
        print(f"\n{'Display Name':<40} {'Type':<12} {'ID'}")
        print("-" * 65)
        for name, info in sorted(name_map.items()):
            print(f"{name:<40} {info['type']:<12} {info['id']}")
        print(f"\n  {len(name_map)} names found")
        return
    # ── Push journal entry ──
    if not args.csv:
        parser.print_help()
        sys.exit(1)
    print(f"\nReading {args.csv}...")
    lines = read_csv(args.csv)
    print(f"  {len(lines)} lines | Journal #{lines[0]['journal_no']} | Date {lines[0]['date']}")
    total_d = sum(l["debit"]  for l in lines)
    total_c = sum(l["credit"] for l in lines)
    if abs(total_d - total_c) > 0.02:
        sys.exit(f"❌ CSV doesn't balance: debits={total_d:.2f} credits={total_c:.2f}")
    print(f"  ✓ Balanced: ${total_d:,.2f}")
    print("\nFetching QBO accounts...")
    accounts = get_accounts(realm_id, access_token)
    print(f"  {len(accounts)} accounts loaded")
    print("Fetching QBO names (customers/vendors/employees)...")
    name_map = get_names(realm_id, access_token)
    print(f"  {len(name_map)} names loaded")
    payload = build_journal_entry(lines, accounts, name_map)
    if args.dry_run:
        print("\n── DRY RUN — payload that would be posted ──")
        print(json.dumps(payload, indent=2))
        return
    print("\nPosting journal entry to QuickBooks...")
    result = qbo_request("POST", "journalentry", realm_id, access_token, payload)
    if result and "JournalEntry" in result:
        je = result["JournalEntry"]
        print(f"\n✓ Journal entry created!")
        print(f"  ID:     {je.get('Id')}")
        print(f"  Date:   {je.get('TxnDate')}")
        print(f"  DocNo:  {je.get('DocNumber')}")
        print(f"  Total:  ${je.get('TotalAmt', 0):,.2f}")
    else:
        print("❌ Something went wrong — see error above.")
        sys.exit(1)
if __name__ == "__main__":
    main()
