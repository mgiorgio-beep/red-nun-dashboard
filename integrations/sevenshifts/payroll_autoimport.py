#!/usr/bin/env python3
"""
7shifts Payroll Auto-Import — books submitted payroll runs into the dashboard
and queues paper checks for the home print agent.

Flow (per cron cycle, per location):
  1. List recent payroll runs from 7shifts
     (GET /api/v2/company/{cid}/payrolls?payroll_company_uuid=..&statuses=..)
  2. For each unseen, non-void run with payday >= EPOCH:
       - download the payroll-journal CSV (available even while Pending)
       - if the run has paper checks, download the paper-checks PDF
         (GET /payrolls/{uuid}/pay_checks — only after processing; defer
         the run to a later cycle if the PDF isn't ready yet)
       - POST both to the dashboard's own upload route on localhost
         (X-Internal-Key = PRINT_AGENT_API_KEY; see admin_or_internal_required
         in routes/payroll_routes.py)
       - queue the run's paper checks as print_jobs for the home agent
  3. Email a summary of everything booked/queued to Mike.

Safety nets (lessons from the payment-scraper era):
  - Manifest dedup: each 7shifts payroll uuid is booked exactly once
    (.payroll_autoimport_state.json), PLUS a DB gate that refuses to book a
    run when a payroll_runs row already matches location+pay_date+gross.
  - EPOCH guard: runs paid before 2026-07-11 are never touched (history was
    booked by hand).
  - Caps: max 2 runs booked per cycle, max 60 checks per run.
  - Exact-location check config for printing — no cross-location fallback
    (the Dennis-checks-on-Chatham-account lesson). Fails loudly.
  - Kill switch:  touch /opt/red-nun-dashboard/.payroll_autoimport_disabled
  - Dry run:      touch /opt/red-nun-dashboard/.payroll_autoimport_dry_run
    (fetches + logs + emails what it WOULD do; books nothing, prints nothing)

Auth against 7shifts, tried in order:
  1. Bearer token from .env (SEVENSHIFTS_TOKEN_CHATHAM / SEVENSHIFTS_TOKEN_DENNIS
     / SEVENSHIFTS_ACCESS_TOKEN) — works if the token's scopes cover payroll.
  2. Webapp session cookies from /opt/red-nun-dashboard/.7shifts_cookies.json
     (Cookie-Editor JSON export of an app.7shifts.com session, same recipe as
     VTInfo). On auth failure an alert email goes out (at most one per day).

Cron (add to the rednun user's crontab):
  */15 * * * * /opt/red-nun-dashboard/venv/bin/python3 \
      /opt/red-nun-dashboard/integrations/sevenshifts/payroll_autoimport.py \
      >> /opt/red-nun-dashboard/monitoring/payroll_autoimport.log 2>&1
"""

import os
import sys
import json
import logging
import smtplib
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText

sys.path.insert(0, "/opt/red-nun-dashboard")

import requests
from dotenv import load_dotenv

load_dotenv("/opt/red-nun-dashboard/.env")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("payroll_autoimport")

# ── Config ────────────────────────────────────────────────────────────────────

APP_BASE      = "https://app.7shifts.com/api/v2"
COMPANIES     = {"chatham": 382225, "dennis": 87880}
# payroll_company_uuid per location — discovered dynamically on first run via
# GET /company/{cid}/payroll_companies and cached in state; these are fallbacks.
PCU_FALLBACK  = {
    "chatham": "55234c6a-130c-4a41-a324-a7438a45bcc9",   # Red Buoy Inc
    "dennis":  "57237594-25df-4802-b3cd-8f3545ff0127",   # Red Nun Public House Inc
}

STATE_PATH    = "/opt/red-nun-dashboard/.payroll_autoimport_state.json"
KILL_SWITCH   = "/opt/red-nun-dashboard/.payroll_autoimport_disabled"
DRY_RUN_FLAG  = "/opt/red-nun-dashboard/.payroll_autoimport_dry_run"
COOKIE_PATH   = "/opt/red-nun-dashboard/.7shifts_cookies.json"
AUDIT_LOG     = "/opt/red-nun-dashboard/monitoring/payroll_autoimport_audit.jsonl"
CHECK_PDF_DIR = "/var/lib/rednun/check_pdfs"

DASHBOARD     = "http://127.0.0.1:8080"
INTERNAL_KEY  = os.environ.get("PRINT_AGENT_API_KEY", "")

# Runs paid before this date are never touched (booked by hand already).
EPOCH               = "2026-07-11"
MAX_RUNS_PER_CYCLE  = 2
MAX_CHECKS_PER_RUN  = 60
LOOKBACK_DAYS       = 45

ALERT_TO   = os.environ.get("REPORT_TO_EMAIL", "mgiorgio@rednun.com")
ALERT_FROM = os.environ.get("REPORT_FROM_EMAIL", "dashboard@rednun.com")

# statuses= accepts a comma list; brackets form ("statuses[]=") 500s.
STATUSES_FULL     = "pending,processing,paid"
STATUSES_FALLBACK = "pending,paid"


# ── Small helpers ─────────────────────────────────────────────────────────────

def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"processed": {}, "deferred": {}, "pcu": {}, "last_auth_alert": ""}


def _save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _audit(entry):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        entry["ts"] = datetime.utcnow().isoformat()
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"audit write failed: {e}")


def _send_email(subject, body, content_type="plain"):
    """Best-effort SMTP send — same env vars as the receipt poller."""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    if not (smtp_user and smtp_pass):
        logger.warning(f"Email skipped (SMTP not configured): {subject}")
        return
    try:
        msg = MIMEText(body, content_type)
        msg["Subject"] = subject
        msg["From"] = ALERT_FROM
        msg["To"] = ALERT_TO
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(ALERT_FROM, [ALERT_TO], msg.as_string())
        logger.info(f"Alert email sent: {subject}")
    except Exception as e:
        logger.error(f"Alert email failed ({subject}): {e}")


# ── 7shifts auth ──────────────────────────────────────────────────────────────

def _cookie_session():
    """Build a requests session from a Cookie-Editor JSON export, or None."""
    try:
        with open(COOKIE_PATH) as f:
            cookies = json.load(f)
    except Exception:
        return None
    s = requests.Session()
    for c in cookies:
        try:
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain", ".7shifts.com"),
                          path=c.get("path", "/"))
        except Exception:
            continue
    s.headers.update({"Accept": "application/json",
                      "User-Agent": "Mozilla/5.0 (rednun payroll importer)"})
    return s


def _bearer_session(location):
    token = (os.environ.get(f"SEVENSHIFTS_TOKEN_{location.upper()}")
             or os.environ.get("SEVENSHIFTS_ACCESS_TOKEN"))
    if not token:
        return None
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}",
                      "Accept": "application/json"})
    return s


def _auth_ok(sess, cid):
    """Probe an endpoint that requires payroll access."""
    try:
        r = sess.get(f"{APP_BASE}/company/{cid}/payroll_companies", timeout=30)
        return r.status_code == 200 and "data" in r.json()
    except Exception:
        return False


def get_session(location, state):
    """Return (session, mode) or (None, None). Tries Bearer, then cookies."""
    cid = COMPANIES[location]
    s = _bearer_session(location)
    if s is not None and _auth_ok(s, cid):
        return s, "bearer"
    s = _cookie_session()
    if s is not None and _auth_ok(s, cid):
        return s, "cookies"
    # Alert at most once per calendar day
    today = date.today().isoformat()
    if state.get("last_auth_alert") != today:
        state["last_auth_alert"] = today
        _send_email(
            "⚠️ Payroll auto-import: 7shifts login expired",
            "The payroll auto-importer can't reach 7shifts (Bearer token and "
            "session cookies both failed).\n\nFix: log into app.7shifts.com in "
            "Chrome, export cookies with Cookie-Editor (JSON), and save them to "
            f"{COOKIE_PATH} on the Beelink — same recipe as VTInfo.\n\n"
            "No payroll runs will be booked until this is fixed.")
    return None, None


# ── 7shifts fetches ───────────────────────────────────────────────────────────

def get_pcu(sess, location, cid, state):
    """payroll_company_uuid for a location, cached in state."""
    cached = state.setdefault("pcu", {}).get(location)
    if cached:
        return cached
    try:
        r = sess.get(f"{APP_BASE}/company/{cid}/payroll_companies", timeout=30)
        pcu = r.json()["data"][0]["uuid"]
        state["pcu"][location] = pcu
        return pcu
    except Exception as e:
        logger.warning(f"{location}: payroll_companies failed ({e}); "
                       f"using fallback uuid")
        return PCU_FALLBACK[location]


def list_runs(sess, cid, pcu):
    for statuses in (STATUSES_FULL, STATUSES_FALLBACK):
        r = sess.get(f"{APP_BASE}/company/{cid}/payrolls",
                     params={"payroll_company_uuid": pcu, "statuses": statuses},
                     timeout=60)
        if r.status_code == 200:
            return r.json().get("data", [])
        logger.warning(f"payrolls statuses={statuses} -> HTTP {r.status_code}")
    raise RuntimeError(f"payrolls list failed for company {cid}")


def fetch_journal_csv(sess, cid, pcu, payroll_uuid):
    r = sess.get(f"{APP_BASE}/company/{cid}/payroll_journal",
                 params={"payroll_company_uuid": pcu,
                         "include_taxable_wages": "false",
                         "payroll_uuid": payroll_uuid,
                         "group_by_location": "false"},
                 timeout=120)
    if r.status_code != 200 or not r.text.strip():
        return None
    if "Last Name" not in r.text[:500]:
        logger.warning(f"journal for {payroll_uuid}: unexpected content")
        return None
    return r.text


def fetch_checks_pdf(sess, cid, payroll_uuid):
    r = sess.get(f"{APP_BASE}/company/{cid}/payrolls/{payroll_uuid}/pay_checks",
                 timeout=120)
    if r.status_code != 200 or not r.content[:5].startswith(b"%PDF"):
        return None
    return r.content


# ── Dashboard side ────────────────────────────────────────────────────────────

def db_already_booked(location, pay_date, total_gross, payroll_uuid):
    """Second dedup gate straight against the dashboard DB."""
    from integrations.toast.data_store import get_connection
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM payroll_runs WHERE memo LIKE ? LIMIT 1",
            (f"%{payroll_uuid}%",)).fetchone()
        if row:
            return row["id"]
        row = conn.execute(
            """SELECT id FROM payroll_runs
               WHERE location = ? AND pay_date = ?
                 AND ABS(total_gross - ?) < 0.011 LIMIT 1""",
            (location, pay_date, float(total_gross))).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def book_run(location, run, csv_text, pdf_bytes):
    """POST to the dashboard's own upload route. Returns response dict."""
    memo = (f"Payroll {run['period_start']}–{run['period_end']} "
            f"({run['type']}, 7shifts {run['uuid']})")
    files = {"journal_csv": (f"journal_{run['uuid']}.csv",
                             csv_text.encode("utf-8"), "text/csv")}
    if pdf_bytes:
        files["checks_pdf"] = (f"checks_{run['uuid']}.pdf",
                               pdf_bytes, "application/pdf")
    r = requests.post(
        f"{DASHBOARD}/api/payroll/runs",
        data={"location": location, "memo": memo},
        files=files,
        headers={"X-Internal-Key": INTERNAL_KEY},
        timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"dashboard upload HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def queue_paper_checks(run_id, location):
    """Assign check numbers, render PDFs, and queue print_jobs for the home
    agent — mirrors /api/print-queue/print + auto_pay's print_jobs pattern."""
    from integrations.toast.data_store import get_connection
    from check_printer import generate_payroll_check_pdf

    conn = get_connection()
    queued = []
    try:
        checks = conn.execute(
            """SELECT * FROM payroll_checks
               WHERE payroll_run_id = ?
                 AND (payment_method IS NULL OR LOWER(payment_method) != 'direct deposit')
                 AND COALESCE(net_pay, 0) > 0
                 AND COALESCE(voided, 0) = 0
                 AND check_number IS NULL
               ORDER BY employee_name""", (run_id,)).fetchall()
        if not checks:
            return []
        if len(checks) > MAX_CHECKS_PER_RUN:
            raise RuntimeError(
                f"run {run_id}: {len(checks)} checks exceeds cap "
                f"{MAX_CHECKS_PER_RUN} — refusing to auto-print")

        # Exact location match only — never fall back to another location's
        # bank account (that's how Dennis checks once printed on Chatham).
        config = conn.execute(
            "SELECT * FROM check_config WHERE location = ?",
            (location,)).fetchone()
        if not config:
            raise RuntimeError(f"no check_config for '{location}' — not printing")
        config_d = dict(config)
        next_num = int(config_d.get("check_number_next") or 2001)

        os.makedirs(CHECK_PDF_DIR, exist_ok=True)
        for c in checks:
            c = dict(c)
            num = str(next_num)
            pdf_path = os.path.join(
                CHECK_PDF_DIR, f"payroll_check_{c['id']}_{num}.pdf")
            generate_payroll_check_pdf(
                payroll={**c, "check_number": num},
                config=config_d,
                check_number=num,
                output_path=pdf_path)
            conn.execute(
                "UPDATE payroll_checks SET check_number = ?, "
                "printed_at = datetime('now') WHERE id = ?", (num, c["id"]))
            conn.execute(
                """INSERT INTO print_jobs
                   (kind, check_number, location, pdf_path, status)
                   VALUES ('check', ?, ?, ?, 'pending')""",
                (num, location, pdf_path))
            queued.append({"employee": c.get("employee_name"),
                           "net": c.get("net_pay"), "check_number": num})
            next_num += 1

        conn.execute(
            "UPDATE check_config SET check_number_next = ? WHERE location = ?",
            (next_num, location))
        conn.commit()
        return queued
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Main cycle ────────────────────────────────────────────────────────────────

def process_location(location, state, dry_run, results):
    cid = COMPANIES[location]
    sess, mode = get_session(location, state)
    if sess is None:
        results["errors"].append(f"{location}: no working 7shifts auth")
        return
    logger.info(f"{location}: authenticated via {mode}")

    pcu = get_pcu(sess, location, cid, state)
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    try:
        runs = list_runs(sess, cid, pcu)
    except Exception as e:
        results["errors"].append(f"{location}: run list failed: {e}")
        return

    for run in runs:
        uuid = run.get("uuid")
        payday = run.get("payday") or ""
        totals = run.get("totals") or {}
        if not uuid or uuid in state["processed"]:
            continue
        if payday < EPOCH or payday < cutoff:
            continue
        if run.get("is_void") or run.get("void_of"):
            results["notes"].append(
                f"{location}: VOID run {uuid} ({payday}) seen — handle manually")
            state["processed"][uuid] = {"skipped": "void", "seen": payday}
            continue
        if results["booked_count"] >= MAX_RUNS_PER_CYCLE:
            results["notes"].append(
                f"{location}: per-cycle cap reached; {uuid} waits for next run")
            continue

        gross = totals.get("employee_gross", 0)
        paper = int(totals.get("manual_payment_count") or 0)

        existing = db_already_booked(location, payday, gross, uuid)
        if existing:
            logger.info(f"{location}: {uuid} already in dashboard (run "
                        f"#{existing}) — marking processed")
            state["processed"][uuid] = {"dashboard_run_id": existing,
                                        "matched_existing": True,
                                        "payday": payday}
            _audit({"event": "matched_existing", "uuid": uuid,
                    "location": location, "run_id": existing})
            continue

        csv_text = fetch_journal_csv(sess, cid, pcu, uuid)
        if not csv_text:
            results["notes"].append(
                f"{location}: journal for {uuid} not ready — will retry")
            continue

        pdf_bytes = None
        if paper > 0:
            pdf_bytes = fetch_checks_pdf(sess, cid, uuid)
            if pdf_bytes is None:
                if run.get("status") == "paid":
                    results["notes"].append(
                        f"{location}: {uuid} is paid but checks PDF unavailable "
                        f"— booking WITHOUT it (stubs will show blended rates; "
                        f"upload the PDF later via Backfill)")
                else:
                    logger.info(f"{location}: {uuid} has {paper} paper checks, "
                                f"PDF not ready (status={run.get('status')}) — "
                                f"deferring")
                    results["notes"].append(
                        f"{location}: {uuid} deferred — waiting on paper-checks "
                        f"PDF (status {run.get('status')})")
                    continue

        if dry_run:
            results["notes"].append(
                f"DRY RUN — would book {location} {run['period_start']}–"
                f"{run['period_end']} payday {payday}: gross ${gross:,.2f}, "
                f"{paper} paper checks, pdf={'yes' if pdf_bytes else 'no'}")
            continue

        try:
            resp = book_run(location, run, csv_text, pdf_bytes)
        except Exception as e:
            results["errors"].append(f"{location}: booking {uuid} FAILED: {e}")
            _audit({"event": "book_failed", "uuid": uuid,
                    "location": location, "error": str(e)})
            continue

        run_id = resp.get("run_id")
        state["processed"][uuid] = {"dashboard_run_id": run_id, "payday": payday,
                                    "gross": gross, "booked": True}
        results["booked_count"] += 1
        _audit({"event": "booked", "uuid": uuid, "location": location,
                "run_id": run_id, "resp": resp})

        queued = []
        if paper > 0:
            try:
                queued = queue_paper_checks(run_id, location)
                _audit({"event": "checks_queued", "run_id": run_id,
                        "count": len(queued)})
            except Exception as e:
                results["errors"].append(
                    f"{location}: run #{run_id} booked but check printing "
                    f"FAILED: {e} — print manually from the dashboard")
                _audit({"event": "print_failed", "run_id": run_id,
                        "error": str(e)})

        results["booked"].append({
            "location": location, "run_id": run_id,
            "period": f"{run['period_start']}–{run['period_end']}",
            "payday": payday, "type": run.get("type"),
            "gross": gross, "net": totals.get("employee_net"),
            "balanced": resp.get("balanced"),
            "employees": totals.get("employee_count"),
            "paper_checks": paper, "checks_queued": queued,
        })


def main():
    if os.path.exists(KILL_SWITCH):
        logger.info("Kill switch present — exiting")
        return
    if not INTERNAL_KEY:
        logger.error("PRINT_AGENT_API_KEY unset — cannot call dashboard; exiting")
        return

    dry_run = os.path.exists(DRY_RUN_FLAG) or \
        os.environ.get("PAYROLL_AUTOIMPORT_DRY_RUN") == "1"
    state = _load_state()
    results = {"booked": [], "booked_count": 0, "notes": [], "errors": []}

    for location in COMPANIES:
        try:
            process_location(location, state, dry_run, results)
        except Exception as e:
            logger.exception(f"{location}: unexpected failure")
            results["errors"].append(f"{location}: unexpected: {e}")

    _save_state(state)

    if results["booked"] or results["errors"] or \
            (dry_run and results["notes"]):
        lines = []
        if dry_run:
            lines.append("*** DRY RUN — nothing was booked or printed ***\n")
        for b in results["booked"]:
            lines.append(
                f"BOOKED run #{b['run_id']} — {b['location'].title()} "
                f"{b['period']} ({b['type']}), payday {b['payday']}\n"
                f"  {b['employees']} employees, gross ${b['gross']:,.2f}, "
                f"net ${b['net']:,.2f}, journal balanced: {b['balanced']}\n"
                f"  paper checks: {b['paper_checks']}")
            for q in b["checks_queued"]:
                lines.append(f"    #{q['check_number']}  {q['employee']}  "
                             f"${q['net']:,.2f} → home printer")
        lines += [f"NOTE: {n}" for n in results["notes"]]
        lines += [f"ERROR: {e}" for e in results["errors"]]
        subject = "Payroll auto-import"
        if results["booked"]:
            subject += f" — booked {len(results['booked'])} run(s)"
        if results["errors"]:
            subject = "⚠️ " + subject + f" ({len(results['errors'])} error(s))"
        _send_email(subject, "\n".join(lines))
    else:
        logger.info("Nothing to do" + (f" ({len(results['notes'])} notes)"
                                       if results["notes"] else ""))
        for n in results["notes"]:
            logger.info(f"note: {n}")


if __name__ == "__main__":
    main()
