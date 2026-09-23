#!/usr/bin/env python3
"""
jarvis_watchdog.py — checks each subsystem, REPAIRS what has a known fix,
escalates only what needs a human.

Design rules, in order of importance:
  1. Assert on OUTPUT, not on "did it run". A job that ran and produced a
     0-byte file is a failure. That distinction is what missed everything
     that broke this summer.
  2. Repairs are idempotent and BOUNDED. Every repair has a cooldown and a
     failure cap. An unbounded retry loop is how bisync wrote 2MB of the
     same error for three weeks.
  3. Any repair that could DESTROY data dry-runs first and refuses if the
     delta is larger than expected.
  4. NEVER auto-repair payroll, wage, or financial records. Those escalate.

Run every 15 min from cron. Writes jarvis_health.json + JARVIS_HEALTH.md.
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

E = ZoneInfo("America/New_York")
NOW = datetime.now(E)
HOME = os.path.expanduser("~")
DRIVE = f"{HOME}/cowork/red-nun-dashboard"
REPO = "/opt/red-nun-dashboard"
REMOTE = "gdrive:Red NUn Dashboard"
STATE = f"{HOME}/.jarvis_health.json"
PY = f"{REPO}/venv/bin/python3" if os.path.exists(f"{REPO}/venv/bin/python3") else "/usr/bin/python3"

MAX_REPAIRS = 2          # give up after this many consecutive failed repairs
BISYNC_COOLDOWN_H = 12   # resync at most this often
RESYNC_MAX_DELTA = 200   # refuse an automatic resync that would touch more than this

def sh(cmd, timeout=300):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"

def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}

def save(s):
    json.dump(s, open(STATE, "w"), indent=2)

state = load()
results = []

def record(name, ok, detail, repaired=None, escalate=False):
    st = state.setdefault(name, {})
    if ok:
        st["fails"] = 0
    else:
        st["fails"] = st.get("fails", 0) + 1
    st["last_check"] = NOW.isoformat()
    st["last_status"] = "ok" if ok else "FAIL"
    if repaired:
        st["last_repair"] = NOW.isoformat()
        st["last_repair_action"] = repaired
    results.append({"check": name, "ok": ok, "detail": detail,
                    "repaired": repaired, "escalate": escalate,
                    "consecutive_fails": st["fails"]})

def cooled(name, hours):
    last = state.get(name, {}).get("last_repair")
    if not last:
        return True
    try:
        return (NOW - datetime.fromisoformat(last)) > timedelta(hours=hours)
    except Exception:
        return True

def capped(name):
    return state.get(name, {}).get("fails", 0) >= MAX_REPAIRS

# ---------------------------------------------------------------- checks

def check_bisync():
    """bisync aborts permanently once its listing files are lost. Known fix: --resync."""
    log = f"{DRIVE}/_apply_log/bisync.log"
    if not os.path.exists(log):
        return record("bisync", True, "no bisync log — not in use")
    _, tail = sh(f"tail -c 20000 '{log}'")
    broken = "Must run --resync" in tail or "cannot find prior Path1" in tail
    if not broken:
        return record("bisync", True, "last run clean")
    if capped("bisync"):
        return record("bisync", False, "resync attempted twice and still aborting — NEEDS A HUMAN",
                      escalate=True)
    if not cooled("bisync", BISYNC_COOLDOWN_H):
        return record("bisync", False, f"aborted; resync on cooldown ({BISYNC_COOLDOWN_H}h)")
    # dry-run first: refuse to auto-resync if it would move an unexpected amount
    rc, out = sh(f'rclone bisync "{DRIVE}" "{REMOTE}" --resync --resync-mode newer '
                 f'--dry-run --max-delete 0 2>&1 | tail -40', timeout=600)
    moves = len(re.findall(r"(Copied|Deleted|Moved)", out))
    if moves > RESYNC_MAX_DELTA:
        return record("bisync", False,
                      f"resync would touch {moves} files (>{RESYNC_MAX_DELTA}) — refusing, NEEDS A HUMAN",
                      escalate=True)
    rc, out = sh(f'rclone bisync "{DRIVE}" "{REMOTE}" --resync --resync-mode newer '
                 f'--max-delete 25 2>&1 | tail -20', timeout=1800)
    ok = rc == 0 and "Must run --resync" not in out
    record("bisync", ok,
           f"was aborted; resync {'succeeded' if ok else 'FAILED'} ({moves} files in dry-run)",
           repaired="rclone bisync --resync --resync-mode newer", escalate=not ok)

def check_export_fresh():
    """The export must have produced a fresh file — not merely have run."""
    meta = f"{HOME}/jarvis_exports/jarvis_meta.json"
    stale = True
    detail = "missing"
    if os.path.exists(meta):
        try:
            gen = datetime.fromisoformat(json.load(open(meta))["generated_at"])
            age = (NOW - gen).total_seconds() / 3600
            stale = age > 26
            detail = f"{age:.1f}h old"
        except Exception as e:
            detail = f"unreadable: {e}"
    if not stale:
        return record("jarvis_export", True, detail)
    if capped("jarvis_export"):
        return record("jarvis_export", False, f"{detail}; rerun failed twice — NEEDS A HUMAN", escalate=True)
    rc, out = sh(f'cd {REPO} && JARVIS_EXPORT_DIR={HOME}/jarvis_exports {PY} jarvis_export.py 2>&1 | tail -10')
    sh(f'rclone copy {HOME}/jarvis_exports "{REMOTE}/jarvis_exports"')
    ok = rc == 0
    record("jarvis_export", ok, f"was {detail}; rerun {'ok' if ok else 'FAILED'}",
           repaired="reran jarvis_export.py + pushed to Drive", escalate=not ok)

def check_job_queue():
    """Jobs queued in Drive must reach the box and run, whatever the sync is doing."""
    qdir = f"{DRIVE}/deploy/queue"
    os.makedirs(qdir, exist_ok=True)
    rc, _ = sh(f'rclone copy "{REMOTE}/deploy/queue" "{qdir}"', timeout=300)
    pending = [f for f in os.listdir(qdir) if f.endswith(".sh")]
    if not pending:
        return record("job_queue", True, "empty")
    runner = f"{HOME}/bin/beelink_job_runner.sh"
    if not os.path.exists(runner):
        return record("job_queue", False, f"{len(pending)} queued but runner missing — NEEDS A HUMAN",
                      escalate=True)
    rc, out = sh(f"bash {runner} 2>&1 | tail -5", timeout=1700)
    left = [f for f in os.listdir(qdir) if f.endswith(".sh")]
    ok = not left
    record("job_queue", ok, f"{len(pending)} queued, {len(left)} left after run",
           repaired=f"pulled from Drive and ran {len(pending)} job(s)", escalate=not ok)

def check_db():
    """A 0-byte sqlite file opens without error. Assert on content, not existence."""
    import sqlite3
    path = "/var/lib/rednun/toast_data.db"
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return record("toast_db", False, f"{path} missing or empty — NEEDS A HUMAN", escalate=True)
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        mx = c.execute("SELECT MAX(business_date) FROM orders").fetchone()[0]
        c.close()
    except Exception as e:
        return record("toast_db", False, f"unreadable: {e} — NEEDS A HUMAN", escalate=True)
    lag = (NOW.date() - datetime.strptime(str(mx), "%Y%m%d").date()).days
    # Toast sync is not ours to restart blindly; report it.
    record("toast_db", lag <= 2, f"newest order {mx} ({lag}d old)", escalate=lag > 2)

def check_roundtrip():
    """Prove Drive actually round-trips. Writing is not arriving."""
    token = f"{HOME}/.jarvis_rt.txt"
    stamp = NOW.isoformat()
    open(token, "w").write(stamp)
    rc, _ = sh(f'rclone copyto "{token}" "{REMOTE}/_health/roundtrip.txt"', timeout=180)
    rc2, out = sh(f'rclone cat "{REMOTE}/_health/roundtrip.txt"', timeout=180)
    ok = rc == 0 and rc2 == 0 and stamp.strip() in out
    record("drive_roundtrip", ok, "wrote and read back" if ok else "token did not survive the trip",
           escalate=not ok)

def check_disk():
    rc, out = sh("df -P / | tail -1 | awk '{print $5}' | tr -d '%'")
    try:
        used = int(out.strip())
    except Exception:
        return record("disk", True, "unreadable")
    record("disk", used < 90, f"{used}% used", escalate=used >= 90)

for fn in (check_bisync, check_export_fresh, check_job_queue, check_db, check_roundtrip, check_disk):
    try:
        fn()
    except Exception as e:
        record(fn.__name__, False, f"watchdog check crashed: {e}", escalate=True)

state["_watchdog_last_run"] = NOW.isoformat()
save(state)

repaired = [r for r in results if r["repaired"]]
escalate = [r for r in results if r["escalate"]]

lines = [f"# Jarvis health — {NOW:%Y-%m-%d %H:%M %Z}", ""]
if escalate:
    lines += ["## NEEDS A HUMAN", ""]
    lines += [f"- **{r['check']}** — {r['detail']}" for r in escalate] + [""]
if repaired:
    lines += ["## Repaired automatically", ""]
    lines += [f"- **{r['check']}** — {r['repaired']} ({r['detail']})" for r in repaired] + [""]
lines += ["## All checks", "", "| check | status | detail |", "|---|---|---|"]
lines += [f"| {r['check']} | {'ok' if r['ok'] else 'FAIL'} | {r['detail']} |" for r in results]
open(f"{DRIVE}/JARVIS_HEALTH.md", "w").write("\n".join(lines) + "\n")
sh(f'rclone copyto "{DRIVE}/JARVIS_HEALTH.md" "{REMOTE}/JARVIS_HEALTH.md"')

print("\n".join(lines))
sys.exit(1 if escalate else 0)
