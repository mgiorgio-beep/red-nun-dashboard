#!/usr/bin/env python3
"""Check QBO for duplicate journal entries on 7shifts payroll dates."""

import json, os, sys, requests

REALM_ID = os.environ.get("QB_REALM_ID", "123146237986854")
CLIENT_ID = os.environ.get("QB_CLIENT_ID")
CLIENT_SECRET = os.environ.get("QB_CLIENT_SECRET")
TOKEN_FILE = os.path.expanduser("~/.qb_tokens.json")
BASE_URL = f"https://quickbooks.api.intuit.com/v3/company/{REALM_ID}"

# Dates and expected DD+Taxes amounts from our pushed CSVs
CHECKS = [
    ("2025-08-11", 1080.64, "08/11 off-cycle"),
    ("2025-08-12", 180.21,  "08/12 off-cycle"),
    ("2025-08-29", 55.59,   "08/29 off-cycle"),
    ("2025-09-24", 357.95,  "09/24 off-cycle"),
    ("2025-10-03", 11868.70,"10/03 regular"),
    ("2025-10-17", 12536.50,"10/17 regular"),
    ("2025-10-31", 7080.64, "10/31 regular"),
    ("2025-11-06", 205.34,  "11/06 off-cycle"),
    ("2025-11-14", 5830.46, "11/14 regular"),
    ("2025-12-26", 8932.10, "12/26 regular"),
]

def load_tokens():
    with open(TOKEN_FILE) as f:
        return json.load(f)

def refresh_token(tokens):
    resp = requests.post("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    resp.raise_for_status()
    new_tokens = resp.json()
    tokens["access_token"] = new_tokens["access_token"]
    tokens["refresh_token"] = new_tokens["refresh_token"]
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f)
    return tokens

def query_qbo(tokens, sql):
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "Accept": "application/json",
    }
    resp = requests.get(f"{BASE_URL}/query", params={"query": sql}, headers=headers)
    if resp.status_code == 401:
        tokens = refresh_token(tokens)
        headers["Authorization"] = f"Bearer {tokens['access_token']}"
        resp = requests.get(f"{BASE_URL}/query", params={"query": sql}, headers=headers)
    resp.raise_for_status()
    return resp.json(), tokens

def main():
    tokens = load_tokens()
    dupes_found = False

    for date, dd_amount, label in CHECKS:
        sql = f"SELECT * FROM JournalEntry WHERE TxnDate = '{date}'"
        result, tokens = query_qbo(tokens, sql)
        entries = result.get("QueryResponse", {}).get("JournalEntry", [])

        if len(entries) == 0:
            print(f"  {label} ({date}): NO entries found — MISSING?")
            continue

        if len(entries) == 1:
            print(f"  {label} ({date}): 1 entry — OK")
            continue

        # Multiple entries on same date — check for duplicates
        print(f"  {label} ({date}): {len(entries)} entries — POSSIBLE DUPLICATE")
        dupes_found = True
        for i, je in enumerate(entries):
            je_id = je.get("Id", "?")
            doc_num = je.get("DocNumber", "?")
            total = je.get("TotalAmt", "?")
            # Look for the DD+Taxes line
            dd_lines = []
            for line in je.get("Line", []):
                desc = line.get("Description", "")
                amt = line.get("Amount", 0)
                if "DD" in desc or "Taxes" in desc:
                    dd_lines.append(amt)
            print(f"    JE #{je_id} DocNum={doc_num} Total={total} DD+Tax lines={dd_lines}")

    if not dupes_found:
        print("\nNo duplicates detected.")
    else:
        print("\nDuplicates found above — review and delete the 7shifts auto-pushed ones.")

if __name__ == "__main__":
    main()
