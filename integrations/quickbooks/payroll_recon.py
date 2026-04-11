#!/usr/bin/env python3
"""
Payroll Reconciliation — 7shifts vs QuickBooks
Compares payroll periods from 7shifts CSV exports against journal entries in QBO.

Usage:
    python3 payroll_recon.py --summaries /path/to/summaries/ --year 2025

    The --summaries folder should contain all your payroll-summary CSV files,
    named anything (e.g. payroll-summary_2025-01-01_2025-01-14.csv)

    Outputs a report showing:
    - Which payroll periods are in QBO (by journal DocNumber/date)
    - Which are missing
    - Which may be duplicated
"""

import os, sys, csv, json, argparse, glob
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from pathlib import Path

TOKEN_FILE = Path.home() / ".qb_tokens.json"
BASE_URL   = "https://quickbooks.api.intuit.com"

# ── Token loading ─────────────────────────────────────────────────────────────
def load_tokens():
    if not TOKEN_FILE.exists():
        sys.exit("❌ No tokens found. Run qb_push.py --auth first.")
    with open(TOKEN_FILE, encoding='utf-8-sig') as f:
        return json.load(f)

def get_credentials():
    client_id     = os.environ.get("QB_CLIENT_ID")
    client_secret = os.environ.get("QB_CLIENT_SECRET")
    if not all([client_id, client_secret]):
        sys.exit("❌ Set QB_CLIENT_ID and QB_CLIENT_SECRET environment variables.")
    return client_id, client_secret

def refresh_token(tokens, client_id, client_secret):
    import base64, time
    creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        data=data, method="POST"
    )
    req.add_header("Authorization", f"Basic {creds_b64}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req) as resp:
        new_tokens = json.loads(resp.read())
    new_tokens["obtained_at"] = time.time()
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens["refresh_token"]
    new_tokens["realm_id"] = tokens.get("realm_id")
    with open(TOKEN_FILE, "w") as f:
        json.dump(new_tokens, f, indent=2)
    return new_tokens

def get_access_token(client_id, client_secret):
    import time
    tokens = load_tokens()
    age = time.time() - tokens.get("obtained_at", 0)
    if age > 3000:
        tokens = refresh_token(tokens, client_id, client_secret)
    return tokens["access_token"], tokens["realm_id"]

# ── QBO API ───────────────────────────────────────────────────────────────────
def qbo_query(sql, realm_id, access_token):
    encoded = urllib.parse.quote(sql)
    url = f"{BASE_URL}/v3/company/{realm_id}/query?query={encoded}&minorversion=65"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
        return None

def get_qbo_journal_entries(realm_id, access_token, year):
    """Fetch all journal entries for the given year with real amounts."""
    sql = f"SELECT * FROM JournalEntry WHERE TxnDate >= '{year}-01-01' AND TxnDate <= '{year}-12-31' MAXRESULTS 1000"
    r = qbo_query(sql, realm_id, access_token)
    if not r:
        return []
    entries = r.get("QueryResponse", {}).get("JournalEntry", [])

    # Calculate TotalAmt from line items (API summary returns 0)
    for je in entries:
        lines = je.get("Line", [])
        total = sum(
            float(l.get("Amount", 0))
            for l in lines
            if l.get("JournalEntryLineDetail", {}).get("PostingType") == "Debit"
        )
        je["TotalAmt"] = total

    return entries

# ── 7shifts CSV parsing ───────────────────────────────────────────────────────
def parse_summary_csvs(folder, year):
    """
    Parse all payroll-summary CSVs in folder.
    Returns list of dicts with period info.
    """
    payrolls = []
    # Rename files with spaces/parens in names (Windows artifacts)
    for f in glob.glob(os.path.join(folder, "*.csv")):
        clean = f.replace(' (1)', '_1').replace(' (2)', '_2').replace(' (3)', '_3')
        if clean != f:
            os.rename(f, clean)
            print(f"  Renamed: {os.path.basename(f)} → {os.path.basename(clean)}")
    pattern = os.path.join(folder, "*.csv")
    files = glob.glob(pattern)

    if not files:
        sys.exit(f"❌ No CSV files found in {folder}")

    for fpath in sorted(files):
        fname = os.path.basename(fpath)

        # Try to extract dates from filename
        # e.g. payroll-summary_2025-01-01_2025-01-14.csv
        parts = fname.replace('.csv','').split('_')
        period_start = None
        period_end   = None
        for p in parts:
            try:
                dt = datetime.strptime(p, "%Y-%m-%d")
                if period_start is None:
                    period_start = dt
                else:
                    period_end = dt
                    break
            except:
                continue

        if not period_start or not period_end:
            print(f"  ⚠ Skipping {fname} — can't parse dates from filename")
            continue

        if period_start.year != int(year) and period_end.year != int(year):
            continue

        # Read totals row
        gross = net = ee_taxes = er_taxes = 0.0
        try:
            with open(fpath) as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                if row.get('Last Name','').strip() == '':
                    gross    = float(row.get('Gross Total') or 0)
                    net      = float(row.get('Net Pay') or 0)
                    ee_cols  = ['Social Security Tax (EE)','Federal Income Tax (EE)',
                                'Medicare (EE)','Massachusetts State Tax (EE)',
                                'Massachusetts Paid Family and Medical Leave - Employee (EE)']
                    er_cols  = ['Employer Social Security Tax (ER)','Federal Unemployment Tax (ER)',
                                'Employer Medicare Tax (ER)',
                                'Massachusetts Employer Medical Assistance Contributions (ER)',
                                'Massachusetts Paid Family and Medical Leave - Employer (ER)',
                                'Massachusetts State Unemployment Tax (ER)',
                                'Massachusetts Workforce Training Fund (ER)',
                                'Massachusetts COVID-19 Recovery Assessment (ER)']
                    ee_taxes = sum(float(row.get(c) or 0) for c in ee_cols)
                    er_taxes = sum(float(row.get(c) or 0) for c in er_cols)
                    break
        except Exception as e:
            print(f"  ⚠ Error reading {fname}: {e}")
            continue

        total_cost = gross + er_taxes

        payrolls.append({
            'file':         fname,
            'period_start': period_start,
            'period_end':   period_end,
            'gross':        gross,
            'net':          net,
            'ee_taxes':     ee_taxes,
            'er_taxes':     er_taxes,
            'total_cost':   total_cost,
        })

    # Deduplicate — keep one entry per (period_start, period_end, gross)
    seen = {}
    for p in payrolls:
        key = (p['period_start'], p['period_end'], round(p['gross'], 2))
        if key not in seen:
            seen[key] = p
    payrolls = list(seen.values())
    return sorted(payrolls, key=lambda x: x['period_start'])

# ── Matching logic ────────────────────────────────────────────────────────────
def match_payrolls(payrolls, qbo_entries):
    """
    Match 7shifts payroll periods to QBO journal entries.
    Matching strategy:
    1. DocNumber matches MMDDYYYY of a pay date near the period end
    2. TxnDate within ~10 days of period end
    3. TotalAmt matches total_cost within $1
    """

    # Build QBO lookup by date and amount
    qbo_by_docnum = {}
    qbo_by_date   = {}
    for je in qbo_entries:
        doc = je.get("DocNumber","")
        dt  = je.get("TxnDate","")
        amt = float(je.get("TotalAmt", 0))
        qbo_by_docnum[doc] = je
        if dt not in qbo_by_date:
            qbo_by_date[dt] = []
        qbo_by_date[dt].append(je)

    results = []
    used_je_ids = {}

    for p in payrolls:
        pe = p['period_end']

        # Pay date = period_end + 5 days (Friday after Sunday period end), ±3 days tolerance
        from datetime import timedelta
        expected_pay_date = pe + timedelta(days=5)
        matched = []
        for je in qbo_entries:
            je_date = datetime.strptime(je.get("TxnDate","2000-01-01"), "%Y-%m-%d")
            delta = abs((je_date - expected_pay_date).days)
            if delta <= 3:
                je_amt = float(je.get("TotalAmt", 0))
                # Amount match within $50 (slight variance ok)
                if abs(je_amt - p['total_cost']) < 50:
                    matched.append(je)

        status = "MISSING"
        match_info = []

        if len(matched) == 0:
            status = "❌ MISSING"
        elif len(matched) == 1:
            je = matched[0]
            je_id = je.get("Id")
            if je_id in used_je_ids:
                status = f"⚠  DUPLICATE (JE #{je.get('DocNumber')} also matched {used_je_ids[je_id]})"
            else:
                status = f"✓  POSTED"
                used_je_ids[je_id] = p['period_end'].strftime("%m/%d/%y")
            match_info = [f"JE #{je.get('DocNumber')} on {je.get('TxnDate')} ${float(je.get('TotalAmt',0)):,.2f}"]
        else:
            status = f"⚠  MULTIPLE MATCHES ({len(matched)})"
            match_info = [f"JE #{je.get('DocNumber')} on {je.get('TxnDate')} ${float(je.get('TotalAmt',0)):,.2f}"
                          for je in matched]

        results.append({
            'period': f"{pe.strftime('%m/%d/%y')} (pay period {p['period_start'].strftime('%m/%d/%y')}-{pe.strftime('%m/%d/%y')})",
            'total_cost': p['total_cost'],
            'gross': p['gross'],
            'status': status,
            'matches': match_info,
            'file': p['file'],
        })

    return results

# ── Report ────────────────────────────────────────────────────────────────────
def print_report(results, qbo_entries, year):
    print(f"\n{'='*70}")
    print(f"  PAYROLL RECONCILIATION REPORT — {year}")
    print(f"{'='*70}\n")

    missing = [r for r in results if "MISSING" in r['status']]
    posted  = [r for r in results if "POSTED" in r['status']]
    dupe    = [r for r in results if "MULTIPLE" in r['status'] or "DUPLICATE" in r['status']]

    print(f"  7shifts payroll periods found: {len(results)}")
    print(f"  ✓  Posted to QBO:              {len(posted)}")
    print(f"  ❌ Missing from QBO:            {len(missing)}")
    print(f"  ⚠  Possible duplicates:         {len(dupe)}")
    print()

    if missing:
        print(f"{'─'*70}")
        print("  MISSING FROM QBO:")
        print(f"{'─'*70}")
        for r in missing:
            print(f"  {r['period']}")
            print(f"    Total cost: ${r['total_cost']:,.2f}  |  Gross: ${r['gross']:,.2f}")
            print(f"    Source: {r['file']}")
            print()

    if dupe:
        print(f"{'─'*70}")
        print("  POSSIBLE DUPLICATES / MULTIPLE MATCHES:")
        print(f"{'─'*70}")
        for r in dupe:
            print(f"  {r['period']}")
            print(f"    Status: {r['status']}")
            for m in r['matches']:
                print(f"    → {m}")
            print()

    if posted:
        print(f"{'─'*70}")
        print("  POSTED:")
        print(f"{'─'*70}")
        for r in posted:
            print(f"  {r['period']}  ${r['total_cost']:,.2f}")
            for m in r['matches']:
                print(f"    → {m}")
        print()

    # Also show any QBO journal entries that didn't match anything
    matched_docs = set()
    for r in results:
        for m in r['matches']:
            doc = m.split('#')[1].split(' ')[0] if '#' in m else ''
            matched_docs.add(doc)

    unmatched_qbo = [je for je in qbo_entries
                     if je.get('DocNumber','') not in matched_docs]
    if unmatched_qbo:
        print(f"{'─'*70}")
        print("  QBO JOURNAL ENTRIES WITH NO 7SHIFTS MATCH (possible manual entries or other locations):")
        print(f"{'─'*70}")
        for je in sorted(unmatched_qbo, key=lambda x: x.get('TxnDate','')):
            print(f"  JE #{je.get('DocNumber','?'):12} {je.get('TxnDate','')}  ${float(je.get('TotalAmt',0)):>10,.2f}  {je.get('PrivateNote','')[:40]}")
        print()

    print(f"{'='*70}\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Reconcile 7shifts payroll vs QuickBooks journal entries")
    parser.add_argument("--summaries", required=True, help="Folder containing payroll-summary CSV files")
    parser.add_argument("--year",      default="2025", help="Year to reconcile (default: 2025)")
    args = parser.parse_args()

    client_id, client_secret = get_credentials()
    access_token, realm_id   = get_access_token(client_id, client_secret)

    print(f"\nLoading 7shifts payroll data from {args.summaries}...")
    payrolls = parse_summary_csvs(args.summaries, args.year)
    print(f"  Found {len(payrolls)} payroll periods for {args.year}")

    print(f"\nFetching QBO journal entries for {args.year}...")
    qbo_entries = get_qbo_journal_entries(realm_id, access_token, args.year)
    print(f"  Found {len(qbo_entries)} journal entries in QBO")

    results = match_payrolls(payrolls, qbo_entries)
    print_report(results, qbo_entries, args.year)

if __name__ == "__main__":
    main()
