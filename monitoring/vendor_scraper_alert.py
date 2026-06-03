#!/usr/bin/env python3
"""
Vendor Scraper Health Alert
===========================
Cross-vendor safety net. Reads vendor_session_status (the same table that
powers the Vendor Scrapers dashboard page) and sends ONE Telegram digest when
a scraper is expired or has gone stale (no successful scrape in STALE_HOURS).

Designed to turn silent scraper drift into an immediate ping, so we stop
discovering missing invoices weeks later.

Anti-spam: state is cached in STATE_FILE. An alert is only sent when the set of
problem vendors CHANGES (a new problem appears, or one recovers) — not every run.

Run from the repo root so `integrations` is importable:
    cd /opt/red-nun-dashboard && venv/bin/python3 monitoring/vendor_scraper_alert.py

Suggested cron (after the 7am run_all.sh, plus a midday check):
    20 7 * * *  cd /opt/red-nun-dashboard && venv/bin/python3 monitoring/vendor_scraper_alert.py >> /opt/red-nun-dashboard/logs/vendor_alert.log 2>&1
    0 13 * * *  cd /opt/red-nun-dashboard && venv/bin/python3 monitoring/vendor_scraper_alert.py >> /opt/red-nun-dashboard/logs/vendor_alert.log 2>&1

Env (from .env): TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID, TOAST_DB_PATH.
"""

import os
import sys
import json
from datetime import datetime, timedelta

# Make the repo root importable when run as a script (sys.path[0] is monitoring/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import requests

# ── Config ───────────────────────────────────────────────────────────────────
# A scraper that hasn't had a SUCCESSFUL scrape in this many hours is "stale".
# Scrapers run daily, so 26h gives a little slack past a missed window.
STALE_HOURS = 26
DASHBOARD_URL = os.getenv("DASHBOARD_PUBLIC_URL", "https://dashboard.rednun.com")


def _load_env():
    """Load .env from the repo root without hard-depending on python-dotenv."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_REPO_ROOT, ".env"))
        return
    except Exception:
        pass
    env_path = os.path.join(_REPO_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _state_file():
    """Cache alert state next to the DB (writable, persistent)."""
    db_path = os.getenv("TOAST_DB_PATH", "/var/lib/rednun/toast_data.db")
    return os.path.join(os.path.dirname(db_path) or ".", "vendor_alert_state.json")


def _read_state():
    try:
        with open(_state_file()) as f:
            return json.load(f)
    except Exception:
        return {"problems": {}}


def _write_state(state):
    path = _state_file()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[WARN] Could not write state file {path}: {e}")


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:26], fmt)
        except ValueError:
            continue
    return None


def _age_str(dt):
    if not dt:
        return "never"
    delta = datetime.now() - dt
    days = delta.days
    hrs = delta.seconds // 3600
    if days > 0:
        return f"{days}d ago"
    if hrs > 0:
        return f"{hrs}h ago"
    return f"{delta.seconds // 60}m ago"


def find_problems():
    """Return {vendor_name: reason} for every expired or stale scraper."""
    from integrations.toast.data_store import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM vendor_session_status ORDER BY vendor_name"
    ).fetchall()
    conn.close()

    cutoff = datetime.now() - timedelta(hours=STALE_HOURS)
    problems = {}
    for r in rows:
        r = dict(r)
        vendor = r.get("vendor_name") or "?"
        status = (r.get("status") or "").lower()
        last_ok = _parse_dt(r.get("last_successful_scrape"))

        if status == "expired":
            reason = r.get("failure_reason") or "expired"
            problems[vendor] = f"EXPIRED — {reason} (last ok {_age_str(last_ok)})"
        elif last_ok is None:
            problems[vendor] = "NEVER scraped successfully"
        elif last_ok < cutoff:
            problems[vendor] = f"STALE — no successful scrape in over {STALE_HOURS}h (last {_age_str(last_ok)})"
    return problems


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[WARN] TELEGRAM_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID not set — printing instead:")
        print(text)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code == 200:
            print("[OK] Telegram alert sent")
            return True
        print(f"[WARN] Telegram returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")
    return False


def main():
    _load_env()
    problems = find_problems()
    state = _read_state()
    prev = state.get("problems", {})

    new = sorted(set(problems) - set(prev))
    resolved = sorted(set(prev) - set(problems))
    changed = bool(new or resolved)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[{stamp}] problems={len(problems)} new={len(new)} resolved={len(resolved)}")

    if problems and changed:
        lines = [f"⚠️ Vendor scraper alert ({len(problems)} need attention)", ""]
        for v in sorted(problems):
            flag = "🆕 " if v in new else ""
            lines.append(f"{flag}• {v}: {problems[v]}")
        if resolved:
            lines.append("")
            lines.append("✅ Recovered: " + ", ".join(resolved))
        lines.append("")
        lines.append(f"{DASHBOARD_URL}  →  Dashboard → Vendor Scrapers")
        send_telegram("\n".join(lines))
    elif not problems and resolved:
        send_telegram(
            "✅ All vendor scrapers healthy again.\nRecovered: " + ", ".join(resolved)
        )
    else:
        print("[OK] No change since last run — no alert sent.")

    state["problems"] = problems
    state["last_run"] = datetime.now().isoformat()
    _write_state(state)


if __name__ == "__main__":
    main()
