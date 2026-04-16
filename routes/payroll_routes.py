"""
Payroll routes — 7shifts CSV upload, run management, QBO journal entry, check printing.
Blueprint: payroll_bp at /api/payroll/*
"""

import csv
import io
import os
import json
import logging
import zipfile
import tempfile
from datetime import datetime, date

from flask import Blueprint, jsonify, request, send_file
from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required, admin_required, admin_or_accountant_required

logger = logging.getLogger(__name__)
payroll_bp = Blueprint("payroll_bp", __name__)

PAYROLL_DIR = "/opt/red-nun-dashboard/payroll_runs"

# ── QBO GL Accounts ────────────────────────────────────────────────────────────
QB_WAGES         = "Payroll Expenses:Wages"
QB_PAYROLL_TIPS  = "Payroll Expenses:Payroll Tips"
QB_PAYROLL_TAXES = "Payroll Expenses:Payroll Taxes"
QB_TIP_BANK      = "Tip Bank"
QB_BANK_CHATHAM  = "Cape Cod Five (5975)"
QB_BANK_DENNIS   = "Cape Cod Five (5975)"   # update if different account

# ── 7shifts payroll-journal CSV columns ───────────────────────────────────────
EE_TAX_COLS = [
    "Social Security Tax (EE)",
    "Federal Income Tax (EE)",
    "Medicare (EE)",
    "Additional Medicare (EE)",
    "Connecticut State Tax (EE)",
    "Massachusetts Paid Family and Medical Leave - Employee (EE)",
    "Massachusetts State Tax (EE)",
    "New Jersey State Tax (EE)",
]
ER_TAX_COLS = [
    "Employer Social Security Tax (ER)",
    "Federal Unemployment Tax (ER)",
    "Employer Medicare Tax (ER)",
    "Massachusetts Employer Medical Assistance Contributions (ER)",
    "Massachusetts Paid Family and Medical Leave - Employer (ER)",
    "Massachusetts State Unemployment Tax (ER)",
    "Massachusetts Workforce Training Fund (ER)",
    "Massachusetts COVID-19 Recovery Assessment (ER)",
]
WAGE_COLS = [
    "Hourly (Regular) Amt",
    "Salaried Amt",
    "Paid Sick Time Amt",
    "Tip Credit Adjustment Amt",
]


# ── DB init ────────────────────────────────────────────────────────────────────

def init_payroll_tables():
    """Create payroll_runs table and extend payroll_checks with new columns."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payroll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            pay_period_start TEXT,
            pay_period_end TEXT,
            pay_date TEXT,
            memo TEXT,
            employee_count INTEGER DEFAULT 0,
            check_count INTEGER DEFAULT 0,
            total_gross REAL DEFAULT 0,
            total_net REAL DEFAULT 0,
            total_wages REAL DEFAULT 0,
            total_paycheck_tips REAL DEFAULT 0,
            total_cash_tips REAL DEFAULT 0,
            total_ee_taxes REAL DEFAULT 0,
            total_er_taxes REAL DEFAULT 0,
            source_csv_path TEXT,
            source_pdf_path TEXT,
            checks_pdf_path TEXT,
            qbo_csv_path TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Add updated_at if upgrading from earlier version
    try:
        conn.execute("ALTER TABLE payroll_runs ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))")
        conn.commit()
    except Exception:
        pass
    for col, defn in [
        ("payroll_run_id",  "INTEGER"),
        ("paycheck_tips",   "REAL DEFAULT 0"),
        ("cash_tips",       "REAL DEFAULT 0"),
        ("wages",           "REAL DEFAULT 0"),
        ("ee_taxes",        "REAL DEFAULT 0"),
        ("er_taxes",        "REAL DEFAULT 0"),
        ("payment_method",  "TEXT DEFAULT 'Manual'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE payroll_checks ADD COLUMN {col} {defn}")
        except Exception:
            pass
    conn.commit()
    conn.close()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _flt(val):
    try:
        return float(val) if val and str(val).strip() else 0.0
    except (ValueError, TypeError):
        return 0.0


def _fmt_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d/%y")
    except Exception:
        return d or ""


def parse_journal_csv(csv_text):
    """
    Parse a 7shifts payroll-journal CSV.
    Returns list of employee dicts. Skips the totals row (blank Last Name + First Name).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    employees = []
    for row in reader:
        last  = (row.get("Last Name") or "").strip()
        first = (row.get("First Name") or "").strip()
        if not last and not first:
            continue  # totals row

        name = f"{first} {last}".strip() if first else last

        wages    = sum(_flt(row.get(c)) for c in WAGE_COLS)
        ee_taxes = sum(_flt(row.get(c)) for c in EE_TAX_COLS)
        er_taxes = sum(_flt(row.get(c)) for c in ER_TAX_COLS)

        deductions = {}
        for col in EE_TAX_COLS:
            v = _flt(row.get(col))
            if v:
                deductions[col] = v

        employees.append({
            "name":             name,
            "gross":            _flt(row.get("Gross Total")),
            "net":              _flt(row.get("Net Pay")),
            "payment_method":   (row.get("Payment Method") or "Manual").strip(),
            "wages":            wages,
            "paycheck_tips":    _flt(row.get("Paycheck Tips Amt")),
            "cash_tips":        _flt(row.get("Cash Tips Amt")),
            "ee_taxes":         ee_taxes,
            "er_taxes":         er_taxes,
            "total_hours":      _flt(row.get("Hourly (Regular) Hrs")),
            "pay_period_start": (row.get("Period Start") or "").strip(),
            "pay_period_end":   (row.get("Period End") or "").strip(),
            "pay_date":         (row.get("Payday") or "").strip(),
            "deductions":       deductions,
        })
    return employees


def build_qbo_csv(employees, pay_date_str, period_str, location):
    """
    Build QBO-importable journal entry CSV text.
    Returns (csv_text, balanced, total_debits, total_credits).
    """
    try:
        dt = datetime.strptime(pay_date_str, "%Y-%m-%d")
    except ValueError:
        dt = datetime.now()

    journal_no   = dt.strftime("%m%d%Y")
    journal_date = dt.strftime("%m/%d/%Y")
    description  = f"Pay period {period_str}"
    bank_acct    = QB_BANK_CHATHAM if "chatham" in location else QB_BANK_DENNIS

    total_wages        = sum(e["wages"]         for e in employees)
    total_paycheck_tips = sum(e["paycheck_tips"] for e in employees)
    total_cash_tips    = sum(e["cash_tips"]      for e in employees)
    total_tips         = total_paycheck_tips + total_cash_tips
    total_ee_taxes     = sum(e["ee_taxes"]       for e in employees)
    total_er_taxes     = sum(e["er_taxes"]       for e in employees)

    paper_checks = [e for e in employees
                    if e["payment_method"].lower() != "direct deposit" and e["net"] > 0]
    dd_employees = [e for e in employees
                    if e["payment_method"].lower() == "direct deposit"]
    dd_net  = sum(e["net"] for e in dd_employees)
    dd_ach  = dd_net + total_ee_taxes + total_er_taxes

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

    if total_wages:
        row(QB_WAGES,         debit=total_wages)
    if total_tips:
        row(QB_PAYROLL_TIPS,  debit=total_tips)
    if total_er_taxes:
        row(QB_PAYROLL_TAXES, debit=total_er_taxes)

    for e in paper_checks:
        row(bank_acct, credit=e["net"], name=e["name"])

    if dd_ach > 0:
        row(bank_acct, credit=dd_ach, desc="DD + Taxes")

    if total_cash_tips > 0:
        row(QB_TIP_BANK, credit=total_cash_tips, desc="Cash Tips Paid")

    total_d = total_wages + total_tips + total_er_taxes
    total_c = sum(e["net"] for e in paper_checks) + dd_ach + total_cash_tips
    balanced = abs(total_d - total_c) < 0.02

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[
        "JournalNo", "JournalDate", "AccountName", "Debits", "Credits", "Description", "Name"
    ])
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue(), balanced, total_d, total_c


# ─────────────────────────────────────────────
#  CREATE RUN — upload 7shifts journal CSV
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/runs", methods=["POST"])
@admin_required
def create_payroll_run():
    """
    Upload a 7shifts payroll-journal CSV + optional source checks PDF.
    Creates a payroll run, inserts per-employee check records, assigns
    sequential check numbers from check_config, generates QBO CSV and
    printable check PDF batch.
    """
    # check_printer lives in scripts/archive — add it to sys.path if needed
    import sys as _sys, os as _os
    _cp_dir = _os.path.join(_os.path.dirname(__file__), "..", "scripts", "archive")
    if _cp_dir not in _sys.path:
        _sys.path.insert(0, _os.path.abspath(_cp_dir))
    try:
        from check_printer import generate_batch_payroll_checks_pdf as _gen_pdf
        _check_printer_available = True
    except ImportError:
        _gen_pdf = None
        _check_printer_available = False
        logger.warning("check_printer not available — checks PDF will be skipped")

    location      = (request.form.get("location") or "chatham").lower()
    memo          = (request.form.get("memo") or "").strip()
    assign_checks = request.form.get("assign_checks", "1") == "1"

    journal_file = request.files.get("journal_csv")
    if not journal_file:
        return jsonify({"error": "journal_csv file required"}), 400

    try:
        csv_text  = journal_file.read().decode("utf-8-sig")
        employees = parse_journal_csv(csv_text)
    except Exception as e:
        return jsonify({"error": f"CSV parse error: {e}"}), 400

    if not employees:
        return jsonify({"error": "No employee rows found in CSV"}), 400

    pay_period_start = employees[0]["pay_period_start"]
    pay_period_end   = employees[0]["pay_period_end"]
    pay_date         = employees[0]["pay_date"] or request.form.get("pay_date") or date.today().isoformat()

    period_str = f"{_fmt_date(pay_period_start)}-{_fmt_date(pay_period_end)}"
    if not memo:
        memo = f"Payroll {period_str}"

    os.makedirs(PAYROLL_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")

    csv_path = os.path.join(PAYROLL_DIR, f"journal_{location}_{ts}.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(csv_text)

    source_pdf_path = None
    source_pdf = request.files.get("checks_pdf")
    if source_pdf:
        source_pdf_path = os.path.join(PAYROLL_DIR, f"source_{location}_{ts}.pdf")
        source_pdf.save(source_pdf_path)

    total_gross        = sum(e["gross"]         for e in employees)
    total_net          = sum(e["net"]            for e in employees)
    total_wages        = sum(e["wages"]          for e in employees)
    total_paycheck_tips = sum(e["paycheck_tips"] for e in employees)
    total_cash_tips    = sum(e["cash_tips"]      for e in employees)
    total_ee_taxes     = sum(e["ee_taxes"]       for e in employees)
    total_er_taxes     = sum(e["er_taxes"]       for e in employees)
    paper_emps = [e for e in employees
                  if e["payment_method"].lower() != "direct deposit" and e["net"] > 0]
    check_count = len(paper_emps)

    conn = get_connection()

    cur = conn.execute("""
        INSERT INTO payroll_runs
        (location, pay_period_start, pay_period_end, pay_date, memo,
         employee_count, check_count, total_gross, total_net,
         total_wages, total_paycheck_tips, total_cash_tips,
         total_ee_taxes, total_er_taxes, source_csv_path, source_pdf_path,
         status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',datetime('now'))
    """, (location, pay_period_start, pay_period_end, pay_date, memo,
          len(employees), check_count, total_gross, total_net,
          total_wages, total_paycheck_tips, total_cash_tips,
          total_ee_taxes, total_er_taxes, csv_path, source_pdf_path))
    run_id = cur.lastrowid

    config = conn.execute("SELECT * FROM check_config WHERE location = ?", (location,)).fetchone()
    if not config:
        config = conn.execute("SELECT * FROM check_config ORDER BY id LIMIT 1").fetchone()
    if not config:
        conn.close()
        return jsonify({"error": "Check config not set up for this location"}), 400

    config_dict = dict(config)
    next_check  = config_dict.get("check_number_next") or 2001

    inserted_checks = []
    payroll_list    = []

    for emp in employees:
        is_paper  = emp["payment_method"].lower() != "direct deposit" and emp["net"] > 0
        # Only assign a check number if assign_checks is True and employee gets a paper check
        check_num = str(next_check) if (is_paper and assign_checks) else None
        if is_paper and assign_checks:
            next_check += 1

        cur2 = conn.execute("""
            INSERT INTO payroll_checks
            (payroll_run_id, employee_name, check_number,
             gross_pay, net_pay, wages, paycheck_tips, cash_tips,
             ee_taxes, er_taxes, deductions, total_hours,
             pay_period_start, pay_period_end, payment_method,
             location, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',datetime('now'),datetime('now'))
        """, (run_id, emp["name"], check_num,
              emp["gross"], emp["net"], emp["wages"],
              emp["paycheck_tips"], emp["cash_tips"],
              emp["ee_taxes"], emp["er_taxes"],
              json.dumps(emp["deductions"]), emp["total_hours"],
              pay_period_start, pay_period_end,
              emp["payment_method"], location))
        check_id = cur2.lastrowid

        if is_paper:
            payroll_list.append({
                "payroll": {
                    "id":               check_id,
                    "employee_name":    emp["name"],
                    "gross_pay":        emp["gross"],
                    "net_pay":          emp["net"],
                    "deductions":       emp["deductions"],
                    "total_hours":      emp["total_hours"],
                    "pay_period_start": pay_period_start,
                    "pay_period_end":   pay_period_end,
                    "printed_at":       pay_date,
                    "location":         location,
                },
                "check_number": check_num,
            })
            inserted_checks.append({
                "id":           check_id,
                "employee":     emp["name"],
                "check_number": check_num,
                "net":          emp["net"],
            })

    if assign_checks:
        conn.execute("UPDATE check_config SET check_number_next = ? WHERE location = ?",
                     (next_check, location))

    qbo_text, balanced, total_d, total_c = build_qbo_csv(employees, pay_date, period_str, location)
    qbo_path = os.path.join(PAYROLL_DIR, f"qbo_{location}_{ts}.csv")
    with open(qbo_path, "w", encoding="utf-8") as f:
        f.write(qbo_text)

    checks_pdf_path = None
    if payroll_list and assign_checks and _check_printer_available:
        checks_pdf_path = os.path.join(PAYROLL_DIR, f"checks_{location}_{ts}.pdf")
        try:
            _gen_pdf(payroll_list, config_dict, checks_pdf_path)
            for ch in inserted_checks:
                conn.execute(
                    "UPDATE payroll_checks SET status='printed', printed_at=datetime('now') WHERE id=?",
                    (ch["id"],)
                )
        except Exception as ex:
            logger.error(f"Check PDF generation failed: {ex}")
            checks_pdf_path = None

    conn.execute("""
        UPDATE payroll_runs SET qbo_csv_path=?, checks_pdf_path=?, status='complete' WHERE id=?
    """, (qbo_path, checks_pdf_path, run_id))
    conn.commit()
    conn.close()

    return jsonify({
        "status":         "ok",
        "run_id":         run_id,
        "employee_count": len(employees),
        "check_count":    check_count,
        "total_gross":    total_gross,
        "total_net":      total_net,
        "balanced":       balanced,
        "total_debits":   round(total_d, 2),
        "total_credits":  round(total_c, 2),
        "checks_pdf":     f"/api/payroll/runs/{run_id}/checks-pdf" if checks_pdf_path else None,
        "qbo_csv":        f"/api/payroll/runs/{run_id}/qbo-csv",
    })


# ─────────────────────────────────────────────
#  LIST RUNS
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/runs")
@login_required
def list_payroll_runs():
    location = request.args.get("location")
    year     = request.args.get("year")
    conn     = get_connection()
    where, params = [], []
    if location:
        where.append("location = ?")
        params.append(location)
    if year:
        where.append("substr(pay_date,1,4) = ?")
        params.append(year)
    sql = "SELECT * FROM payroll_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pay_date DESC, created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────
#  RUN DETAIL
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/runs/<int:run_id>")
@login_required
def get_payroll_run(run_id):
    conn   = get_connection()
    run    = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    checks = conn.execute(
        "SELECT * FROM payroll_checks WHERE payroll_run_id=? ORDER BY employee_name",
        (run_id,)
    ).fetchall()
    conn.close()
    return jsonify({"run": dict(run), "checks": [dict(c) for c in checks]})


# ─────────────────────────────────────────────
#  DOWNLOAD ENDPOINTS
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/runs/<int:run_id>/qbo-csv")
@login_required
def download_qbo_csv(run_id):
    conn = get_connection()
    run  = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not run or not run["qbo_csv_path"] or not os.path.exists(run["qbo_csv_path"]):
        return jsonify({"error": "QBO CSV not available"}), 404
    fname = f"QBO_Payroll_{run['location']}_{run['pay_date']}.csv"
    return send_file(run["qbo_csv_path"], as_attachment=True, download_name=fname)


@payroll_bp.route("/api/payroll/runs/<int:run_id>/checks-pdf")
@login_required
def download_checks_pdf(run_id):
    conn = get_connection()
    run  = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not run or not run["checks_pdf_path"] or not os.path.exists(run["checks_pdf_path"]):
        return jsonify({"error": "Checks PDF not available"}), 404
    fname = f"Payroll_Checks_{run['location']}_{run['pay_date']}.pdf"
    return send_file(run["checks_pdf_path"], as_attachment=True, download_name=fname)


@payroll_bp.route("/api/payroll/runs/<int:run_id>/summary-csv")
@login_required
def download_summary_csv(run_id):
    conn = get_connection()
    run  = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    checks = conn.execute(
        "SELECT * FROM payroll_checks WHERE payroll_run_id=? ORDER BY employee_name",
        (run_id,)
    ).fetchall()
    conn.close()

    out = io.StringIO()
    w   = csv.DictWriter(out, fieldnames=[
        "Employee", "Payment Method", "Hours", "Wages",
        "Paycheck Tips", "Cash Tips", "Gross Pay",
        "EE Taxes", "ER Taxes", "Net Pay", "Check #"
    ])
    w.writeheader()
    for c in checks:
        w.writerow({
            "Employee":       c["employee_name"],
            "Payment Method": c["payment_method"] or "Manual",
            "Hours":          f"{c['total_hours'] or 0:.2f}",
            "Wages":          f"{c['wages'] or 0:.2f}",
            "Paycheck Tips":  f"{c['paycheck_tips'] or 0:.2f}",
            "Cash Tips":      f"{c['cash_tips'] or 0:.2f}",
            "Gross Pay":      f"{c['gross_pay']:.2f}",
            "EE Taxes":       f"{c['ee_taxes'] or 0:.2f}",
            "ER Taxes":       f"{c['er_taxes'] or 0:.2f}",
            "Net Pay":        f"{c['net_pay']:.2f}",
            "Check #":        c["check_number"] or "DD",
        })
    fname = f"Payroll_Summary_{run['location']}_{run['pay_date']}.csv"
    return send_file(
        io.BytesIO(out.getvalue().encode()),
        as_attachment=True, download_name=fname, mimetype="text/csv"
    )


@payroll_bp.route("/api/payroll/runs/<int:run_id>/ytd-csv")
@login_required
def download_ytd_csv(run_id):
    conn = get_connection()
    run  = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    year = (run["pay_date"] or "")[:4] or str(date.today().year)
    rows = conn.execute("""
        SELECT pc.employee_name,
               pc.gross_pay, pc.wages, pc.paycheck_tips, pc.cash_tips,
               pc.ee_taxes,  pc.er_taxes,  pc.net_pay
        FROM payroll_checks pc
        JOIN payroll_runs pr ON pc.payroll_run_id = pr.id
        WHERE pr.location = ? AND substr(pr.pay_date,1,4) = ?
          AND pr.status = 'complete' AND (pc.voided IS NULL OR pc.voided = 0)
        ORDER BY pc.employee_name, pr.pay_date
    """, (run["location"], year)).fetchall()
    conn.close()

    ytd = {}
    for c in rows:
        n = c["employee_name"]
        if n not in ytd:
            ytd[n] = dict(gross=0, wages=0, paycheck_tips=0, cash_tips=0,
                          ee_taxes=0, er_taxes=0, net=0)
        ytd[n]["gross"]        += c["gross_pay"]       or 0
        ytd[n]["wages"]        += c["wages"]           or 0
        ytd[n]["paycheck_tips"] += c["paycheck_tips"]  or 0
        ytd[n]["cash_tips"]    += c["cash_tips"]       or 0
        ytd[n]["ee_taxes"]     += c["ee_taxes"]        or 0
        ytd[n]["er_taxes"]     += c["er_taxes"]        or 0
        ytd[n]["net"]          += c["net_pay"]         or 0

    out = io.StringIO()
    w   = csv.DictWriter(out, fieldnames=[
        "Employee", "YTD Gross", "YTD Wages",
        "YTD Paycheck Tips", "YTD Cash Tips",
        "YTD EE Taxes", "YTD ER Taxes", "YTD Net"
    ])
    w.writeheader()
    for name, d in sorted(ytd.items()):
        w.writerow({
            "Employee":         name,
            "YTD Gross":        f"{d['gross']:.2f}",
            "YTD Wages":        f"{d['wages']:.2f}",
            "YTD Paycheck Tips": f"{d['paycheck_tips']:.2f}",
            "YTD Cash Tips":    f"{d['cash_tips']:.2f}",
            "YTD EE Taxes":     f"{d['ee_taxes']:.2f}",
            "YTD ER Taxes":     f"{d['er_taxes']:.2f}",
            "YTD Net":          f"{d['net']:.2f}",
        })
    fname = f"Payroll_YTD_{run['location']}_{year}.csv"
    return send_file(
        io.BytesIO(out.getvalue().encode()),
        as_attachment=True, download_name=fname, mimetype="text/csv"
    )


@payroll_bp.route("/api/payroll/runs/<int:run_id>/zip")
@login_required
def download_run_zip(run_id):
    conn = get_connection()
    run  = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not run:
        return jsonify({"error": "Not found"}), 404
    run = dict(run)

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w") as zf:
        for path, arcname in [
            (run.get("qbo_csv_path"),    f"QBO_Import_{run['location']}_{run['pay_date']}.csv"),
            (run.get("checks_pdf_path"), f"Checks_{run['location']}_{run['pay_date']}.pdf"),
            (run.get("source_pdf_path"), f"7shifts_Checks_{run['location']}_{run['pay_date']}.pdf"),
            (run.get("source_csv_path"), f"7shifts_Journal_{run['location']}_{run['pay_date']}.csv"),
        ]:
            if path and os.path.exists(path):
                zf.write(path, arcname)

    fname = f"Payroll_{run['location']}_{run['pay_date']}.zip"
    return send_file(tmp.name, as_attachment=True, download_name=fname,
                     mimetype="application/zip")


# ─────────────────────────────────────────────
#  REPORTS API (for reports page)
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/report/runs")
@admin_or_accountant_required
def report_payroll_runs():
    """List all runs for the reports page with optional year/location filter."""
    location = request.args.get("location")
    year     = request.args.get("year")
    conn     = get_connection()
    where, params = ["status='complete'"], []
    if location:
        where.append("location = ?")
        params.append(location)
    if year:
        where.append("substr(pay_date,1,4) = ?")
        params.append(year)
    sql  = "SELECT * FROM payroll_runs WHERE " + " AND ".join(where)
    sql += " ORDER BY pay_date DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@payroll_bp.route("/api/payroll/report/ytd")
@admin_or_accountant_required
def report_payroll_ytd():
    """YTD totals per employee across all completed runs for a location/year."""
    location = request.args.get("location", "chatham")
    year     = request.args.get("year", str(date.today().year))
    conn     = get_connection()
    rows = conn.execute("""
        SELECT pc.employee_name,
               COUNT(DISTINCT pc.payroll_run_id) as payroll_count,
               SUM(pc.gross_pay)       as ytd_gross,
               SUM(pc.wages)           as ytd_wages,
               SUM(pc.paycheck_tips)   as ytd_paycheck_tips,
               SUM(pc.cash_tips)       as ytd_cash_tips,
               SUM(pc.ee_taxes)        as ytd_ee_taxes,
               SUM(pc.er_taxes)        as ytd_er_taxes,
               SUM(pc.net_pay)         as ytd_net
        FROM payroll_checks pc
        JOIN payroll_runs pr ON pc.payroll_run_id = pr.id
        WHERE pr.location = ? AND substr(pr.pay_date,1,4) = ?
          AND pr.status = 'complete' AND (pc.voided IS NULL OR pc.voided = 0)
        GROUP BY pc.employee_name
        ORDER BY pc.employee_name
    """, (location, year)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────
#  CONVERT LEGACY CHECKS → PAYROLL RUNS
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/import-legacy", methods=["POST"])
@admin_required
def import_legacy_checks():
    """
    Group existing payroll_checks (payroll_run_id IS NULL) by location +
    pay_period_start + pay_period_end, create a payroll_run for each group,
    and link the checks to it. Preserves all existing check numbers.
    """
    conn = get_connection()

    orphans = conn.execute("""
        SELECT * FROM payroll_checks
        WHERE (payroll_run_id IS NULL OR payroll_run_id = '')
          AND (voided IS NULL OR voided = 0)
        ORDER BY location, pay_period_start, pay_period_end
    """).fetchall()

    if not orphans:
        conn.close()
        return jsonify({"status": "ok", "runs_created": 0, "message": "No legacy checks found"})

    # Group by (location, pay_period_start, pay_period_end)
    groups = {}
    for c in orphans:
        key = (
            c["location"] or "chatham",
            c["pay_period_start"] or "",
            c["pay_period_end"] or "",
        )
        groups.setdefault(key, []).append(dict(c))

    runs_created = 0
    for (location, period_start, period_end), checks in groups.items():
        # Derive pay_date: use printed_at of first check, or pay_period_end
        pay_date = None
        for c in checks:
            raw = c.get("printed_at") or c.get("created_at") or ""
            if raw:
                pay_date = raw.split("T")[0].split(" ")[0]
                break
        if not pay_date:
            pay_date = period_end or date.today().isoformat()

        paper_checks = [c for c in checks
                        if (c.get("payment_method") or "Manual").lower() != "direct deposit"
                        and (c.get("net_pay") or 0) > 0
                        and c.get("check_number")]

        total_gross = sum(c.get("gross_pay") or 0 for c in checks)
        total_net   = sum(c.get("net_pay")   or 0 for c in checks)
        total_wages = sum(c.get("wages")      or 0 for c in checks)
        total_pt    = sum(c.get("paycheck_tips") or 0 for c in checks)
        total_ct    = sum(c.get("cash_tips")     or 0 for c in checks)
        total_ee    = sum(c.get("ee_taxes")      or 0 for c in checks)
        total_er    = sum(c.get("er_taxes")      or 0 for c in checks)

        def fmt(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d/%y")
            except Exception:
                return d or ""

        period_str = f"{fmt(period_start)}-{fmt(period_end)}"
        memo = f"Payroll {period_str}"

        cur = conn.execute("""
            INSERT INTO payroll_runs
            (location, pay_period_start, pay_period_end, pay_date, memo,
             employee_count, check_count, total_gross, total_net,
             total_wages, total_paycheck_tips, total_cash_tips,
             total_ee_taxes, total_er_taxes, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'complete',datetime('now'))
        """, (location, period_start, period_end, pay_date, memo,
              len(checks), len(paper_checks),
              total_gross, total_net, total_wages, total_pt, total_ct,
              total_ee, total_er))
        run_id = cur.lastrowid

        for c in checks:
            conn.execute(
                "UPDATE payroll_checks SET payroll_run_id = ? WHERE id = ?",
                (run_id, c["id"])
            )

        runs_created += 1

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "runs_created": runs_created})


# ─────────────────────────────────────────────
#  BACKFILL RUN FROM CSV
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/runs/<int:run_id>/backfill", methods=["POST"])
@admin_required
def backfill_run_from_csv(run_id):
    """
    Upload a 7shifts payroll-journal CSV for an existing run.
    Matches employees by name (case-insensitive), fills in wages / tips /
    EE taxes / ER taxes / deductions on each payroll_check, updates run
    totals, and regenerates the QBO journal entry CSV.
    Check numbers and net/gross pay are NOT overwritten.
    """
    conn = get_connection()
    run = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        return jsonify({"error": "Run not found"}), 404
    run = dict(run)

    journal_file = request.files.get("journal_csv")
    if not journal_file:
        conn.close()
        return jsonify({"error": "journal_csv file required"}), 400

    try:
        csv_text  = journal_file.read().decode("utf-8-sig")
        employees = parse_journal_csv(csv_text)
    except Exception as e:
        conn.close()
        return jsonify({"error": f"CSV parse error: {e}"}), 400

    # Build lookup: normalised name → employee dict
    def norm(name):
        return " ".join(name.lower().split())

    emp_map = {norm(e["name"]): e for e in employees}

    checks = conn.execute(
        "SELECT * FROM payroll_checks WHERE payroll_run_id=?", (run_id,)
    ).fetchall()

    matched = 0
    unmatched = []
    for c in checks:
        key = norm(c["employee_name"] or "")
        emp = emp_map.get(key)
        if not emp:
            # Try last-name-only match as fallback
            last = key.split()[-1] if key else ""
            emp = next((v for k, v in emp_map.items() if k.split()[-1] == last), None)
        if not emp:
            unmatched.append(c["employee_name"])
            continue

        conn.execute("""
            UPDATE payroll_checks SET
                wages           = ?,
                paycheck_tips   = ?,
                cash_tips       = ?,
                ee_taxes        = ?,
                er_taxes        = ?,
                deductions      = ?,
                total_hours     = ?,
                payment_method  = ?,
                updated_at      = datetime('now')
            WHERE id = ?
        """, (emp["wages"], emp["paycheck_tips"], emp["cash_tips"],
              emp["ee_taxes"], emp["er_taxes"],
              json.dumps(emp["deductions"]), emp["total_hours"],
              emp["payment_method"], c["id"]))
        matched += 1

    # Recompute run totals from CSV
    total_wages = sum(e["wages"]         for e in employees)
    total_pt    = sum(e["paycheck_tips"] for e in employees)
    total_ct    = sum(e["cash_tips"]     for e in employees)
    total_ee    = sum(e["ee_taxes"]      for e in employees)
    total_er    = sum(e["er_taxes"]      for e in employees)

    conn.execute("""
        UPDATE payroll_runs SET
            total_wages=?, total_paycheck_tips=?, total_cash_tips=?,
            total_ee_taxes=?, total_er_taxes=?, updated_at=datetime('now')
        WHERE id=?
    """, (total_wages, total_pt, total_ct, total_ee, total_er, run_id))

    # Regen QBO CSV
    pay_date   = run["pay_date"] or date.today().isoformat()
    def fmt(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d/%y")
        except Exception:
            return d or ""
    period_str = f"{fmt(run['pay_period_start'])}-{fmt(run['pay_period_end'])}"

    qbo_text, balanced, total_d, total_c = build_qbo_csv(
        employees, pay_date, period_str, run["location"]
    )

    os.makedirs(PAYROLL_DIR, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d%H%M%S")
    qbo_path = os.path.join(PAYROLL_DIR, f"qbo_{run['location']}_{ts}.csv")
    with open(qbo_path, "w", encoding="utf-8") as f:
        f.write(qbo_text)

    conn.execute("UPDATE payroll_runs SET qbo_csv_path=? WHERE id=?", (qbo_path, run_id))
    conn.commit()
    conn.close()

    return jsonify({
        "status":    "ok",
        "matched":   matched,
        "unmatched": unmatched,
        "balanced":  balanced,
        "total_debits":  round(total_d, 2),
        "total_credits": round(total_c, 2),
        "qbo_csv":   f"/api/payroll/runs/{run_id}/qbo-csv",
    })
