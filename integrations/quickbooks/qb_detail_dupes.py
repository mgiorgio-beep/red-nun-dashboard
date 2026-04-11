#!/usr/bin/env python3
"""Show full line detail for the 7shifts auto-pushed journal entries."""

import json, os, requests

REALM_ID = os.environ.get("QB_REALM_ID", "123146237986854")
CLIENT_ID = os.environ.get("QB_CLIENT_ID")
CLIENT_SECRET = os.environ.get("QB_CLIENT_SECRET")
TOKEN_FILE = os.path.expanduser("~/.qb_tokens.json")
BASE_URL = f"https://quickbooks.api.intuit.com/v3/company/{REALM_ID}"

DUPES = [
    (25618, "MJ5936ME", "08/11"),
    (25623, "MJ5940ME", "08/12"),
    (25693, "MJ6068ME", "08/29"),
    (25967, "MJ6224ME", "09/24"),
    (25888, "???",      "09/24 mystery"),
    (26106, "MJ6276ME", "10/03"),
    (26155, "MJ6371ME", "10/17"),
    (26217, "MJ6460ME", "10/31"),
    (26238, "MJ6512ME", "11/06"),
    (26267, "MJ6553ME", "11/14"),
    (26401, "MJ6777ME", "12/26"),
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

def get_je(tokens, je_id):
    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "Accept": "application/json",
    }
    resp = requests.get(f"{BASE_URL}/journalentry/{je_id}", headers=headers)
    if resp.status_code == 401:
        tokens = refresh_token(tokens)
        headers["Authorization"] = f"Bearer {tokens['access_token']}"
        resp = requests.get(f"{BASE_URL}/journalentry/{je_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()["JournalEntry"], tokens

def main():
    tokens = load_tokens()

    for je_id, doc_num, label in DUPES:
        try:
            je, tokens = get_je(tokens, je_id)
            txn_date = je.get("TxnDate", "?")
            print(f"=== JE #{je_id} DocNum={doc_num} Date={txn_date} ({label}) ===")
            for line in je.get("Line", []):
                amt = line.get("Amount", 0)
                desc = line.get("Description", "")
                detail = line.get("JournalEntryLineDetail", {})
                posting = detail.get("PostingType", "?")
                acct = detail.get("AccountRef", {}).get("name", "?")
                entity = detail.get("Entity", {}).get("EntityRef", {}).get("name", "") if "Entity" in detail else ""
                print(f"  {posting:6s} ${amt:>10.2f}  {acct:<30s} {desc:<25s} {entity}")
            print()
        except Exception as e:
            print(f"=== JE #{je_id} ({doc_num}) ERROR: {e} ===\n")

if __name__ == "__main__":
    main()
