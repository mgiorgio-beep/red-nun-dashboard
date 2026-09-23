#!/usr/bin/env python3
"""jarvis_payment_health.py — nightly payment-record health checks for Jarvis (job 044).

Read-only. Writes jarvis_exports/jarvis_payment_health.json + JARVIS_PAYMENT_HEALTH.md
into the Drive-synced folder. Born from the 2026-09-21 blank-location incident.
"""
import json, os, sqlite3
from datetime import datetime, timedelta

OUT_DIR = os.environ.get("JARVIS_EXPORT_DIR", "/home/rednun/cowork/red-nun-dashboard/jarvis_exports")
DB_PATH = os.environ.get("TOAST_DB_PATH") or os.environ.get("DB_PATH") or "/var/lib/rednun/toast_data.db"
PENDING_DAYS = 14
LIVE = "COALESCE(status,'') NOT IN ('void','failed')"

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda s, *a: [dict(r) for r in con.execute(s, a).fetchall()]
    cutoff = (datetime.now() - timedelta(days=PENDING_DAYS)).strftime("%Y-%m-%d")
    checks, errors = {}, {}

    def run(key, title, sql, *args):
        try:
            rows = q(sql, *args)
            total = round(sum(float(r.get("payment_total") or r.get("amount") or 0) for r in rows), 2)
            checks[key] = {"title": title, "n": len(rows), "dollars": total, "rows": rows[:300]}
        except Exception as e:
            errors[key] = str(e)

    cols = "id, vendor, location, bank_account_id, payment_date, payment_total, payment_ref, payment_method, source, status"
    run("P1_unassigned", "Payments with no location or no bank account (they land in the Chatham register by default)",
        f"SELECT {cols} FROM vendor_payments WHERE {LIVE} AND (COALESCE(location,'')='' OR bank_account_id IS NULL) ORDER BY payment_date DESC")
    run("P2_pending_over_14d", f"Payments still 'pending' more than {PENDING_DAYS} days after payment date (never matched to the bank)",
        f"SELECT {cols} FROM vendor_payments WHERE status='pending' AND payment_date < ? ORDER BY payment_date", cutoff)
    run("P3_location_bank_mismatch", "Location and bank account disagree (real cross-entity payment = intercompany; otherwise a mislabel)",
        f"""SELECT vp.id, vp.vendor, vp.location, vp.bank_account_id, b.location AS bank_location, vp.payment_date,
                   vp.payment_total, vp.payment_ref, vp.source, vp.status
            FROM vendor_payments vp JOIN bank_accounts b ON b.id = vp.bank_account_id
            WHERE {LIVE.replace('status','vp.status')} AND COALESCE(vp.location,'')<>'' AND b.location <> vp.location
            ORDER BY vp.payment_date""")
    run("P4_billpay_not_mirrored", "Bill Pay payments (ap_payments) with no row in vendor_payments — mirror failed, invisible to register",
        """SELECT ap.id, ap.vendor_name AS vendor, ap.payment_date, ap.amount, ap.payment_method, ap.status
           FROM ap_payments ap LEFT JOIN vendor_payments vp ON vp.ap_payment_id = ap.id
           WHERE vp.id IS NULL AND COALESCE(ap.status,'') NOT IN ('void','failed') ORDER BY ap.payment_date DESC""")
    try:
        checks["P2_pending_by_location"] = q(
            "SELECT COALESCE(NULLIF(location,''),'(blank)') location, COUNT(*) n, ROUND(SUM(payment_total),2) dollars, "
            "MIN(payment_date) oldest FROM vendor_payments WHERE status='pending' AND payment_date < ? GROUP BY 1", cutoff)
    except Exception as e:
        errors["P2_pending_by_location"] = str(e)
    con.close()

    flags = [k for k in ("P1_unassigned", "P4_billpay_not_mirrored") if checks.get(k, {}).get("n")]
    snap = {"generated_at": datetime.now().isoformat(), "db_path": DB_PATH, "pending_cutoff": cutoff,
            "hard_flags": flags, "checks": checks, "errors": errors}
    path = os.path.join(OUT_DIR, "jarvis_payment_health.json")
    with open(path + ".tmp", "w") as f:
        json.dump(snap, f, default=str)
    os.replace(path + ".tmp", path)

    lines = [f"# Jarvis payment health — {snap['generated_at']}",
             f"HARD FLAGS: {len(flags)} {flags if flags else '(none)'}", "",
             "| check | rows | dollars |", "|---|---|---|"]
    for k in ("P1_unassigned", "P2_pending_over_14d", "P3_location_bank_mismatch", "P4_billpay_not_mirrored"):
        c = checks.get(k)
        lines.append(f"| {k} — {c['title']} | {c['n']} | ${c['dollars']:,.2f} |" if c else f"| {k} | ERROR | {errors.get(k)} |")
    lines += ["", "## Pending >14d by location"] + [f"- {r}" for r in checks.get("P2_pending_by_location", [])]
    with open(os.path.join(OUT_DIR, "JARVIS_PAYMENT_HEALTH.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
