#!/usr/bin/env python3
"""Delete a QBO journal entry by DocNumber."""

import json, os, sys, requests

REALM_ID = os.environ.get("QB_REALM_ID", "123146237986854")
CLIENT_ID = os.environ.get("QB_CLIENT_ID")
CLIENT_SECRET = os.environ.get("QB_CLIENT_SECRET")
TOKEN_FILE = os.path.expanduser("~/.qb_tokens.json")
BASE_URL = f"https://quickbooks.api.intuit.com/v3/company/{REALM_ID}"

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

def api_get(tokens, url, params=None):
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json"}
    resp = requests.get(url, params=params, headers=headers)
    if resp.status_code == 401:
        tokens = refresh_token(tokens)
        headers["Authorization"] = f"Bearer {tokens['access_token']}"
        resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp.json(), tokens

def api_post(tokens, url, payload):
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "Content-Type": "application/json", "Accept": "application/json"}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code == 401:
        tokens = refresh_token(tokens)
        headers["Authorization"] = f"Bearer {tokens['access_token']}"
        resp = requests.post(url, headers=headers, json=payload)
    return resp.status_code, resp.text, tokens

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 qb_delete_by_docnum.py <DocNumber> [DocNumber2] ...")
        sys.exit(1)

    tokens = load_tokens()

    for doc_num in sys.argv[1:]:
        sql = f"SELECT * FROM JournalEntry WHERE DocNumber = '{doc_num}'"
        result, tokens = api_get(tokens, f"{BASE_URL}/query", {"query": sql})
        entries = result.get("QueryResponse", {}).get("JournalEntry", [])

        if not entries:
            print(f"  {doc_num}: NOT FOUND")
            continue

        for je in entries:
            je_id = je["Id"]
            sync_token = je["SyncToken"]
            status, resp_text, tokens = api_post(tokens,
                f"{BASE_URL}/journalentry?operation=delete",
                {"Id": str(je_id), "SyncToken": str(sync_token)})
            if status == 200:
                print(f"  {doc_num}: DELETED (JE #{je_id})")
            else:
                print(f"  {doc_num}: FAILED (JE #{je_id}) — {status} {resp_text}")

if __name__ == "__main__":
    main()
