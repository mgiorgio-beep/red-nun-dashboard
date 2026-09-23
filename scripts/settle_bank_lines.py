#!/usr/bin/env python3
"""Settle statement lines against invoices already on file.

For each plan entry: the invoices must sum to the bank draft (to the cent).
A vendor payment is created from the draft (or an existing uncleared payment
that already lists exactly those invoices is reused), the invoices are linked
and marked paid, the statement line is merged into the payment with a
register_merge_audit row (statement line captured in full, reversible), and
the settlement is coded to Accounts Payable: the invoice is the cost, the
draft settles it. The tie-out of every touched period is asserted unchanged.

Plan file: JSON list of {"line": <manual_bank_entries.id>, "vendor": <name as
on vendor_payments>, "invoices": [<invoice_number>, ...]}.

    python scripts/settle_bank_lines.py plan.json            # dry run
    python scripts/settle_bank_lines.py plan.json --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

AP = {"dennis": 2, "chatham": 258}


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["identity_holds"])


def settle(conn, who, now, entry):
    me = conn.execute(
        "SELECT m.*, b.location FROM manual_bank_entries m JOIN bank_accounts b ON b.id=m.bank_account_id WHERE m.id=?",
        (entry["line"],)).fetchone()
    assert me, f"line {entry['line']} not found (already merged?)"
    loc, amt, day = me["location"], round(abs(me["amount"]), 2), me["entry_date"]
    invs = []
    for num in entry["invoices"]:
        rows = conn.execute("SELECT * FROM scanned_invoices WHERE location=? AND invoice_number=? AND status='confirmed'",
                            (loc, str(num))).fetchall()
        assert len(rows) == 1, f"invoice {num} ({loc}): {len(rows)} rows"
        invs.append(rows[0])
    tot = round(sum(i["total"] for i in invs), 2)
    assert abs(tot - amt) < 0.005, f"line {me['id']}: invoices sum {tot} != draft {amt}"
    nums = {i["invoice_number"] for i in invs}
    # An uncleared payment already listing exactly these invoices is the same
    # settlement recorded ahead of the bank; reuse it rather than duplicate it.
    reuse = None
    for p in conn.execute("SELECT * FROM vendor_payments WHERE location=? AND cleared=0 AND status NOT IN ('void','failed') "
                          "AND UPPER(vendor) LIKE ?", (loc, entry["vendor"].upper()[:8] + "%")):
        linked = {r[0] for r in conn.execute("SELECT invoice_number FROM vendor_payment_invoices WHERE payment_id=?", (p["id"],))}
        if linked == nums:
            reuse = p
            break
    if reuse:
        pid = reuse["id"]
        conn.execute("""UPDATE vendor_payments SET status='cleared', payment_total=?, bank_account_id=?, cleared=1, cleared_date=?,
                        gl_account_id=?, gl_source='human', gl_status='confirmed', updated_at=?,
                        memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?""",
                     (amt, me["bank_account_id"], day, AP[loc], now, f"{who}: settled by the bank draft of {day}", pid))
        how = f"reused payment {pid}"
    else:
        pid = conn.execute(
            """INSERT INTO vendor_payments (vendor, location, payment_date, payment_ref, payment_method, payment_total, memo, status,
                   source, bank_account_id, cleared, cleared_date, gl_account_id, gl_source, gl_status, updated_at)
               VALUES (?,?,?,?,?,?,?,'cleared','bank',?,1,?,?,'human','confirmed',?)""",
            (entry["vendor"], loc, day, f"BANK-{loc[:3].upper()}-{day.replace('-', '')}-{amt:.2f}", "ACH", amt,
             f"{who}: recorded from the bank draft; invoices matched on file", me["bank_account_id"], day, AP[loc], now)).lastrowid
        for i in invs:
            conn.execute("INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, amount_paid) VALUES (?,?,?,?)",
                         (pid, i["invoice_number"], i["invoice_date"], i["total"]))
        how = f"new payment {pid}"
    for i in invs:
        conn.execute("UPDATE scanned_invoices SET payment_status='paid', paid_date=COALESCE(paid_date, ?), amount_paid=total, balance=0, "
                     "payment_reference=COALESCE(payment_reference, ?) WHERE id=?", (day, f"payment {pid}", i["id"]))
    conn.execute("UPDATE vendor_payments SET reconciliation_id=COALESCE(reconciliation_id, ?) WHERE id=?", (me["reconciliation_id"], pid))
    conn.execute(
        """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label, target_cleared_date,
               deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json, match_amount, match_date_diff_days,
               match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (who, me["bank_account_id"], "vendor_payment", pid, f"{entry['vendor']} {', '.join(sorted(nums))}", day,
         me["id"], day, me["amount"], json.dumps({k: me[k] for k in me.keys()}), amt, 0, 0,
         f"manual ({who}): invoices on file sum to the draft"))
    conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (me["id"],))
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,detail) "
                 "VALUES ('row_remap','vendor_payments',?,NULL,?,'settlement',?)",
                 (pid, AP[loc], f"{who}: bank draft settles {entry['vendor']} invoices -> Accounts Payable"))
    return f"line {me['id']} {loc} {day} {amt:>8.2f} -> {how}, invoices {', '.join(sorted(nums))}", me["statement_upload_id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--who", default="mike-" + datetime.now().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = {r[0] for r in conn.execute(
            f"SELECT statement_upload_id FROM manual_bank_entries WHERE id IN ({','.join('?' * len(plan))})", [e["line"] for e in plan])}
        before = {u: state(conn, u) for u in uploads}
        for e in plan:
            msg, _ = settle(conn, a.who, now, e)
            print(msg)
        for u in sorted(uploads):
            after = state(conn, u)
            print(f"upload {u}: bank {before[u][0]} -> {after[0]}, delta {before[u][1]} -> {after[1]}, identity {after[2]}")
            assert before[u][:2] == after[:2], "tie-out moved"
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
