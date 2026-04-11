#!/usr/bin/env python3
"""
7shifts → QuickBooks Journal Entry Import
Red Buoy Inc / Red Nun - Chatham

Usage:
    export SHIFTS_TOKEN=your_access_token_here
    python3 7shifts_to_qb.py --start 2025-11-10 --end 2025-11-23 --pay-date 2025-11-28

Outputs:
    QB_import_MMDDYYYY.csv  — ready to import into QuickBooks
"""

import os, sys, csv, argparse, json
from datetime import datetime, date
import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
QB_BANK_ACCOUNT   = "Cape Cod Five (5975)"
QB_WAGES          = "Payroll Expenses:Wages"
QB_PAYROLL_TIPS   = "Payroll Expenses:Payroll Tips"
QB_PAYROLL_TAXES  = "Payroll Expenses:Payroll Taxes"
QB_TIP_BANK       = "Tip Bank"

BASE_URL = "https://api.7shifts.com/v2"

# ── API helpers ───────────────────────────────────────────────────────────────
def api_get(path, token, company_guid=None, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    if company_guid:
        req.add_header("x-company-guid", company_guid)

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  HTTP {e.code} on {path}: {body[:200]}")
        return None

def get_company(token):
    """Discover company ID and GUID via whoami."""
    r = api_get("/whoami", token)
    if not r:
        sys.exit("❌ Auth failed — check your SHIFTS_TOKEN")
    data = r.get("data", {})
    users = data.get("users", [])
    if not users:
        sys.exit("❌ No users found in whoami response")
    # Pick the oldest hire_date (primary company)
    users_sorted = sorted(users, key=lambda u: u.get("hire_date","9999"))
    user = users_sorted[0]
    company_id   = user.get("company_id")
    company_guid = user.get("company_uuid") or user.get("company_guid")
    print(f"✓ Authenticated — company_id={company_id}  guid={company_guid}")
    if len(users) > 1:
        print(f"  (multiple companies found, using oldest: {user.get('hire_date')})")
    return company_id, company_guid

def get_locations(token, company_id, company_guid):
    r = api_get(f"/company/{company_id}/locations", token, company_guid)
    if not r:
        return []
    return r.get("data", [])

def get_payroll_report(token, company_id, company_guid, location_id, start, end):
    """
    Try the payroll report endpoint. Returns list of employee payroll rows.
    Each row should have: name, gross, net, payment_method, wages, tips, taxes, er_taxes
    """
    params = {
        "location_id": location_id,
        "start_date":  start,
        "end_date":    end,
    }
    r = api_get(f"/company/{company_id}/payroll_report", token, company_guid, params)
    if r:
        return r.get("data", [])

    # Fallback: try /payrolls endpoint
    r = api_get(f"/company/{company_id}/payrolls", token, company_guid, {
        "location_id": location_id,
        "from":        start,
        "to":          end,
    })
    if r:
        return r.get("data", [])

    return None

def get_time_punches(token, company_id, company_guid, location_id, start, end):
    """Raw time punches — fallback if no payroll report endpoint."""
    params = {
        "location_id":        location_id,
        "clocked_in[gte]":    f"{start}T00:00:00",
        "clocked_in[lte]":    f"{end}T23:59:59",
        "approved":           "true",
        "limit":              200,
    }
    r = api_get(f"/company/{company_id}/time_punches", token, company_guid, params)
    if not r:
        return []
    return r.get("data", [])

# ── CSV builder ───────────────────────────────────────────────────────────────
def build_qb_csv(employees, pay_date_str, period_str, output_path):
    """
    employees: list of dicts with keys:
        name, gross, net, payment_method (Direct Deposit | Manual/Paper Check),
        wages, paycheck_tips, cash_tips, ee_taxes, er_taxes
    """
    dt = datetime.strptime(pay_date_str, "%Y-%m-%d")
    journal_no  = dt.strftime("%m%d%Y")
    journal_date = dt.strftime("%m/%d/%Y")
    description = f"Pay period {period_str}"

    total_wages     = sum(e["wages"] for e in employees)
    total_tips      = sum(e["paycheck_tips"] + e["cash_tips"] for e in employees)
    total_cash_tips = sum(e["cash_tips"] for e in employees)
    total_er_taxes  = sum(e["er_taxes"] for e in employees)

    paper_checks = [e for e in employees if e["payment_method"] != "Direct Deposit" and e["net"] > 0]
    dd_employees = [e for e in employees if e["payment_method"] == "Direct Deposit"]
    dd_net       = sum(e["net"] for e in dd_employees)
    total_ee_tax = sum(e["ee_taxes"] for e in employees)
    dd_ach       = dd_net + total_ee_tax + total_er_taxes

    rows = []
    def row(account, debit="", credit="", desc=description, name=""):
        rows.append({
            "JournalNo":   journal_no,
            "JournalDate": journal_date,
            "AccountName": account,
            "Debits":      f"{debit:.2f}" if debit != "" else "",
            "Credits":     f"{credit:.2f}" if credit != "" else "",
            "Description": desc,
            "Name":        name,
        })

    # Debits
    row(QB_WAGES,         debit=total_wages)
    if total_tips > 0:
        row(QB_PAYROLL_TIPS, debit=total_tips)
    row(QB_PAYROLL_TAXES, debit=total_er_taxes)

    # Credits — individual paper checks
    for e in paper_checks:
        row(QB_BANK_ACCOUNT, credit=e["net"], name=e["name"])

    # Credits — DD + taxes lump sum
    if dd_ach > 0:
        row(QB_BANK_ACCOUNT, credit=dd_ach, desc="DD + Taxes")

    # Credits — cash tips out of tip bank
    if total_cash_tips > 0:
        row(QB_TIP_BANK, credit=total_cash_tips, desc="Cash Tips Paid")

    # Verify
    total_d = total_wages + total_tips + total_er_taxes
    total_c = sum(e["net"] for e in paper_checks) + dd_ach + total_cash_tips
    balanced = abs(total_d - total_c) < 0.02
    print(f"\nDebits: ${total_d:,.2f}  Credits: ${total_c:,.2f}  {'✓ BALANCED' if balanced else '⚠ MISMATCH'}")

    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["JournalNo","JournalDate","AccountName",
                                           "Debits","Credits","Description","Name"])
        w.writeheader()
        w.writerows(rows)

    print(f"Saved: {output_path}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="7shifts → QuickBooks payroll CSV")
    parser.add_argument("--start",    required=True, help="Period start YYYY-MM-DD")
    parser.add_argument("--end",      required=True, help="Period end YYYY-MM-DD")
    parser.add_argument("--pay-date", required=True, help="Pay date YYYY-MM-DD")
    parser.add_argument("--location", default=None,  help="Location ID (optional, auto-detected)")
    parser.add_argument("--out",      default=None,  help="Output CSV path")
    args = parser.parse_args()

    token = os.environ.get("SHIFTS_TOKEN")
    if not token:
        sys.exit("❌ Set SHIFTS_TOKEN environment variable first:\n   export SHIFTS_TOKEN=your_token_here")

    print(f"\n7shifts → QB Import")
    print(f"Period: {args.start} to {args.end}  |  Pay date: {args.pay_date}\n")

    # Auth + company discovery
    company_id, company_guid = get_company(token)

    # Location
    location_id = args.location
    if not location_id:
        locations = get_locations(token, company_id, company_guid)
        if not locations:
            sys.exit("❌ No locations found — pass --location ID manually")
        # Pick the Chatham location
        chatham = next((l for l in locations if "chatham" in l.get("name","").lower()), locations[0])
        location_id = chatham["id"]
        print(f"✓ Using location: {chatham.get('name')} (id={location_id})")

    # Pull payroll data
    print(f"Fetching payroll report...")
    emp_rows = get_payroll_report(token, company_id, company_guid, location_id, args.start, args.end)

    if emp_rows is None:
        print("⚠  Payroll report endpoint not available — falling back to raw time punches.")
        print("   Note: tax calculations will not be available from raw punches.")
        print("   Export the payroll CSVs from 7shifts and run:")
        print("   python3 7shifts_to_qb.py --csv payroll-journal.csv payroll-summary.csv ...\n")
        punches = get_time_punches(token, company_id, company_guid, location_id, args.start, args.end)
        print(f"   Got {len(punches)} time punches — manual tax calculation required.")
        sys.exit(1)

    if not emp_rows:
        sys.exit("❌ No payroll data found for that period/location.")

    # Normalize rows from API response
    employees = []
    for e in emp_rows:
        employees.append({
            "name":           f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
            "gross":          float(e.get("gross_pay", 0)),
            "net":            float(e.get("net_pay", 0)),
            "payment_method": e.get("payment_method", "Manual"),
            "wages":          float(e.get("hourly_earnings", 0)) + float(e.get("salaried_earnings", 0))
                              + float(e.get("overtime_earnings", 0)) + float(e.get("tip_credit_adjustment", 0)),
            "paycheck_tips":  float(e.get("paycheck_tips", 0)),
            "cash_tips":      float(e.get("cash_tips", 0)),
            "ee_taxes":       float(e.get("employee_taxes", 0)),
            "er_taxes":       float(e.get("employer_taxes", 0)),
        })
        print(f"  {employees[-1]['name']:30} gross={employees[-1]['gross']:>8.2f}  net={employees[-1]['net']:>8.2f}  method={employees[-1]['payment_method']}")

    # Output path
    dt = datetime.strptime(args.pay_date, "%Y-%m-%d")
    out_path = args.out or f"QB_import_{dt.strftime('%m%d%Y')}.csv"

    start_fmt = datetime.strptime(args.start, "%Y-%m-%d").strftime("%m/%d/%y")
    end_fmt   = datetime.strptime(args.end,   "%Y-%m-%d").strftime("%m/%d/%y")
    period_str = f"{start_fmt}-{end_fmt}"

    build_qb_csv(employees, args.pay_date, period_str, out_path)


if __name__ == "__main__":
    main()
