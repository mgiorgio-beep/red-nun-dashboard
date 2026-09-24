"""Payroll mode of the register dedupe (_dedupe_period(payroll_mode=True)).

Mike, 2026-09-24: employees cash paper checks weeks late, so statement check
lines sat beside the uncleared paycheck they paid. Payroll mode pairs on exact
amount, the check clearing on or after its pay date (3 days' slack) and within
60 days, and the OCR'd payee naming the employee when it is readable. More
than one candidate is ambiguous — never a tie-break. Synthetic fixture; always
runnable.
"""
import sqlite3

import pytest

from tests.test_bank_close_cleared_date import SCHEMA

ACCT = 1
PAY = "2026-06-12"


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.execute("INSERT INTO bank_accounts (id, name, location, account_last4) VALUES (1, 'T (5975)', 'chatham', '5975')")
    c.execute("INSERT INTO payroll_runs (id, location, pay_period_start, pay_period_end, pay_date) "
              "VALUES (1, 'chatham', '2026-05-25', '2026-06-07', ?)", (PAY,))
    return c


def _check(c, pid, name, net, num="2100"):
    c.execute("INSERT INTO payroll_checks (id, employee_name, gross_pay, net_pay, pay_period_start, pay_period_end, "
              "check_number, location, payroll_run_id, payment_method, bank_account_id) "
              "VALUES (?, ?, ?, ?, '2026-05-25', '2026-06-07', ?, 'chatham', 1, 'Manual', 1)",
              (pid, name, net * 1.3, net, num))


def _line(c, lid, d, amt, memo="[stmt #1]", ref="2100"):
    c.execute("INSERT INTO manual_bank_entries (id, bank_account_id, entry_date, entry_type, payee, memo, ref_number, "
              "amount, statement_upload_id) VALUES (?, 1, ?, 'other', ?, ?, ?, ?, 1)",
              (lid, d, f"Check {ref}", memo, ref, -amt))


def _run(c, commit=False, payroll_mode=True, tol=5):
    from routes import bank_reconcile_routes as brr
    bank = {"id": ACCT, "account_last4": "5975"}
    return brr._dedupe_period(c, bank, "2026-06-01", "2026-09-30", tol, False, True, commit, "pytest",
                              payroll_mode=payroll_mode)


def _by_line(r):
    return {x["manual_entry_id"]: x for x in r["candidates"]}


def test_a_check_cashed_40_days_late_pairs_and_merges_on_the_statement_date():
    c = _db()
    _check(c, 1, "Chloe Nash", 367.37)
    _line(c, 10, "2026-07-22", 367.37)
    r = _run(c, commit=True)
    assert r["merged_count"] == 1
    pc = c.execute("SELECT cleared, cleared_date FROM payroll_checks WHERE id=1").fetchone()
    assert (pc["cleared"], pc["cleared_date"]) == (1, "2026-07-22")
    audit = c.execute("SELECT * FROM register_merge_audit").fetchone()
    assert audit["match_rule"].startswith("payroll_mode;") and audit["match_tolerance_days"] == 60
    assert c.execute("SELECT COUNT(*) FROM manual_bank_entries").fetchone()[0] == 0


def test_the_default_mode_is_unchanged_and_still_misses_it():
    c = _db()
    _check(c, 1, "Chloe Nash", 367.37)
    _line(c, 10, "2026-07-22", 367.37)
    assert _run(c, payroll_mode=False)["candidates"][0]["match"] is None


@pytest.mark.parametrize("d,pairs", [("2026-06-09", True), ("2026-06-08", False),
                                     ("2026-08-11", True), ("2026-08-12", False)])
def test_window_is_three_days_before_to_sixty_days_after_the_pay_date(d, pairs):
    c = _db()
    _check(c, 1, "Chloe Nash", 100.00)
    _line(c, 10, d, 100.00)
    assert (_run(c)["candidates"][0]["match"] is not None) is pairs


def test_two_same_amount_paychecks_are_ambiguous_not_closest_date():
    c = _db()
    _check(c, 1, "Chloe Nash", 250.00, "2101")
    _check(c, 2, "Miles Yerkes", 250.00, "2102")
    _line(c, 10, "2026-06-15", 250.00)
    x = _run(c, commit=True)["candidates"][0]
    assert x["match"] is None and x["skip_reason"] == "ambiguous" and len(x["options"]) == 2
    assert c.execute("SELECT COUNT(*) FROM register_merge_audit").fetchone()[0] == 0


def test_one_paycheck_claimed_by_two_lines_is_ambiguous_for_both():
    c = _db()
    _check(c, 1, "Chloe Nash", 80.00)
    _line(c, 10, "2026-06-15", 80.00)
    _line(c, 11, "2026-07-01", 80.00)
    got = _by_line(_run(c))
    assert all(got[i]["match"] is None and got[i]["skip_reason"] == "ambiguous" for i in (10, 11))


def test_a_readable_payee_must_name_the_employee():
    c = _db()
    _check(c, 1, "Chloe Nash", 142.77)
    _line(c, 10, "2026-06-20", 142.77, memo="[stmt #1] | CHK: Miles Yerkes")
    x = _run(c)["candidates"][0]
    assert x["match"] is None and x["skip_reason"] == "name_mismatch"


def test_a_readable_payee_naming_the_employee_confirms():
    c = _db()
    _check(c, 1, "Chloe Nash", 142.77)
    _line(c, 10, "2026-06-20", 142.77, memo="[stmt #1] | CHK: Chloe Nash")
    assert _run(c)["candidates"][0]["match"]["name_check"] == "match"


def test_unreadable_ocr_is_no_name_not_a_mismatch():
    c = _db()
    _check(c, 1, "Chloe Nash", 142.77)
    _line(c, 10, "2026-06-20", 142.77, memo="[stmt #1] | CHK: teen and .. 6 6 686")
    m = _run(c)["candidates"][0]["match"]
    assert m is not None and m["name_check"] is None
