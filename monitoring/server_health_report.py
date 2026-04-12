#!/usr/bin/env python3
"""
Weekly Server Health Report — Red Nun Dashboard
Checks disk, CPU, memory, services, SSL, database, backups.
Emails a formatted report to mgiorgio@rednun.com.

Cron: 0 8 * * 1 (Monday 8 AM)
"""

import os
import shutil
import smtplib
import subprocess
import ssl
import socket
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/opt/red-nun-dashboard/.env")

RECIPIENT = "mgiorgio@rednun.com"
DB_PATH = "/opt/red-nun-dashboard/toast_data.db"
BACKUP_DIR = "/opt/backups"
SERVICES = ["rednun", "nginx"]
DASHBOARD_HOST = "dashboard.rednun.com"


def check_disk():
    usage = shutil.disk_usage("/")
    total_gb = usage.total / (1024 ** 3)
    used_gb = usage.used / (1024 ** 3)
    free_gb = usage.free / (1024 ** 3)
    pct = (usage.used / usage.total) * 100
    status = "OK" if pct < 85 else ("WARN" if pct < 95 else "CRITICAL")
    return {
        "total_gb": round(total_gb, 1),
        "used_gb": round(used_gb, 1),
        "free_gb": round(free_gb, 1),
        "pct": round(pct, 1),
        "status": status,
    }


def check_cpu_memory():
    # Load average
    load1, load5, load15 = os.getloadavg()

    # Memory from /proc/meminfo
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip().split()[0]
                meminfo[key] = int(val)

    mem_total = meminfo.get("MemTotal", 0) / 1024  # MB
    mem_avail = meminfo.get("MemAvailable", 0) / 1024
    mem_used = mem_total - mem_avail
    mem_pct = (mem_used / mem_total * 100) if mem_total > 0 else 0

    return {
        "load_1": round(load1, 2),
        "load_5": round(load5, 2),
        "load_15": round(load15, 2),
        "mem_total_mb": round(mem_total),
        "mem_used_mb": round(mem_used),
        "mem_avail_mb": round(mem_avail),
        "mem_pct": round(mem_pct, 1),
    }


def check_services():
    results = []
    for svc in SERVICES:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            active = r.stdout.strip()
        except Exception as e:
            active = f"error: {e}"
        results.append({"name": svc, "status": active})
    return results


def check_ssl():
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=DASHBOARD_HOST) as s:
            s.settimeout(10)
            s.connect((DASHBOARD_HOST, 443))
            cert = s.getpeercert()
        not_after = cert.get("notAfter", "")
        # Parse: 'May 10 15:33:15 2026 GMT'
        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
        days_left = (expiry.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        status = "OK" if days_left > 14 else ("WARN" if days_left > 3 else "CRITICAL")
        return {
            "expiry": expiry.strftime("%Y-%m-%d"),
            "days_left": days_left,
            "status": status,
        }
    except Exception as e:
        return {"expiry": "unknown", "days_left": -1, "status": f"ERROR: {e}"}


def check_database():
    try:
        size_bytes = os.path.getsize(DB_PATH)
        size_gb = size_bytes / (1024 ** 3)
        mtime = datetime.fromtimestamp(os.path.getmtime(DB_PATH))
        age_min = (datetime.now() - mtime).total_seconds() / 60
        return {
            "size_gb": round(size_gb, 2),
            "last_modified": mtime.strftime("%Y-%m-%d %H:%M"),
            "age_min": round(age_min),
        }
    except Exception as e:
        return {"size_gb": 0, "last_modified": "unknown", "age_min": -1, "error": str(e)}


def check_backups():
    backup_dir = Path(BACKUP_DIR)
    if not backup_dir.exists():
        return {"last_backup": "none", "days_ago": -1, "status": "CRITICAL"}

    db_backups = sorted(backup_dir.glob("toast_data_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not db_backups:
        return {"last_backup": "none", "days_ago": -1, "status": "CRITICAL"}

    latest = db_backups[0]
    mtime = datetime.fromtimestamp(latest.stat().st_mtime)
    days_ago = (datetime.now() - mtime).days
    size_gb = latest.stat().st_size / (1024 ** 3)
    status = "OK" if days_ago <= 2 else ("WARN" if days_ago <= 7 else "CRITICAL")

    return {
        "last_backup": mtime.strftime("%Y-%m-%d %H:%M"),
        "file": latest.name,
        "size_gb": round(size_gb, 2),
        "days_ago": days_ago,
        "status": status,
    }


def check_uptime():
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days = int(secs // 86400)
        hours = int((secs % 86400) // 3600)
        return f"{days}d {hours}h"
    except Exception:
        return "unknown"


def build_report():
    disk = check_disk()
    cpu_mem = check_cpu_memory()
    services = check_services()
    ssl_info = check_ssl()
    db = check_database()
    backup = check_backups()
    uptime = check_uptime()

    now = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")

    # Determine overall status
    issues = []
    if disk["status"] != "OK":
        issues.append(f"Disk {disk['status']}: {disk['pct']}% used")
    if cpu_mem["mem_pct"] > 90:
        issues.append(f"Memory high: {cpu_mem['mem_pct']}%")
    for svc in services:
        if svc["status"] != "active":
            issues.append(f"Service {svc['name']}: {svc['status']}")
    if ssl_info["status"] != "OK":
        issues.append(f"SSL {ssl_info['status']}: {ssl_info['days_left']} days left")
    if backup["status"] != "OK":
        issues.append(f"Backup {backup['status']}: {backup['days_ago']} days ago")

    overall = "ALL CLEAR" if not issues else "ISSUES DETECTED"

    def status_dot(s):
        if s in ("OK", "active"):
            return '<span style="color:#22c55e;">&#9679;</span>'
        elif "WARN" in str(s):
            return '<span style="color:#f59e0b;">&#9679;</span>'
        else:
            return '<span style="color:#ef4444;">&#9679;</span>'

    svc_rows = ""
    for svc in services:
        svc_rows += f"<tr><td>{svc['name']}</td><td>{status_dot(svc['status'])} {svc['status']}</td></tr>"

    issue_section = ""
    if issues:
        items = "".join(f"<li>{i}</li>" for i in issues)
        issue_section = f"""
        <div style="background:#451a03;border:1px solid #f59e0b;border-radius:8px;padding:12px 16px;margin-bottom:20px;">
            <strong style="color:#f59e0b;">Issues:</strong>
            <ul style="margin:8px 0 0 0;padding-left:20px;">{items}</ul>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,system-ui,sans-serif;background:#020617;color:#e2e8f0;padding:20px;margin:0;">
    <div style="max-width:600px;margin:0 auto;">

    <h2 style="color:#38bdf8;margin-bottom:4px;">Red Nun Server Health Report</h2>
    <p style="color:#94a3b8;margin-top:0;">{now} &middot; Uptime: {uptime}</p>

    <div style="background:{'#0f2a0f' if not issues else '#2a1a0f'};border:1px solid {'#22c55e' if not issues else '#f59e0b'};border-radius:8px;padding:12px 16px;margin-bottom:20px;">
        <strong style="color:{'#22c55e' if not issues else '#f59e0b'};font-size:18px;">{overall}</strong>
    </div>

    {issue_section}

    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">Disk Usage</td>
        <td style="padding:8px 0;">{status_dot(disk['status'])} {disk['used_gb']} / {disk['total_gb']} GB ({disk['pct']}%) &mdash; {disk['free_gb']} GB free</td>
    </tr>
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">CPU Load</td>
        <td style="padding:8px 0;">{cpu_mem['load_1']} / {cpu_mem['load_5']} / {cpu_mem['load_15']} (1/5/15 min)</td>
    </tr>
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">Memory</td>
        <td style="padding:8px 0;">{cpu_mem['mem_used_mb']} / {cpu_mem['mem_total_mb']} MB ({cpu_mem['mem_pct']}%)</td>
    </tr>
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">Database</td>
        <td style="padding:8px 0;">{db['size_gb']} GB &mdash; last modified {db['last_modified']}</td>
    </tr>
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">Last Backup</td>
        <td style="padding:8px 0;">{status_dot(backup['status'])} {backup.get('file', 'none')} &mdash; {backup['last_backup']} ({backup['days_ago']}d ago)</td>
    </tr>
    <tr style="border-bottom:1px solid #1e293b;">
        <td style="padding:8px 0;color:#94a3b8;">SSL Certificate</td>
        <td style="padding:8px 0;">{status_dot(ssl_info['status'])} Expires {ssl_info['expiry']} ({ssl_info['days_left']} days)</td>
    </tr>
    </table>

    <h3 style="color:#38bdf8;margin-bottom:8px;">Services</h3>
    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
    {svc_rows}
    </table>

    <p style="color:#475569;font-size:12px;margin-top:30px;">
        Red Nun Dashboard &middot; Beelink SER5 &middot; dashboard.rednun.com
    </p>

    </div>
</body>
</html>"""

    return html, overall, issues


def send_report():
    gmail_user = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_password:
        print("ERROR: GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env")
        return False

    html, overall, issues = build_report()

    subject = f"[Red Nun] Server Health: {overall}"
    if issues:
        subject = f"[Red Nun] Server Health: {len(issues)} issue(s)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, [RECIPIENT], msg.as_string())
        print(f"Health report sent to {RECIPIENT} — {overall}")
        return True
    except Exception as e:
        print(f"ERROR sending report: {e}")
        return False


if __name__ == "__main__":
    send_report()
