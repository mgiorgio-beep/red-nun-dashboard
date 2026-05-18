#!/usr/bin/env python3
"""
Dashboard Down Check — Red Nun Dashboard
Checks if dashboard.rednun.com returns HTTP 200, with a localhost sanity check
to suppress false positives caused by transient Cloudflare/Comcast path blips.

Logic:
  1. Hit https://dashboard.rednun.com. If it returns 200, all good.
  2. If the public check fails, hit http://127.0.0.1:8080/ (gunicorn direct).
       - If gunicorn answers locally, the server itself is fine. The failure
         is somewhere in the public network path (DNS, Cloudflare, Comcast).
         Log it quietly. DO NOT email.
       - If gunicorn also fails locally, retry the public check once after
         30s. Only if the retry also fails do we send a DOWN alert.

Cron: 7,37 * * * * (every 30 minutes, offset off the :00/:30 stampede)
"""

import json
import os
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv("/opt/red-nun-dashboard/.env")

RECIPIENT = "mgiorgio@rednun.com"
DASHBOARD_URL = "https://dashboard.rednun.com"
LOCAL_URL = "http://127.0.0.1:8080/"
STATE_FILE = Path("/opt/red-nun-dashboard/monitoring/.down_state.json")
LOG_FILE = Path("/opt/red-nun-dashboard/monitoring/down_check.log")
PUBLIC_TIMEOUT = 25       # was 15 — too aggressive for the Cloudflare hairpin
LOCAL_TIMEOUT = 5
RETRY_WAIT = 30


def _classify(exc):
    """Translate a requests exception into a precise error label."""
    if isinstance(exc, requests.exceptions.SSLError):
        return f"SSL error: {exc}"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"Timeout ({PUBLIC_TIMEOUT}s)"
    if isinstance(exc, requests.exceptions.ConnectionError):
        # ConnectionError covers DNS failures, TCP refused, network unreachable,
        # and connection resets. Try to tell them apart from the inner message.
        msg = str(exc).lower()
        if "name or service not known" in msg or "temporary failure in name resolution" in msg:
            return "DNS failure"
        if "connection refused" in msg:
            return "Connection refused"
        if "network is unreachable" in msg:
            return "Network unreachable"
        if "connection reset" in msg:
            return "Connection reset"
        return f"Network error: {exc.__class__.__name__}"
    return str(exc)


def check_url(url, timeout):
    """Returns (is_up, status_code_or_error)."""
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return True, 200
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, _classify(e)


def check_dashboard():
    """Returns (is_up, error_label). Wraps the public check with timeout."""
    return check_url(DASHBOARD_URL, PUBLIC_TIMEOUT)


def check_local():
    """Returns (is_up, error_label). Hits gunicorn directly on 127.0.0.1:8080."""
    return check_url(LOCAL_URL, LOCAL_TIMEOUT)


def log_line(msg):
    """Append a timestamped line to the local log."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass
    print(msg)


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
        This check runs every 30 minutes. Gunicorn was also verified
        unreachable on 127.0.0.1:8080, and a 30-second retry confirmed the
        failure — so this is a real outage, not a network blip.
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
    # ---- Public check (round-trip through Cloudflare + Comcast) ----
    public_up, public_err = check_dashboard()
    state = load_state()

    if public_up:
        # Healthy. If we were previously down + alerted, send a RECOVERED.
        if state["is_down"] and state.get("alerted"):
            send_recovery_alert(state.get("since", "unknown"))
        if state["is_down"]:
            log_line(f"Dashboard recovered (public OK)")
        else:
            log_line(f"Dashboard OK — HTTP 200")
        save_state({"is_down": False, "since": None, "alerted": False})
        return

    # ---- Public failed. Sanity-check the server itself. ----
    local_up, local_err = check_local()

    if local_up:
        # Gunicorn is fine on localhost — the public path is the only thing
        # that failed. This is a network blip (DNS, Cloudflare, or the
        # Comcast hairpin), NOT a server outage. Log it, don't email,
        # and don't change down-state.
        log_line(
            f"Public check failed but local OK — suppressing alert "
            f"(public_err={public_err}, local=HTTP 200)"
        )
        return

    # ---- Both failed. Wait and retry the public check once. ----
    log_line(
        f"Public AND local failed (public={public_err}, local={local_err}); "
        f"retrying public in {RETRY_WAIT}s before alerting"
    )
    time.sleep(RETRY_WAIT)
    public_up2, public_err2 = check_dashboard()

    if public_up2:
        # Came back on retry. Treat as transient. Don't email.
        log_line(f"Public check recovered on retry — suppressing alert")
        return

    # ---- Confirmed outage. Send the alert (once per outage). ----
    now = datetime.now().isoformat()
    if not state["is_down"]:
        log_line(f"Dashboard DOWN (confirmed): {public_err2}")
        sent = send_down_alert(public_err2)
        save_state({"is_down": True, "since": now, "alerted": sent})
    else:
        log_line(f"Dashboard still down (since {state.get('since', '?')}): {public_err2}")


if __name__ == "__main__":
    main()
