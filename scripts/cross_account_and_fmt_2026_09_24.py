#!/usr/bin/env python3
"""Mike, 2026-09-24 — the cross-account check on Dennis's 8/31 outstanding, and
Chatham's "Loan to RNPH" cleanup. One transaction.

A  Dennis bills Chatham's bank paid (intercompany, Chatham paid for Dennis):
   the payment keeps location='dennis', moves to the Chatham account, is
   cleared by its Chatham line (register_merge_audit), and is coded Loan to Red
   Nun Dennisport on Chatham's chart. The Chatham lines were expense on top of
   the Dennis invoices.
     #242 Bay State Sewage 1,680.00 <- Chatham check 2049 (5/05; image names
          Dennis invoices 140007/150097)
     #370 PFG 3,469.48 <- Chatham AR PAYMENT PERFORMANCEBOS 6/02
     #498 Fore & Aft 1,680.00 <- Chatham card 7/15 (Dennis inv 16775)
     #508 Dependable 766.34 <- Chatham card 7/21 (Dennis inv 3833)
   #258, #499, #501 left as they are (Mike).
B  Dennis checks that cleared on Dennis's own account weeks after the Bill Pay
   date, their statement lines coded to expense beside the invoice: merged
   into the Bill Pay row.
C  Duplicate Bill Pay rows voided only where the invoice numbers match the row
   they duplicate: #300 UniFirst = #383 (same five invoices; #383 cleared 6/12).
   Not voided (no match): #255 / #392 Caron (Caron bills carry no invoice
   numbers), #355 / #357 Cozzini (their own invoice numbers).
D  Chatham "Loan to RNPH" (QBO 112, typed Income — type left alone): its eight
   2026 Dennis repayments (x2757 -> x5975) move to Loan to Red Nun Dennisport.
E  FMT Holdings (x1239) per the standing rule: Chatham OUT -> Building Rent,
   IN -> FMT Loan (the five sat on Loan to RNPH).

    python scripts/cross_account_and_fmt_2026_09_24.py            # dry run
    python scripts/cross_account_and_fmt_2026_09_24.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-24"
LOAN_TO_DENNIS, LOAN_TO_RNPH, BUILDING_RENT, FMT_LOAN = 527, 398, 443, 316
A = [(242, 2259), (370, 2447), (498, 2714), (508, 2759)]
B = [(241, 1511), (296, 1547), (282, 3040), (480, 3107), (523, 3356)]
C_VOID = [(300, 383)]
FMT = {2173: BUILDING_RENT, 2182: BUILDING_RENT, 2242: BUILDING_RENT, 2362: FMT_LOAN, 2363: FMT_LOAN}


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["outstanding_net"])


def log(conn, table, tid, old, new, rule, detail):
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,"
                 "detail) VALUES ('row_remap',?,?,?,?,?,?)", (table, tid, old, new, rule, f"{WHO}: {detail}"))


def merge(conn, vid, line, rule, bank_to=None, gl=None, note=None):
    vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
    me = conn.execute("SELECT m.*, g.name gl_name FROM manual_bank_entries m LEFT JOIN gl_accounts g ON g.id=m.gl_account_id "
                      "WHERE m.id=?", (line,)).fetchone()
    assert vp and me, (vid, line)
    assert not vp["cleared"] and (vp["status"] or "") not in ("void", "failed"), f"#{vid} already cleared/void"
    assert abs(vp["payment_total"] + me["amount"]) < 0.005, (vid, line, vp["payment_total"], me["amount"])
    sets = ["cleared=1", "cleared_date=?", "updated_at=?"]
    args = [me["entry_date"], datetime.now().isoformat(timespec="seconds")]
    if bank_to:
        sets.append("bank_account_id=?"); args.append(bank_to)
    if gl:
        sets += ["gl_account_id=?", "gl_source='human'", "gl_status='confirmed'"]; args.append(gl)
        log(conn, "vendor_payments", vid, vp["gl_account_id"], gl, rule, note)
    if note:
        sets.append("memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |')"); args.append(f"{WHO}: {note}")
    conn.execute(f"UPDATE vendor_payments SET {', '.join(sets)} WHERE id=?", (*args, vid))
    conn.execute(
        """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
               target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
               match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (WHO, me["bank_account_id"], "vendor_payment", vid, f"{vp['vendor']} {vp['payment_ref']}", me["entry_date"], line,
         me["entry_date"], me["amount"], json.dumps({k: me[k] for k in me.keys() if k != "gl_name"}), vp["payment_total"],
         None, None, f"manual ({WHO}): {rule}"))
    conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (line,))
    return vp, me


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2)")]
        before = {u: state(conn, u) for u in uploads}
        tot = 0.0
        for vid, line in A:
            vp, me = merge(conn, vid, line, "intercompany — Dennis bill paid from Chatham's account", bank_to=1,
                           gl=LOAN_TO_DENNIS, note="intercompany — Chatham 5975 paid this Dennis bill; Chatham books Loan "
                                                   "to Red Nun Dennisport, Dennis books Dr AP / Cr Loan to Red Buoy Inc.")
            tot += vp["payment_total"]
            print(f"A  #{vid:<4} {vp['vendor'][:26]:<26} {vp['payment_total']:>9,.2f} <- Chatham line {line} "
                  f"{me['entry_date']} (was {me['gl_name']})")
        print(f"A  total {tot:,.2f}")
        for vid, line in B:
            vp, me = merge(conn, vid, line, "Dennis check cleared on Dennis weeks after the Bill Pay date",
                           note=f"cleared {me_date(conn, line)} by statement line {line}")
            gl = conn.execute("SELECT g.name FROM vendor_payments v LEFT JOIN gl_accounts g ON g.id=v.gl_account_id "
                              "WHERE v.id=?", (vid,)).fetchone()[0]
            print(f"B  #{vid:<4} {vp['vendor'][:26]:<26} {vp['payment_total']:>9,.2f} <- Dennis line {line} {me['entry_date']} "
                  f"{me['payee']} (line was {me['gl_name']}; payment stays {gl})")
        for vid, orig in C_VOID:
            v = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
            inv = lambda p: sorted(r[0] for r in conn.execute(  # noqa: E731
                "SELECT invoice_number FROM vendor_payment_invoices WHERE payment_id=?", (p,)))
            assert inv(vid) and inv(vid) == inv(orig) and not v["cleared"]
            conn.execute("UPDATE vendor_payments SET status='void', updated_at=?, memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') "
                         "WHERE id=?", (datetime.now().isoformat(timespec="seconds"),
                                        f"{WHO}: void — duplicate of #{orig} (same invoices {', '.join(inv(vid))}; #{orig} cleared)", vid))
            print(f"C  #{vid} {v['vendor']} {v['payment_total']:,.2f} voided: duplicate of #{orig} (same {len(inv(vid))} invoices)")
        n = 0
        for r in conn.execute("SELECT * FROM manual_bank_entries WHERE gl_account_id=? AND entry_date>='2026-01-01'",
                              (LOAN_TO_RNPH,)).fetchall():
            if r["id"] in FMT:
                continue
            assert "X2757" in (r["payee"] or "").upper(), r["id"]
            conn.execute("UPDATE manual_bank_entries SET gl_account_id=?, gl_source='human', gl_status='confirmed' WHERE id=?",
                         (LOAN_TO_DENNIS, r["id"]))
            log(conn, "manual_bank_entries", r["id"], LOAN_TO_RNPH, LOAN_TO_DENNIS, "intercompany repayment",
                "Dennis repayment x2757 -> x5975 moved off Loan to RNPH (typed Income)")
            n += 1
        print(f"D  {n} Dennis repayments Loan to RNPH -> Loan to Red Nun Dennisport")
        for line, gl in FMT.items():
            r = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (line,)).fetchone()
            assert r["gl_account_id"] == LOAN_TO_RNPH and "X1239" in r["payee"].upper()
            conn.execute("UPDATE manual_bank_entries SET gl_account_id=?, gl_source='human', gl_status='confirmed' WHERE id=?",
                         (gl, line))
            log(conn, "manual_bank_entries", line, LOAN_TO_RNPH, gl, "FMT Holdings",
                "x1239 = FMT Holdings: OUT is rent, IN is FMT covering a shortfall")
            print(f"E  line {line} {r['entry_date']} {r['amount']:>9,.2f} {r['payee']} -> "
                  f"{'Building Rent' if gl == BUILDING_RENT else 'FMT Loan'}")
        left = conn.execute("SELECT COUNT(*) FROM manual_bank_entries WHERE gl_account_id=? AND entry_date>='2026-01-01'",
                            (LOAN_TO_RNPH,)).fetchone()[0]
        print(f"Loan to RNPH 2026 rows left: {left}")
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


def me_date(conn, line):
    r = conn.execute("SELECT entry_date FROM manual_bank_entries WHERE id=?", (line,)).fetchone()
    return r[0] if r else "?"


if __name__ == "__main__":
    main()
