#!/usr/bin/env python3
"""
Toast Online Ordering Monitor — Red Nun Dashboard
Checks that www.rednun.com is serving Toast ordering correctly.
Detects both failure modes:
  1. Cloudflare proxy flip on www (immediate 403 host_not_allowed)
  2. SSL/DCV failure from _acme-challenge proxy flip (delayed cert break)

Only emails on failure. Tracks state to avoid repeat alerts.

Cron: */5 * * * * /opt/rednun/venv/bin/python3 /opt/red-nun-dashboard/monitoring/check_toast_ordering.py
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

load_dotenv("/opt/red-nun-dashboard/.env")

RECIPIENT = "mgiorgio@rednun.com"
ORDERING_URL = "https://www.rednun.com/"
STATE_FILE = Path("/opt/red-nun-dashboard/monitoring/.toast_ordering_state.json")
TIMEOUT = 15


def check_ordering():
    """Returns (is_ok, detail_string).

    Healthy: Toast returns 200/301/302 without 'host_not_allowed'.
    Broken:  403 with x-deny-reason: host_not_allowed (proxy flip),
             SSL error (cert expired / DCV failure),
             or connection refused / timeout.
    """
    try:
        r = requests.get(ORDERING_URL, timeout=TIMEOUT, allow_redirects=False)
        deny = r.headers.get("x-deny-reason", "")
        if deny == "host_not_allowed":
            return False, f"HTTP {r.status_code} — x-deny-reason: host_not_allowed (likely www CNAME proxied in Cloudflare)"
        if r.status_code in (200, 301, 302, 303, 307, 308):
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}"
    except requests.exceptions.SSLError as e:
        return False, f"SSL error (likely _acme-challenge CNAME proxied — cert renewal failed): {e}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
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
        <h2 style="color:#ef4444;margin:0 0 8px 0;">TOAST ORDERING IS DOWN</h2>
        <p style="margin:0;color:#fca5a5;">Online ordering at www.rednun.com is not working. Customers cannot place orders.</p>
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
    </table>
    <div style="margin-top:20px;padding:12px;background:#0f172a;border-radius:8px;">
        <strong style="color:#94a3b8;">Likely cause &amp; fix:</strong>
        <p style="color:#e2e8f0;margin:8px 0 0 0;font-size:14px;">
            Someone edited a DNS record in Cloudflare and it flipped to <strong>Proxied</strong> (orange cloud).
        </p>
        <ol style="color:#38bdf8;margin:8px 0 0 0;font-size:13px;padding-left:20px;">
            <li>Open Cloudflare &rarr; rednun.com zone &rarr; DNS &rarr; Records</li>
            <li>Check <code>www</code> CNAME and <code>_acme-challenge</code> CNAME</li>
            <li>Both MUST be <strong>DNS only (grey cloud)</strong></li>
            <li>If either is orange/proxied, click it grey and Save</li>
        </ol>
        <p style="color:#94a3b8;margin:8px 0 0 0;font-size:12px;">
            Expected values: www &rarr; sites.toasttab.com | _acme-challenge &rarr; rednun.com.cec5188867cef154.dcv.cloudflare.com
        </p>
    </div>
    <p style="color:#475569;font-size:12px;margin-top:20px;">
        This check runs every 5 minutes. You will receive one alert per outage, plus a recovery notice.
    </p>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "[Red Nun] ALERT: Toast online ordering is DOWN"
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
        <h2 style="color:#22c55e;margin:0 0 8px 0;">Toast Ordering is BACK UP</h2>
        <p style="margin:0;">www.rednun.com is serving orders normally.</p>
        <p style="margin:8px 0 0 0;color:#94a3b8;">Downtime: {duration} (since {down_since})</p>
    </div>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "[Red Nun] RECOVERED: Toast ordering is back up"
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
    is_ok, detail = check_ordering()
    state = load_state()

    if is_ok:
        if state["is_down"] and state.get("alerted"):
            send_recovery_alert(state.get("since", "unknown"))
        if state["is_down"]:
            print(f"Toast ordering recovered at {datetime.now().isoformat()}")
        else:
            print(f"Toast ordering OK — {detail}")
        save_state({"is_down": False, "since": None, "alerted": False})
    else:
        now = datetime.now().isoformat()
        if not state["is_down"]:
            print(f"Toast ordering DOWN: {detail}")
            sent = send_down_alert(detail)
            save_state({"is_down": True, "since": now, "alerted": sent})
        else:
            print(f"Toast ordering still down (since {state.get('since', '?')}): {detail}")


if __name__ == "__main__":
    main()
