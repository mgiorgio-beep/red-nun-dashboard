#!/usr/bin/env python3
"""
Query QBO for bank statement payments/debits to see which ones are already entered.
Searches across Bill Payments, Purchases, Checks, Expenses, JournalEntries, Transfers.
Source: Jan 2025 bank statement for Red Nun Chatham (Cape Cod Five 5975)
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

# ── All debits/payments from Jan 2025 bank statement ──
# (bank_date, amount, description)
PAYMENTS = [
    # Page 1 — 1/02
    ("2025-01-02", 0.86, "Check 5743"),
    ("2025-01-02", 10.82, "Check 6066"),
    ("2025-01-02", 1000.00, "Transf to Nun Dport 102250061"),
    ("2025-01-02", 89.30, "POS DEB STOP & SHOP 0475"),
    ("2025-01-02", 82.90, "DBT CRD AIRGAS - NORTH"),
    ("2025-01-02", 28.64, "Check 6471"),
    ("2025-01-02", 2246.16, "Check 6347"),
    ("2025-01-02", 2246.16, "Check 6472"),
    ("2025-01-02", 4.89, "INST XFER PAYPAL METAPLATFOR"),
    ("2025-01-02", 68.33, "PACERPYRLC THE HARTFORD"),
    # Page 2
    ("2025-01-02", 191.90, "DAVO TECHNOLOGIE C88FBFBB-8"),
    ("2025-01-02", 220.00, "SALE SHAUN KALINOWSKI"),
    ("2025-01-02", 375.00, "SALE TELESMANICK PROP"),
    ("2025-01-02", 410.02, "NGRID07 National Grid"),
    ("2025-01-02", 2438.21, "Retainer Kickfin Inc"),
    ("2025-01-02", 4109.45, "VENDOR PAY US FOODSERVICE"),
    ("2025-01-03", 2500.00, "Transf to FMT Holdings 103250139"),
    ("2025-01-03", 58.43, "DAVO TECHNOLOGIE B3B2A2BE-E"),
    ("2025-01-03", 60.00, "INST XFER PAYPAL DIIGO INC"),
    ("2025-01-03", 182.09, "DAVO TECHNOLOGIE 2C702836-5"),
    ("2025-01-03", 336.42, "Retainer Kickfin Inc"),
    ("2025-01-03", 458.55, "WEB PAY NUCO2 LLC"),
    ("2025-01-03", 702.94, "DAVO TECHNOLOGIE 27EAFD2E-2"),
    # Page 3 — 1/06
    ("2025-01-06", 42.57, "Check 6469"),
    ("2025-01-06", 85.00, "Check 1000109"),
    ("2025-01-06", 435.00, "Bill Paid CARON GROUP #705"),
    ("2025-01-06", 1400.00, "Transf to FMT Holdings 106250175"),
    ("2025-01-06", 132.33, "DBT CRD TIGER EXCHANGE"),
    ("2025-01-06", 164.57, "DDA B/P UNIFIRST CORPORATION"),
    ("2025-01-06", 220.80, "Check 6477"),
    ("2025-01-06", 352.22, "Check 6482"),
    ("2025-01-06", 10.00, "INST XFER PAYPAL UBER"),
    ("2025-01-06", 26.97, "INST XFER PAYPAL UBER"),
    ("2025-01-06", 108.81, "DAVO TECHNOLOGIE 12F48860-9"),
    ("2025-01-06", 363.00, "SALE MARGINEDGE CO"),
    ("2025-01-06", 906.09, "Retainer Kickfin Inc"),
    # Page 4 — 1/07, 1/08
    ("2025-01-07", 345.00, "DBT CRD DENNIS COC INV-1968"),
    ("2025-01-07", 794.58, "DBT CRD BENJAMIN T NICKERSON"),
    ("2025-01-07", 89.83, "DAVO TECHNOLOGIE 924AC79E-1"),
    ("2025-01-07", 125.00, "SALE FORE & AFT INC"),
    ("2025-01-07", 163.47, "DAVO TECHNOLOGIE B671B6D1-7"),
    ("2025-01-07", 235.58, "DAVO TECHNOLOGIE 20ACIEF8-B"),
    ("2025-01-07", 385.00, "SALE FORE & AFT INC"),
    ("2025-01-07", 452.83, "HORIZON BEVERAGE"),
    ("2025-01-07", 840.00, "SALE FORE & AFT INC"),
    ("2025-01-07", 1175.60, "INVOICES L. KNIFE & SON"),
    ("2025-01-07", 1175.75, "INVOICES COLONIAL WHOLESA"),
    ("2025-01-08", 34.01, "DAVO TECHNOLOGIE 42A22421-E"),
    ("2025-01-08", 198.00, "PAYMENT SBA EIDL LOAN"),
    ("2025-01-08", 322.22, "Retainer Kickfin Inc"),
    # Page 5 — 1/09, 1/10
    ("2025-01-09", 323.95, "DBT CRD SUBURBAN SUPPLY CO"),
    ("2025-01-09", 357.82, "DBT CRD BASKIN S HARDWARE"),
    ("2025-01-09", 160.71, "DAVO TECHNOLOGIE 9C4C3C5C-4"),
    ("2025-01-09", 206.41, "Retainer Kickfin Inc"),
    ("2025-01-09", 294.55, "SALE DEPENDABLE RESTA"),
    ("2025-01-09", 6979.67, "Payroll E29998 RED BUO"),
    ("2025-01-10", 400.54, "Check 6483"),
    ("2025-01-10", 86.26, "Kickfin Inc 7372526785"),
    ("2025-01-10", 127.42, "DAVO TECHNOLOGIE 96F59B49-6"),
    ("2025-01-10", 665.38, "Retainer Kickfin Inc"),
    # Page 6 — 1/13, 1/14
    ("2025-01-13", 374.34, "Check 6485"),
    ("2025-01-13", 435.00, "Bill Paid CARON GROUP #707"),
    ("2025-01-13", 159.74, "DAVO TECHNOLOGIE E891B4AF-7"),
    ("2025-01-13", 197.30, "SERVICES DTT INC"),
    ("2025-01-13", 221.19, "DAVO TECHNOLOGIE E6E47AF6-A"),
    ("2025-01-13", 313.39, "COMCAST 740118320"),
    ("2025-01-13", 741.38, "Retainer Kickfin Inc"),
    ("2025-01-14", 29.99, "DDA B/P Roku for NESN"),
    ("2025-01-14", 141.00, "DBT CRD A1 EXTERMINATORS"),
    ("2025-01-14", 31.57, "Check 6475"),
    ("2025-01-14", 110.16, "PACERPYRLC THE HARTFORD"),
    # Page 7 — 1/14, 1/15, 1/16
    ("2025-01-14", 140.13, "DAVO TECHNOLOGIE 9A0CEC6D-1"),
    ("2025-01-14", 144.43, "DAVO TECHNOLOGIE 1F3945BC-1"),
    ("2025-01-14", 617.18, "MARTIGNETTI COMP 00151157"),
    ("2025-01-14", 1189.00, "INVOICES L. KNIFE & SON"),
    ("2025-01-15", 1923.97, "Check 6495"),
    ("2025-01-15", 2260.87, "Check 6490"),
    ("2025-01-15", 53.93, "DAVO TECHNOLOGIE 9DF98F1C-2"),
    ("2025-01-15", 236.61, "Retainer Kickfin Inc"),
    ("2025-01-15", 309.77, "MARGINEDGE CO MEEP000065"),
    ("2025-01-16", 327.57, "DDA B/P DIRECTV SERVICE"),
    ("2025-01-16", 270.56, "Check 6465"),
    ("2025-01-16", 110.58, "DAVO TECHNOLOGIE 3FD2A474-D"),
    ("2025-01-16", 152.32, "AGNT PMT Clearway 16"),
    ("2025-01-16", 315.91, "Retainer Kickfin Inc"),
    # Page 8 — 1/16, 1/17
    ("2025-01-16", 380.00, "SALE THE CHATHAM CHAM"),
    ("2025-01-16", 478.47, "PPD PAYMNT Clearway 15"),
    ("2025-01-16", 893.97, "AR PAYMENT PERFORMANCEBOS"),
    ("2025-01-17", 13.79, "Check 6493"),
    ("2025-01-17", 435.00, "Bill Paid CARON GROUP #709"),
    ("2025-01-17", 116.80, "Check 6479"),
    ("2025-01-17", 124.90, "DAVO TECHNOLOGIE 893FEB94-7"),
    # Page 8 — 1/21
    ("2025-01-21", 451.99, "Check 6484"),
    ("2025-01-21", 484.14, "Check 6501"),
    ("2025-01-21", 225.00, "Bill Paid FOX ASSOCIATES #713"),
    ("2025-01-21", 10.00, "DBT CRD CHECK-PRINT.COM"),
    ("2025-01-21", 48.00, "DBT CRD CAMCLOUD"),
    # Page 9 — 1/21 continued
    ("2025-01-21", 150.62, "DDA B/P UNIFIRST CORPORATION"),
    ("2025-01-21", 47.48, "Check 6489"),
    ("2025-01-21", 326.92, "Check 6496"),
    ("2025-01-21", 783.06, "Check 6498"),
    ("2025-01-21", 100.00, "INVOICES COLONIAL WHOLESA"),
    ("2025-01-21", 119.45, "PREM EFT QuincyMutual462"),
    ("2025-01-21", 121.36, "DAVO TECHNOLOGIE 3E74BD62-6"),
    ("2025-01-21", 217.01, "DAVO TECHNOLOGIE 3F4070C6-F"),
    ("2025-01-21", 236.16, "PCS SVC T-MOBILE"),
    ("2025-01-21", 250.00, "PAYMENT VENMO"),
    ("2025-01-21", 825.90, "INVOICES L. KNIFE & SON"),
    ("2025-01-21", 1023.25, "Retainer Kickfin Inc"),
    ("2025-01-21", 1110.06, "AR PAYMENT PERFORMANCEBOS"),
    ("2025-01-21", 1543.11, "VENDOR PAY US FOODSERVICE"),
    ("2025-01-21", 3669.08, "VENDOR PAY US FOODSERVICE"),
    ("2025-01-21", 0.48, "Int Fee CAMCLOUD"),
    # Page 10 — 1/22, 1/23, 1/24
    ("2025-01-22", 666.81, "Check 6491"),
    ("2025-01-22", 56.08, "DAVO TECHNOLOGIE FB049AB6-B"),
    ("2025-01-22", 174.58, "DAVO TECHNOLOGIE 1DB276C4-B"),
    ("2025-01-22", 208.56, "DAVO TECHNOLOGIE 908CD577-C"),
    ("2025-01-22", 275.42, "Retainer Kickfin Inc"),
    ("2025-01-22", 1037.25, "HORIZON BEVERAGE"),
    ("2025-01-23", 471.22, "Check 6499"),
    ("2025-01-23", 894.64, "Check 6492"),
    ("2025-01-23", 97.81, "DAVO TECHNOLOGIE 3608CE02-8"),
    ("2025-01-24", 43.98, "POS DEB Chatham Paint and Hard"),
    ("2025-01-24", 105.19, "QBooks Onl INTUIT"),
    ("2025-01-24", 139.10, "DAVO TECHNOLOGIE D8EED9FD-5"),
    ("2025-01-24", 797.01, "Retainer Kickfin Inc"),
    ("2025-01-24", 4526.56, "Payroll E29998 RED BUO"),
    # Page 11 — 1/27, 1/28
    ("2025-01-27", 34.15, "Check 6486"),
    ("2025-01-27", 435.00, "Bill Paid CARON GROUP #711"),
    ("2025-01-27", 104.35, "DAVO TECHNOLOGIE C16E2789-9"),
    ("2025-01-27", 120.96, "INST XFER PAYPAL GOOGLE"),
    ("2025-01-27", 209.68, "DAVO TECHNOLOGIE DAE5E33D-8"),
    ("2025-01-27", 1328.77, "Retainer Kickfin Inc"),
    ("2025-01-27", 1984.73, "AR PAYMENT PERFORMANCEBOS"),
    ("2025-01-28", 754.57, "Check 6478"),
    ("2025-01-28", 29.30, "Retainer Kickfin Inc"),
    # Page 12 — 1/28, 1/29, 1/30
    ("2025-01-28", 172.54, "DAVO TECHNOLOGIE DECA4308-4"),
    ("2025-01-28", 306.90, "INVOICES COLONIAL WHOLESA"),
    ("2025-01-28", 497.76, "MARTIGNETTI COMP 00151157"),
    ("2025-01-28", 521.95, "HORIZON BEVERAGE"),
    ("2025-01-28", 1426.50, "INVOICES L. KNIFE & SON"),
    ("2025-01-28", 2416.29, "VENDOR PAY US FOODSERVICE"),
    ("2025-01-29", 125.00, "Check 1000110"),
    ("2025-01-29", 190.73, "Check 6464"),
    ("2025-01-29", 396.16, "Check 6497"),
    ("2025-01-29", 298.59, "Check 6508"),
    ("2025-01-29", 38.20, "DAVO TECHNOLOGIE 5CAD8B6D-2"),
    ("2025-01-29", 63.22, "PACERPYRLC THE HARTFORD"),
    ("2025-01-29", 106.25, "SALE GLANOLA NORTH AM"),
    ("2025-01-29", 538.70, "Toast Inc Toast Inc"),
    ("2025-01-30", 1597.15, "Check 6511"),
    ("2025-01-30", 92.68, "DAVO TECHNOLOGIE 3ACE4C50-F"),
    ("2025-01-30", 517.86, "Retainer Kickfin Inc"),
    # Page 13 — 1/31
    ("2025-01-31", 73.00, "POS DEB USPS"),
    ("2025-01-31", 320.61, "Check 6500"),
    ("2025-01-31", 481.19, "Check 6517"),
    ("2025-01-31", 138.91, "DAVO TECHNOLOGIE 91EDB1C0-6"),
    ("2025-01-31", 290.85, "Retainer Kickfin Inc"),
    ("2025-01-31", 7.00, "Maintenance Fee"),
]

def main():
    access_token, tokens = get_valid_token()

    print(f"Checking {len(PAYMENTS)} payments/debits from Jan 2025 bank statement against QBO...")
    print(f"Realm ID: {REALM_ID}")
    print("=" * 110)

    # Fetch all relevant QBO transaction types
    qbo_all = []

    for txn_type, label in [
        ("Purchase", "Purchase/Expense"),
        ("Bill", "Bill"),
        ("BillPayment", "BillPayment"),
        ("JournalEntry", "JournalEntry"),
        ("Transfer", "Transfer"),
        ("VendorCredit", "VendorCredit"),
        ("Check", "Check"),
    ]:
        print(f"Fetching QBO {label} records for Dec 2024 - Feb 2025...")
        sql = f"SELECT * FROM {txn_type} WHERE TxnDate >= '2024-12-15' AND TxnDate <= '2025-02-15' MAXRESULTS 1000"
        result, tokens = qbo_query(sql, tokens)
        if result:
            items = result.get("QueryResponse", {}).get(txn_type, [])
            print(f"  Found {len(items)} {label} records")

            for item in items:
                total = item.get("TotalAmt", item.get("Amount", 0))
                txn_date = item.get("TxnDate", "")
                item_id = item.get("Id", "?")
                doc_num = item.get("DocNumber", "")
                vendor = ""
                if "VendorRef" in item:
                    vendor = item["VendorRef"].get("name", "")
                elif "EntityRef" in item:
                    vendor = item["EntityRef"].get("name", "")
                memo = item.get("PrivateNote", "") or ""

                ref_info = doc_num or vendor or memo
                qbo_all.append((txn_type, txn_date, total, item_id, ref_info[:50]))

                # Also index line-level amounts for multi-line transactions
                for line in item.get("Line", []):
                    line_amt = line.get("Amount", 0)
                    if line_amt > 0 and abs(line_amt - total) > 0.01:
                        line_desc = line.get("Description", "")[:30]
                        qbo_all.append((f"{txn_type}-Line", txn_date, line_amt, item_id, line_desc))
        else:
            print(f"  ⚠ Could not fetch {label}")

    print(f"\nTotal QBO transaction entries to search: {len(qbo_all)}")
    print("=" * 110)
    print(f"\n{'BANK DATE':<14} {'AMOUNT':>10}  {'BANK DESCRIPTION':<40} {'QBO MATCH?'}")
    print("-" * 110)

    found = []
    not_found = []

    for bank_date, amount, desc in PAYMENTS:
        matches = []
        for qbo_type, qbo_date, qbo_amt, qbo_id, qbo_ref in qbo_all:
            if abs(qbo_amt - amount) < 0.01:
                matches.append((qbo_type, qbo_date, qbo_id, qbo_ref))

        if matches:
            exact_date = [m for m in matches if m[1] == bank_date]
            close_date = [m for m in matches if m[1] != bank_date]

            if exact_date:
                m = exact_date[0]
                print(f"{bank_date:<14} {amount:>10.2f}  {desc:<40} ✅ {m[0]} #{m[2]} on {m[1]} {m[3]}")
                found.append((bank_date, amount, desc, exact_date))
            elif close_date:
                m = close_date[0]
                print(f"{bank_date:<14} {amount:>10.2f}  {desc:<40} ⚠️  DIFF DATE {m[0]} #{m[2]} on {m[1]} {m[3]}")
                found.append((bank_date, amount, desc, close_date))
            if len(matches) > 1:
                for m in matches[1:3]:  # show up to 2 extra matches
                    print(f"{'':>67} ↳ also: {m[0]} #{m[2]} on {m[1]} {m[3]}")
        else:
            print(f"{bank_date:<14} {amount:>10.2f}  {desc:<40} ❌ NOT FOUND")
            not_found.append((bank_date, amount, desc))

    # Summary
    print("\n" + "=" * 110)
    print(f"\nSUMMARY")
    print(f"  Total debits on bank statement: {len(PAYMENTS)}")
    print(f"  Found in QBO:                   {len(found)}")
    print(f"  NOT found in QBO:               {len(not_found)}")

    if not_found:
        print(f"\n  Total $ NOT in QBO: ${sum(nf[1] for nf in not_found):,.2f}")
        print(f"\n  MISSING PAYMENTS:")
        for bank_date, amount, desc in not_found:
            print(f"    {bank_date}  ${amount:>10,.2f}  {desc}")

    if found:
        print(f"\n  Total $ found in QBO: ${sum(f[1] for f in found):,.2f}")

if __name__ == "__main__":
    main()
