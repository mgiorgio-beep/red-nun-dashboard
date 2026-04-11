#!/usr/bin/env python3
"""
Dashboard Down Check — Red Nun Dashboard
Checks if dashboard.rednun.com returns HTTP 200.
Only emails on failure. Tracks state to avoid repeat alerts.

Cron: */30 * * * * (every 30 minutes)
"""

import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv("/opt/rednun/.env")

RECIPIENT = "mgiorgio@rednun.com"
DASHBOARD_URL = "https://dashboard.rednun.com"
STATE_FILE = Path("/opt/rednun/monitoring/.down_state.json")
TIMEOUT = 15


def check_dashboard():
    """Returns (is_up, status_code_or_error)."""
    try:
        r = requests.get(DASHBOARD_URL, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return True, 200
        return False, r.status_code
    except requests.exceptions.SSLError as e:
        return False, f"SSL error: {e}"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection refused"
    except requests.exceptions.Timeout:
        return False, f"Timeout ({TIMEOUT}s)"
    except Exception as e:
        return False, str(e)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"is_down": False, "since": None, "alerted": False}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def send_down_alert(error):
    gmail_user = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_password:
        print("ERROR: GMAIL_ADDRESS and GMAIL_APP_PASSWORD not set")
        return False

    now = datetime.now().strftime("%I:%M %p on %A, %B %d")

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,system-ui,sans-serif;background:#020617;color:#e2e8f0;padding:20px;margin:0;">
<div style="max-width:600px;margin:0 auto;">
    <div style="background:#450a0a;border:1px solid #ef4444;border-radius:8px;padding:16px;margin-bottom:20px;">
        <h2 style="color:#ef4444;margin:0 0 8px 0;">Dashboard is DOWN</h2>
        <p style="margin:0;color:#fca5a5;">{DASHBOARD_URL} is not responding.</p>
    </div>
    <table style="width:100%;border-collapse:collapse;">
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">Detected</td>
        <td style="padding:8px 0;">{now}</td>
    </tr>
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">Error</td>
        <td style="padding:8px 0;color:#ef4444;">{error}</td>
    </tr>
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">Server</td>
        <td style="padding:8px 0;">Beelink SER5 (ssh -p 2222 rednun@ssh.rednun.com)</td>
    </tr>
    </table>
    <div style="margin-top:20px;padding:12px;background:#0f172a;border-radius:8px;">
        <strong style="color:#94a3b8;">Quick fixes:</strong>
        <pre style="color:#38bdf8;margin:8px 0 0 0;font-size:13px;">sudo systemctl restart rednun
sudo systemctl restart nginx
journalctl -u rednun --since "10 min ago"</pre>
    </div>
    <p style="color:#475569;font-size:12px;margin-top:20px;">
        This check runs every 30 minutes. You will receive one alert per outage.
    </p>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Red Nun] ALERT: Dashboard is DOWN"
    msg["From"] = gmail_user
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, [RECIPIENT], msg.as_string())
        print(f"Down alert sent to {RECIPIENT}")
        return True
    except Exception as e:
        print(f"ERROR sending alert: {e}")
        return False


def send_recovery_alert(down_since):
    gmail_user = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_password:
        return False

    now = datetime.now().strftime("%I:%M %p on %A, %B %d")
    try:
        since_dt = datetime.fromisoformat(down_since)
        duration_min = int((datetime.now() - since_dt).total_seconds() / 60)
        duration = f"{duration_min} minutes" if duration_min < 120 else f"{duration_min // 60}h {duration_min % 60}m"
    except Exception:
        duration = "unknown"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,system-ui,sans-serif;background:#020617;color:#e2e8f0;padding:20px;margin:0;">
<div style="max-width:600px;margin:0 auto;">
    <div style="background:#0f2a0f;border:1px solid #22c55e;border-radius:8px;padding:16px;">
        <h2 style="color:#22c55e;margin:0 0 8px 0;">Dashboard is BACK UP</h2>
        <p style="margin:0;">{DASHBOARD_URL} is responding normally.</p>
        <p style="margin:8px 0 0 0;color:#94a3b8;">Downtime: {duration} (since {down_since})</p>
    </div>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Red Nun] RECOVERED: Dashboard is back up"
    msg["From"] = gmail_user
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, [RECIPIENT], msg.as_string())
        print(f"Recovery alert sent to {RECIPIENT}")
        return True
    except Exception:
        return False


def main():
    is_up, result = check_dashboard()
    state = load_state()

    if is_up:
        if state["is_down"] and state.get("alerted"):
            send_recovery_alert(state.get("since", "unknown"))
        if state["is_down"]:
            print(f"Dashboard recovered at {datetime.now().isoformat()}")
        else:
            print(f"Dashboard OK — HTTP {result}")
        save_state({"is_down": False, "since": None, "alerted": False})
    else:
        now = datetime.now().isoformat()
        if not state["is_down"]:
            # First failure detection
            print(f"Dashboard DOWN: {result}")
            sent = send_down_alert(result)
            save_state({"is_down": True, "since": now, "alerted": sent})
        else:
            # Already known to be down — don't re-alert
            print(f"Dashboard still down (since {state.get('since', '?')}): {result}")


if __name__ == "__main__":
    main()
