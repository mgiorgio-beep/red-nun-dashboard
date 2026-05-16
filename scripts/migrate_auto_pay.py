"""
Migration: auto-pay + print-agent tables.

Idempotent — safe to run multiple times. Adds:

  - ap_payments.auto_paid INTEGER DEFAULT 0
  - auto_pay_decisions  (audit log; every confirmed invoice gets a row)
  - print_jobs          (queue consumed by the Windows print agent)

Usage:
    cd /opt/red-nun-dashboard
    /opt/red-nun-dashboard/venv/bin/python3 scripts/migrate_auto_pay.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from integrations.toast.data_store import get_connection


def _add_column_if_missing(conn, table, column, ddl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column in cols:
        print(f"  [skip] {table}.{column} already exists")
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    print(f"  [ok]   added {table}.{column}")
    return True


def main():
    conn = get_connection()
    cur = conn.cursor()

    print("== Auto-pay schema migration ==")

    # 1) ap_payments.auto_paid
    _add_column_if_missing(conn, "ap_payments", "auto_paid",
                           "auto_paid INTEGER DEFAULT 0")

    # 2) vendor_bill_pay.auto_pay already exists per billpay_routes.py,
    #    but make sure (older DBs may not have it)
    _add_column_if_missing(conn, "vendor_bill_pay", "auto_pay",
                           "auto_pay INTEGER DEFAULT 0")

    # 3) auto_pay_decisions audit log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS auto_pay_decisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id      INTEGER NOT NULL,
            vendor_name     TEXT,
            invoice_total   REAL,
            decision        TEXT NOT NULL,      -- 'paid' | 'skipped' | 'error'
            reason          TEXT NOT NULL,      -- machine-readable code
            details         TEXT,               -- human-readable extra info
            ap_payment_id   INTEGER,            -- set if decision = 'paid'
            check_number    TEXT,
            print_job_id    INTEGER,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_apd_created ON auto_pay_decisions(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_apd_decision ON auto_pay_decisions(decision)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_apd_invoice ON auto_pay_decisions(invoice_id)")
    print("  [ok]   auto_pay_decisions table")

    # 4) print_jobs queue
    cur.execute("""
        CREATE TABLE IF NOT EXISTS print_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            kind            TEXT NOT NULL DEFAULT 'check',  -- future: 'check' | 'invoice' | 'receipt'
            payment_id      INTEGER,                         -- ap_payments.id
            check_number    TEXT,
            location        TEXT,                            -- 'chatham' | 'dennis'
            pdf_path        TEXT NOT NULL,                   -- absolute path on server
            status          TEXT NOT NULL DEFAULT 'pending', -- 'pending'|'claimed'|'printed'|'error'|'cancelled'
            attempts        INTEGER DEFAULT 0,
            last_error      TEXT,
            claimed_by      TEXT,                            -- agent id / hostname
            claimed_at      TEXT,
            printed_at      TEXT,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pj_status ON print_jobs(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pj_created ON print_jobs(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pj_payment ON print_jobs(payment_id)")
    print("  [ok]   print_jobs table")

    conn.commit()
    conn.close()
    print("== Migration complete ==")


if __name__ == "__main__":
    main()
