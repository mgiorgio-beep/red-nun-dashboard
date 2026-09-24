#!/usr/bin/env python3
"""Settle the 24 open Martignetti bank drafts against invoices on file.

Mike, 2026-09-24. Source: Martignetti_Invoices_Dec25-Aug26.xlsx (Martignetti
Exchange payment confirmations, Drive folder). Every invoice it lists is
already in scanned_invoices; what was missing is the payment behind each
draft. Accounts: 12027592 (Red Nun Bar & Grill) = Chatham, 12021893 (Red Nun)
= Dennis (yesterday's 3/24 settlements proved the mapping).

Every draft ties to its invoices to the cent. No draft needs a new payment:

  * Jan–Mar: the 2026-03-26 import left one vendor_payments row per
    confirmation (117–131) carrying the REAL confirmation number but made-up
    amounts, dates and S-numbered "invoices". Each is corrected in place from
    the workbook (paid date, total, real invoice links) and cleared by its draft.
    vp#121 is the "$1,342.08 never cleared" of question R4: it is confirmation
    A0570632 = the 2/18 draft of 881.67.
  * Apr–Aug: the portal/external payments already on file are cleared by their
    drafts; wrong totals (#186, #275, #511, #555) take the bank amount, wrong
    invoice links (#251 typo, #556, #539) are corrected.
  * Intercompany (Mike, 2026-09-24): seven Apr–May drafts, 5,389.11, left
    CHATHAM's bank (5975) but paid DENNIS invoices (acct 12021893). The
    payment keeps location='dennis' (where the cost belongs) and moves to the
    Chatham bank account (where the cash left) — the PFG #27–29 precedent.
    Coded on Chatham's chart to Loan to Red Nun Dennisport. The Dennis side
    (Dr Accounts Payable / Cr Loan to Red Buoy Inc.) is a book-only entry the
    cash register does not carry; it is noted on each payment for the QBO JE.
    JARVIS_KNOWLEDGE had "Martignetti $5,394 Dennis-owes-Chatham": the 4.92
    difference is #275's wrong total (496.66 recorded, 491.74 drafted).

The 1/05 confirmation A0548768 (937.65, flagged "paid twice?") never reached
either bank; its rows (#116, #125) stay void. #123/#132/#156 are voided: the
3/24 drafts they stood for were settled 2026-09-23 by #659 and #152.

Every merge writes register_merge_audit with the statement line in full.
Bank balance and delta of every statement period on both accounts are
asserted unchanged.

    python scripts/martignetti_settle.py            # dry run
    python scripts/martignetti_settle.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402

WHO = "mike-2026-09-24"
AP = {"dennis": 2, "chatham": 258}
LOAN_TO_DENNIS = 527          # Chatham chart: Loan to Red Nun Dennisport
CHATHAM_BANK = 1

# statement line -> (payment to clear, paid date from the workbook or None to
# keep the payment's own date, invoice numbers)
PLAN = [
    # Chatham, acct 12027592
    (324, 117, "2026-01-13", ["US1-103026887", "US1-102986516"]),
    (375, 118, "2026-01-19", ["US1-103050833"]),
    (432, 119, "2026-01-29", ["US1-103068329"]),
    (1602, 120, "2026-02-02", ["US1-103085687"]),
    (1718, 121, "2026-02-16", ["US1-103127610"]),
    (1850, 122, "2026-03-04", ["US1-103177556"]),
    (2061, 186, None, ["US1-103244104", "US1-103260372"]),
    (2769, 511, None, ["US1-103660094", "US1-103695419"]),
    (2897, 556, None, ["US1-103724415", "US1-103733721", "US1-103751365", "US1-103795201"]),
    # Dennis, acct 12021893
    (469, 124, "2026-01-05", ["US1-102955797", "US1-102986443"]),
    (515, 126, "2026-01-13", ["US1-103008444", "US1-103026559"]),
    (555, 127, "2026-01-19", ["US1-103050597"]),
    (609, 128, "2026-01-29", ["US1-103068322"]),
    (819, 129, "2026-02-02", ["US1-103085765"]),
    (863, 130, "2026-02-09", ["US1-103113605"]),
    (996, 131, "2026-03-04", ["US1-103159470", "US1-103176789"]),
    (3391, 555, None, ["US1-103733535", "US1-103751490", "US1-103771564"]),
    # Intercompany: Chatham bank paid Dennis invoices
    (2016, 155, None, ["US1-103243297"]),
    (2057, 187, None, ["US1-103259758"]),
    (2131, 202, None, ["US1-103272821"]),
    (2165, 207, None, ["US1-103299657", "US1-103314544"]),
    (2262, 251, None, ["US1-103337603"]),
    (2311, 275, None, ["US1-103353546"]),
    (2413, 323, None, ["US1-103379921", "US1-103423460", "US1-103440196"]),
]
# already cleared 7/28 at 1,999.81, but linked to a number not on file
RELINK = [(539, ["US1-103708032", "US1-103717676"])]
VOID = {123: "3/24 draft 664.31 settled 2026-09-23 by payment 659",
        132: "3/24 draft 1073.08 settled 2026-09-23 by payment 152",
        156: "duplicate of payment 659 (US1-103221379, cleared 3/24)"}


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["identity_holds"])


def relink(conn, pid, invs):
    conn.execute("DELETE FROM vendor_payment_invoices WHERE payment_id=?", (pid,))
    for i in invs:
        conn.execute("INSERT INTO vendor_payment_invoices (payment_id, invoice_number, invoice_date, amount_paid) "
                     "VALUES (?,?,?,?)", (pid, i["invoice_number"], i["invoice_date"], i["total"]))


def invoices(conn, loc, nums):
    out = []
    for n in nums:
        rows = conn.execute("SELECT * FROM scanned_invoices WHERE location=? AND invoice_number=? AND status='confirmed' "
                            "AND UPPER(vendor_name) LIKE '%MARTIG%'", (loc, n)).fetchall()
        assert len(rows) == 1, f"invoice {n} ({loc}): {len(rows)} rows"
        out.append(rows[0])
    return out


def settle(conn, now, line, pid, paid, nums):
    me = conn.execute(
        "SELECT m.*, b.location bank_loc FROM manual_bank_entries m JOIN bank_accounts b ON b.id=m.bank_account_id "
        "WHERE m.id=?", (line,)).fetchone()
    assert me, f"line {line} not found (already merged?)"
    vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (pid,)).fetchone()
    assert vp and not vp["cleared"], f"payment {pid} missing or already cleared"
    assert "MARTIG" in (vp["vendor"] or "").upper()
    loc, amt, day = vp["location"], round(abs(me["amount"]), 2), me["entry_date"]
    invs = invoices(conn, loc, nums)
    tot = round(sum(i["total"] for i in invs), 2)
    assert abs(tot - amt) < 0.005, f"line {line}: invoices {tot} != draft {amt}"
    ic = loc != me["bank_loc"]
    assert not ic or (loc == "dennis" and me["bank_account_id"] == CHATHAM_BANK)
    gl = LOAN_TO_DENNIS if ic else AP[loc]
    note = (f"{WHO}: intercompany — Chatham 5975 paid Dennis Martignetti invoices; Chatham books Loan to Red Nun "
            f"Dennisport, Dennis books Dr Accounts Payable / Cr Loan to Red Buoy Inc. (QBO JE)" if ic else
            f"{WHO}: settled by the bank draft of {day}")
    changed = []
    if abs((vp["payment_total"] or 0) - amt) >= 0.005:
        changed.append(f"total {vp['payment_total']} -> {amt}")
    if paid and paid != vp["payment_date"]:
        changed.append(f"date {vp['payment_date']} -> {paid}")
    if vp["status"] == "void":
        changed.append("un-voided")
    if ic:
        changed.append("bank -> Chatham 5975")
    old = sorted(r[0] for r in conn.execute("SELECT invoice_number FROM vendor_payment_invoices WHERE payment_id=?", (pid,)))
    if old != sorted(nums):
        changed.append(f"invoices {','.join(old) or '-'} -> real")
        note += f"; invoice links corrected from the Martignetti Exchange confirmations (were {','.join(old) or 'none'})"
    relink(conn, pid, invs)
    conn.execute(
        """UPDATE vendor_payments SET status='cleared', payment_total=?, payment_date=COALESCE(?, payment_date),
               bank_account_id=?, cleared=1, cleared_date=?, gl_account_id=?, gl_source='human', gl_status='confirmed',
               reconciliation_id=COALESCE(reconciliation_id, ?), updated_at=?,
               memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?""",
        (amt, paid, me["bank_account_id"], day, gl, me["reconciliation_id"], now, note, pid))
    for i in invs:
        conn.execute("UPDATE scanned_invoices SET payment_status='paid', paid_date=COALESCE(paid_date, ?), amount_paid=total, "
                     "balance=0, payment_reference=COALESCE(payment_reference, ?) WHERE id=?",
                     (day, vp["payment_ref"] or f"payment {pid}", i["id"]))
    conn.execute(
        """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
               target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
               match_amount, match_date_diff_days, match_tolerance_days, match_rule)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (WHO, me["bank_account_id"], "vendor_payment", pid, f"Martignetti {vp['payment_ref']} {', '.join(nums)}", day,
         me["id"], day, me["amount"], json.dumps({k: me[k] for k in me.keys() if k != "bank_loc"}), amt, 0, 0,
         f"manual ({WHO}): Martignetti Exchange confirmation {vp['payment_ref']} names the invoices"
         f"{'; intercompany' if ic else ''}"))
    conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (me["id"],))
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,detail) "
                 "VALUES ('row_remap','vendor_payments',?,?,?,?,?)",
                 (pid, vp["gl_account_id"], gl, "intercompany settlement" if ic else "settlement",
                  f"{WHO}: Martignetti draft {day} {amt:.2f} -> "
                  f"{'Loan to Red Nun Dennisport' if ic else 'Accounts Payable'}"))
    return (f"line {line:>4} {me['bank_loc'][:3]} {day} {amt:>8.2f} -> vp#{pid:<3} {'IC ' if ic else '   '}"
            f"{'; '.join(changed) or 'as recorded'}"), ic, amt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2) ORDER BY id")]
        before = {u: state(conn, u) for u in uploads}
        ic_total = 0.0
        for line, pid, paid, nums in PLAN:
            msg, ic, amt = settle(conn, now, line, pid, paid, nums)
            ic_total += amt if ic else 0
            print(msg)
        for pid, nums in RELINK:
            vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (pid,)).fetchone()
            invs = invoices(conn, vp["location"], nums)
            assert abs(sum(i["total"] for i in invs) - vp["payment_total"]) < 0.005
            relink(conn, pid, invs)
            conn.execute("UPDATE vendor_payments SET updated_at=?, memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?",
                         (now, f"{WHO}: invoice links corrected to {', '.join(nums)}", pid))
            print(f"vp#{pid} relinked to {', '.join(nums)}")
        for pid, why in VOID.items():
            vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (pid,)).fetchone()
            assert vp and not vp["cleared"] and "MARTIG" in vp["vendor"].upper()
            conn.execute("UPDATE vendor_payments SET status='void', updated_at=?, "
                         "memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?", (now, f"{WHO}: void — {why}", pid))
            print(f"vp#{pid} voided: {why}")
        print(f"intercompany (Dennis owes Chatham): {ic_total:,.2f}")
        moved = False
        for u in uploads:
            after = state(conn, u)
            if before[u] != after:
                print(f"upload {u}: bank {before[u][0]} -> {after[0]}, delta {before[u][1]} -> {after[1]}, "
                      f"identity {before[u][2]} -> {after[2]}")
            if before[u][:2] != after[:2] or (before[u][2] and not after[2]):
                moved = True
        assert not moved, "tie-out moved"
        print(f"tie-out unchanged on all {len(uploads)} periods")
        left = conn.execute("SELECT COUNT(*) FROM manual_bank_entries WHERE UPPER(payee) LIKE '%MARTIG%'").fetchone()[0]
        print("Martignetti statement lines left:", left)
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
