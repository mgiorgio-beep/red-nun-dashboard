#!/usr/bin/env python3
"""Load historical 7shifts payroll journals that never went through the dashboard.

Mike, 2026-09-24. Labor moves to the payroll runs (gross less tips plus
employer taxes), so every 2026 pay date needs a complete run. Missing:
Dennis 1/09, 1/23, 2/06, 2/20, 3/06, 4/03, 5/15; incomplete (paper checks
only, no direct deposits, no taxes): Chatham 4/03 (#2) and Dennis 3/20 (#3).

Why not POST /api/payroll/runs: that route assigns check numbers from the
live check_config sequence, bumps it, and renders a printable checks PDF.
These checks were written months ago outside the dashboard. Here:

  * same parser (parse_journal_csv), same run/check columns;
  * check_number stays NULL (the bank's number was never the dashboard's
    before the cutoff — TestCheckNumberIsNotAKey), nothing is printed, the
    sequence is untouched; paper checks are status 'printed' (issued), so
    no print queue picks them up;
  * bank_account_id is set (the dedupe only sees paychecks on an account);
  * a run already on file for the same location and period (#2, #3) is
    completed in place: its paper checks are kept (several are cleared and
    carry audit rows) and matched by employee + net; the missing employees
    are added and the run totals rewritten from the journal.

Input: the folder holding the 7shifts payroll-journal CSVs. The CSV does not
name the entity, so MANIFEST maps each file (as Mike exported them,
2026-09-24) to its entity and the paydays to take from it. A file may carry
several paydays; rows are grouped by Payday and only the listed ones load
(the combined Dennis file also holds 4/17 and 5/01, already on file).

The combined Dennis file carries a 3/31 zero-gross adjustment run: employer
SUTA -553.67 and COVID -282.69 (-836.36), a Q1 employer-tax reduction. It
loads as its own run (negative employer taxes, no pay); the cash came back
as the 7shifts TAX credit, which payroll_tieout.py matches.

    python scripts/payroll_history_load.py --dir <folder>            # dry run
    python scripts/payroll_history_load.py --dir <folder> --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations.toast.data_store import get_connection  # noqa: E402
from routes.payroll_routes import PAYROLL_DIR, parse_journal_csv  # noqa: E402

WHO = "mike-2026-09-24"
BANK = {"chatham": 1, "dennis": 2}
EXPECTED = {("dennis", d) for d in ("2026-01-09", "2026-01-23", "2026-02-06", "2026-02-20", "2026-03-06",
                                    "2026-03-20", "2026-03-31", "2026-04-03", "2026-05-15")} | {("chatham", "2026-04-03")}

# file -> (entity, paydays to load)
MANIFEST = {
    "payroll-journal_2026-01-09_2026-01-09.csv": ("dennis", ["2026-01-09"]),
    "payroll-journal_2026-01-23_2026-05-15.csv": ("dennis", ["2026-01-23", "2026-02-06", "2026-02-20", "2026-03-06",
                                                             "2026-03-20", "2026-03-31", "2026-04-03", "2026-05-15"]),
    "payroll-journal_2026-04-03_2026-04-03 chaham.csv": ("chatham", ["2026-04-03"]),
}


def totals(emps):
    paper = [e for e in emps if e["payment_method"].lower() != "direct deposit" and e["net"] > 0]
    return {"employee_count": len(emps), "check_count": len(paper),
            **{f"total_{k}": round(sum(e[k2] for e in emps), 2) for k, k2 in
               (("gross", "gross"), ("net", "net"), ("wages", "wages"), ("paycheck_tips", "paycheck_tips"),
                ("cash_tips", "cash_tips"), ("ee_taxes", "ee_taxes"), ("er_taxes", "er_taxes"))}}


def insert_check(conn, run_id, loc, e, start, end):
    paper = e["payment_method"].lower() != "direct deposit"
    conn.execute(
        """INSERT INTO payroll_checks (payroll_run_id, employee_name, check_number, gross_pay, net_pay, wages,
               paycheck_tips, cash_tips, ee_taxes, er_taxes, deductions, total_hours, pay_period_start, pay_period_end,
               payment_method, location, bank_account_id, memo, status, created_at, updated_at)
           VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (run_id, e["name"], e["gross"], e["net"], e["wages"], e["paycheck_tips"], e["cash_tips"], e["ee_taxes"],
         e["er_taxes"], json.dumps(e["deductions"]), e["total_hours"], start, end, e["payment_method"], loc,
         BANK[loc], f"{WHO}: historical 7shifts journal; issued outside the dashboard",
         "printed" if paper and e["net"] > 0 else "pending"))


def load(conn, loc, path, emps, now, apply):
    assert emps, f"{path}: no employee rows"
    start, end, pay = emps[0]["pay_period_start"], emps[0]["pay_period_end"], emps[0]["pay_date"]
    assert all((e["pay_period_start"], e["pay_period_end"], e["pay_date"]) == (start, end, pay) for e in emps), path
    assert (loc, pay) in EXPECTED, f"{path}: {loc} pay date {pay} is not one of the missing runs"
    t = totals(emps)
    dest = os.path.join(PAYROLL_DIR, f"journal_{loc}_hist_{pay.replace('-', '')}.csv")
    adjustment = abs(t["total_gross"]) < 0.005
    existing = [] if adjustment else conn.execute(
        "SELECT * FROM payroll_runs WHERE location=? AND pay_period_start=? AND pay_period_end=? "
        "AND total_gross > 1000", (loc, start, end)).fetchall()
    assert len(existing) <= 1, f"{loc} {start}..{end}: {len(existing)} runs on file"
    msg = (f"{loc:<8} pay {pay}  period {start}..{end}  {t['employee_count']:>3} employees  "
           f"gross {t['total_gross']:>10,.2f}  cash {t['total_net'] + t['total_ee_taxes'] + t['total_er_taxes']:>10,.2f}"
           f"{'  ADJUSTMENT: ER ' + format(t['total_er_taxes'], ',.2f') if adjustment else ''}")
    if existing:
        run = existing[0]
        have = [dict(r) for r in conn.execute("SELECT * FROM payroll_checks WHERE payroll_run_id=?", (run["id"],))]
        added = kept = 0
        for e in emps:
            hit = next((h for h in have if h["employee_name"].strip().lower() == e["name"].strip().lower()
                        and abs((h["net_pay"] or 0) - e["net"]) < 0.005), None)
            if hit:
                have.remove(hit)
                kept += 1
                conn.execute("UPDATE payroll_checks SET gross_pay=?, wages=?, paycheck_tips=?, cash_tips=?, ee_taxes=?, "
                             "er_taxes=?, deductions=?, total_hours=?, updated_at=datetime('now') WHERE id=?",
                             (e["gross"], e["wages"], e["paycheck_tips"], e["cash_tips"], e["ee_taxes"], e["er_taxes"],
                              json.dumps(e["deductions"]), e["total_hours"], hit["id"]))
            else:
                insert_check(conn, run["id"], loc, e, start, end)
                added += 1
        assert not have, f"run {run['id']}: paychecks on file not in the journal: {[h['employee_name'] for h in have]}"
        conn.execute(f"UPDATE payroll_runs SET {', '.join(k + '=?' for k in t)}, source_csv_path=?, updated_at=?, "
                     f"memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?",
                     (*t.values(), dest, now, f"{WHO}: completed from the 7shifts journal (was paper checks only)",
                      run["id"]))
        msg += f"  -> run #{run['id']} completed: kept {kept}, added {added}"
    else:
        run_id = conn.execute(
            f"""INSERT INTO payroll_runs (location, pay_period_start, pay_period_end, pay_date, memo, source_csv_path,
                   status, created_at, updated_at, {', '.join(t)})
                VALUES (?,?,?,?,?,?,'complete',datetime('now'),datetime('now'),{','.join('?' * len(t))})""",
            (loc, start, end, pay, (f"Employer-tax adjustment {pay} — {WHO}: historical 7shifts journal" if adjustment
                                    else f"Payroll {start}..{end} — {WHO}: historical 7shifts journal"), dest,
             *t.values())).lastrowid
        for e in emps:
            insert_check(conn, run_id, loc, e, start, end)
        msg += f"  -> new run #{run_id}"
    if apply:
        import csv, io
        src = open(path, encoding="utf-8-sig").read()
        rows = list(csv.reader(io.StringIO(src)))
        keep = [rows[0]] + [r for r in rows[1:] if dict(zip(rows[0], r)).get("Payday", "").strip() == pay]
        with open(dest, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(keep)
    return msg, (loc, pay)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        done = set()
        for fname, (loc, paydays) in MANIFEST.items():
            f = os.path.join(a.dir, fname)
            assert os.path.exists(f), f"missing {f}"
            groups = {}
            for e in parse_journal_csv(open(f, encoding="utf-8-sig").read()):
                groups.setdefault(e["pay_date"], []).append(e)
            skipped = sorted(set(groups) - set(paydays))
            print(f"{fname}: paydays in file {sorted(groups)}; skipping {skipped or 'none'}")
            for pay in paydays:
                assert pay in groups, f"{fname}: payday {pay} not in the file"
                msg, key = load(conn, loc, f, groups[pay], now, a.apply)
                assert key not in done, f"two journals for {key}"
                done.add(key)
                print("   " + msg)
        missing = sorted(EXPECTED - done)
        print("still missing:", missing or "none")
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
