#!/usr/bin/env python3
"""Undo the three 9/23 job066 merges that swallowed a Dennis Venmo payment
(Mike, 2026-09-24).

job066 paired on amount + date alone. Three times it merged a Dennis
"PAYMENT VENMO" line (band pay; Venmo is never a vendor bill) into a Bill Pay
row, deleting the Venmo line:
  audit 54  Venmo 5/04 -400.00 -> #235 Colonial (a CHATHAM bill)
  audit 64  Venmo 5/18 -500.00 -> #295 KOD Holdings (Chatham check 2068)
  audit 71  Venmo 5/26 -500.00 -> #347 Barrows (Dennis)
Each Venmo line is restored from the audit row exactly as it was (Bands,
suggested) and the audit row marked reversed. Then each payment is put
where the bank actually cleared it:
  #235 -> Chatham line 2249 (5/04 "INVOICES COLONIAL WHOLESA ... RED NUN BAR
          & GRILL"); back on Chatham's Accounts Payable (it settles a Colonial
          invoice; the 'Dennis paid a Chatham bill' coding rested on the bad
          match).
  #295 -> Chatham line 2405 (5/27 check 2068; the image is Red Buoy's check
          to KOD Holdings). Coding held for Mike's accountant: #295 takes the
          line's own Ask My Accountant, so nothing about it changes.
  #347 -> no clearing found on either account: left uncleared (outstanding).
Bank balance and delta of every statement period asserted unchanged.

    python scripts/undo_job066_venmo_merges.py            # dry run
    python scripts/undo_job066_venmo_merges.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-24"
AUDITS = {54: 235, 64: 295, 71: 347}
REPAIR = {235: (2249, "ap"), 295: (2405, "keep_line_coding")}
AP_CHATHAM = 258


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["outstanding_net"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2)")]
        before = {u: state(conn, u) for u in uploads}
        cols = [r[1] for r in conn.execute("PRAGMA table_info(manual_bank_entries)")]
        for aid, vid in AUDITS.items():
            au = conn.execute("SELECT * FROM register_merge_audit WHERE id=?", (aid,)).fetchone()
            assert au and au["target_id"] == vid and au["reversed_at"] is None and au["merged_by"] == "jarvis-job066"
            me = json.loads(au["deleted_entry_json"])
            assert "VENMO" in (me.get("payee") or "").upper()
            assert not conn.execute("SELECT 1 FROM manual_bank_entries WHERE id=?", (me["id"],)).fetchone()
            keep = [k for k in cols if k in me]
            conn.execute(f"INSERT INTO manual_bank_entries ({','.join(keep)}) VALUES ({','.join('?' * len(keep))})",
                         [me[k] for k in keep])
            conn.execute("UPDATE register_merge_audit SET reversed_at=? WHERE id=?", (now, aid))
            vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
            conn.execute("""UPDATE vendor_payments SET cleared=0, cleared_date=NULL, updated_at=?,
                                memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?""",
                         (now, f"{WHO}: unpaired from Dennis Venmo line {me['id']} (job066 amount-only match, audit {aid})", vid))
            print(f"audit {aid}: restored Dennis Venmo line {me['id']} {me['entry_date']} {me['amount']:,.2f} "
                  f"(gl {me.get('gl_account_id')}, {me.get('gl_status')}); #{vid} {vp['vendor']} unpaired")
        for vid, (line, how) in REPAIR.items():
            vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
            me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (line,)).fetchone()
            assert me and me["bank_account_id"] == 1 and abs(me["amount"] + vp["payment_total"]) < 0.005
            if how == "ap":
                gl, note = AP_CHATHAM, "settles a Colonial invoice -> Chatham Accounts Payable"
            else:
                gl, note = me["gl_account_id"], "coding held for Mike's accountant: carries the line's own coding"
            conn.execute("""UPDATE vendor_payments SET bank_account_id=1, cleared=1, cleared_date=?, gl_account_id=?,
                                gl_source=?, gl_status=?, updated_at=?, memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |')
                            WHERE id=?""",
                         (me["entry_date"], gl, "human" if how == "ap" else me["gl_source"],
                          "confirmed" if how == "ap" else me["gl_status"], now,
                          f"{WHO}: cleared on Chatham {me['entry_date']} by statement line {line}; {note}", vid))
            conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,"
                         "detail) VALUES ('row_remap','vendor_payments',?,?,?,'job066 repair',?)",
                         (vid, vp["gl_account_id"], gl, f"{WHO}: {note}"))
            conn.execute(
                """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
                       target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
                       match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (WHO, 1, "vendor_payment", vid, vp["vendor"], me["entry_date"], line, me["entry_date"], me["amount"],
                 json.dumps(dict(me)), vp["payment_total"], None, None,
                 f"manual ({WHO}): replaces job066's amount-only Venmo match; statement names the payee / check image confirms"))
            conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (line,))
            print(f"#{vid} {vp['vendor']} -> Chatham line {line} {me['entry_date']} ({me['payee']}); gl -> {gl}")
        # 347: any other clearing?
        cands = conn.execute("SELECT m.id, m.bank_account_id, m.entry_date, m.payee FROM manual_bank_entries m WHERE "
                             "ABS(m.amount+500)<0.005 AND m.entry_date BETWEEN '2026-05-01' AND '2026-08-31' AND "
                             "UPPER(m.payee||' '||COALESCE(m.memo,'')) LIKE '%BARROWS%'").fetchall()
        print("#347 Barrows 500: live lines naming Barrows for 500:", [tuple(r) for r in cands], "-> left uncleared" if not cands else "")
        for u in uploads:
            b, a_ = before[u], state(conn, u)
            assert b[:2] == a_[:2], f"upload {u} bank side moved"
            if b[2] != a_[2]:
                print(f"   upload {u}: outstanding {b[2]:,.2f} -> {a_[2]:,.2f}")
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
