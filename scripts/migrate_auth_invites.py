"""
One-time migration: User auth & invite system
Run: cd /opt/red-nun-dashboard && source venv/bin/activate && python scripts/migrate_auth_invites.py
"""
import sqlite3
import os
import sys

DB_PATH = os.getenv('DB_PATH', '/var/lib/rednun/toast_data.db')


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table, column):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def table_exists(conn, table):
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] > 0


def migrate():
    conn = get_connection()

    # 1. Add location column to users
    if not column_exists(conn, 'users', 'location'):
        conn.execute("ALTER TABLE users ADD COLUMN location TEXT DEFAULT 'both'")
        print("  Added users.location column")
    else:
        print("  users.location already exists")

    # 2. Migrate role='user' to role='staff'
    count = conn.execute("UPDATE users SET role='manager' WHERE role IN ('user', 'staff')").rowcount
    if count:
        print(f"  Migrated {count} users to role='manager'")
    else:
        print("  No role='user'/'staff' records to migrate")

    # 3. Set admin email and deactivate rob
    conn.execute("UPDATE users SET email = 'mgiorgio@rednun.com' WHERE username = 'admin'")
    print("  Set admin email to mgiorgio@rednun.com")

    conn.execute("UPDATE users SET active = 0 WHERE username = 'rob'")
    print("  Deactivated rob account")

    # 4. Create invites table
    if not table_exists(conn, 'invites'):
        conn.execute("""
            CREATE TABLE invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                email TEXT NOT NULL,
                role TEXT DEFAULT 'staff',
                location TEXT DEFAULT 'both',
                invited_by INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                accepted_at TEXT,
                revoked_at TEXT,
                FOREIGN KEY (invited_by) REFERENCES users(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invites_token ON invites(token)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invites_email ON invites(email)")
        print("\n  Created invites table with indexes")
    else:
        print("\n  invites table already exists")

    conn.commit()

    # Verify
    users = conn.execute("SELECT id, username, email, role, location, full_name FROM users").fetchall()
    print("\nCurrent users:")
    for u in users:
        print(f"  #{u['id']} {u['username']} email={u['email']} role={u['role']} location={u['location']} name={u['full_name']}")

    conn.close()
    print("\nMigration complete.")


if __name__ == '__main__':
    print(f"Database: {DB_PATH}")
    print("Migrating...\n")
    migrate()
