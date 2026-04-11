"""
Authentication Routes
Handles login, logout, and session management.
"""

from flask import Blueprint, render_template_string, request, redirect, session, jsonify
from data_store import get_connection
import hashlib
from functools import wraps
from datetime import datetime
import logging

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)


def hash_password(password, salt):
    """Hash password with salt."""
    return hashlib.sha256((password + salt).encode()).hexdigest()


def notify_admin_login(username, full_name, role, ip_address):
    """Notify admin of non-admin login via email and log."""
    try:
        # Log to console/file
        logger.warning(f"🔔 NON-ADMIN LOGIN: {username} ({full_name}) - Role: {role} - IP: {ip_address}")

        # Try to send email notification
        try:
            from email_report import send_email
            import os

            admin_email = os.getenv('ADMIN_EMAIL')
            if admin_email:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                subject = f"🔔 Login Alert: {username}"
                body = f"""
A non-admin user has logged into Red Nun Analytics:

Username: {username}
Full Name: {full_name}
Role: {role}
IP Address: {ip_address}
Time: {timestamp}

View login history: http://159.65.180.102:8080/admin/logins
                """

                send_email(admin_email, subject, body)
                logger.info(f"Login notification email sent to {admin_email}")
        except ImportError:
            logger.debug("Email notification not available")
        except Exception as e:
            logger.error(f"Failed to send login notification email: {e}")

    except Exception as e:
        logger.error(f"Error in notify_admin_login: {e}")


def login_required(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and handler."""
    if request.method == 'POST':
        username = request.json.get('username')
        password = request.json.get('password')
        remember = request.json.get('remember', False)

        conn = get_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1",
            (username,)
        ).fetchone()
        conn.close()

        if user:
            pwd_hash = hash_password(password, user['salt'])
            if pwd_hash == user['password_hash']:
                # Login successful
                session.permanent = remember  # Extend session if "remember me"
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['full_name'] = user['full_name']
                session['role'] = user['role']

                # Update last login
                conn = get_connection()
                conn.execute(
                    "UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?",
                    (user['id'],)
                )

                # Log the login
                ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
                user_agent = request.headers.get('User-Agent', '')[:200]

                conn.execute("""
                    INSERT INTO login_log (user_id, username, ip_address, user_agent, success)
                    VALUES (?, ?, ?, ?, 1)
                """, (user['id'], username, ip_address, user_agent))

                conn.commit()
                conn.close()

                # Send notification for non-admin logins
                if user['role'] != 'admin':
                    notify_admin_login(user['username'], user['full_name'], user['role'], ip_address)

                return jsonify({'success': True, 'redirect': '/'})

        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

    # GET - show login form
    return render_template_string(LOGIN_HTML)


@auth_bp.route('/logout')
def logout():
    """Logout handler."""
    session.clear()
    return redirect('/login')


@auth_bp.route('/api/auth/check')
def check_auth():
    """Check if user is authenticated."""
    if 'user_id' in session:
        return jsonify({
            'authenticated': True,
            'username': session.get('username'),
            'full_name': session.get('full_name'),
            'role': session.get('role')
        })
    return jsonify({'authenticated': False}), 401


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

    # Build HTML table
    rows = []
    for log in logins:
        status = "✅ Success" if log['success'] else "❌ Failed"
        role_color = {
            'admin': '#10b981',
            'manager': '#f59e0b',
            'user': '#6b7280'
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


# Login page HTML
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Red Nun Analytics - Login</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: linear-gradient(135deg, #1a1a1a 0%, #2d1810 100%);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
    }

    .login-container {
      background: rgba(255, 255, 255, 0.05);
      backdrop-filter: blur(10px);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 16px;
      padding: 48px;
      width: 100%;
      max-width: 420px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    }

    .logo {
      text-align: center;
      margin-bottom: 32px;
    }

    .logo h1 {
      font-size: 32px;
      font-weight: 700;
      color: #fff;
      margin-bottom: 8px;
    }

    .logo p {
      color: rgba(255, 255, 255, 0.6);
      font-size: 14px;
    }

    .form-group {
      margin-bottom: 24px;
    }

    label {
      display: block;
      margin-bottom: 8px;
      font-weight: 500;
      font-size: 14px;
      color: rgba(255, 255, 255, 0.9);
    }

    input {
      width: 100%;
      padding: 14px 16px;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 8px;
      font-size: 16px;
      color: #fff;
      transition: all 0.2s;
    }

    input:focus {
      outline: none;
      border-color: rgba(255, 255, 255, 0.3);
      background: rgba(255, 255, 255, 0.12);
    }

    .btn {
      width: 100%;
      padding: 14px;
      background: linear-gradient(135deg, #8b0000 0%, #b22222 100%);
      border: none;
      border-radius: 8px;
      color: #fff;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      margin-top: 8px;
    }

    .btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 20px rgba(139, 0, 0, 0.4);
    }

    .btn:active {
      transform: translateY(0);
    }

    .error {
      background: rgba(220, 38, 38, 0.2);
      border: 1px solid rgba(220, 38, 38, 0.4);
      color: #fca5a5;
      padding: 12px;
      border-radius: 8px;
      margin-bottom: 24px;
      font-size: 14px;
      display: none;
    }

    .error.show {
      display: block;
    }
  </style>
</head>
<body>
  <div class="login-container">
    <div class="logo">
      <h1>🍷 Red Nun Analytics</h1>
      <p>Inventory & Recipe Management</p>
    </div>

    <div class="error" id="error"></div>

    <form id="loginForm">
      <div class="form-group">
        <label>Username</label>
        <input type="text" name="username" id="username" required autofocus>
      </div>

      <div class="form-group">
        <label>Password</label>
        <input type="password" name="password" id="password" required>
      </div>

      <div style="margin-bottom: 24px;">
        <label style="display: flex; align-items: center; cursor: pointer; font-size: 14px;">
          <input type="checkbox" id="remember" style="width: auto; margin-right: 8px;">
          <span>Stay signed in</span>
        </label>
      </div>

      <button type="submit" class="btn">Sign In</button>
    </form>
  </div>

  <script>
    document.getElementById('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();

      const username = document.getElementById('username').value;
      const password = document.getElementById('password').value;
      const remember = document.getElementById('remember').checked;
      const errorDiv = document.getElementById('error');
      const submitBtn = e.target.querySelector('button[type="submit"]');

      // Disable button and show loading
      submitBtn.disabled = true;
      submitBtn.textContent = 'Signing in...';
      errorDiv.classList.remove('show');

      try {
        const response = await fetch('/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password, remember })
        });

        const data = await response.json();

        if (response.ok && data.success) {
          submitBtn.textContent = '✓ Success!';
          // Add a small delay to show success message
          setTimeout(() => {
            window.location.href = data.redirect || '/';
          }, 500);
        } else {
          throw new Error(data.error || 'Invalid credentials');
        }
      } catch (err) {
        console.error('Login error:', err);
        errorDiv.textContent = err.message || 'Login failed. Please check your credentials.';
        errorDiv.classList.add('show');
        document.getElementById('password').value = '';
        document.getElementById('password').focus();

        // Re-enable button
        submitBtn.disabled = false;
        submitBtn.textContent = 'Sign In';
      }
    });
  </script>
</body>
</html>
"""

# Login history page HTML
LOGIN_HISTORY_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Login History - Red Nun Analytics</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f5f5f5;
      padding: 24px;
    }
    .container {
      max-width: 1200px;
      margin: 0 auto;
      background: white;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
      padding: 32px;
    }
    h1 {
      margin-bottom: 8px;
      color: #1a1a1a;
    }
    .subtitle {
      color: #666;
      margin-bottom: 24px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 16px;
    }
    th {
      background: #f9fafb;
      padding: 12px;
      text-align: left;
      font-weight: 600;
      color: #374151;
      border-bottom: 2px solid #e5e7eb;
      font-size: 14px;
    }
    td {
      padding: 12px;
      border-bottom: 1px solid #e5e7eb;
      font-size: 14px;
      color: #1f2937;
    }
    tr:hover {
      background: #f9fafb;
    }
    .back-btn {
      display: inline-block;
      padding: 10px 20px;
      background: #8b0000;
      color: white;
      text-decoration: none;
      border-radius: 6px;
      margin-bottom: 24px;
      font-weight: 500;
    }
    .back-btn:hover {
      background: #6b0000;
    }
  </style>
</head>
<body>
  <div class="container">
    <a href="/" class="back-btn">← Back to Dashboard</a>
    <h1>🔐 Login History</h1>
    <p class="subtitle">Recent login attempts (last 100)</p>

    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Username</th>
          <th>Full Name</th>
          <th>Role</th>
          <th>IP Address</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {{ rows|safe }}
      </tbody>
    </table>
  </div>
</body>
</html>
"""

