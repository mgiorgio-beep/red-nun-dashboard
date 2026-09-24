"""A new coding rule must not backfill an invoice-settling Bill Pay payment
onto an expense account (Mike, 2026-09-24).

2026-09-23 evening: ten vendor rules created on Bank Transactions each ran
_backfill_unassigned_for_pattern, which coded every uncoded payment for that
vendor — 93 of them with invoices attached — to the vendor's expense account.
The cost then counted at invoice date and again at payment. Synthetic DB.
"""
import sqlite3


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE gl_accounts (id INTEGER PRIMARY KEY, name TEXT, account_type TEXT, location TEXT,
                                  active INTEGER DEFAULT 1, qbo_id TEXT);
        CREATE TABLE bank_accounts (id INTEGER PRIMARY KEY, location TEXT);
        CREATE TABLE vendor_payments (id INTEGER PRIMARY KEY, vendor TEXT, bank_account_id INTEGER,
                                      ap_payment_id INTEGER, gl_account_id INTEGER, gl_source TEXT, gl_status TEXT);
        CREATE TABLE vendor_payment_invoices (id INTEGER PRIMARY KEY, payment_id INTEGER, invoice_number TEXT);
        CREATE TABLE ap_payment_invoices (id INTEGER PRIMARY KEY, payment_id INTEGER);
        CREATE TABLE payroll_checks (id INTEGER PRIMARY KEY, employee_name TEXT, bank_account_id INTEGER,
                                     gl_account_id INTEGER, gl_source TEXT, gl_status TEXT);
        CREATE TABLE bank_deposits (id INTEGER PRIMARY KEY, description TEXT, memo TEXT, bank_account_id INTEGER,
                                    gl_account_id INTEGER, gl_source TEXT, gl_status TEXT);
        CREATE TABLE manual_bank_entries (id INTEGER PRIMARY KEY, payee TEXT, memo TEXT, bank_account_id INTEGER,
                                          gl_account_id INTEGER, gl_source TEXT, gl_status TEXT);
        INSERT INTO gl_accounts VALUES (197, 'Daily Cleaning', 'Expense', 'dennis', 1, '1');
        INSERT INTO bank_accounts VALUES (2, 'dennis');
        INSERT INTO vendor_payments (id, vendor, bank_account_id, ap_payment_id) VALUES
            (1, 'The Caron Group', 2, NULL),   -- invoice attached directly
            (2, 'The Caron Group', 2, 77),     -- invoice attached through ap_payment
            (3, 'The Caron Group', 2, NULL);   -- no invoice: free to take the rule
        INSERT INTO vendor_payment_invoices (payment_id, invoice_number) VALUES (1, 'C-1');
        INSERT INTO ap_payment_invoices (payment_id) VALUES (77);
    """)
    return c


def test_backfill_leaves_invoice_settling_payments_uncoded():
    from routes.register_routes import _backfill_unassigned_for_pattern
    c = _db()
    n = _backfill_unassigned_for_pattern(c, "THE CARON", 197, "dennis")
    got = {r["id"]: r["gl_account_id"] for r in c.execute("SELECT id, gl_account_id FROM vendor_payments")}
    assert got == {1: None, 2: None, 3: 197}
    assert n == 1
