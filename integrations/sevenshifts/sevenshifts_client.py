"""
7shifts API Client
Pulls labor data (hours, wages, tips) from 7shifts for complete payroll reporting.
Includes both hourly and salaried employees.

API Docs: https://developers.7shifts.com
Auth: Bearer token via Authorization header
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# 7shifts company IDs (from /whoami endpoint)
COMPANY_IDS = {
    "dennis": int(os.getenv("SEVENSHIFTS_COMPANY_DENNIS", "87880")),
    "chatham": int(os.getenv("SEVENSHIFTS_COMPANY_CHATHAM", "382225")),
}

BASE_URL = "https://api.7shifts.com/v2"


class SevenShiftsClient:
    """Client for the 7shifts V2 API."""

    def __init__(self, token=None):
        self.token = token or os.getenv("SEVENSHIFTS_TOKEN_CHATHAM") or os.getenv("SEVENSHIFTS_ACCESS_TOKEN")
        if not self.token:
            logger.warning("No 7shifts token found in .env")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        })

    def _get(self, endpoint, params=None):
        """Make a GET request with rate limit and retry handling."""
        url = f"{BASE_URL}{endpoint}"
        max_retries = 3

        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=30)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"7shifts rate limited. Waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                if resp.status_code == 503 and attempt < max_retries - 1:
                    wait = 3 * (attempt + 1)
                    logger.warning(f"7shifts 503, retrying in {wait}s... (attempt {attempt+1})")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.HTTPError as e:
                if attempt < max_retries - 1 and resp.status_code in (502, 503, 504):
                    wait = 3 * (attempt + 1)
                    logger.warning(f"7shifts {resp.status_code}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"7shifts API error: {e} - {resp.text[:500]}")
                raise
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
                logger.error(f"7shifts request failed: {e}")
                raise

        raise Exception(f"7shifts API failed after {max_retries} retries: {url}")

    # ------------------------------------------------------------------
    # Identity / Company Info
    # ------------------------------------------------------------------

    def whoami(self):
        """Get current user identity and companies."""
        return self._get("/whoami")

    def get_companies(self):
        """List companies accessible with this token."""
        data = self._get("/companies")
        return data.get("data", [])

    # ------------------------------------------------------------------
    # Hours & Wages Report
    # ------------------------------------------------------------------

    def get_hours_and_wages(self, company_id, start_date, end_date, punches=True):
        """
        Get worked hours & wages report.

        Args:
            company_id: 7shifts company ID
            start_date: YYYY-MM-DD
            end_date: YYYY-MM-DD
            punches: If True, use punch data (actual worked). If False, use scheduled.

        Returns:
            List of user records with shifts, hours, wages, tips.
        """
        data = self._get("/reports/hours_and_wages", params={
            "company_id": company_id,
            "from": start_date,
            "to": end_date,
            "punches": str(punches).lower(),
        })
        return data.get("users", [])

    # ------------------------------------------------------------------
    # Daily Sales & Labor Report
    # ------------------------------------------------------------------

    def get_daily_sales_labor(self, company_id, start_date, end_date):
        """
        Get daily sales and labor report.

        Args:
            company_id: 7shifts company ID
            start_date: YYYY-MM-DD
            end_date: YYYY-MM-DD
        """
        data = self._get("/reports/daily_sales_and_labor", params={
            "company_id": company_id,
            "from": start_date,
            "to": end_date,
        })
        return data.get("data", [])

    # ------------------------------------------------------------------
    # Labor Summary (aggregated from hours_and_wages)
    # ------------------------------------------------------------------

    def get_labor_summary(self, location, start_date, end_date):
        """
        Get aggregated labor summary for a location.

        Returns dict with:
            total_hours, total_pay, total_tips, overtime_hours, overtime_pay,
            salaried_pay, hourly_pay, foh_pay, boh_pay, foh_hours, boh_hours,
            by_role: [{role, hours, pay, tips, count}]
            by_employee: [{name, role, hours, pay, tips, salaried}]
        """
        company_id = COMPANY_IDS.get(location)
        if not company_id:
            logger.error(f"Unknown location: {location}")
            return {}

        users = self.get_hours_and_wages(company_id, start_date, end_date, punches=True)

        # FOH roles (case-insensitive matching)
        FOH_ROLES = {"bartender", "server", "host", "hostess", "barback",
                      "cashier", "busser", "food runner", "bar manager",
                      "front of house", "foh"}
        BOH_ROLES = {"cook", "line cook", "prep cook", "dishwasher", "dish",
                      "kitchen", "chef", "sous chef", "expo", "back of house",
                      "boh", "kitchen manager"}

        totals = {
            "total_hours": 0, "total_pay": 0, "total_tips": 0,
            "overtime_hours": 0, "overtime_pay": 0,
            "salaried_pay": 0, "hourly_pay": 0,
            "foh_pay": 0, "boh_pay": 0, "mgmt_pay": 0,
            "foh_hours": 0, "boh_hours": 0, "mgmt_hours": 0,
        }
        by_role = {}
        by_employee = []

        for user_record in users:
            user = user_record.get("user", {})
            user_name = f"{user.get('first_name', '')} {user.get('last_name', '')[:1]}."
            emp_total_hours = 0
            emp_total_pay = 0
            emp_total_tips = 0
            emp_salaried = False
            emp_roles = set()

            for week in user_record.get("weeks", []):
                is_salaried = week.get("salaried", False)
                if is_salaried:
                    emp_salaried = True

                for shift in week.get("shifts", []):
                    t = shift.get("total", {})
                    hours = t.get("total_hours", 0) or 0
                    pay = t.get("total_pay", 0) or 0
                    tips = t.get("total_tips", 0) or 0
                    ot_hours = t.get("overtime_hours", 0) or 0
                    ot_pay = t.get("overtime_pay", 0) or 0
                    role_label = shift.get("role_label", "Unknown")

                    totals["total_hours"] += hours
                    totals["total_pay"] += pay
                    totals["total_tips"] += tips
                    totals["overtime_hours"] += ot_hours
                    totals["overtime_pay"] += ot_pay

                    emp_total_hours += hours
                    emp_total_pay += pay
                    emp_total_tips += tips
                    emp_roles.add(role_label)

                    if is_salaried:
                        totals["salaried_pay"] += pay
                    else:
                        totals["hourly_pay"] += pay

                    # Categorize FOH/BOH/Mgmt
                    role_lower = role_label.lower()
                    if any(r in role_lower for r in FOH_ROLES):
                        totals["foh_pay"] += pay
                        totals["foh_hours"] += hours
                    elif any(r in role_lower for r in BOH_ROLES):
                        totals["boh_pay"] += pay
                        totals["boh_hours"] += hours
                    else:
                        totals["mgmt_pay"] += pay
                        totals["mgmt_hours"] += hours

                    # By role
                    if role_label not in by_role:
                        by_role[role_label] = {
                            "role": role_label, "hours": 0,
                            "pay": 0, "tips": 0, "shifts": 0,
                        }
                    by_role[role_label]["hours"] += hours
                    by_role[role_label]["pay"] += pay
                    by_role[role_label]["tips"] += tips
                    by_role[role_label]["shifts"] += 1

                # Handle salaried weeks without shifts (salary still accrues)
                week_totals = week.get("total", {})
                if is_salaried and not week.get("shifts"):
                    sal_pay = week_totals.get("total_pay", 0) or 0
                    if sal_pay > 0:
                        totals["total_pay"] += sal_pay
                        totals["salaried_pay"] += sal_pay
                        totals["mgmt_pay"] += sal_pay
                        emp_total_pay += sal_pay

            if emp_total_hours > 0 or emp_total_pay > 0:
                by_employee.append({
                    "name": user_name,
                    "roles": ", ".join(emp_roles) if emp_roles else "Salaried",
                    "hours": round(emp_total_hours, 2),
                    "pay": round(emp_total_pay, 2),
                    "tips": round(emp_total_tips, 2),
                    "salaried": emp_salaried,
                })

        # Sort
        by_employee.sort(key=lambda x: x["pay"], reverse=True)
        role_list = sorted(by_role.values(), key=lambda x: x["pay"], reverse=True)

        # ----------------------------------------------------------
        # Add salaried employers/managers not in hours_and_wages
        # (employer-type users with weekly_salary don't appear in
        #  the report because they don't clock in)
        # ----------------------------------------------------------
        try:
            reported_user_ids = set()
            for u in users:
                uid = u.get("user", {}).get("id")
                if uid:
                    reported_user_ids.add(uid)

            # Get all active users for this company
            all_users_resp = self._get(f"/company/{company_id}/users",
                                        params={"status": "active"})
            all_users = all_users_resp if isinstance(all_users_resp, list) else all_users_resp.get("data", [])

            # Calculate weeks in the date range for pro-rating
            from datetime import datetime as dt
            d_start = dt.strptime(start_date, "%Y-%m-%d")
            d_end = dt.strptime(end_date, "%Y-%m-%d")
            days_in_range = (d_end - d_start).days + 1
            weeks_in_range = days_in_range / 7.0

            seen_salaried = set()  # track by name to avoid double-counting duplicates

            for user in all_users:
                uid = user.get("id")
                if uid in reported_user_ids:
                    continue

                # Check if this user has a weekly salary
                try:
                    wages_resp = self._get(
                        f"/company/{company_id}/users/{uid}/wages")
                    wages_data = wages_resp.get("data", {})
                    current_wages = wages_data.get("current_wages", [])

                    for w in current_wages:
                        if w.get("wage_type") == "weekly_salary":
                            weekly_pay = (w.get("wage_cents", 0) or 0) / 100.0
                            period_pay = round(weekly_pay * weeks_in_range, 2)

                            if period_pay > 0:
                                full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".upper().strip()
                                # Dedup: skip if we already added someone with same last name + salary
                                dedup_key = f"{user.get('last_name', '').upper()}_{weekly_pay}"
                                if dedup_key in seen_salaried:
                                    logger.info(f"  Skipping duplicate salaried: {full_name}")
                                    break
                                seen_salaried.add(dedup_key)

                                name = f"{user.get('first_name', '')} {user.get('last_name', '')[:1]}."
                                totals["total_pay"] += period_pay
                                totals["salaried_pay"] += period_pay
                                totals["mgmt_pay"] += period_pay

                                by_employee.append({
                                    "name": name,
                                    "roles": "Management (Salary)",
                                    "hours": 0,
                                    "pay": period_pay,
                                    "tips": 0,
                                    "salaried": True,
                                })

                                if "Management (Salary)" not in by_role:
                                    by_role["Management (Salary)"] = {
                                        "role": "Management (Salary)",
                                        "hours": 0, "pay": 0,
                                        "tips": 0, "shifts": 0,
                                    }
                                by_role["Management (Salary)"]["pay"] += period_pay

                                logger.info(f"  Added salaried: {name} ${period_pay:,.2f}/period")
                            break
                except Exception as e:
                    logger.debug(f"Could not check wages for user {uid}: {e}")

            # Re-sort after adding salaried
            by_employee.sort(key=lambda x: x["pay"], reverse=True)
            role_list = sorted(by_role.values(), key=lambda x: x["pay"], reverse=True)

        except Exception as e:
            logger.warning(f"Could not check for salaried employers: {e}")

        # Round everything
        for k in totals:
            totals[k] = round(totals[k], 2)

        totals["by_role"] = role_list
        totals["by_employee"] = by_employee
        totals["employee_count"] = len(by_employee)

        return totals


def get_labor_for_report(location, start_date_yyyymmdd, end_date_yyyymmdd):
    """
    Convenience function for email_report.py.
    Accepts dates in YYYYMMDD format (matching Toast convention).

    Returns dict compatible with the email report:
        total_labor_cost, labor_pct (requires revenue), total_hours,
        foh_cost, boh_cost, mgmt_cost, overtime_cost,
        salaried_cost, hourly_cost, by_role, by_employee
    """
    # Convert YYYYMMDD to YYYY-MM-DD
    start = f"{start_date_yyyymmdd[:4]}-{start_date_yyyymmdd[4:6]}-{start_date_yyyymmdd[6:]}"
    end = f"{end_date_yyyymmdd[:4]}-{end_date_yyyymmdd[4:6]}-{end_date_yyyymmdd[6:]}"

    client = SevenShiftsClient()
    data = client.get_labor_summary(location, start, end)

    return {
        "total_labor_cost": data.get("total_pay", 0),
        "total_hours": data.get("total_hours", 0),
        "total_tips": data.get("total_tips", 0),
        "foh_cost": data.get("foh_pay", 0),
        "boh_cost": data.get("boh_pay", 0),
        "mgmt_cost": data.get("mgmt_pay", 0),
        "foh_hours": data.get("foh_hours", 0),
        "boh_hours": data.get("boh_hours", 0),
        "mgmt_hours": data.get("mgmt_hours", 0),
        "overtime_hours": data.get("overtime_hours", 0),
        "overtime_cost": data.get("overtime_pay", 0),
        "salaried_cost": data.get("salaried_pay", 0),
        "hourly_cost": data.get("hourly_pay", 0),
        "employee_count": data.get("employee_count", 0),
        "by_role": data.get("by_role", []),
        "by_employee": data.get("by_employee", []),
    }


# ------------------------------------------------------------------
# CLI Test
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    client = SevenShiftsClient()

    if not client.token:
        print("ERROR: Set SEVENSHIFTS_TOKEN_CHATHAM in your .env file")
        exit(1)

    print("\n" + "=" * 60)
    print("  7shifts API Connection Test")
    print("=" * 60)

    # Test both locations
    today = datetime.now()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    start = last_monday.strftime("%Y-%m-%d")
    end = last_sunday.strftime("%Y-%m-%d")

    print(f"\n  Period: {start} to {end}\n")

    for loc_name, company_id in COMPANY_IDS.items():
        print(f"\n--- {loc_name.upper()} (company {company_id}) ---")
        summary = client.get_labor_summary(loc_name, start, end)

        print(f"  Total Labor:   ${summary['total_pay']:,.2f}")
        print(f"  Total Hours:   {summary['total_hours']:,.1f}")
        print(f"  Total Tips:    ${summary['total_tips']:,.2f}")
        print(f"  Overtime:      {summary['overtime_hours']:.1f} hrs (${summary['overtime_pay']:,.2f})")
        print(f"  Salaried:      ${summary['salaried_pay']:,.2f}")
        print(f"  Hourly:        ${summary['hourly_pay']:,.2f}")
        print(f"  FOH:           ${summary['foh_pay']:,.2f} ({summary['foh_hours']:.1f} hrs)")
        print(f"  BOH:           ${summary['boh_pay']:,.2f} ({summary['boh_hours']:.1f} hrs)")
        print(f"  Mgmt/Other:    ${summary['mgmt_pay']:,.2f} ({summary['mgmt_hours']:.1f} hrs)")
        print(f"  Employees:     {summary['employee_count']}")

        print(f"\n  By Role:")
        for r in summary.get("by_role", []):
            print(f"    {r['role']:20} {r['hours']:6.1f} hrs  ${r['pay']:>8,.2f}  ({r['shifts']} shifts)")

        print(f"\n  Top 5 by Pay:")
        for e in summary.get("by_employee", [])[:5]:
            sal = " [SAL]" if e["salaried"] else ""
            print(f"    {e['name']:20} {e['hours']:6.1f} hrs  ${e['pay']:>8,.2f}  tips: ${e['tips']:>7,.2f}{sal}")

    print("\n" + "=" * 60)
    print("  7shifts Test Complete!")
    print("=" * 60)
