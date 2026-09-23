#!/usr/bin/env python3
"""
jarvis_controller_export.py — nightly controller snapshot for Jarvis (job 041).

Why this exists
---------------
The controller watchlist (JARVIS_CONTROLLER_WATCHLIST.md, Section A) reads
dashboard endpoints that sit behind Mike's browser login. Scheduled Jarvis runs
live in Anthropic's cloud, which cannot reach dashboard.rednun.com with an auth
header at all — so a service token would give them nothing. Instead this script
runs ON the Beelink, calls the same Flask routes in-process through a test
client (no network, no token, no session cookie leaves the box), and drops the
answers into the Drive-synced folder next to jarvis_export.py's CSVs. Any Jarvis
session then reads jarvis_exports/jarvis_controller_snapshot.json.

What it does
------------
1. Builds a throwaway Flask app from the dashboard's blueprints (NOT web/server.py,
   which starts schedulers on import).
2. Enumerates every GET rule under /api/ that takes no path parameters, minus a
   deny-list of anything that uploads/imports/syncs/exports/prints — GET-only,
   read-only by construction. Rules with <int:account_id> are called once per
   bank account.
3. Calls each with an in-process admin session and stores status + JSON body
   (capped) keyed by path.
4. Adds the A/P watchlist checks (Section B) as plain read-only SQL.
5. Writes jarvis_exports/jarvis_controller_snapshot.json + a short .md index.

Runs from cron at 3:45am after jarvis_export.py. Safe to run by hand any time:
    cd /opt/red-nun-dashboard && venv/bin/python3 jarvis_controller_export.py
"""

import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.abspath(__file__))
os.chdir(REPO)
sys.path.insert(0, REPO)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO, ".env"))
except Exception:
    pass

OUT_DIR = os.environ.get(
    "JARVIS_EXPORT_DIR", "/home/rednun/cowork/red-nun-dashboard/jarvis_exports"
)
DB_PATH = os.environ.get("TOAST_DB_PATH") or os.environ.get("DB_PATH") or "/var/lib/rednun/toast_data.db"
MAX_BODY = 1_500_000          # bytes stored per endpoint
CALL_TIMEOUT_NOTE = "in-process; no per-call timeout — heavy routes are deny-listed"

DENY = re.compile(
    r"(upload(?!s)|import|sync|export|download|pdf|csv|raw-text|delete|refresh|dedupe|"
    r"print|login|logout|invite|drive_|email|send|push|/run(?!s)|process|scan|ocr|"
    r"static|stream|image|thumbnail|file|backup|restore|reset|clear|purge|migrate)",
    re.I,
)
PREFIX_ALLOW = ("/api/",)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- app assembly
def build_app():
    from flask import Flask
    app = Flask("jarvis_controller_export", static_folder=None)
    app.config["SECRET_KEY"] = "jarvis-export-local-only"
    app.config["TESTING"] = True
    loaded, failed = [], []
    # (module, attribute) pairs. Missing ones are skipped, not fatal.
    wanted = [
        ("routes.auth_routes", "auth_bp"),
        ("routes.bank_reconcile_routes", "bank_reconcile_bp"),
        ("routes.register_routes", "register_bp"),
        ("routes.report_routes", "report_bp"),
        ("routes.billpay_routes", "billpay_bp"),
        ("routes.payment_routes", "payment_bp"),
        ("routes.invoice_routes", "invoice_bp"),
        ("routes.daily_sales_routes", "daily_sales_bp"),
        ("routes.payroll_routes", "payroll_bp"),
        ("routes.vendor_routes", "vendor_bp"),
    ]
    import importlib
    for mod, attr in wanted:
        try:
            m = importlib.import_module(mod)
            bp = getattr(m, attr)
            app.register_blueprint(bp)
            loaded.append(f"{mod}.{attr}")
        except Exception as e:
            failed.append(f"{mod}.{attr}: {type(e).__name__}: {e}")
    return app, loaded, failed


def bank_account_ids(con):
    try:
        return [r[0] for r in con.execute("SELECT id FROM bank_accounts WHERE COALESCE(active,1)=1 ORDER BY id")]
    except Exception:
        return [1, 2]


def enumerate_calls(app, account_ids):
    """Return (calls, all_get_rules). calls = list of concrete paths to GET."""
    calls, all_rules = [], []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        r = str(rule)
        all_rules.append({"rule": r, "endpoint": rule.endpoint,
                          "methods": sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))})
        if not r.startswith(PREFIX_ALLOW) or DENY.search(r):
            continue
        if "<" not in r:
            calls.append(r)
        elif re.fullmatch(r"[^<]*<int:(bank_)?account_id>[^<]*", r):
            for aid in account_ids:
                calls.append(re.sub(r"<int:(bank_)?account_id>", str(aid), r))
    all_rules.sort(key=lambda x: x["rule"])
    return sorted(set(calls)), all_rules


def snapshot_calls(app, paths):
    out = {}
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 0
        sess["username"] = "jarvis"
        sess["full_name"] = "Jarvis (controller export)"
        sess["role"] = "admin"
        sess["location"] = "both"
    for p in paths:
        t0 = time.time()
        rec = {"status": None, "elapsed_ms": None}
        try:
            resp = client.get(p)
            body = resp.get_data()
            rec["status"] = resp.status_code
            rec["content_type"] = resp.content_type
            rec["bytes"] = len(body)
            if len(body) > MAX_BODY:
                body = body[:MAX_BODY]
                rec["truncated"] = True
            if resp.is_json or (resp.content_type or "").startswith("application/json"):
                try:
                    rec["json"] = json.loads(body.decode("utf-8", "replace"))
                except Exception:
                    rec["text"] = body.decode("utf-8", "replace")[:20000]
            else:
                rec["text"] = body.decode("utf-8", "replace")[:20000]
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["trace"] = traceback.format_exc()[-2000:]
        rec["elapsed_ms"] = int((time.time() - t0) * 1000)
        out[p] = rec
        log(f"GET {p} -> {rec.get('status')} {rec.get('bytes', 0)}B {rec['elapsed_ms']}ms")
    return out


# ---------------------------------------------------------------- Section B: A/P, plain SQL
def ap_checks(con):
    q = lambda s, *a: [dict(r) for r in con.execute(s, a).fetchall()]
    ap = {}
    since14 = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    try:  # B2 — auto_pay_decisions skips nobody reads
        ap["B2_autopay_skips_14d_by_reason"] = q("""
            SELECT reason, COUNT(*) n, ROUND(SUM(COALESCE(invoice_total,0)),2) dollars,
                   MIN(created_at) first_seen, MAX(created_at) last_seen
            FROM auto_pay_decisions
            WHERE decision='skipped' AND created_at >= ?
            GROUP BY reason ORDER BY dollars DESC""", since14)
        ap["B2_autopay_skips_14d_rows"] = q("""
            SELECT id, invoice_id, vendor_name, invoice_total, reason, created_at
            FROM auto_pay_decisions WHERE decision='skipped' AND created_at >= ?
            ORDER BY invoice_total DESC LIMIT 200""", since14)
    except Exception as e:
        ap["B2_error"] = str(e)
    try:  # B6 — duplicate invoice numbers (same vendor, same number, >1 row)
        ap["B6_duplicate_invoice_numbers"] = q("""
            SELECT vendor_name, invoice_number, COUNT(*) n,
                   GROUP_CONCAT(id) ids, ROUND(SUM(COALESCE(total,0)),2) total_sum
            FROM scanned_invoices
            WHERE invoice_number IS NOT NULL AND TRIM(invoice_number) <> ''
            GROUP BY vendor_name, invoice_number HAVING COUNT(*) > 1
            ORDER BY total_sum DESC LIMIT 200""")
    except Exception as e:
        ap["B6_error"] = str(e)
    try:  # B7 — aging of everything not marked paid
        ap["B7_open_invoice_aging_by_vendor"] = q("""
            SELECT vendor_name,
                   COUNT(*) n,
                   ROUND(SUM(COALESCE(balance, total, 0)),2) open_total,
                   ROUND(SUM(CASE WHEN julianday('now')-julianday(invoice_date) > 60
                                  THEN COALESCE(balance,total,0) ELSE 0 END),2) over_60,
                   ROUND(SUM(CASE WHEN julianday('now')-julianday(invoice_date) > 90
                                  THEN COALESCE(balance,total,0) ELSE 0 END),2) over_90,
                   MIN(invoice_date) oldest
            FROM scanned_invoices
            WHERE COALESCE(payment_status,'') NOT IN ('paid','void','voided')
              AND COALESCE(status,'') <> 'rejected'
              AND invoice_date IS NOT NULL
            GROUP BY vendor_name ORDER BY over_60 DESC, open_total DESC LIMIT 200""")
        ap["B7_open_over_60_rows"] = q("""
            SELECT id, location, vendor_name, invoice_number, invoice_date, total, balance,
                   payment_status, status, invoice_type
            FROM scanned_invoices
            WHERE COALESCE(payment_status,'') NOT IN ('paid','void','voided')
              AND COALESCE(status,'') <> 'rejected'
              AND julianday('now')-julianday(invoice_date) > 60
            ORDER BY invoice_date LIMIT 300""")
    except Exception as e:
        ap["B7_error"] = str(e)
    try:  # B1 heuristic — a row whose total equals the sum of the vendor's OTHER open rows
        ap["B1_statement_lookalikes"] = q("""
            SELECT a.id, a.vendor_name, a.invoice_number, a.invoice_date, a.total,
                   ROUND(SUM(b.total),2) sum_of_others, COUNT(b.id) n_others
            FROM scanned_invoices a
            JOIN scanned_invoices b ON b.vendor_name=a.vendor_name AND b.id<>a.id
                 AND COALESCE(b.payment_status,'') NOT IN ('paid','void','voided')
            WHERE COALESCE(a.payment_status,'') NOT IN ('paid','void','voided')
              AND a.invoice_date >= date('now','-120 days')
            GROUP BY a.id HAVING ABS(a.total - SUM(b.total)) < 0.02 AND COUNT(b.id) >= 2
            ORDER BY a.total DESC LIMIT 50""")
    except Exception as e:
        ap["B1_error"] = str(e)
    try:  # B3 — vendor name variants (case/punctuation-insensitive)
        ap["B3_vendor_name_variants"] = q("""
            SELECT LOWER(REPLACE(REPLACE(REPLACE(vendor_name,'.',''),',',''),'  ',' ')) k,
                   GROUP_CONCAT(DISTINCT vendor_name) variants, COUNT(DISTINCT vendor_name) n
            FROM scanned_invoices WHERE vendor_name IS NOT NULL
            GROUP BY k HAVING COUNT(DISTINCT vendor_name) > 1 ORDER BY n DESC LIMIT 100""")
    except Exception as e:
        ap["B3_error"] = str(e)
    return ap


# ---------------------------------------------------------------- main
def main():
    started = datetime.now()
    os.makedirs(OUT_DIR, exist_ok=True)
    snap = {"generated_at": started.isoformat(), "db_path": DB_PATH, "repo": REPO,
            "note": "GET-only, in-process test client with a synthetic admin session; nothing written."}

    app, loaded, failed = build_app()
    snap["blueprints_loaded"] = loaded
    snap["blueprints_failed"] = failed
    log(f"blueprints loaded={len(loaded)} failed={len(failed)}")
    for f in failed:
        log("  FAILED " + f)

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    acct_ids = bank_account_ids(con)
    try:
        snap["bank_accounts"] = [dict(r) for r in con.execute(
            "SELECT id, name, short_name, location, account_last4, active FROM bank_accounts ORDER BY id")]
    except Exception as e:
        snap["bank_accounts"] = f"error: {e}"

    calls, all_rules = enumerate_calls(app, acct_ids)
    snap["all_get_rules"] = all_rules
    snap["called_paths"] = calls
    log(f"{len(all_rules)} GET rules known, {len(calls)} will be called")
    snap["calls"] = snapshot_calls(app, calls)

    log("A/P SQL checks")
    snap["ap"] = ap_checks(con)
    con.close()

    snap["finished_at"] = datetime.now().isoformat()
    snap["elapsed_s"] = round((datetime.now() - started).total_seconds(), 1)

    path = os.path.join(OUT_DIR, "jarvis_controller_snapshot.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, default=str)
    os.replace(tmp, path)

    # short human index
    lines = [f"# Jarvis controller snapshot — {snap['generated_at']}",
             f"db: {DB_PATH}  elapsed: {snap['elapsed_s']}s  size: {os.path.getsize(path):,} bytes", "",
             "| path | status | bytes | ms |", "|---|---|---|---|"]
    for p, r in snap["calls"].items():
        lines.append(f"| `{p}` | {r.get('status') or r.get('error','?')} | {r.get('bytes',0):,} | {r['elapsed_ms']} |")
    lines += ["", "## A/P checks (Section B)"]
    for k, v in snap["ap"].items():
        n = len(v) if isinstance(v, list) else v
        lines.append(f"- {k}: {n if isinstance(n,int) else n}")
    if failed:
        lines += ["", "## Blueprints that failed to load"] + [f"- {f}" for f in failed]
    with open(os.path.join(OUT_DIR, "JARVIS_CONTROLLER_SNAPSHOT.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"wrote {path} ({os.path.getsize(path):,} bytes)")


if __name__ == "__main__":
    main()
