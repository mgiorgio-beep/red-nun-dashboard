#!/usr/bin/env python3
"""
Query QBO for bank statement deposits to see which ones are already entered.
Searches across Deposits, JournalEntries, SalesReceipts, and Payments.
"""

import json, os, sys, time, base64
import urllib.request, urllib.parse, urllib.error
from pathlib import Path
from datetime import datetime, timedelta

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

def qbo_query(sql, tokens):
    """Run a QBO query, auto-refresh on 401."""
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

# ── All deposits from Jan 2025 bank statement (Cape Cod Five 5975) ──
# Format: (bank_date, amount, description)
DEPOSITS = [
    # Page 1
    ("2025-01-02", 471.00, "Deposit"),
    ("2025-01-02", 1402.00, "Deposit"),
    ("2025-01-02", 2742.03, "DEP DEC 31 TOAST"),
    ("2025-01-02", 9492.15, "DEP JAN 01 TOAST"),
    # Page 2
    ("2025-01-03", 4000.00, "Trsf from Nun Dport"),
    ("2025-01-03", 2830.99, "DEP JAN 02 TOAST"),
    ("2025-01-06", 142.00, "Deposit"),
    ("2025-01-06", 205.00, "Deposit"),
    ("2025-01-06", 243.00, "Deposit"),
    ("2025-01-06", 356.00, "Deposit"),
    ("2025-01-06", 803.00, "Deposit"),
    ("2025-01-06", 72.52, "SQ250106 Square Inc"),
    # Page 3
    ("2025-01-06", 1567.09, "DEP JAN 03 TOAST"),
    ("2025-01-06", 2336.01, "DEP JAN 05 TOAST"),
    ("2025-01-06", 2993.55, "DEP JAN 04 TOAST"),
    # Page 4
    ("2025-01-07", 1309.57, "DEP JAN 06 TOAST"),
    ("2025-01-08", 328.62, "DEP JAN 07 TOAST"),
    # Page 5
    ("2025-01-09", 193.00, "Deposit"),
    ("2025-01-09", 202.00, "Deposit"),
    ("2025-01-09", 296.00, "Deposit"),
    ("2025-01-09", 2293.76, "DEP JAN 08 TOAST"),
    ("2025-01-10", 1500.00, "Trsf from Nun Dport"),
    ("2025-01-10", 1741.99, "DEP JAN 09 TOAST"),
    ("2025-01-13", 149.00, "Deposit"),
    ("2025-01-13", 268.00, "Deposit"),
    ("2025-01-13", 404.00, "Deposit"),
    # Page 6
    ("2025-01-13", 3.49, "DAVO TECHNOLOGIE credit"),
    ("2025-01-13", 1918.10, "DEP JAN 12 TOAST"),
    ("2025-01-13", 2533.91, "DEP JAN 10 TOAST"),
    ("2025-01-13", 3250.86, "DEP JAN 12 TOAST"),
    # Page 7
    ("2025-01-14", 2320.59, "DEP JAN 13 TOAST"),
    ("2025-01-15", 115.00, "Deposit"),
    ("2025-01-15", 153.00, "Deposit"),
    ("2025-01-15", 279.00, "Deposit"),
    ("2025-01-15", 781.83, "DEP JAN 14 TOAST"),
    ("2025-01-16", 370.00, "Deposit"),
    ("2025-01-16", 1518.08, "DEP JAN 15 TOAST"),
    # Page 8
    ("2025-01-17", 1564.07, "DEP JAN 16 TOAST"),
    ("2025-01-21", 316.00, "Deposit"),
    ("2025-01-21", 353.00, "Deposit"),
    ("2025-01-21", 476.00, "Deposit"),
    ("2025-01-21", 476.00, "Deposit (2nd)"),
    ("2025-01-21", 872.00, "Deposit"),
    ("2025-01-21", 1489.55, "DEP JAN 17 TOAST"),
    ("2025-01-21", 2229.49, "DEP JAN 20 TOAST"),
    ("2025-01-21", 2599.00, "DEP JAN 18 TOAST"),
    ("2025-01-21", 2967.76, "DEP JAN 19 TOAST"),
    # Page 9
    ("2025-01-22", 802.36, "DEP JAN 21 TOAST"),
    # Page 10
    ("2025-01-23", 104.00, "Deposit"),
    ("2025-01-23", 139.00, "Deposit"),
    ("2025-01-23", 261.00, "Deposit"),
    ("2025-01-23", 1433.92, "DEP JAN 22 TOAST"),
    ("2025-01-24", 983.85, "Real Time ACH Credit / VENMO"),
    ("2025-01-24", 1300.00, "Trsf from FMT Holdings"),
    ("2025-01-24", 2073.07, "DEP JAN 23 TOAST"),
    # Page 11
    ("2025-01-27", 253.00, "Deposit"),
    ("2025-01-27", 307.00, "Deposit"),
    ("2025-01-27", 336.00, "Deposit"),
    ("2025-01-27", 580.00, "Deposit"),
    ("2025-01-27", 1406.04, "DEP JAN 24 TOAST"),
    ("2025-01-27", 2176.50, "DEP JAN 26 TOAST"),
    ("2025-01-27", 3026.62, "DEP JAN 25 TOAST"),
    ("2025-01-28", 2000.00, "Trsf from Nun Dport"),
    ("2025-01-28", 2496.15, "DEP JAN 27 TOAST"),
    # Page 12
    ("2025-01-29", 83.00, "Deposit"),
    ("2025-01-29", 400.00, "Deposit"),
    ("2025-01-29", 533.93, "DEP JAN 28 TOAST"),
    ("2025-01-30", 337.00, "Deposit"),
    ("2025-01-30", 1276.70, "DEP JAN 29 TOAST"),
    # Page 13
    ("2025-01-31", 1914.73, "DEP JAN 30 TOAST"),
]

def main():
    access_token, tokens = get_valid_token()

    print(f"Checking {len(DEPOSITS)} deposits from Jan 2025 bank statement against QBO...")
    print(f"Realm ID: {REALM_ID}")
    print("=" * 100)

    found = []
    not_found = []

    # Query all Deposits in Jan 2025
    print("\nFetching all QBO Deposit transactions for Dec 2024 - Feb 2025...")
    sql_dep = "SELECT * FROM Deposit WHERE TxnDate >= '2024-12-15' AND TxnDate <= '2025-02-15' MAXRESULTS 500"
    result, tokens = qbo_query(sql_dep, tokens)
    qbo_deposits = result.get("QueryResponse", {}).get("Deposit", []) if result else []
    print(f"  Found {len(qbo_deposits)} QBO Deposit records")

    # Query all JournalEntries in Jan 2025
    print("Fetching all QBO Journal Entries for Dec 2024 - Feb 2025...")
    sql_je = "SELECT * FROM JournalEntry WHERE TxnDate >= '2024-12-15' AND TxnDate <= '2025-02-15' MAXRESULTS 500"
    result, tokens = qbo_query(sql_je, tokens)
    qbo_jes = result.get("QueryResponse", {}).get("JournalEntry", []) if result else []
    print(f"  Found {len(qbo_jes)} QBO Journal Entry records")

    # Query all SalesReceipts in Jan 2025
    print("Fetching all QBO Sales Receipts for Dec 2024 - Feb 2025...")
    sql_sr = "SELECT * FROM SalesReceipt WHERE TxnDate >= '2024-12-15' AND TxnDate <= '2025-02-15' MAXRESULTS 500"
    result, tokens = qbo_query(sql_sr, tokens)
    qbo_srs = result.get("QueryResponse", {}).get("SalesReceipt", []) if result else []
    print(f"  Found {len(qbo_srs)} QBO Sales Receipt records")

    # Query all Payments in Jan 2025
    print("Fetching all QBO Payments for Dec 2024 - Feb 2025...")
    sql_pay = "SELECT * FROM Payment WHERE TxnDate >= '2024-12-15' AND TxnDate <= '2025-02-15' MAXRESULTS 500"
    result, tokens = qbo_query(sql_pay, tokens)
    qbo_pays = result.get("QueryResponse", {}).get("Payment", []) if result else []
    print(f"  Found {len(qbo_pays)} QBO Payment records")

    # Query all Transfers in Jan 2025
    print("Fetching all QBO Transfers for Dec 2024 - Feb 2025...")
    sql_xfr = "SELECT * FROM Transfer WHERE TxnDate >= '2024-12-15' AND TxnDate <= '2025-02-15' MAXRESULTS 500"
    result, tokens = qbo_query(sql_xfr, tokens)
    qbo_xfrs = result.get("QueryResponse", {}).get("Transfer", []) if result else []
    print(f"  Found {len(qbo_xfrs)} QBO Transfer records")

    # Build a lookup of all QBO amounts for matching
    # Each entry: (type, date, amount, id, docnum/memo, detail)
    qbo_all = []

    for dep in qbo_deposits:
        total = dep.get("TotalAmt", 0)
        txn_date = dep.get("TxnDate", "")
        dep_id = dep.get("Id", "?")
        memo = dep.get("PrivateNote", "") or ""
        # Also check individual line amounts
        qbo_all.append(("Deposit", txn_date, total, dep_id, memo, f"Total={total}"))
        for line in dep.get("Line", []):
            line_amt = line.get("Amount", 0)
            line_desc = line.get("Description", "")
            if line_amt != total:  # don't double-count if single line
                qbo_all.append(("Deposit-Line", txn_date, line_amt, dep_id, line_desc, f"Line in Deposit #{dep_id}"))

    for je in qbo_jes:
        total = je.get("TotalAmt", 0)
        txn_date = je.get("TxnDate", "")
        je_id = je.get("Id", "?")
        doc_num = je.get("DocNumber", "")
        qbo_all.append(("JournalEntry", txn_date, total, je_id, doc_num, f"Total={total}"))
        # Check individual credit lines (credits to bank = deposits)
        for line in je.get("Line", []):
            detail = line.get("JournalEntryLineDetail", {})
            posting = detail.get("PostingType", "")
            acct_name = detail.get("AccountRef", {}).get("name", "")
            line_amt = line.get("Amount", 0)
            line_desc = line.get("Description", "")
            if posting == "Debit" and "5975" in acct_name:
                qbo_all.append(("JE-BankDebit", txn_date, line_amt, je_id, doc_num, f"Debit to {acct_name}: {line_desc}"))

    for sr in qbo_srs:
        total = sr.get("TotalAmt", 0)
        txn_date = sr.get("TxnDate", "")
        sr_id = sr.get("Id", "?")
        doc_num = sr.get("DocNumber", "")
        qbo_all.append(("SalesReceipt", txn_date, total, sr_id, doc_num, f"Total={total}"))

    for pay in qbo_pays:
        total = pay.get("TotalAmt", 0)
        txn_date = pay.get("TxnDate", "")
        pay_id = pay.get("Id", "?")
        qbo_all.append(("Payment", txn_date, total, pay_id, "", f"Total={total}"))

    for xfr in qbo_xfrs:
        amt = xfr.get("Amount", 0)
        txn_date = xfr.get("TxnDate", "")
        xfr_id = xfr.get("Id", "?")
        from_acct = xfr.get("FromAccountRef", {}).get("name", "?")
        to_acct = xfr.get("ToAccountRef", {}).get("name", "?")
        qbo_all.append(("Transfer", txn_date, amt, xfr_id, "", f"{from_acct} → {to_acct}"))

    print(f"\nTotal QBO transaction lines to search: {len(qbo_all)}")
    print("=" * 100)
    print(f"\n{'BANK DATE':<14} {'AMOUNT':>10}  {'BANK DESCRIPTION':<30} {'QBO MATCH?'}")
    print("-" * 100)

    for bank_date, amount, desc in DEPOSITS:
        # Search for exact amount match across all QBO transactions
        matches = []
        for qbo_type, qbo_date, qbo_amt, qbo_id, qbo_ref, qbo_detail in qbo_all:
            if abs(qbo_amt - amount) < 0.01:
                matches.append((qbo_type, qbo_date, qbo_id, qbo_ref, qbo_detail))

        if matches:
            # Check if any match is on the same date or within a few days
            exact_date = [m for m in matches if m[1] == bank_date]
            close_date = [m for m in matches if m[1] != bank_date]

            if exact_date:
                m = exact_date[0]
                print(f"{bank_date:<14} {amount:>10.2f}  {desc:<30} ✅ YES  {m[0]} #{m[2]} on {m[1]} ref={m[3]} {m[4]}")
                found.append((bank_date, amount, desc, exact_date))
            elif close_date:
                m = close_date[0]
                print(f"{bank_date:<14} {amount:>10.2f}  {desc:<30} ⚠️  DIFF DATE  {m[0]} #{m[2]} on {m[1]} ref={m[3]} {m[4]}")
                found.append((bank_date, amount, desc, close_date))
            # Show additional matches if any
            if len(matches) > 1:
                for m in matches[1:]:
                    print(f"{'':>57} ↳ also: {m[0]} #{m[2]} on {m[1]} ref={m[3]} {m[4]}")
        else:
            print(f"{bank_date:<14} {amount:>10.2f}  {desc:<30} ❌ NOT FOUND")
            not_found.append((bank_date, amount, desc))

    # Summary
    print("\n" + "=" * 100)
    print(f"\nSUMMARY")
    print(f"  Total deposits on bank statement: {len(DEPOSITS)}")
    print(f"  Found in QBO:                     {len(found)}")
    print(f"  NOT found in QBO:                 {len(not_found)}")

    if not_found:
        print(f"\n  Total $ NOT in QBO: ${sum(nf[1] for nf in not_found):,.2f}")
        print(f"\n  MISSING DEPOSITS:")
        for bank_date, amount, desc in not_found:
            print(f"    {bank_date}  ${amount:>10,.2f}  {desc}")

    if found:
        print(f"\n  Total $ found in QBO: ${sum(f[1] for f in found):,.2f}")

if __name__ == "__main__":
    main()
