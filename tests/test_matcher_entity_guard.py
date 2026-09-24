"""No automatic matcher pairs a payment with a bank line on the other
entity's account (Mike, 2026-09-24).

job066 matched Chatham's KOD check (#295, which carried Dennis's bank id) to a
Dennis Venmo line on amount alone. Payments made by the other entity's bank
are paired only by a human, as intercompany; once cleared that way they stay
visible to the import matcher. Synthetic DB; always runs.
"""
import sqlite3

from tests.test_bank_close_cleared_date import SCHEMA


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.execute("INSERT INTO bank_accounts (id, name, location, account_last4) VALUES (1, 'Chatham (5975)', 'chatham', '5975')")
    c.execute("INSERT INTO bank_accounts (id, name, location, account_last4) VALUES (2, 'Dennis (2757)', 'dennis', '2757')")
    # Chatham bill stamped with Dennis's bank id, uncleared (the #295 shape)
    c.execute("INSERT INTO vendor_payments (id, vendor, location, payment_date, payment_total, status, bank_account_id, cleared) "
              "VALUES (10, 'KOD Holdings', 'chatham', '2026-05-19', 500.00, 'printed', 2, 0)")
    # Dennis bill on Dennis's account (a legitimate candidate)
    c.execute("INSERT INTO vendor_payments (id, vendor, location, payment_date, payment_total, status, bank_account_id, cleared) "
              "VALUES (11, 'Barrows', 'dennis', '2026-05-20', 480.00, 'printed', 2, 0)")
    # Dennis bill Chatham's bank paid, already paired by a human (intercompany)
    c.execute("INSERT INTO vendor_payments (id, vendor, location, payment_date, payment_total, status, bank_account_id, cleared, cleared_date) "
              "VALUES (12, 'PFG', 'dennis', '2026-05-13', 4814.51, 'cleared', 1, 1, '2026-05-13')")
    c.execute("INSERT INTO manual_bank_entries (id, bank_account_id, entry_date, entry_type, payee, amount, statement_upload_id) "
              "VALUES (100, 2, '2026-05-18', 'other', 'PAYMENT VENMO', -500.00, 1)")
    c.execute("INSERT INTO manual_bank_entries (id, bank_account_id, entry_date, entry_type, payee, amount, statement_upload_id) "
              "VALUES (101, 2, '2026-05-21', 'other', 'Check 9721', -480.00, 1)")
    return c


def test_dedupe_never_pairs_across_entities():
    from routes import bank_reconcile_routes as brr
    c = _db()
    r = brr._dedupe_period(c, {"id": 2, "account_last4": "2757"}, "2026-05-01", "2026-05-31", 7, True, True, False, "t")
    got = {x["manual_entry_id"]: (x["match"] or {}).get("id") for x in r["candidates"]}
    assert got[100] is None          # the Venmo line is not KOD's check
    assert got[101] == 11            # same-entity pairing still works


def test_import_matcher_sees_only_own_entity_plus_human_intercompany():
    from routes import bank_reconcile_routes as brr
    c = _db()
    dennis = {x["id"] for x in brr._load_register_rows_for_period(c, 2, {"period_start": "2026-05-01", "period_end": "2026-05-31"})
              if x["source"] == "bill_pay"}
    assert 10 not in dennis and 11 in dennis
    chatham = {x["id"] for x in brr._load_register_rows_for_period(c, 1, {"period_start": "2026-05-01", "period_end": "2026-05-31"})
               if x["source"] == "bill_pay"}
    assert 12 in chatham             # cleared intercompany pairing stays visible
