#!/usr/bin/env python3
"""
Scraper Diagnostics Writer
==========================
Writes a complete, human- and AI-readable health report for every vendor
scraper into the Drive-synced folder, so a failure can be triaged by simply
OPENING the file — no SSH, no cut-and-paste of commands.

Why this exists: the Beelink bisyncs ~/cowork/red-nun-dashboard/ <-> Google
Drive (= G:\\My Drive\\Red NUn Dashboard) every ~15 min. By dropping the report
into that folder, the report shows up wherever the Drive folder is readable.

What it captures:
  - vendor_session_status for every vendor (status, last scrape, failure reason)
  - the latest run_all.log run summary
  - the full log block for any vendor that failed (so the error is right there)
  - the last alert state

Run from the repo root:
    cd /opt/red-nun-dashboard && venv/bin/python3 monitoring/scraper_diagnostics.py

Suggested cron (hourly + right after the morning scrape):
    25 7 * * *  cd /opt/red-nun-dashboard && venv/bin/python3 monitoring/scraper_diagnostics.py >> /opt/red-nun-dashboard/logs/diagnostics.log 2>&1
    15 * * * *  cd /opt/red-nun-dashboard && venv/bin/python3 monitoring/scraper_diagnostics.py >> /opt/red-nun-dashboard/logs/diagnostics.log 2>&1

Env: TOAST_DB_PATH, DIAG_OUTPUT (override output path), RUN_ALL_LOG.
"""

import os
import re
import sys
import json
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Default output: the Drive-synced cowork mirror (bisyncs to G:\My Drive\Red NUn Dashboard).
DEFAULT_OUTPUT = os.path.expanduser("~/cowork/red-nun-dashboard/diagnostics/scraper_health.md")
RUN_ALL_LOG = os.getenv("RUN_ALL_LOG", os.path.expanduser("~/vendor-scrapers/run_all.log"))
STALE_HOURS = 26


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_REPO_ROOT, ".env"))
    except Exception:
        env_path = os.path.join(_REPO_ROOT, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _read_sessions():
    from integrations.toast.data_store import get_connection
    conn = get_connection()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM vendor_session_status ORDER BY vendor_name"
    ).fetchall()]
    conn.close()
    return rows


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:26], fmt)
        except ValueError:
            continue
    return None


def _age(s):
    dt = _parse_dt(s)
    if not dt:
        return "never"
    d = datetime.now() - dt
    if d.days > 0:
        return f"{d.days}d ago"
    if d.seconds >= 3600:
        return f"{d.seconds // 3600}h ago"
    return f"{d.seconds // 60}m ago"


def _is_problem(row):
    status = (row.get("status") or "").lower()
    last_ok = _parse_dt(row.get("last_successful_scrape"))
    if status == "expired":
        return f"EXPIRED — {row.get('failure_reason') or 'expired'}"
    if last_ok is None:
        return "NEVER scraped successfully"
    from datetime import timedelta
    if last_ok < datetime.now() - timedelta(hours=STALE_HOURS):
        return f"STALE — last success {_age(row.get('last_successful_scrape'))}"
    return None


def _latest_run_text():
    """Return text of the most recent run_all.log run (after the last run header)."""
    if not os.path.exists(RUN_ALL_LOG):
        return ""
    try:
        with open(RUN_ALL_LOG, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return f"(could not read {RUN_ALL_LOG}: {e})"
    markers = [m.start() for m in re.finditer(r"Vendor Scraper Run:", text)]
    return text[markers[-1]:] if markers else text[-8000:]


def _vendor_block(run_text, vendor_display):
    """Extract one vendor's section from the latest run text."""
    start = run_text.find(f"Starting: {vendor_display}")
    if start == -1:
        return None
    nxt = run_text.find("Starting: ", start + 10)
    summary = run_text.find("SUMMARY", start)
    end = min(x for x in (nxt, summary, len(run_text)) if x != -1)
    return run_text[start:end].strip()


# Map session vendor names -> the display name used in run_all.log "Starting:" lines.
_LOG_NAME = {
    "US Foods": "US Foods",
    "Performance Foodservice": "PFG",
    "Colonial Wholesale Beverage": "VTInfo (Colonial)",
    "L. Knife & Son, Inc.": "L. Knife (Connect)",
    "Southern Glazer's Beverage Company (chatham)": "Southern Glazer's (Chatham)",
    "Southern Glazer's Beverage Company (dennis)": "Southern Glazer's (Dennis)",
    "Martignetti Companies": "Martignetti",
    "Craft Collective Inc": "Craft Collective",
    "Cintas": "Cintas",
    "UniFirst": "UniFirst",
}


def build_report():
    sessions = _read_sessions()
    run_text = _latest_run_text()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    problems = [(r, _is_problem(r)) for r in sessions]
    problems = [(r, p) for r, p in problems if p]
    healthy_n = len(sessions) - len(problems)

    out = []
    out.append("# Vendor Scraper Health")
    out.append("")
    out.append(f"_Generated {now} · auto-refreshes hourly. If this looks stale, the diagnostics cron or Drive bisync stopped._")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- Vendors: **{len(sessions)}** · Healthy: **{healthy_n}** · Problems: **{len(problems)}**")
    if problems:
        for r, p in problems:
            out.append(f"- 🔴 **{r.get('vendor_name')}** — {p}")
    else:
        out.append("- ✅ All scrapers healthy.")
    out.append("")

    out.append("## Per-vendor status")
    out.append("")
    out.append("| Vendor | Status | Last scrape OK | Last invoice | Invoices last run | Failure reason |")
    out.append("|---|---|---|---|---|---|")
    for r in sessions:
        out.append("| {} | {} | {} | {} | {} | {} |".format(
            r.get("vendor_name") or "?",
            r.get("status") or "?",
            _age(r.get("last_successful_scrape")),
            _age(r.get("last_invoice_date")),
            r.get("invoices_scraped_last_run") if r.get("invoices_scraped_last_run") is not None else "—",
            (r.get("failure_reason") or "—").replace("|", "/").replace("\n", " ")[:120],
        ))
    out.append("")

    # Full log block for each failing vendor — the error, right here.
    if problems:
        out.append("## Failure detail (from run_all.log latest run)")
        out.append("")
        for r, p in problems:
            disp = _LOG_NAME.get(r.get("vendor_name"), r.get("vendor_name"))
            block = _vendor_block(run_text, disp) if disp else None
            out.append(f"### {r.get('vendor_name')} — {p}")
            out.append("")
            if block:
                out.append("```")
                out.append(block[-3000:])
                out.append("```")
            else:
                out.append("_No matching block in the latest run_all.log (scraper may not have run, or runs separately — e.g. gmail-qbo via its own cron)._")
            out.append("")

    # Latest run summary
    summ_idx = run_text.find("SUMMARY")
    if summ_idx != -1:
        out.append("## Latest run_all.log summary")
        out.append("")
        out.append("```")
        out.append(run_text[summ_idx:summ_idx + 1200].strip())
        out.append("```")
        out.append("")

    # Alert state
    db_dir = os.path.dirname(os.getenv("TOAST_DB_PATH", "/var/lib/rednun/toast_data.db"))
    alert_state = os.path.join(db_dir or ".", "vendor_alert_state.json")
    if os.path.exists(alert_state):
        try:
            with open(alert_state) as f:
                st = json.load(f)
            out.append("## Last alert state")
            out.append("")
            out.append("```json")
            out.append(json.dumps(st, indent=2)[:1500])
            out.append("```")
        except Exception:
            pass

    return "\n".join(out) + "\n"


def main():
    _load_env()
    report = build_report()
    output = os.getenv("DIAG_OUTPUT", DEFAULT_OUTPUT)
    try:
        os.makedirs(os.path.dirname(output), exist_ok=True)
        tmp = output + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(report)
        os.replace(tmp, output)
        print(f"[OK] Wrote diagnostics report to {output} ({len(report)} bytes)")
    except Exception as e:
        print(f"[ERROR] Could not write {output}: {e}")
        print(report)


if __name__ == "__main__":
    main()
