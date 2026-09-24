"""FMT Holdings (x1239) transfers — standing rule, Mike 2026-09-24.

Chatham (5975) OUT to 1239 is rent. IN from 1239 reverses rent: coded
Building Rent (a credit) only when a same-amount outflow to 1239 is on the
books the same day or within FMT_REVERSAL_WINDOW_DAYS before; an unpaired
inflow stays uncoded for Mike. Any other 1239 pairing goes to review.
"""
import sqlite3

import pytest

from routes.register_routes import classify_transfer
from tests.test_bank_close_cleared_date import SCHEMA


@pytest.mark.parametrize("desc,amt,last4,name", [
    ("Transfer from x5975 to x1239", -1200.00, "5975", "Building Rent"),
    ("Transfer from x5975 to x1239", -8000.00, "5975", "Building Rent"),   # no hold (Mike: not sending it)
    ("Transfer from x1239 to x5975", 4000.00, "5975", "Building Rent"),
    ("Transfer from x2757 to x1239", -500.00, "2757", None),
    ("Transfer from x1239 to x2757", 500.00, "2757", None),
])
def test_classifier(desc, amt, last4, name):
    got, reason = classify_transfer(desc, amt, last4)
    assert got == name
    if name is None:
        assert reason


def test_intercompany_and_realty_rules_unchanged():
    assert classify_transfer("Transfer from x2757 to x5087", -3000.0, "2757")[0] == "Building Rent"
    assert classify_transfer("Transfer from x2757 to x5975", -2000.0, "2757")[0] is None


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.execute("INSERT INTO bank_accounts (id, name, location, account_last4) VALUES (1, 'T (5975)', 'chatham', '5975')")
    c.execute("INSERT INTO gl_accounts (id, name, account_type, location, active) VALUES (443, 'Building Rent', 'Expense', 'chatham', 1)")
    c.execute("INSERT INTO manual_bank_entries (bank_account_id, entry_date, entry_type, payee, amount) "
              "VALUES (1, '2026-05-22', 'other', 'Transfer from x5975 to x1239', -4000.00)")
    return c


@pytest.mark.parametrize("date,amt,coded", [
    ("2026-05-22", 4000.00, True),    # same day, same amount: reversal
    ("2026-06-01", 4000.00, True),    # within 14 days
    ("2026-06-10", 4000.00, False),   # 19 days later: unpaired
    ("2026-05-22", 3000.00, False),   # no outflow of that amount
])
def test_inflow_is_a_rent_reversal_only_when_it_pairs(date, amt, coded):
    from routes.bank_reconcile_routes import resolve_import_gl
    c = _db()
    gl = resolve_import_gl(c, {"description": "Transfer from x1239 to x5975", "memo": "", "date": date},
                           amt, "chatham", "5975", quiet=True)
    assert (gl == 443) is coded
