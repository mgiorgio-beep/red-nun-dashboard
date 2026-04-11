"""
Add User - Quick Script
Usage: python add_user.py <username> <password> <full_name> [role]
"""

import sys
import sqlite3
import hashlib
import secrets

DB_PATH = 'toast_data.db'


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.sha256((password + salt).encode()).hexdigest()
    return pwd_hash, salt


def add_user(username, password, full_name, role='user'):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Check if exists
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        print(f"❌ User '{username}' already exists")
        conn.close()
        return False

    # Hash password
    pwd_hash, salt = hash_password(password)

    # Insert
    cursor = conn.execute("""
        INSERT INTO users (username, password_hash, salt, full_name, role)
        VALUES (?, ?, ?, ?, ?)
    """, (username, pwd_hash, salt, full_name, role))

    conn.commit()
    user_id = cursor.lastrowid
    conn.close()

    print(f"✅ User created successfully!")
    print(f"   ID: {user_id}")
    print(f"   Username: {username}")
    print(f"   Full Name: {full_name}")
    print(f"   Role: {role}")
    print(f"\n   Login at: http://159.65.180.102:8080/login")
    return True


def list_users():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    users = conn.execute("""
        SELECT id, username, full_name, role, active,
               datetime(created_at, 'localtime') as created,
               datetime(last_login, 'localtime') as last_login
        FROM users
        ORDER BY id
    """).fetchall()

    print("\n" + "=" * 90)
    print("  USERS")
    print("=" * 90)
    print(f"{'ID':<4} {'Username':<15} {'Full Name':<20} {'Role':<10} {'Status':<10} {'Last Login':<20}")
    print("-" * 90)

    for u in users:
        status = "Active" if u['active'] else "Inactive"
        last_login = u['last_login'] or "Never"
        print(f"{u['id']:<4} {u['username']:<15} {u['full_name']:<20} {u['role']:<10} {status:<10} {last_login:<20}")

    print("=" * 90)
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python add_user.py <username> <password> <full_name> [role]")
        print("  python add_user.py list")
        print("")
        print("Examples:")
        print("  python add_user.py john pass123 'John Smith' user")
        print("  python add_user.py manager mgr456 'Jane Manager' manager")
        print("  python add_user.py list")
        print("")
        print("Roles: admin, manager, user")
        sys.exit(1)

    if sys.argv[1] == 'list':
        list_users()
        sys.exit(0)

    username = sys.argv[1]
    password = sys.argv[2] if len(sys.argv) > 2 else None
    full_name = sys.argv[3] if len(sys.argv) > 3 else username
    role = sys.argv[4] if len(sys.argv) > 4 else 'user'

    if not password:
        print("❌ Password required")
        sys.exit(1)

    if role not in ['admin', 'manager', 'user']:
        print(f"❌ Invalid role: {role}")
        print("   Valid roles: admin, manager, user")
        sys.exit(1)

    success = add_user(username, password, full_name, role)
    sys.exit(0 if success else 1)
