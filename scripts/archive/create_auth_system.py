"""
Create Authentication System
Adds users table and creates initial admin user.
"""

import os
import sqlite3
import hashlib
import secrets
from getpass import getpass

DB_PATH = os.getenv("DB_PATH", "toast_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password, salt=None):
    """Hash password with salt using SHA-256."""
    if salt is None:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.sha256((password + salt).encode()).hexdigest()
    return pwd_hash, salt


def create_users_table():
    """Create users table if it doesn't exist."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            full_name TEXT,
            email TEXT,
            role TEXT DEFAULT 'user',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT
        )
    """)
    conn.commit()
    conn.close()
    print("✅ Users table created")


def create_user(username, password, full_name, email, role='user'):
    """Create a new user."""
    conn = get_connection()

    # Check if user exists
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        print(f"❌ User '{username}' already exists")
        conn.close()
        return None

    # Hash password
    pwd_hash, salt = hash_password(password)

    # Insert user
    cursor = conn.execute("""
        INSERT INTO users (username, password_hash, salt, full_name, email, role)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (username, pwd_hash, salt, full_name, email, role))

    user_id = cursor.lastrowid
    conn.commit()
    conn.close()

    print(f"✅ User '{username}' created successfully (ID: {user_id})")
    return user_id


def list_users():
    """List all users."""
    conn = get_connection()
    users = conn.execute("""
        SELECT id, username, full_name, email, role, active, created_at, last_login
        FROM users
        ORDER BY created_at
    """).fetchall()
    conn.close()

    if not users:
        print("\nNo users found")
        return

    print("\n" + "=" * 100)
    print("  USERS")
    print("=" * 100)
    for u in users:
        status = "✓ Active" if u['active'] else "✗ Inactive"
        last_login = u['last_login'] or "Never"
        print(f"{u['id']:2}. {u['username']:15} {u['full_name']:20} {u['role']:10} {status:10} (Last: {last_login})")
    print("=" * 100)


def interactive_create():
    """Interactive user creation."""
    print("\n" + "=" * 60)
    print("  Create New User")
    print("=" * 60)

    username = input("\nUsername: ").strip()
    if not username:
        print("❌ Username required")
        return

    password = getpass("Password: ")
    if not password:
        print("❌ Password required")
        return

    confirm = getpass("Confirm password: ")
    if password != confirm:
        print("❌ Passwords don't match")
        return

    full_name = input("Full name: ").strip()
    email = input("Email (optional): ").strip() or None

    print("\nRole:")
    print("  1. admin (full access)")
    print("  2. manager (most access)")
    print("  3. user (view only)")
    role_choice = input("Choose (1-3) [3]: ").strip() or "3"

    role_map = {"1": "admin", "2": "manager", "3": "user"}
    role = role_map.get(role_choice, "user")

    print(f"\nCreating user '{username}' with role '{role}'...")
    create_user(username, password, full_name, email, role)


def main():
    print("\n" + "=" * 60)
    print("  Red Nun Authentication System Setup")
    print("=" * 60)

    # Create table
    create_users_table()

    # Check if any users exist
    conn = get_connection()
    user_count = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()['cnt']
    conn.close()

    if user_count == 0:
        print("\n⚠️  No users found. Let's create an admin user.")
        username = input("\nAdmin username [admin]: ").strip() or "admin"
        password = getpass("Admin password: ")

        if not password:
            print("❌ Password required")
            return 1

        full_name = input("Full name: ").strip() or "Administrator"

        create_user(username, password, full_name, None, 'admin')
        print("\n🎉 Admin user created!")

    # List users
    list_users()

    # Offer to create more
    while True:
        choice = input("\nCreate another user? (y/n): ").strip().lower()
        if choice == 'y':
            interactive_create()
            list_users()
        else:
            break

    print("\n✅ Authentication system ready!")
    print("\n📝 Next steps:")
    print("   1. Restart the server")
    print("   2. Go to http://159.65.180.102:8080/login")
    print("   3. Log in with your credentials")


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
