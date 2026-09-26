"""Per-vendor GL accounts for invoice lines with no real category (Mike,
2026-09-26). Live database: the P&L must route mapped vendors to their own
accounts, leave food lines alone, and every line must still drill to its total."""
import os

import pytest

from integrations.toast.data_store import DB_PATH, get_connection
from routes.register_routes import vendor_key

pytestmark = pytest.mark.skipif(not os.path.exists(DB_PATH), reason="live database not present")

START, END = "2026-01-01", "2026-08-31"


@pytest.fixture
def conn():
    c = get_connection()
    yield c
    c.close()


def test_vendor_key_matches_the_sql_normalisation(conn):
    from reports.profit_loss import _VENDOR_KEY_SQL
    for name in ("L. Knife & Son, Inc.", "7shifts (US), Corp", "Southern Glazer's Beverage Company",
                 "J. W. Dubis & Sons, Inc.", "Fore & Aft, Inc."):
        sql = conn.execute(f"SELECT {_VENDOR_KEY_SQL} FROM (SELECT ? AS vendor_name) si", (name,)).fetchone()[0]
        assert sql == vendor_key(name), name


@pytest.mark.parametrize("location", ["chatham", "dennis"])
def test_no_catch_all_left_for_mapped_vendors(location):
    from reports.profit_loss import build_profit_loss
    pl = build_profit_loss(location, START, END)
    names = [l["name"] for l in pl["operating_expenses"]["invoiced"]]
    assert "Other Business Expenses" not in names, "a NON_COGS/OTHER vendor has no mapping"
    assert len(names) == len(set(names)), "one line per account"


@pytest.mark.parametrize("location", ["chatham", "dennis"])
def test_every_invoice_line_drills_to_its_total(location, conn):
    from reports.profit_loss import build_profit_loss, drill
    pl = build_profit_loss(location, START, END)
    for src, lines in (("cogs", pl["cogs"]["lines"]), ("opex_invoiced", pl["operating_expenses"]["invoiced"])):
        for l in lines:
            d = drill(conn, location, START, END, src, l["drill"]["key"])
            assert abs(d["total"] - l["amount"]) < 0.01, (src, l["name"], l["amount"], d["total"])


def test_vendor_mapping_never_touches_food_lines(conn):
    """A mapping covers only the no-category buckets; US Foods' FOOD lines
    stay on the category mapping."""
    for r in conn.execute("SELECT category_types FROM gl_vendor_mapping"):
        cats = set(r["category_types"].split(","))
        assert cats <= {"NON_COGS", "OTHER", "TAX"}, cats
