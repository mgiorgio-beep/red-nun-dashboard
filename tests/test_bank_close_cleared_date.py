"""
Bank close by CLEARED DATE — synthetic fixture, no live database.

test_bank_register.py replays the real Cape Cod Five statements and is the
proof that *these books* balance. This file is the proof that the mechanism
is right in the cases that broke on 2026-09-22, using a database built from
scratch so each case is isolated and the expected numbers are worked by hand:

  * a check dated 7/28 that the bank pays 8/03 is OUTSTANDING at 7/31 and
    AUGUST's bank movement, and its cleared_date is the statement line's
    date (8/03), not the book date;
  * a Bill Pay ACH matched to a statement line takes that line's date;
  * an amount-only candidate six months away must NOT pair — neither in the
    import matcher nor in the dedupe;
  * a book row the bank already cleared in July is never re-paired with an
    August line of the same amount (cleared_elsewhere);
  * a zero-net payroll check is not a register row at all;
  * import-all stops at the first statement that breaks continuity;
  * dedupe merges a statement row into the uncleared book row it duplicates,
    stamps the STATEMENT date, and writes a restorable audit row.

Run:  venv/bin/python3 -m pytest tests/test_bank_close_cleared_date.py -v
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cents(x):
    return int(round(float(x or 0) * 100))


# ── Schema: the columns the register / reconcile path actually touches ──────
SCHEMA = """
CREATE TABLE bank_accounts (
    id INTEGER PRIMARY KEY, name TEXT, short_name TEXT, qbo_account_id TEXT,
    qbo_account_name TEXT, location TEXT, account_last4 TEXT,
    opening_balance REAL DEFAULT 0, opening_date TEXT, active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE bank_statement_uploads (
    id INTEGER PRIMARY KEY, bank_account_id INTEGER, filename TEXT, file_path TEXT,
    uploaded_by TEXT, uploaded_at TEXT, period_start TEXT, period_end TEXT,
    beginning_balance REAL, ending_balance REAL, total_debits REAL, total_credits REAL,
    transaction_count INTEGER DEFAULT 0, imported_count INTEGER DEFAULT 0,
    parsed_json TEXT, warnings_json TEXT);
CREATE TABLE manual_bank_entries (
    id INTEGER PRIMARY KEY, bank_account_id INTEGER NOT NULL, entry_date TEXT NOT NULL,
    entry_type TEXT NOT NULL, payee TEXT, memo TEXT, ref_number TEXT, amount REAL NOT NULL,
    cleared INTEGER DEFAULT 0, cleared_date TEXT, created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, statement_upload_id INTEGER,
    gl_account_id INTEGER, gl_source TEXT, gl_status TEXT, reconciliation_id INTEGER);
CREATE TABLE vendor_payments (
    id INTEGER PRIMARY KEY, vendor TEXT NOT NULL, location TEXT, payment_date TEXT NOT NULL,
    payment_ref TEXT, payment_method TEXT, payment_total REAL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, check_number TEXT, memo TEXT,
    status TEXT DEFAULT 'cleared', source TEXT, updated_at TEXT, ap_payment_id INTEGER,
    bank_account_id INTEGER, cleared INTEGER DEFAULT 0, cleared_date TEXT,
    gl_account_id INTEGER, error_detail TEXT, gl_source TEXT, gl_status TEXT,
    reconciliation_id INTEGER);
CREATE TABLE payroll_runs (id INTEGER PRIMARY KEY, location TEXT, pay_period_start TEXT,
    pay_period_end TEXT, pay_date TEXT);
CREATE TABLE payroll_checks (
    id INTEGER PRIMARY KEY, employee_name TEXT NOT NULL, gross_pay REAL, net_pay REAL,
    pay_period_start TEXT, pay_period_end TEXT, check_number TEXT, location TEXT,
    memo TEXT, voided INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
    payroll_run_id INTEGER, payment_method TEXT DEFAULT 'Manual', bank_account_id INTEGER,
    cleared INTEGER DEFAULT 0, cleared_date TEXT, gl_account_id INTEGER, gl_source TEXT,
    gl_status TEXT, reconciliation_id INTEGER);
CREATE TABLE bank_deposits (
    id INTEGER PRIMARY KEY, bank_account_id INTEGER NOT NULL, deposit_date TEXT NOT NULL,
    amount REAL NOT NULL, description TEXT, memo TEXT, source TEXT, qbo_txn_id TEXT,
    qbo_txn_type TEXT, cleared INTEGER DEFAULT 1, cleared_date TEXT, synced_at TEXT,
    gl_account_id INTEGER, gl_source TEXT, gl_status TEXT, reconciliation_id INTEGER);
CREATE TABLE gl_accounts (id INTEGER PRIMARY KEY, qbo_id TEXT, acct_num TEXT, name TEXT,
    account_type TEXT, account_subtype TEXT, location TEXT, active INTEGER DEFAULT 1);
CREATE TABLE gl_account_rules (id INTEGER PRIMARY KEY, location TEXT, pattern TEXT,
    gl_account_id INTEGER, created_by TEXT, created_at TEXT, UNIQUE(location, pattern));
CREATE TABLE register_merge_audit (
    id INTEGER PRIMARY KEY, merged_at TEXT DEFAULT CURRENT_TIMESTAMP, merged_by TEXT,
    bank_account_id INTEGER, target_source TEXT, target_id INTEGER, target_label TEXT,
    target_cleared_date TEXT, deleted_entry_id INTEGER, deleted_entry_date TEXT,
    deleted_entry_amount REAL, deleted_entry_json TEXT, match_amount REAL,
    match_date_diff_days INTEGER, match_tolerance_days INTEGER, match_rule TEXT,
    reversed_at TEXT);
CREATE TABLE bank_reconciliations (
    id INTEGER PRIMARY KEY, bank_account_id INTEGER NOT NULL, statement_upload_id INTEGER,
    period_start TEXT NOT NULL, period_end TEXT NOT NULL, status TEXT DEFAULT 'open',
    beginning_balance REAL, ending_balance REAL, bank_balance REAL, book_balance REAL,
    outstanding_net REAL, delta REAL, closed_by TEXT, closed_at TEXT, notes TEXT);
CREATE TABLE bank_reconciliation_items (
    id INTEGER PRIMARY KEY, reconciliation_id INTEGER NOT NULL, source TEXT NOT NULL,
    source_id INTEGER NOT NULL, entry_date TEXT, payee TEXT, memo TEXT, amount REAL,
    age_days INTEGER, carried_from_item_id INTEGER, carry_count INTEGER DEFAULT 0);
"""

ACCT = 1


def _tx(d, desc, debit=0.0, credit=0.0, ref=None, tx_type="other", memo=None):
    return {"date": d, "description": desc, "debit": debit, "credit": credit,
            "ref": ref, "tx_type": tx_type, "memo": memo}


# Worked by hand. July: 1000 + 500 - 120 - 55 = 1325.
JULY = {
    "period_start": "2026-07-01", "period_end": "2026-07-31",
    "beginning_balance": 1000.00, "ending_balance": 1325.00,
    "transactions": [
        _tx("2026-07-10", "DEP TOAST", credit=500.00, tx_type="deposit"),
        _tx("2026-07-15", "SALE FORE & AFT INC.", debit=120.00),
        _tx("2026-07-15", "MEBillPay Elsewhere Inc", debit=55.00),
    ],
}
# August: 1325 - 100 - 250 - 300 - 77.77 + 900 - 55 = 1442.23.
AUGUST = {
    "period_start": "2026-08-01", "period_end": "2026-08-31",
    "beginning_balance": 1325.00, "ending_balance": 1442.23,
    "transactions": [
        _tx("2026-08-03", "Check 1001", debit=100.00, ref="1001", tx_type="check"),
        _tx("2026-08-11", "MEBillPay Acme Supply", debit=250.00),
        _tx("2026-08-12", "Check 2001", debit=300.00, ref="2001", tx_type="check"),
        _tx("2026-08-20", "PAYMENT WIDGETS CO", debit=77.77),
        _tx("2026-08-25", "DEP TOAST", credit=900.00, tx_type="deposit"),
        _tx("2026-08-28", "MEBillPay Elsewhere Inc", debit=55.00),
    ],
}
# September: WRONG beginning balance — continuity must stop here.
SEPTEMBER = {
    "period_start": "2026-09-01", "period_end": "2026-09-30",
    "beginning_balance": 9999.00, "ending_balance": 9999.00,
    "transactions": [],
}


def _seed(conn):
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO bank_accounts (id, name, location, account_last4, opening_balance, opening_date) "
        "VALUES (?, 'Test Bank (5975)', 'chatham', '5975', 1000.00, '2026-02-01')", (ACCT,))
    for parsed in (JULY, AUGUST, SEPTEMBER):
        conn.execute(
            "INSERT INTO bank_statement_uploads (bank_account_id, period_start, period_end, "
            "beginning_balance, ending_balance, transaction_count, imported_count, parsed_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (ACCT, parsed["period_start"], parsed["period_end"], parsed["beginning_balance"],
             parsed["ending_balance"], len(parsed["transactions"]), json.dumps(parsed)))
    aug_id = conn.execute(
        "SELECT id FROM bank_statement_uploads WHERE period_start = '2026-08-01'").fetchone()[0]

    # Payroll: the 7/28 check the bank pays 8/03, and a zero-net stub.
    conn.execute("INSERT INTO payroll_runs (id, location, pay_period_start, pay_period_end, pay_date) "
                 "VALUES (1, 'chatham', '2026-07-13', '2026-07-26', '2026-07-28')")
    conn.execute("INSERT INTO payroll_runs (id, location, pay_period_start, pay_period_end, pay_date) "
                 "VALUES (2, 'chatham', '2026-07-27', '2026-08-02', '2026-08-07')")
    conn.execute("INSERT INTO payroll_checks (id, employee_name, gross_pay, net_pay, pay_period_start, "
                 "pay_period_end, check_number, location, payroll_run_id, payment_method, bank_account_id) "
                 "VALUES (1, 'Pat Example', 130, 100.00, '2026-07-13', '2026-07-26', '1001', 'chatham', 1, 'Manual', ?)", (ACCT,))
    conn.execute("INSERT INTO payroll_checks (id, employee_name, gross_pay, net_pay, pay_period_start, "
                 "pay_period_end, check_number, location, payroll_run_id, payment_method, bank_account_id) "
                 "VALUES (2, 'Zero Net', 80, 0.00, '2026-07-27', '2026-08-02', '1002', 'chatham', 2, 'Manual', ?)", (ACCT,))

    # Bill Pay rows.
    vps = [
        (1, "Acme Supply", "2026-08-10", "ach", 250.00, "pending", None),      # matched 8/11
        (2, "Widgets Co", "2026-02-15", "ach", 77.77, "pending", None),       # 6 months from the 8/20 line
        (3, "Elsewhere Inc", "2026-07-14", "ach", 55.00, "pending", None),    # cleared by July's line
        (4, "Foo Linen", "2026-08-10", "check", 300.00, "printed", "2001"),   # dedupe target
    ]
    for (i, vendor, d, method, amt, status, chk) in vps:
        conn.execute("INSERT INTO vendor_payments (id, vendor, location, payment_date, payment_ref, "
                     "payment_method, payment_total, status, check_number, bank_account_id) "
                     "VALUES (?, ?, 'chatham', ?, ?, ?, ?, ?, ?, ?)",
                     (i, vendor, d, f"REF-{i}", method, amt, status, chk, ACCT))

    # A statement row an earlier partial import already created for the 8/12
    # check — the dedupe's job is to fold it into vendor_payment 4.
    conn.execute("INSERT INTO manual_bank_entries (bank_account_id, entry_date, entry_type, payee, memo, "
                 "ref_number, amount, cleared, cleared_date, created_by, statement_upload_id) "
                 "VALUES (?, '2026-08-12', 'other', 'Check 2001', '[stmt #%d]', '2001', -300.00, 1, "
                 "'2026-08-12', 'pytest', ?)" % aug_id, (ACCT, aug_id))
    conn.commit()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("bankclose") / "fixture.db"
    conn = sqlite3.connect(str(p))
    _seed(conn)
    conn.close()
    return str(p)


@pytest.fixture(scope="module")
def modules(db_path):
    """Point every module's get_connection at the fixture DB. Module-scoped
    monkeypatching by hand so the client fixture can share it."""
    import routes.register_routes as rr
    import routes.bank_reconcile_routes as brr

    def get_connection():
        c = sqlite3.connect(db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    saved = (rr.get_connection, brr.get_connection, brr._ocr_checks, brr._post_import_audit)
    rr.get_connection = get_connection
    brr.get_connection = get_connection
    # OCR needs a PDF on disk and the audit needs the invoice tables; both are
    # wrapped in try/except in production and are covered by the live suite.
    brr._ocr_checks = lambda conn, upload_id: {"ok": True, "banner": "stubbed"}
    brr._post_import_audit = lambda conn, loc, upload_id: {"ok": True, "checks": []}
    yield rr, brr, get_connection
    rr.get_connection, brr.get_connection, brr._ocr_checks, brr._post_import_audit = saved


@pytest.fixture(scope="module")
def client(modules):
    from flask import Flask
    rr, brr, _ = modules
    app = Flask(__name__)
    app.secret_key = "pytest"
    app.config["TESTING"] = True
    app.register_blueprint(rr.register_bp)
    app.register_blueprint(brr.bank_reconcile_bp)
    with app.test_client() as cl:
        with cl.session_transaction() as s:
            s["user_id"] = 1
            s["role"] = "admin"
            s["username"] = "pytest"
        yield cl


@pytest.fixture(scope="module")
def imported(client, modules):
    """Run import-all once; every test below reads the result."""
    r = client.post("/api/bank-reconcile/import-all", json={"account_id": ACCT})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _state(modules, period_start):
    _, brr, get_connection = modules
    conn = get_connection()
    try:
        up = conn.execute("SELECT * FROM bank_statement_uploads WHERE period_start = ?",
                          (period_start,)).fetchone()
        return brr._reconciliation_state(conn, up)
    finally:
        conn.close()


def _row(modules, sql, *args):
    _, _, get_connection = modules
    conn = get_connection()
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


class TestImportAll:
    def test_imports_in_order_and_stops_at_the_continuity_break(self, imported):
        actions = [(p["period"], p["action"]) for p in imported["periods"]]
        assert actions == [
            ("2026-07-01..2026-07-31", "imported"),
            ("2026-08-01..2026-08-31", "imported"),
            ("2026-09-01..2026-09-30", "stopped"),
        ]
        assert imported["status"] == "stopped"
        assert "continuity" in imported["periods"][2]["reason"]
        assert "9999" in imported["periods"][2]["reason"]

    def test_the_stopped_period_was_not_touched(self, imported, modules):
        up = _row(modules, "SELECT imported_count FROM bank_statement_uploads WHERE period_start = '2026-09-01'")
        assert up["imported_count"] == 0

    def test_per_period_counts(self, imported):
        july, aug = imported["periods"][0], imported["periods"][1]
        # July: deposit + Fore & Aft inserted; Elsewhere matched vp 3.
        assert (july["inserted"], july["cleared"]) == (2, 1)
        # August: 8/03 -> payroll 1, 8/11 -> vp 1, 8/12 -> the existing
        # statement row; 8/20, 8/25 and 8/28 inserted.
        assert (aug["inserted"], aug["cleared"]) == (3, 3)
        assert july["ties"] and aug["ties"]

    def test_a_second_run_imports_nothing(self, imported, client):
        r = client.post("/api/bank-reconcile/import-all", json={"account_id": ACCT}).get_json()
        assert [p["action"] for p in r["periods"]][:2] == ["already_imported", "already_imported"]
        assert r["imported_periods"] == 0


class TestTieOutByClearedDate:
    def test_july_ties_with_the_check_outstanding(self, imported, modules):
        st = _state(modules, "2026-07-01")
        assert st["ties"], st["delta"]
        assert cents(st["bank_balance"]) == 132500
        # Outstanding at 7/31: the 7/28 check (paid 8/03) and February's
        # Widgets payment that never cleared — carried in, and still open.
        items = {(i["source"], i["source_id"]): i for i in st["outstanding_items"]}
        assert set(items) == {("payroll", 1), ("bill_pay", 2)}
        assert items[("payroll", 1)]["cleared_date"] == "2026-08-03"
        assert items[("bill_pay", 2)]["carried"] is True
        assert cents(st["outstanding_net"]) == -17777
        assert cents(st["book_balance"]) == 132500 - 17777
        assert st["identity_holds"]

    def test_august_ties_and_the_check_is_augusts_money(self, imported, modules):
        st = _state(modules, "2026-08-01")
        assert st["ties"], st["delta"]
        assert cents(st["bank_balance"]) == 144223
        # A book-date tie-out would be off by the 7/28 check (-100) and the
        # 7/14 Elsewhere row (-55 in July, cleared there): make sure the
        # cleared-date figure is the one reported.
        assert st["book_balance_by_date"] is not None
        assert st["opening_drift"] == 0.0

    def test_register_opening_equals_statement_beginning(self, imported, modules):
        for p in ("2026-07-01", "2026-08-01"):
            assert _state(modules, p)["opening_drift"] == 0.0

    def test_register_bank_balance_equals_statement_ending(self, imported, client):
        for (s, e, end) in (("2026-07-01", "2026-07-31", 132500),
                            ("2026-08-01", "2026-08-31", 144223)):
            j = client.get(f"/api/register/{ACCT}?start={s}&end={e}").get_json()
            assert cents(j["summary"]["bank_balance"]) == end
            assert (cents(j["summary"]["book_balance"]) - cents(j["summary"]["bank_balance"])
                    == cents(j["summary"]["outstanding_net"]))


class TestClearedDates:
    def test_check_takes_the_statement_date_not_the_book_date(self, imported, modules):
        r = _row(modules, "SELECT cleared, cleared_date FROM payroll_checks WHERE id = 1")
        assert (r["cleared"], r["cleared_date"]) == (1, "2026-08-03")

    def test_bill_pay_ach_takes_the_statement_line_date(self, imported, modules):
        r = _row(modules, "SELECT cleared, cleared_date FROM vendor_payments WHERE id = 1")
        assert (r["cleared"], r["cleared_date"]) == (1, "2026-08-11")

    def test_july_cleared_row_is_not_repaired_with_an_august_line(self, imported, modules):
        r = _row(modules, "SELECT cleared_date FROM vendor_payments WHERE id = 3")
        assert r["cleared_date"] == "2026-07-15"
        # The 8/28 line of the same amount became its own statement row.
        m = _row(modules, "SELECT entry_date, amount FROM manual_bank_entries "
                          "WHERE payee = 'MEBillPay Elsewhere Inc' AND entry_date = '2026-08-28'")
        assert m and cents(m["amount"]) == -5500

    def test_six_months_apart_is_not_a_pair(self, imported, modules):
        r = _row(modules, "SELECT cleared, cleared_date FROM vendor_payments WHERE id = 2")
        assert (r["cleared"], r["cleared_date"]) == (0, None)
        m = _row(modules, "SELECT id FROM manual_bank_entries WHERE payee = 'PAYMENT WIDGETS CO'")
        assert m, "the 8/20 line must have been imported as its own row"

    def test_mark_cleared_keeps_the_first_date_unless_forced(self, imported, modules):
        _, brr, get_connection = modules
        conn = get_connection()
        try:
            brr._mark_cleared(conn, "bill_pay", 1, "2026-08-30")
            assert conn.execute("SELECT cleared_date FROM vendor_payments WHERE id = 1").fetchone()[0] == "2026-08-11"
            brr._mark_cleared(conn, "bill_pay", 1, "2026-08-30", force=True)
            assert conn.execute("SELECT cleared_date FROM vendor_payments WHERE id = 1").fetchone()[0] == "2026-08-30"
            brr._mark_cleared(conn, "bill_pay", 1, "2026-08-11", force=True)
        finally:
            conn.rollback()
            conn.close()


class TestZeroNetPayroll:
    def test_zero_net_check_is_not_a_register_row(self, imported, client, modules):
        j = client.get(f"/api/register/{ACCT}?start=2026-08-01&end=2026-08-31").get_json()
        assert not [r for r in j["rows"] if r["source"] == "payroll" and r["source_id"] == 2]
        st = _state(modules, "2026-08-01")
        assert ("payroll", 2) not in {(i["source"], i["source_id"]) for i in st["outstanding_items"]}


class TestDedupe:
    def test_amount_only_six_months_apart_is_not_merged(self, imported, client):
        r = client.post("/api/bank-reconcile/dedupe", json={
            "account_id": ACCT, "all_periods": True, "date_tolerance_days": 7, "commit": False,
        }).get_json()
        widgets = [c for c in r["candidates"] if c["manual_entry_payee"] == "PAYMENT WIDGETS CO"]
        assert widgets and widgets[0]["match"] is None

    def test_all_periods_merges_the_statement_row_on_the_statement_date(self, imported, client, modules):
        before = _state(modules, "2026-08-01")
        r = client.post("/api/bank-reconcile/dedupe", json={
            "account_id": ACCT, "all_periods": True, "date_tolerance_days": 7, "commit": True,
        }).get_json()
        assert r["applied"] and r["merged_count"] == 1
        assert [p["period"] for p in r["periods"]] == ["2026-07-01..2026-07-31",
                                                       "2026-08-01..2026-08-31",
                                                       "2026-09-01..2026-09-30"]
        vp = _row(modules, "SELECT cleared, cleared_date FROM vendor_payments WHERE id = 4")
        assert (vp["cleared"], vp["cleared_date"]) == (1, "2026-08-12")   # statement date, not 8/10
        assert _row(modules, "SELECT id FROM manual_bank_entries WHERE payee = 'Check 2001'") is None
        audit = _row(modules, "SELECT * FROM register_merge_audit WHERE target_id = 4")
        assert audit["match_rule"] and "cleared_date=statement_date" in audit["match_rule"]
        assert json.loads(audit["deleted_entry_json"])["amount"] == -300.0
        # The bank side did not move: one -300 on 8/12 replaced another.
        after = _state(modules, "2026-08-01")
        assert after["ties"] and cents(after["bank_balance"]) == cents(before["bank_balance"])
        assert ("bill_pay", 4) not in {(i["source"], i["source_id"]) for i in after["outstanding_items"]}


class TestCloseLocksByClearedDate:
    def test_close_stamps_rows_cleared_in_the_period(self, imported, client, modules):
        up = _row(modules, "SELECT id FROM bank_statement_uploads WHERE period_start = '2026-08-01'")
        r = client.post("/api/bank-reconcile/reconciliation/close", json={"upload_id": up["id"]})
        assert r.status_code == 200, r.get_data(as_text=True)
        # The 7/28 check cleared 8/03 is locked by AUGUST's sign-off …
        assert _row(modules, "SELECT reconciliation_id FROM payroll_checks WHERE id = 1")["reconciliation_id"]
        # … and the never-cleared February payment is not locked by anyone.
        assert _row(modules, "SELECT reconciliation_id FROM vendor_payments WHERE id = 2")["reconciliation_id"] is None
        items = client.get(f"/api/bank-reconcile/reconciliations?account_id={ACCT}").get_json()
        aug = [x for x in items["reconciliations"] if x["period_start"] == "2026-08-01"][0]
        assert {(i["source"], i["source_id"]) for i in aug["items"]} == {("bill_pay", 2)}
        assert cents(sum(i["amount"] for i in aug["items"])) == cents(aug["outstanding_net"])
