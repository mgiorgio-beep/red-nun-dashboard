#!/usr/bin/env python3
"""Per-pay-date payroll tie-out: the acceptance test for labor-from-runs.

Mike, 2026-09-24. For every pay date Jan–Aug 2026, per entity:

    run cash   = direct-deposit net + employee taxes + employer taxes + paper-check net
    bank       = the 7shifts impound(s) (PCR 7shifts, pay date -7..0)  +  paper checks
    residual   = run cash - bank   (must be 0.00: the run and the bank agree)

Payroll Liabilities (116) takes the run's cash as a credit and each bank
settlement as a debit, so after a pay date its balance is exactly the paper
checks not yet cashed; it reaches 0.00 as they clear. `outstanding` shows
that balance today. Tips never touch 116: paycheck tips relieve Tip Bank in
the run's accrual, cash tips were paid out of Tip Bank already (7SHIFTS TI /
Kickfin), and those bank lines stay on Tip Bank.

A pay date with no complete run, an impound that does not tie, or a nonzero
residual is listed as a FAILURE. Exit status 1 if any.

    python scripts/payroll_tieout.py [--through 2026-08-31]
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations.toast.data_store import get_connection  # noqa: E402


def tieout(conn, through):
    rows, failures = [], []
    for loc in ("chatham", "dennis"):
        impounds = [dict(r) for r in conn.execute(
            """SELECT m.id, m.entry_date, -m.amount AS amt FROM manual_bank_entries m
               JOIN bank_accounts b ON b.id = m.bank_account_id
               WHERE b.location = ? AND m.payee LIKE 'PCR 7shifts%' AND m.entry_date <= ? ORDER BY m.entry_date""",
            (loc, through))]
        # 7shifts money coming BACK: tax-adjustment refunds (TAX 7shifts) and
        # returned direct deposits (PAYROLL 7shifts, RTP ... from 7shifts).
        # Tip reloads (7shifts ti) are Tip Bank's, not payroll's.
        credits = [dict(r) for r in conn.execute(
            """SELECT m.id, m.entry_date, m.amount AS amt, m.payee FROM manual_bank_entries m
               JOIN bank_accounts b ON b.id = m.bank_account_id
               WHERE b.location = ? AND UPPER(m.payee) LIKE '%7SHIFTS%' AND m.amount > 0
                 AND UPPER(m.payee) NOT LIKE '%7SHIFTS TI%' AND m.entry_date <= ? ORDER BY m.entry_date""",
            (loc, through))]
        runs = [dict(r) for r in conn.execute(
            "SELECT * FROM payroll_runs WHERE location = ? AND pay_date <= ? ORDER BY pay_date, id", (loc, through))]
        used = set()
        # Match run by run, largest first, so a supplemental run whose taxes
        # are drawn a few days later (Chatham #13 on 5/04, Dennis #20 on 6/29)
        # finds its own impound instead of stealing the main run's.
        for r in sorted(runs, key=lambda r: -(r["total_gross"] or 0)):
            q = lambda sql: conn.execute(sql, (r["id"],)).fetchone()[0] or 0.0  # noqa: E731
            dd = q("SELECT SUM(net_pay) FROM payroll_checks WHERE payroll_run_id = ? "
                   "AND payment_method = 'Direct Deposit' AND COALESCE(voided,0) = 0")
            paper = q("SELECT SUM(net_pay) FROM payroll_checks WHERE payroll_run_id = ? "
                      "AND payment_method <> 'Direct Deposit' AND COALESCE(voided,0) = 0")
            out_ = q("SELECT SUM(net_pay) FROM payroll_checks WHERE payroll_run_id = ? "
                     "AND payment_method <> 'Direct Deposit' AND COALESCE(voided,0) = 0 AND COALESCE(cleared,0) = 0")
            want = round(dd + r["total_ee_taxes"] + r["total_er_taxes"], 2)
            run_cash = round(want + paper, 2)
            pay = r["pay_date"]
            if want < -0.005:
                # An adjustment run hands cash back: match the 7shifts credit
                # within 30 days. Cents 7shifts rounds differently are a
                # reported variance, never absorbed silently.
                hit = [c_ for c_ in credits if c_["id"] not in used and pay <= c_["entry_date"] <= _minus(pay, -30)
                       and abs(c_["amt"] + want) < 0.10][:1]
                back = round(sum(c_["amt"] for c_ in hit), 2)
                used.update(c_["id"] for c_ in hit)
                var = round(back + want, 2) if hit else None
                row = {"loc": loc, "pay": pay, "runs": [r["id"]], "run_cash": run_cash, "impound": -back,
                       "paper": round(paper, 2), "outstanding": round(out_, 2),
                       "residual": 0.0 if hit else run_cash, "ok": bool(hit),
                       "note": (f"adjustment refund: bank credit line {hit[0]['id']} {hit[0]['entry_date']} "
                                f"+{back:,.2f}; variance {var:+.2f} vs the journal" if hit
                                else "adjustment refund not found on the bank")}
                rows.append(row)
                if not hit:
                    failures.append(row)
                continue
            hit = [i for i in impounds if i["id"] not in used and _minus(pay, 7) <= i["entry_date"] <= _minus(pay, -5)
                   and abs(i["amt"] - want) < 0.01][:1]
            impound = round(sum(i["amt"] for i in hit), 2)
            used.update(i["id"] for i in hit)
            residual = round(run_cash - impound - paper, 2)
            ok = abs(residual) < 0.01
            row = {"loc": loc, "pay": pay, "runs": [r["id"]], "run_cash": run_cash, "impound": impound,
                   "paper": round(paper, 2), "outstanding": round(out_, 2), "residual": residual, "ok": ok,
                   "note": "" if ok else ("impound missing or does not tie" if want > 0.005 else "")}
            rows.append(row)
            if not ok:
                failures.append(row)
        for c_ in credits:
            if c_["id"] not in used and c_["entry_date"] >= "2026-01-01":
                tax = c_["payee"].upper().startswith("TAX ")
                row = {"loc": loc, "pay": c_["entry_date"], "runs": [], "run_cash": 0.0,
                       "impound": -round(c_["amt"], 2), "paper": 0.0, "outstanding": 0.0,
                       "residual": round(c_["amt"], 2) if tax else 0.0, "ok": not tax,
                       "note": (f"7shifts TAX credit line {c_['id']} +{c_['amt']:,.2f} has no adjustment run "
                                f"— its journal is missing" if tax else
                                f"7shifts credit line {c_['id']} ({c_['payee'].strip()}) — returned pay, "
                                f"settles through 116")}
                rows.append(row)
                if tax:
                    failures.append(row)
        for i in impounds:
            if i["id"] not in used and i["entry_date"] >= "2026-01-01":
                f = {"loc": loc, "pay": i["entry_date"], "runs": [], "run_cash": 0.0, "impound": round(i["amt"], 2),
                     "paper": 0.0, "outstanding": 0.0, "residual": round(-i["amt"], 2), "ok": False,
                     "note": f"impound line {i['id']} has no run"}
                rows.append(f)
                failures.append(f)
    return rows, failures


def _minus(iso, days):
    from datetime import date, timedelta
    return (date.fromisoformat(iso) - timedelta(days=days)).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--through", default="2026-08-31")
    a = ap.parse_args()
    conn = get_connection()
    rows, failures = tieout(conn, a.through)
    print(f"{'entity':<8} {'pay date':<10} {'runs':<10} {'run cash':>11} {'impound':>11} {'paper':>10} "
          f"{'residual':>9} {'116 today':>10}")
    for r in sorted(rows, key=lambda r: (r["loc"], r["pay"])):
        if r["pay"] < "2026-01-01":
            continue
        print(f"{r['loc']:<8} {r['pay']:<10} {','.join(map(str, r['runs'])) or '-':<10} {r['run_cash']:>11,.2f} "
              f"{r['impound']:>11,.2f} {r['paper']:>10,.2f} {r['residual']:>9,.2f} {r['outstanding']:>10,.2f}"
              f"{('   ' + r['note']) if r['ok'] and r.get('note') else ''}"
              f"{'' if r['ok'] else '   FAIL ' + r.get('note', '')}")
    bad = [f for f in failures if f["pay"] >= "2026-01-01"]
    print(f"\n{len(bad)} failure(s)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
