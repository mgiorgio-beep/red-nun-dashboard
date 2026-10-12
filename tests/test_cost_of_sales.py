"""Food cost / bev cost for the Analytics tabs (reports/cost_of_sales.py, 2026-10-12).

Synthetic database — always runnable. Sales come from the sales journal's
"Gross Sales: <category>" lines, cost from confirmed invoice lines on COGS
accounts (the P&L's own query), so the tabs agree with the P&L.
"""
import sqlite3

import pytest

from reports.cost_of_sales import cost_of_sales

SCHEMA = """
CREATE TABLE qb_journal_entries (id INTEGER PRIMARY KEY, location TEXT, entry_date TEXT, entry_type TEXT);
CREATE TABLE qb_journal_line_items (id INTEGER PRIMARY KEY, entry_id INT, journal_name TEXT, debit REAL, credit REAL);
CREATE TABLE scanned_invoices (id INTEGER PRIMARY KEY, location TEXT, vendor_name TEXT, invoice_number TEXT,
                               invoice_date TEXT, status TEXT);
CREATE TABLE scanned_invoice_items (id INTEGER PRIMARY KEY, invoice_id INT, product_name TEXT, quantity REAL,
                                    total_price REAL, category_type TEXT);
CREATE TABLE gl_accounts (id INTEGER PRIMARY KEY, name TEXT, account_type TEXT, active INT, location TEXT);
CREATE TABLE gl_category_mapping (location TEXT, category_type TEXT, gl_account_id INT, confidence TEXT, note TEXT);
CREATE TABLE gl_vendor_mapping (id INTEGER PRIMARY KEY, location TEXT, category_types TEXT, vendor_key TEXT,
                                gl_account_id INT, as_category TEXT, note TEXT);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    acct = 0
    for loc in ("dennis", "chatham"):
        for cat, name, typ in (("FOOD", "Food COGS", "Cost of Goods Sold"), ("BEER", "Beer COGS", "Cost of Goods Sold"),
                               ("LIQUOR", "Liquor COGS", "Cost of Goods Sold"), ("WINE", "Wine COGS", "Cost of Goods Sold"),
                               ("NA_BEVERAGES", "NA Bev COGS", "Cost of Goods Sold"),
                               ("TAKEOUT_SUPPLIES", "TakeOut Supplies", "Cost of Goods Sold"),
                               ("NON_COGS", "Other Business Expenses", "Expense")):
            acct += 1
            c.execute("INSERT INTO gl_accounts VALUES (?,?,?,1,?)", (acct, name, typ, loc))
            c.execute("INSERT INTO gl_category_mapping VALUES (?,?,?,'exact','')", (loc, cat, acct))

    def journal(loc, day, lines):
        eid = c.execute("INSERT INTO qb_journal_entries (location, entry_date, entry_type) VALUES (?,?,'sales_journal')",
                        (loc, day)).lastrowid
        for name, credit, debit in lines:
            c.execute("INSERT INTO qb_journal_line_items (entry_id, journal_name, debit, credit) VALUES (?,?,?,?)",
                      (eid, name, debit, credit))

    journal("dennis", "2026-09-10", [("Gross Sales: Food", 6000, None), ("Gross Sales: Beer", 2000, None),
                                     ("Gross Sales: Liquor", 3000, None), ("Gross Sales: Wine", 1000, None),
                                     ("Gross Sales: NA Beverage", 500, None), ("Summary: Discounts", None, 400),
                                     ("Summary: Tax", 900, None)])
    journal("dennis", "2026-09-11", [("Gross Sales: Food", 4000, None)])
    journal("chatham", "2026-09-10", [("Gross Sales: Food", 5000, None)])
    journal("dennis", "2026-10-01", [("Gross Sales: Food", 99999, None)])   # outside the period

    def invoice(loc, vendor, number, day, lines, status="confirmed"):
        iid = c.execute("INSERT INTO scanned_invoices (location, vendor_name, invoice_number, invoice_date, status) "
                        "VALUES (?,?,?,?,?)", (loc, vendor, number, day, status)).lastrowid
        for cat, amt in lines:
            c.execute("INSERT INTO scanned_invoice_items (invoice_id, product_name, quantity, total_price, category_type) "
                      "VALUES (?,?,1,?,?)", (iid, cat.lower(), amt, cat))

    invoice("dennis", "US Foods", "A1", "2026-09-05", [("FOOD", 2000), ("TAKEOUT_SUPPLIES", 100)])
    invoice("dennis", "US Foods", "A2", "2026-09-12", [("FOOD", 1000), ("NON_COGS", 50)])
    invoice("dennis", "Colonial Wholesale Beverage", "C1", "2026-09-07", [("BEER", 500)])
    invoice("dennis", "Martignetti", "M1", "2026-09-08", [("LIQUOR", 600), ("WINE", 250), ("NA_BEVERAGES", 80)])
    invoice("dennis", "US Foods", "P1", "2026-09-09", [("FOOD", 999)], status="pending")   # held: not a cost yet
    invoice("chatham", "PFG", "F1", "2026-09-06", [("FOOD", 1500)])
    yield c
    c.close()


def test_food_cost_is_food_cost_over_food_sales(conn):
    r = cost_of_sales("dennis", "20260901", "20260930", conn=conn)
    assert r["food"]["sales"] == 10000 and r["food"]["cost"] == 3000 and r["food"]["pct"] == 30.0
    assert r["journal_days"] == {"dennis": 2}
    assert r["food"]["vendors"] == [{"vendor_name": "US Foods", "total_spent": 3000, "invoice_count": 2}]


def test_bev_cost_by_type(conn):
    r = cost_of_sales("dennis", "2026-09-01", "2026-09-30", conn=conn)
    assert r["bev"]["sales"] == 6000 and r["bev"]["cost"] == 1350 and r["bev"]["pct"] == 22.5
    by = {t["type"]: t for t in r["bev"]["by_type"]}
    assert (by["BEER"]["pct"], by["LIQUOR"]["pct"], by["WINE"]["pct"]) == (25.0, 20.0, 25.0)
    assert [v["vendor_name"] for v in r["bev"]["vendors"]] == ["Martignetti", "Colonial Wholesale Beverage"]


def test_other_cogs_listed_not_counted(conn):
    r = cost_of_sales("dennis", "20260901", "20260930", conn=conn)
    assert r["na_bev"] == {"sales": 500, "cost": 80}
    assert r["other_cogs"] == [{"category": "TAKEOUT_SUPPLIES", "cost": 100}]   # NON_COGS is an expense, not here


def test_both_houses_add_up(conn):
    r = cost_of_sales(None, "20260901", "20260930", conn=conn)
    assert r["locations"] == ["chatham", "dennis"]
    assert r["food"]["sales"] == 15000 and r["food"]["cost"] == 4500 and r["food"]["pct"] == 30.0


def test_no_sales_means_no_percentage(conn):
    r = cost_of_sales("chatham", "20260901", "20260930", conn=conn)
    assert r["bev"]["pct"] is None and r["bev"]["sales"] == 0
