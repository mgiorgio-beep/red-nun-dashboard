"""One vendor screen API (routes/vendor_directory_routes.py, 2026-10-15). Synthetic DB."""
import sqlite3

import pytest
from flask import Flask

import integrations.toast.data_store as ds
from routes.vendor_directory_routes import vendor_directory_bp


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE vendors (id INTEGER PRIMARY KEY, name TEXT UNIQUE, category TEXT, contact_name TEXT,
            email TEXT, phone TEXT, address TEXT, payment_terms TEXT, account_number TEXT, notes TEXT,
            active INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT);
        CREATE TABLE vendor_bill_pay (id INTEGER PRIMARY KEY, vendor_name TEXT, bill_pay_enabled INT,
            portal_pay_enabled INT, auto_pay INT, remit_address_1 TEXT, remit_city TEXT, remit_state TEXT,
            payment_term_type TEXT);
        CREATE TABLE scanned_invoices (id INTEGER PRIMARY KEY, vendor_name TEXT, status TEXT, invoice_date TEXT,
            total REAL, balance REAL, payment_status TEXT);
        INSERT INTO vendors (name, category, phone) VALUES ('US Foods', 'FOOD', '508-555-1000');
        INSERT INTO vendor_bill_pay (vendor_name, bill_pay_enabled, remit_city, payment_term_type)
            VALUES ('TIGER EXCHANGE INC.', 1, 'Boston', 'net_days');
        INSERT INTO scanned_invoices (vendor_name, status, invoice_date, total, balance, payment_status) VALUES
            ('US Foods', 'confirmed', '2026-10-01', 100, 0, 'paid'),
            ('US Foods, Inc.', 'confirmed', '2026-10-08', 300, 300, NULL),
            ('Tiger Exchange Inc.', 'confirmed', '2026-10-01', 50, 50, NULL),
            ('Tiger Exchange Inc.', 'confirmed', '2026-10-02', 40, 40, NULL),
            ('TIGER EXCHANGE INC.', 'confirmed', '2026-09-20', 30, 0, 'paid'),
            ('Pending Co', 'pending', '2026-10-03', 10, 10, NULL);
    """)
    c.commit()
    c.close()
    monkeypatch.setattr(ds, "DB_PATH", str(db))
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(vendor_directory_bp)
    cl = app.test_client()
    cl.db = db
    return cl


def login(cl, role):
    with cl.session_transaction() as s:
        s["user_id"] = 1
        s["role"] = role


def by_name(resp):
    return {v["name"]: v for v in resp.get_json()["vendors"]}


def test_needs_login(client):
    assert client.get("/api/vendor-directory").status_code in (302, 401)


def test_admin_sees_one_row_per_vendor_with_bill_pay(client):
    login(client, "admin")
    v = by_name(client.get("/api/vendor-directory"))
    assert set(v) == {"US Foods", "Tiger Exchange Inc."}          # pending-only names aren't vendors
    assert sorted(v["US Foods"]["spellings"]) == ["US Foods", "US Foods, Inc."]
    assert v["US Foods"]["invoices"] == 2 and v["US Foods"]["phone"] == "508-555-1000"
    t = v["Tiger Exchange Inc."]
    assert t["in_vendor_list"] is False and t["invoices"] == 3
    assert t["bill_pay"]["enabled"] and t["bill_pay"]["settings_name"] == "TIGER EXCHANGE INC."
    assert t["bill_pay"]["open_total"] == 90.0


def test_manager_gets_no_bill_pay(client):
    login(client, "manager")
    r = client.get("/api/vendor-directory").get_json()
    assert r["bill_pay_visible"] is False
    assert all("bill_pay" not in v for v in r["vendors"])


def test_saving_contact_creates_then_updates(client):
    login(client, "manager")
    r = client.put("/api/vendor-directory/contact", json={"name": "Tiger Exchange Inc.", "category": "SUPPLIES",
                                                          "contact_name": "Sam"}).get_json()
    assert r["created"] is True
    v = by_name(client.get("/api/vendor-directory"))["Tiger Exchange Inc."]
    assert v["in_vendor_list"] and v["category"] == "SUPPLIES" and v["contact_name"] == "Sam"
    r2 = client.put("/api/vendor-directory/contact", json={"vendor_id": r["vendor_id"], "phone": "617-555-0100",
                                                           "contact_name": ""}).get_json()
    assert r2["created"] is False
    v = by_name(client.get("/api/vendor-directory"))["Tiger Exchange Inc."]
    assert v["phone"] == "617-555-0100" and v["contact_name"] is None and v["category"] == "SUPPLIES"
