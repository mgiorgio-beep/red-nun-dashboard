"""
Authentication Routes
Handles login, logout, session management, user invites, and registration.
"""

from flask import Blueprint, render_template_string, request, redirect, session, jsonify
from integrations.toast.data_store import get_connection
import hashlib
import secrets
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from datetime import datetime, timedelta
import logging

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def hash_password(password, salt):
    """Hash password with salt."""
    return hashlib.sha256((password + salt).encode()).hexdigest()


def notify_admin_login(username, full_name, role, ip_address):
    """Log a non-admin login. (Email notification removed in Phase 2.)"""
    try:
        logger.warning(f"NON-ADMIN LOGIN: {username} ({full_name}) - Role: {role} - IP: {ip_address}")
    except Exception as e:
        logger.error(f"Error in notify_admin_login: {e}")


# ------------------------------------------------------------------
# Decorators
# ------------------------------------------------------------------

def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Decorator: must be logged in AND role == 'admin'."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        if session.get('role') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


def admin_or_accountant_required(f):
    """Decorator: must be admin or accountant (read-only bill pay access)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        if session.get('role') not in ('admin', 'accountant'):
            return jsonify({'error': 'Access restricted'}), 403
        return f(*args, **kwargs)
    return decorated


# ------------------------------------------------------------------
# Email
# ------------------------------------------------------------------

def send_invite_email(to_email, invite_url, location_label):
    """Send invite email via SMTP (same pattern as morning_report.py)."""
    smtp_host = os.getenv('SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER')
    smtp_pass = os.getenv('SMTP_PASSWORD')
    from_addr = os.getenv('REPORT_FROM_EMAIL', 'dashboard@rednun.com')

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:520px;margin:0 auto;">
      <div style="background:#1a1a1a;padding:24px 32px;border-radius:8px 8px 0 0;">
        <h2 style="color:#fff;margin:0;font-size:20px;">Red Nun Dashboard</h2>
        <p style="color:#aaa;margin:4px 0 0;font-size:13px;">{location_label}</p>
      </div>
      <div style="background:#f9f9f9;padding:32px;border-radius:0 0 8px 8px;border:1px solid #e0e0e0;border-top:none;">
        <p style="font-size:16px;margin-top:0;">You've been invited to access the <strong>Red Nun Dashboard</strong>.</p>
        <p style="color:#555;">Click below to set up your account. This link expires in 48 hours.</p>
        <div style="text-align:center;margin:32px 0;">
          <a href="{invite_url}" style="background:linear-gradient(135deg,#8b0000,#b22222);color:#fff;padding:14px 32px;border-radius:6px;text-decoration:none;font-size:15px;display:inline-block;">Set up my account</a>
        </div>
        <p style="font-size:12px;color:#999;margin-bottom:0;">Or copy: <a href="{invite_url}" style="color:#555;">{invite_url}</a></p>
      </div>
    </div>
    """

    plain = f"You've been invited to the Red Nun Dashboard ({location_label}).\n\nSet up your account: {invite_url}\n\nLink expires in 48 hours."

    msg = MIMEMultipart('alternative')
    msg['Subject'] = "You're invited to the Red Nun Dashboard"
    msg['From'] = from_addr
    msg['To'] = to_email
    msg.attach(MIMEText(plain, 'plain'))
    msg.attach(MIMEText(html, 'html'))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to_email], msg.as_string())
        logger.info(f"Invite email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Invite email failed for {to_email}: {e}")
        return False


LOCATION_LABELS = {
    'dennis': 'Dennis Port',
    'chatham': 'Chatham',
    'both': 'Both Locations',
}


# ------------------------------------------------------------------
# Login / Logout
# ------------------------------------------------------------------

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and handler."""
    if request.method == 'POST':
        email = (request.json.get('email') or '').strip().lower()
        password = request.json.get('password')
        remember = request.json.get('remember', False)

        conn = get_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ? AND active = 1",
            (email,)
        ).fetchone()
        conn.close()

        if user:
            pwd_hash = hash_password(password, user['salt'])
            if pwd_hash == user['password_hash']:
                session.permanent = remember
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['email'] = user['email']
                session['full_name'] = user['full_name']
                session['role'] = user['role']
                session['location'] = user['location'] or 'both'

                conn = get_connection()
                conn.execute(
                    "UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?",
                    (user['id'],)
                )

                ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
                user_agent = request.headers.get('User-Agent', '')[:200]

                conn.execute("""
                    INSERT INTO login_log (user_id, username, ip_address, user_agent, success)
                    VALUES (?, ?, ?, ?, 1)
                """, (user['id'], user['username'], ip_address, user_agent))

                conn.commit()
                conn.close()

                if user['role'] != 'admin':
                    notify_admin_login(user['username'], user['full_name'], user['role'], ip_address)

                return jsonify({'success': True, 'redirect': '/'})

        return jsonify({'success': False, 'error': 'Invalid email or password'}), 401

    return render_template_string(LOGIN_HTML)


@auth_bp.route('/logout')
def logout():
    """Logout handler."""
    session.clear()
    return redirect('/login')


# ------------------------------------------------------------------
# Auth check / profile
# ------------------------------------------------------------------

@auth_bp.route('/api/auth/check')
def check_auth():
    """Check if user is authenticated."""
    if 'user_id' in session:
        return jsonify({
            'authenticated': True,
            'user_id': session.get('user_id'),
            'username': session.get('username'),
            'email': session.get('email'),
            'full_name': session.get('full_name'),
            'role': session.get('role'),
            'location': session.get('location', 'both'),
        })
    return jsonify({'authenticated': False}), 401


# ------------------------------------------------------------------
# Change password
# ------------------------------------------------------------------

@auth_bp.route('/api/auth/change-password', methods=['POST'])
@login_required
def change_password():
    """Change the current user's password."""
    data = request.json or {}
    current_pw = data.get('current_password', '')
    new_pw = data.get('new_password', '')

    if len(new_pw) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400

    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()

    if not user or hash_password(current_pw, user['salt']) != user['password_hash']:
        conn.close()
        return jsonify({'error': 'Current password is incorrect'}), 400

    new_salt = secrets.token_hex(16)
    new_hash = hash_password(new_pw, new_salt)
    conn.execute(
        "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
        (new_hash, new_salt, user['id'])
    )
    conn.commit()
    conn.close()

    return jsonify({'ok': True})


# ------------------------------------------------------------------
# Invite management (admin only)
# ------------------------------------------------------------------

@auth_bp.route('/api/admin/users')
@admin_required
def list_users_and_invites():
    """List all users and invites for the admin page."""
    conn = get_connection()

    users = conn.execute("""
        SELECT id, username, email, full_name, role, location, active,
               created_at, last_login
        FROM users ORDER BY active DESC, created_at DESC
    """).fetchall()

    invites = conn.execute("""
        SELECT i.*, u.full_name as inviter_name
        FROM invites i
        LEFT JOIN users u ON i.invited_by = u.id
        ORDER BY i.created_at DESC
    """).fetchall()
    conn.close()

    now = datetime.utcnow().isoformat()

    def invite_status(inv):
        if inv['revoked_at']:
            return 'revoked'
        if inv['accepted_at']:
            return 'accepted'
        if inv['expires_at'] and inv['expires_at'] < now:
            return 'expired'
        return 'pending'

    return jsonify({
        'users': [dict(u) for u in users],
        'invites': [{**dict(inv), 'status': invite_status(inv)} for inv in invites],
    })


@auth_bp.route('/api/admin/invite', methods=['POST'])
@admin_required
def send_invite():
    """Create and send an invite."""
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    role = data.get('role', 'manager')
    location = data.get('location', 'both')

    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'error': 'Valid email required'}), 400
    if role not in ('manager', 'accountant', 'admin'):
        return jsonify({'error': 'Invalid role'}), 400
    if location not in ('dennis', 'chatham', 'both'):
        return jsonify({'error': 'Invalid location'}), 400

    conn = get_connection()

    existing = conn.execute(
        "SELECT id FROM users WHERE email = ? AND active = 1", (email,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'A user with that email already exists'}), 409

    # Revoke any existing pending invites for this email
    conn.execute("""
        UPDATE invites SET revoked_at = datetime('now')
        WHERE email = ? AND accepted_at IS NULL AND revoked_at IS NULL
    """, (email,))

    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=48)).isoformat()

    conn.execute("""
        INSERT INTO invites (token, email, role, location, invited_by, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (token, email, role, location, session['user_id'], expires))
    conn.commit()
    conn.close()

    invite_url = request.url_root.rstrip('/') + f'/register/{token}'
    location_label = LOCATION_LABELS.get(location, location)
    email_sent = send_invite_email(email, invite_url, location_label)

    return jsonify({
        'ok': True,
        'email_sent': email_sent,
        'message': f'Invite sent to {email}' if email_sent else f'Invite created but email failed — share link manually: {invite_url}',
    })


@auth_bp.route('/api/admin/invite/<int:invite_id>/resend', methods=['POST'])
@admin_required
def resend_invite(invite_id):
    """Revoke old invite and create a fresh one, then resend email."""
    conn = get_connection()
    inv = conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()
    if not inv:
        conn.close()
        return jsonify({'error': 'Invite not found'}), 404

    # Revoke old
    conn.execute("UPDATE invites SET revoked_at = datetime('now') WHERE id = ?", (invite_id,))

    # Create new
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(hours=48)).isoformat()
    conn.execute("""
        INSERT INTO invites (token, email, role, location, invited_by, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (token, inv['email'], inv['role'], inv['location'], session['user_id'], expires))
    conn.commit()
    conn.close()

    invite_url = request.url_root.rstrip('/') + f'/register/{token}'
    location_label = LOCATION_LABELS.get(inv['location'], inv['location'])
    email_sent = send_invite_email(inv['email'], invite_url, location_label)

    return jsonify({
        'ok': True,
        'email_sent': email_sent,
        'message': f'Resent to {inv["email"]}' if email_sent else 'Resend failed',
    })


@auth_bp.route('/api/admin/invite/<int:invite_id>/revoke', methods=['POST'])
@admin_required
def revoke_invite(invite_id):
    """Revoke a pending invite."""
    conn = get_connection()
    conn.execute(
        "UPDATE invites SET revoked_at = datetime('now') WHERE id = ? AND accepted_at IS NULL AND revoked_at IS NULL",
        (invite_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@auth_bp.route('/api/admin/users/<int:user_id>/deactivate', methods=['POST'])
@admin_required
def deactivate_user(user_id):
    """Deactivate a user account."""
    if user_id == session.get('user_id'):
        return jsonify({'error': "You can't deactivate yourself"}), 400
    conn = get_connection()
    conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@auth_bp.route('/api/admin/users/<int:user_id>/reactivate', methods=['POST'])
@admin_required
def reactivate_user(user_id):
    """Reactivate a user account."""
    conn = get_connection()
    conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ------------------------------------------------------------------
# Registration (token-gated)
# ------------------------------------------------------------------

def _get_valid_invite(token):
    """Look up a valid (non-expired, non-revoked, non-accepted) invite."""
    conn = get_connection()
    inv = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not inv:
        return None
    now = datetime.utcnow().isoformat()
    if inv['accepted_at'] or inv['revoked_at'] or (inv['expires_at'] and inv['expires_at'] < now):
        return None
    return inv


@auth_bp.route('/register/<token>', methods=['GET'])
def register_page(token):
    """Show registration form if invite is valid."""
    inv = _get_valid_invite(token)
    if not inv:
        return render_template_string(INVALID_INVITE_HTML)

    location_label = LOCATION_LABELS.get(inv['location'], inv['location'])
    return render_template_string(
        REGISTER_HTML,
        token=token,
        email=inv['email'],
        location_label=location_label,
        role=inv['role'],
    )


@auth_bp.route('/api/register/<token>', methods=['POST'])
def register_submit(token):
    """Complete registration via invite token."""
    inv = _get_valid_invite(token)
    if not inv:
        return jsonify({'error': 'This invite link is no longer valid'}), 400

    data = request.json or {}
    full_name = (data.get('full_name') or '').strip()
    password = data.get('password', '')

    if not full_name or len(full_name) < 2:
        return jsonify({'error': 'Full name is required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    # Generate username from email (part before @)
    username = inv['email'].split('@')[0].lower()

    conn = get_connection()

    # Ensure username is unique — append number if needed
    base_username = username
    counter = 1
    while conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        username = f"{base_username}{counter}"
        counter += 1

    salt = secrets.token_hex(16)
    pwd_hash = hash_password(password, salt)

    conn.execute("""
        INSERT INTO users (username, email, full_name, password_hash, salt, role, location, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, (username, inv['email'], full_name, pwd_hash, salt, inv['role'], inv['location']))

    conn.execute(
        "UPDATE invites SET accepted_at = datetime('now') WHERE id = ?",
        (inv['id'],)
    )
    conn.commit()
    conn.close()

    return jsonify({'ok': True, 'redirect': '/login'})


# ------------------------------------------------------------------
# Login history (admin only)
# ------------------------------------------------------------------

@auth_bp.route('/admin/logins')
def view_logins():
    """View login history (admin only)."""
    if session.get('role') != 'admin':
        return "Access denied. Admin only.", 403

    conn = get_connection()
    logins = conn.execute("""
        SELECT
            ll.*,
            u.full_name,
            u.role,
            datetime(ll.login_time, 'localtime') as login_time_local
        FROM login_log ll
        LEFT JOIN users u ON ll.user_id = u.id
        ORDER BY ll.login_time DESC
        LIMIT 100
    """).fetchall()
    conn.close()

    rows = []
    for log in logins:
        status = "Success" if log['success'] else "Failed"
        role_color = {
            'admin': '#10b981',
            'manager': '#f59e0b',
            'manager': '#6b7280'
        }.get(log['role'], '#6b7280')

        rows.append(f"""
            <tr>
                <td>{log['login_time_local']}</td>
                <td><strong>{log['username']}</strong></td>
                <td>{log['full_name'] or '--'}</td>
                <td><span style="background:{role_color};color:white;padding:4px 8px;border-radius:4px;font-size:12px;">{log['role'] or '--'}</span></td>
                <td>{log['ip_address']}</td>
                <td>{status}</td>
            </tr>
        """)

    return render_template_string(LOGIN_HISTORY_HTML, rows=''.join(rows))


# ══════════════════════════════════════════════════════════════════
# HTML Templates
# ══════════════════════════════════════════════════════════════════

_AUTH_STYLES = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: linear-gradient(135deg, #1a1a1a 0%, #2d1810 100%);
  min-height: 100vh; display: flex; align-items: center; justify-content: center; color: #fff;
}
.auth-container {
  background: rgba(255,255,255,0.05); backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,0.1); border-radius: 16px;
  padding: 48px; width: 100%; max-width: 420px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.logo { text-align: center; margin-bottom: 32px; }
.logo h1 { font-size: 28px; font-weight: 700; color: #fff; margin-bottom: 6px; }
.logo p { color: rgba(255,255,255,0.6); font-size: 13px; }
.form-group { margin-bottom: 20px; }
label { display: block; margin-bottom: 8px; font-weight: 500; font-size: 14px; color: rgba(255,255,255,0.9); }
input[type="email"], input[type="password"], input[type="text"] {
  width: 100%; padding: 14px 16px; background: rgba(255,255,255,0.08);
  border: 1px solid rgba(255,255,255,0.15); border-radius: 8px;
  font-size: 16px; color: #fff; transition: all 0.2s;
}
input:focus { outline: none; border-color: rgba(255,255,255,0.3); background: rgba(255,255,255,0.12); }
.btn {
  width: 100%; padding: 14px; background: linear-gradient(135deg, #8b0000, #b22222);
  border: none; border-radius: 8px; color: #fff; font-size: 16px;
  font-weight: 600; cursor: pointer; transition: all 0.2s; margin-top: 8px;
}
.btn:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(139,0,0,0.4); }
.btn:active { transform: translateY(0); }
.btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
.error {
  background: rgba(220,38,38,0.2); border: 1px solid rgba(220,38,38,0.4);
  color: #fca5a5; padding: 12px; border-radius: 8px; margin-bottom: 20px;
  font-size: 14px; display: none;
}
.error.show { display: block; }
.success {
  background: rgba(16,185,129,0.2); border: 1px solid rgba(16,185,129,0.4);
  color: #6ee7b7; padding: 12px; border-radius: 8px; margin-bottom: 20px;
  font-size: 14px; display: none;
}
.success.show { display: block; }
.badge {
  display: inline-block; padding: 3px 10px; border-radius: 12px;
  font-size: 12px; font-weight: 500; background: rgba(255,255,255,0.1);
  color: rgba(255,255,255,0.7);
}
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Red Nun Dashboard - Sign In</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>""" + _AUTH_STYLES + """</style>
</head>
<body>
  <div class="auth-container">
    <div class="logo">
      <h1>Red Nun Dashboard</h1>
      <p>Sign in to your account</p>
    </div>
    <div class="error" id="error"></div>
    <form id="loginForm">
      <div class="form-group">
        <label>Email</label>
        <input type="email" id="email" required autofocus placeholder="you@example.com" autocomplete="email">
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" id="password" required autocomplete="current-password">
      </div>
      <div style="margin-bottom: 24px;">
        <label style="display:flex;align-items:center;cursor:pointer;font-size:14px;">
          <input type="checkbox" id="remember" style="width:auto;margin-right:8px;">
          <span>Stay signed in</span>
        </label>
      </div>
      <button type="submit" class="btn">Sign In</button>
    </form>
  </div>
  <script>
    document.getElementById('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const email = document.getElementById('email').value;
      const password = document.getElementById('password').value;
      const remember = document.getElementById('remember').checked;
      const errorDiv = document.getElementById('error');
      const btn = e.target.querySelector('button[type="submit"]');
      btn.disabled = true; btn.textContent = 'Signing in...';
      errorDiv.classList.remove('show');
      try {
        const r = await fetch('/login', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({email, password, remember})
        });
        const data = await r.json();
        if (r.ok && data.success) {
          btn.textContent = 'Success!';
          setTimeout(() => { window.location.href = data.redirect || '/'; }, 400);
        } else { throw new Error(data.error || 'Invalid credentials'); }
      } catch (err) {
        errorDiv.textContent = err.message; errorDiv.classList.add('show');
        document.getElementById('password').value = '';
        document.getElementById('password').focus();
        btn.disabled = false; btn.textContent = 'Sign In';
      }
    });
  </script>
</body>
</html>
"""

REGISTER_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Create your account - Red Nun Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>""" + _AUTH_STYLES + """</style>
</head>
<body>
  <div class="auth-container">
    <div class="logo">
      <h1>Create your account</h1>
      <p>
        Invited as <strong>{{ email }}</strong>
        <span class="badge" style="margin-left:6px;">{{ location_label }}</span>
      </p>
    </div>
    <div class="error" id="error"></div>
    <div class="success" id="success"></div>
    <form id="registerForm">
      <div class="form-group">
        <label>Full Name</label>
        <input type="text" id="full_name" required autofocus placeholder="Your full name" autocomplete="name">
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" id="password" required placeholder="Min. 8 characters" autocomplete="new-password">
      </div>
      <div class="form-group">
        <label>Confirm Password</label>
        <input type="password" id="password_confirm" required placeholder="Repeat password" autocomplete="new-password">
      </div>
      <button type="submit" class="btn">Create my account</button>
    </form>
  </div>
  <script>
    document.getElementById('registerForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const full_name = document.getElementById('full_name').value.trim();
      const password = document.getElementById('password').value;
      const password_confirm = document.getElementById('password_confirm').value;
      const errorDiv = document.getElementById('error');
      const successDiv = document.getElementById('success');
      const btn = e.target.querySelector('button[type="submit"]');

      errorDiv.classList.remove('show'); successDiv.classList.remove('show');

      if (password !== password_confirm) {
        errorDiv.textContent = 'Passwords do not match'; errorDiv.classList.add('show'); return;
      }
      if (password.length < 8) {
        errorDiv.textContent = 'Password must be at least 8 characters'; errorDiv.classList.add('show'); return;
      }

      btn.disabled = true; btn.textContent = 'Creating account...';
      try {
        const token = window.location.pathname.split('/register/')[1];
        const r = await fetch('/api/register/' + token, {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({full_name, password})
        });
        const data = await r.json();
        if (r.ok && data.ok) {
          successDiv.textContent = 'Account created! Redirecting to sign in...';
          successDiv.classList.add('show');
          btn.textContent = 'Success!';
          setTimeout(() => { window.location.href = '/login'; }, 1500);
        } else { throw new Error(data.error || 'Registration failed'); }
      } catch (err) {
        errorDiv.textContent = err.message; errorDiv.classList.add('show');
        btn.disabled = false; btn.textContent = 'Create my account';
      }
    });
  </script>
</body>
</html>
"""

INVALID_INVITE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Invalid invite - Red Nun Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>""" + _AUTH_STYLES + """</style>
</head>
<body>
  <div class="auth-container" style="text-align:center;">
    <div style="font-size:48px;margin-bottom:16px;">&#9888;</div>
    <h2 style="font-size:20px;font-weight:600;margin-bottom:12px;">This invite link is no longer valid</h2>
    <p style="color:rgba(255,255,255,0.6);font-size:14px;line-height:1.6;">
      It may have expired, already been used, or been revoked.<br>
      Contact your manager to request a new invite.
    </p>
  </div>
</body>
</html>
"""

LOGIN_HISTORY_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Login History - Red Nun Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 24px; }
    .container { max-width: 1200px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); padding: 32px; }
    h1 { margin-bottom: 8px; color: #1a1a1a; }
    .subtitle { color: #666; margin-bottom: 24px; }
    table { width: 100%; border-collapse: collapse; margin-top: 16px; }
    th { background: #f9fafb; padding: 12px; text-align: left; font-weight: 600; color: #374151; border-bottom: 2px solid #e5e7eb; font-size: 14px; }
    td { padding: 12px; border-bottom: 1px solid #e5e7eb; font-size: 14px; color: #1f2937; }
    tr:hover { background: #f9fafb; }
    .back-btn { display: inline-block; padding: 10px 20px; background: #8b0000; color: white; text-decoration: none; border-radius: 6px; margin-bottom: 24px; font-weight: 500; }
    .back-btn:hover { background: #6b0000; }
  </style>
</head>
<body>
  <div class="container">
    <a href="/" class="back-btn">&larr; Back to Dashboard</a>
    <h1>Login History</h1>
    <p class="subtitle">Recent login attempts (last 100)</p>
    <table>
      <thead><tr><th>Time</th><th>Username</th><th>Full Name</th><th>Role</th><th>IP Address</th><th>Status</th></tr></thead>
      <tbody>{{ rows|safe }}</tbody>
    </table>
  </div>
</body>
</html>
"""
