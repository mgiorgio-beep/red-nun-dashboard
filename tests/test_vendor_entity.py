"""Single-entity vendors (Mike, 2026-09-24): Fore & Aft and Nickerson are
Chatham's, Barrows is Dennis's. Intake holds a bill filed on the wrong entity;
bank coding gives the owner the expense and the other entity the loan account.
"""
import sqlite3

import pytest

from integrations.invoices.ingest_guard import check_invoice
from integrations.vendors.vendor_entity import bank_account_name, owner_for_text
from tests.test_bank_close_cleared_date import SCHEMA


@pytest.mark.parametrize("text,owner", [
    ("Fore & Aft, Inc.", "chatham"),
    ("SALE FORE & AFT INC. CCD | MICHAEL GIORGIO", "chatham"),
    ("IN *FORE & AFT INC. | 508-4321076 MA", "chatham"),
    ("Fore and Aft, Inc.", "chatham"),
    ("BENJAMIN T NICKERSON I | 508-4302500 MA", "chatham"),
    ("Barrows Waste Systems, LLC", "dennis"),
    ("BARROWS WASTE SYSTEMS | 508-430-1715 MA C# 4708", "dennis"),
    ("Performance Foodservice", None),
    ("FOREST PRODUCTS", None),
])
def test_owner(text, owner):
    hit = owner_for_text(text)
    assert (hit[0] if hit else None) == owner


def test_bank_coding():
    # owner's own bank: the expense
    assert bank_account_name("SALE FORE & AFT INC.", "chatham", -260.0)[0] == "Landscaping"
    assert bank_account_name("BARROWS WASTE SYSTEMS", "dennis", -500.0)[0] == "Trash Removal"
    # the other entity's bank: intercompany, never an expense
    assert bank_account_name("BARROWS WASTE SYSTEMS C# 4708", "chatham", -500.0)[0] == "Loan to Red Nun Dennisport"
    assert bank_account_name("SALE FORE & AFT INC.", "dennis", -325.0)[0] == "Loan to Red Buoy Inc."
    # refunds and other vendors: no rule
    assert bank_account_name("SALE FORE & AFT INC.", "chatham", 50.0) == (None, None)
    assert bank_account_name("PERFORMANCEBOS", "chatham", -100.0) == (None, None)


def _guard_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE scanned_invoices (id INTEGER PRIMARY KEY, vendor_name TEXT, invoice_number TEXT,
                 invoice_date TEXT, location TEXT, total REAL, subtotal REAL, amount_paid REAL, status TEXT,
                 created_at TEXT)""")
    c.execute("CREATE TABLE scanned_invoice_items (invoice_id INTEGER, total_price REAL)")
    return c


@pytest.mark.parametrize("vendor,location,held", [
    ("Fore & Aft, Inc.", "dennis", True),
    ("Fore & Aft, Inc.", "chatham", False),
    ("Barrows Waste Systems, LLC", "chatham", True),
    ("Barrows Waste Systems, LLC", "dennis", False),
    ("Nickerson Trash", "dennis", True),
    ("Performance Foodservice", "dennis", False),
])
def test_intake_holds_wrong_entity(vendor, location, held):
    hits = check_invoice(_guard_db(), {"vendor_name": vendor, "location": location, "invoice_number": "1",
                                       "invoice_date": "2026-09-24", "total": 100.0, "items_sum": 100.0})
    assert any(h["rule"] == 5 for h in hits) is held


def test_import_codes_barrows_on_chatham_as_intercompany():
    from routes.bank_reconcile_routes import resolve_import_gl
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.execute("INSERT INTO gl_accounts (id, name, account_type, location, active) "
              "VALUES (527, 'Loan to Red Nun Dennisport', 'Other Asset', 'chatham', 1)")
    c.execute("INSERT INTO gl_accounts (id, name, account_type, location, active) "
              "VALUES (494, 'Trash Removal', 'Expense', 'chatham', 1)")
    gl = resolve_import_gl(c, {"description": "DBT CRD 1233 05/29/26 507698",
                               "memo": "BARROWS WASTE SYSTEMS | 508-430-1715 MA C# 4708", "date": "2026-05-29"},
                           -500.0, "chatham", "5975", quiet=True)
    assert gl == 527
