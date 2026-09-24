#!/usr/bin/env python3
"""Mike, 2026-09-24 — single-entity vendors, #345, PFG March lines and the
Caron check chain. One transaction; every statement period's bank balance and
delta asserted unchanged.

A  Fore & Aft = Chatham landscaping only. Every Fore & Aft bill on Dennis was
   entered on the wrong entity: the five invoices and four payments move to
   Chatham as Chatham's own (Chatham A/P), each paid where Intuit's QuickBooks
   Payments receipts (Mike's inbox) say:
     #258 inv 16565   325.00  receipt 4/30  -> Chatham 5/01 "SALE FORE & AFT" line 2228
     #498 inv 16775 1,680.00  receipt 7/15  -> already on Chatham line 2714; intercompany reversed
     #499 inv 16737   260.00  receipt 7/15  -> Chatham 7/15 line 2710 (NOT intercompany)
     #501 inv 16825   185.00  receipt 7/15, paid by MASTERCARD -> no bank line exists on
          either account; the Bill Pay row is voided as a register row, the invoice stays paid
     #440 inv 16741   410.00  receipt 7/15  -> Chatham 7/15 line 2711 (approved)
   Chatham line 2417 (5/28 card 325) paid Chatham invoice 16726 (receipt 5/27,
   VISA; invoice not on file) -> Landscaping.
B  Barrows = Dennis trash only. I18736 (May) was paid twice: Chatham card 4708
   on 5/28 (Barrows' receipt, scanned onto Dennis as a second "invoice") and
   Dennis check 9681 (6/03, memo "Inv #I18736", cleared 6/10).
     #347 -> Chatham line 2422 (5/29, card 4708), intercompany (Loan to Red Nun Dennisport)
     invoice row 100557 (the receipt) -> status 'duplicate' (was counting a second $500)
     #375 noted: Barrows holds a $500 credit for Dennis.
C  #345 is Dennis check 10004 to Red Buoy Inc, "Intercompany - Martignetti
   reimb 3/30-5/25/26" (check image): Liquor COGS -> Loan to Red Buoy Inc.
   Chatham's 5/27 deposit of it (line 2403, coded Cash Sales) -> Loan to Red
   Nun Dennisport.
D  PFG 729650 and 731144 (Dennis, March) had no line items: loaded from the
   PFG portal CSV export (same parser the import uses).
E  Caron checks: the import matcher and job066 paired them on amount alone,
   oldest first, shifting the chain by one check on both accounts (rows
   "cleared" before their own check date). Each statement check number now
   clears the book row carrying that number; check images confirm payee and
   date. Cleared counts per account are unchanged, so bank sides do not move.

    python scripts/vendor_entity_and_caron_2026_09_24.py            # dry run
    python scripts/vendor_entity_and_caron_2026_09_24.py --apply
"""
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from routes import bank_reconcile_routes as brr  # noqa: E402
from integrations.invoices.processor import parse_pfg_csv_invoice  # noqa: E402

WHO = "mike-2026-09-24"
AP_CHATHAM, LANDSCAPING_CHATHAM = 258, 509
LOAN_TO_DENNIS, LOAN_TO_RED_BUOY = 527, 140
PFG_CSV = {100304: "729650", 100303: "731144"}
PFG_DIR = os.environ.get("PFG_CSV_DIR", "")

CARON_CHATHAM = {  # payment id -> (check number in payment_ref, statement clearing date or None)
    239: ("2048", "2026-05-04"), 365: ("2093", "2026-06-05"), 366: ("2094", "2026-06-11"),
    378: ("2097", "2026-06-16"), 393: ("2100", None), 444: ("2124", "2026-07-02"),
    461: ("2152", "2026-07-10"), 482: ("2181", "2026-07-16"), 503: ("2184", "2026-07-30"),
    559: ("2218", "2026-08-17"), 562: ("2221", "2026-08-17"), 563: ("2222", "2026-08-17"),
    583: ("2225", "2026-08-24"), 591: ("2242", "2026-08-26"), 609: ("2245", None),
}
CARON_DENNIS = {
    255: ("AP39", "2026-05-22"),   # statement check 9718 of 5/22; image: Caron, May 07 = #255's date
    364: ("9679", "2026-06-05"), 367: ("9680", "2026-06-11"), 377: ("9682", "2026-06-16"),
    392: ("9685", None), 445: ("9697", "2026-07-02"), 460: ("9707", "2026-07-10"),
    483: ("9718", "2026-07-16"), 505: ("9720", "2026-07-30"), 558: ("9731", "2026-08-17"),
    566: ("9733", "2026-08-17"), 568: ("9735", "2026-08-17"), 585: ("9743", "2026-08-24"),
    590: ("9747", "2026-08-26"), 608: ("9750", None),
}
# job066 audits that recorded the wrong target: audit id -> (wrong target, right target)
RETARGET = {25: (609, 591), 78: (608, 590), 69: (367, 255)}


def state(conn, uid):
    up = conn.execute("SELECT * FROM bank_statement_uploads WHERE id=?", (uid,)).fetchone()
    st = brr._reconciliation_state(conn, up)
    return (st["bank_balance"], st["delta"], st["outstanding_net"])


def note(conn, table, rid, text):
    conn.execute(f"UPDATE {table} SET memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=?",
                 (f"{WHO}: {text}", rid))


def log(conn, table, rid, old, new, rule, detail):
    conn.execute("INSERT INTO gl_repair_log (kind,target_table,target_id,old_gl_account_id,new_gl_account_id,match_rule,"
                 "detail) VALUES ('row_remap',?,?,?,?,?,?)", (table, rid, old, new, rule, f"{WHO}: {detail}"))


def merge(conn, vid, line, amt, date, rule, gl, extra_sets=None):
    """Clear Bill Pay row `vid` by Chatham statement line `line` (deleted, captured in the audit)."""
    vp = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
    me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=?", (line,)).fetchone()
    assert vp and not vp["cleared"] and (vp["status"] or "") not in ("void", "failed"), f"#{vid}"
    assert me and me["bank_account_id"] == 1 and me["entry_date"] == date and abs(me["amount"] + amt) < 0.005, line
    assert abs(vp["payment_total"] - amt) < 0.005, vid
    sets = {"bank_account_id": 1, "cleared": 1, "cleared_date": date, "gl_account_id": gl,
            "gl_source": "human", "gl_status": "confirmed", "updated_at": datetime.now().isoformat(timespec="seconds")}
    sets.update(extra_sets or {})
    conn.execute(f"UPDATE vendor_payments SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), vid))
    log(conn, "vendor_payments", vid, vp["gl_account_id"], gl, rule, f"cleared by Chatham line {line} ({date})")
    conn.execute(
        """INSERT INTO register_merge_audit (merged_by, bank_account_id, target_source, target_id, target_label,
               target_cleared_date, deleted_entry_id, deleted_entry_date, deleted_entry_amount, deleted_entry_json,
               match_amount, match_date_diff_days, match_tolerance_days, match_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (WHO, 1, "vendor_payment", vid, f"{vp['vendor']} {vp['payment_ref']}", date, line, date, me["amount"],
         json.dumps(dict(me)), amt, None, None, f"manual ({WHO}): {rule}"))
    conn.execute("DELETE FROM manual_bank_entries WHERE id=?", (line,))
    return vp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    now = datetime.now().isoformat(timespec="seconds")
    conn = brr.get_connection()
    try:
        uploads = [r[0] for r in conn.execute("SELECT id FROM bank_statement_uploads WHERE bank_account_id IN (1,2)")]
        before = {u: state(conn, u) for u in uploads}

        # ── A  Fore & Aft ────────────────────────────────────────────────
        fa = conn.execute("SELECT id, invoice_number FROM scanned_invoices WHERE vendor_name LIKE 'Fore%Aft%' "
                          "AND location='dennis' AND status='confirmed'").fetchall()
        assert sorted(r["invoice_number"] for r in fa) == ["16565", "16737", "16775", "16825", "16876"], fa
        conn.execute(f"UPDATE scanned_invoices SET location='chatham' WHERE id IN ({','.join(str(r['id']) for r in fa)})")
        print(f"A  {len(fa)} Fore & Aft invoices Dennis -> Chatham: {', '.join(r['invoice_number'] for r in fa)}")

        v = conn.execute("SELECT * FROM vendor_payments WHERE id=498").fetchone()
        assert v["location"] == "dennis" and v["bank_account_id"] == 1 and v["cleared"] and v["gl_account_id"] == LOAN_TO_DENNIS
        conn.execute("UPDATE vendor_payments SET location='chatham', gl_account_id=?, gl_source='human', gl_status='confirmed', "
                     "updated_at=? WHERE id=498", (AP_CHATHAM, now))
        log(conn, "vendor_payments", 498, LOAN_TO_DENNIS, AP_CHATHAM, "Fore & Aft is Chatham's",
            "intercompany pairing reversed: Chatham's own landscaping bill")
        note(conn, "vendor_payments", 498, "Fore & Aft is Chatham's own bill (invoice 16775 was filed on Dennis in "
                                           "error) — intercompany coding reversed, Chatham A/P")
        print("A  #498 1,680.00 inv 16775: stays on Chatham line 2714 (7/15); intercompany reversed -> Chatham A/P")

        merge(conn, 499, 2710, 260.00, "2026-07-15", "Fore & Aft inv 16737, QB receipt 7/15", AP_CHATHAM,
              {"location": "chatham"})
        note(conn, "vendor_payments", 499, "Chatham's own bill (16737 filed on Dennis in error); paid 7/15 per QB receipt")
        merge(conn, 258, 2228, 325.00, "2026-05-01", "Fore & Aft inv 16565, QB receipt 4/30", AP_CHATHAM,
              {"location": "chatham", "payment_date": "2026-04-30", "payment_method": "qb_autopay"})
        note(conn, "vendor_payments", 258, "Chatham's own bill (16565 filed on Dennis in error); QB receipt: paid 4/30, "
                                           "cleared Chatham 5/01")
        merge(conn, 440, 2711, 410.00, "2026-07-15", "Fore & Aft inv 16741, QB receipt 7/15", AP_CHATHAM)
        note(conn, "vendor_payments", 440, "paid 7/15 per QB receipt; cleared Chatham 7/15 (line was Landscaping "
                                           "beside the invoice — counted twice)")
        print("A  #499 260.00 -> Chatham line 2710 (7/15); #258 325.00 -> Chatham line 2228 (5/01); "
              "#440 410.00 -> Chatham line 2711 (7/15)")

        v = conn.execute("SELECT * FROM vendor_payments WHERE id=501").fetchone()
        assert v["location"] == "dennis" and not v["cleared"] and abs(v["payment_total"] - 185) < 0.005
        conn.execute("UPDATE vendor_payments SET location='chatham', bank_account_id=1, status='void', "
                     "payment_method='mastercard', updated_at=? WHERE id=501", (now,))
        note(conn, "vendor_payments", 501, "paid 7/15 by MASTERCARD (QB receipt, invoice 16825) — not a payment from "
                                           "either bank account, so not a register row; invoice stays paid")
        conn.execute("UPDATE scanned_invoices SET payment_reference='Mastercard 7/15/2026 (QuickBooks Payments receipt)' "
                     "WHERE location='chatham' AND invoice_number='16825' AND vendor_name LIKE 'Fore%Aft%'")
        print("A  #501 185.00 inv 16825: paid by Mastercard -> register row voided, invoice stays paid (Chatham)")

        me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=2417").fetchone()
        assert me["bank_account_id"] == 1 and abs(me["amount"] + 325) < 0.005 and me["gl_account_id"] is None
        conn.execute("UPDATE manual_bank_entries SET gl_account_id=?, gl_source='human', gl_status='confirmed', "
                     "memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=2417",
                     (LANDSCAPING_CHATHAM, f"{WHO}: Fore & Aft invoice 16726 (QB receipt 5/27, VISA)"))
        log(conn, "manual_bank_entries", 2417, None, LANDSCAPING_CHATHAM, "Fore & Aft is Chatham's", "invoice 16726")
        print("A  Chatham line 2417 (5/28 card 325, invoice 16726) uncoded -> Landscaping")

        # ── B  Barrows ───────────────────────────────────────────────────
        merge(conn, 347, 2422, 500.00, "2026-05-29", "intercompany — Chatham card 4708 paid Dennis Barrows I18736",
              LOAN_TO_DENNIS)
        note(conn, "vendor_payments", 347, "intercompany — Chatham 5975 (card 4708, Barrows receipt 5/28) paid Dennis's "
                                           "I18736; Chatham books Loan to Red Nun Dennisport, Dennis Dr AP / Cr Loan to Red Buoy Inc.")
        inv = conn.execute("SELECT * FROM scanned_invoices WHERE id=100557").fetchone()
        assert inv["invoice_number"] == "I18736" and inv["status"] == "confirmed" and "Payment made via Visa" in inv["notes"]
        conn.execute("UPDATE scanned_invoices SET status='duplicate', notes=TRIM(COALESCE(notes,'') || ' | ' || ?, ' |') "
                     "WHERE id=100557", (f"{WHO}: this is Barrows' PAYMENT RECEIPT for I18736, not a second invoice — "
                                         f"I18736 is invoice 100526 (5/31)",))
        note(conn, "vendor_payments", 375, "check 9681 paid I18736 a second time (Chatham card 4708 paid it 5/28) — "
                                           "Barrows holds a $500 credit for Dennis")
        print("B  #347 500.00 -> Chatham line 2422 (5/29, card 4708) intercompany; receipt row 100557 -> duplicate; "
              "#375 flagged as a second payment of I18736")

        # ── C  #345 ──────────────────────────────────────────────────────
        v = conn.execute("SELECT * FROM vendor_payments WHERE id=345").fetchone()
        assert v["vendor"] == "Red Buoy Inc" and v["gl_account_id"] == 12 and abs(v["payment_total"] - 5394.03) < 0.005
        conn.execute("UPDATE vendor_payments SET gl_account_id=?, gl_source='human', gl_status='confirmed', updated_at=? "
                     "WHERE id=345", (LOAN_TO_RED_BUOY, now))
        log(conn, "vendor_payments", 345, 12, LOAN_TO_RED_BUOY, "intercompany repayment",
            "check 10004 to Red Buoy Inc: 'Intercompany - Martignetti reimb 3/30-5/25/26'")
        me = conn.execute("SELECT * FROM manual_bank_entries WHERE id=2403").fetchone()
        assert me["bank_account_id"] == 1 and abs(me["amount"] - 5394.03) < 0.005
        conn.execute("UPDATE manual_bank_entries SET gl_account_id=?, gl_source='human', gl_status='confirmed', "
                     "memo=TRIM(COALESCE(memo,'') || ' | ' || ?, ' |') WHERE id=2403",
                     (LOAN_TO_DENNIS, f"{WHO}: Dennis check 10004 — intercompany Martignetti reimbursement"))
        log(conn, "manual_bank_entries", 2403, me["gl_account_id"], LOAN_TO_DENNIS, "intercompany repayment",
            "deposit of Dennis check 10004 was coded Cash Sales")
        print("C  #345 5,394.03 Liquor COGS -> Loan to Red Buoy Inc.; Chatham deposit 2403 Cash Sales -> Loan to Red Nun Dennisport")

        # ── D  PFG lines ─────────────────────────────────────────────────
        for inv_id, num in PFG_CSV.items():
            inv = conn.execute("SELECT * FROM scanned_invoices WHERE id=?", (inv_id,)).fetchone()
            assert inv["invoice_number"] == num and inv["location"] == "dennis"
            assert not conn.execute("SELECT 1 FROM scanned_invoice_items WHERE invoice_id=?", (inv_id,)).fetchone()
            path = [os.path.join(PFG_DIR, f) for f in os.listdir(PFG_DIR) if num in f and f.endswith(".csv")][0]
            (p,) = parse_pfg_csv_invoice(open(path, "rb").read(), location="dennis")
            assert p["invoice_number"] == num and abs(p["total"] - inv["total"]) < 0.005, (p["total"], inv["total"])
            for it in p["line_items"]:
                conn.execute("""INSERT INTO scanned_invoice_items (invoice_id, product_name, description, quantity, unit,
                                    unit_price, total_price, category_type, price_change_pct, is_price_spike, pack_size,
                                    vendor_item_code) VALUES (?,?,?,?,?,?,?,?,0,0,?,?)""",
                             (inv_id, it["product_name"], it.get("description"), it.get("quantity") or 0, it.get("unit"),
                              it.get("unit_price") or 0, it.get("total_price") or 0, it.get("category"),
                              it.get("pack_size"), it.get("vendor_item_code")))
            conn.execute("UPDATE scanned_invoices SET subtotal=?, tax=?, notes=TRIM(COALESCE(notes,'') || ' | ' || ?, ' |') "
                         "WHERE id=?", (p["subtotal"], p["tax"], f"{WHO}: {len(p['line_items'])} lines loaded from the "
                                                                   f"PFG portal CSV export", inv_id))
            unm = conn.execute("""SELECT COUNT(*) FROM scanned_invoice_items ii LEFT JOIN gl_category_mapping cm
                                   ON cm.location='dennis' AND cm.category_type=ii.category_type
                                   WHERE ii.invoice_id=? AND cm.gl_account_id IS NULL""", (inv_id,)).fetchone()[0]
            assert unm == 0, f"{num}: {unm} unmapped lines"
            print(f"D  PFG {num}: {len(p['line_items'])} lines {p['subtotal']:,.2f} + tax {p['tax']:,.2f} = {p['total']:,.2f}")

        # ── E  Caron chain ───────────────────────────────────────────────
        for bank, chain in ((1, CARON_CHATHAM), (2, CARON_DENNIS)):
            n_before = sum(1 for vid in chain if conn.execute("SELECT cleared FROM vendor_payments WHERE id=?", (vid,)).fetchone()[0])
            moved = []
            for vid, (num, date) in chain.items():
                v = conn.execute("SELECT * FROM vendor_payments WHERE id=?", (vid,)).fetchone()
                assert v["bank_account_id"] == bank and "Caron" in v["vendor"] and num in (v["payment_ref"] or ""), vid
                if (v["cleared_date"] or None) != date:
                    moved.append(f"#{vid} {num}: {v['cleared_date'] or 'outstanding'} -> {date or 'outstanding'}")
                conn.execute("UPDATE vendor_payments SET cleared=?, cleared_date=?, updated_at=? WHERE id=?",
                             (1 if date else 0, date, now, vid))
            n_after = sum(1 for d in chain.values() if d[1])
            assert n_before == n_after, (bank, n_before, n_after)
            print(f"E  {'Chatham' if bank == 1 else 'Dennis'} Caron: {len(moved)} rows re-dated")
            for m in moved:
                print("     " + m)
        for aid, (wrong, right) in RETARGET.items():
            au = conn.execute("SELECT * FROM register_merge_audit WHERE id=?", (aid,)).fetchone()
            assert au["target_id"] == wrong and au["reversed_at"] is None
            label = conn.execute("SELECT vendor || ' ' || payment_ref FROM vendor_payments WHERE id=?", (right,)).fetchone()[0]
            conn.execute("UPDATE register_merge_audit SET target_id=?, target_label=?, match_rule=match_rule || ? WHERE id=?",
                         (right, label, f" | {WHO}: retargeted #{wrong} -> #{right} by check number + image", aid))

        for u in uploads:
            b, a_ = before[u], state(conn, u)
            assert b[:2] == a_[:2], f"upload {u} bank side moved {b} -> {a_}"
            if abs(b[2] - a_[2]) > 0.004:
                print(f"   upload {u}: outstanding {b[2]:,.2f} -> {a_[2]:,.2f}")
        if a.apply:
            conn.commit(); print("APPLIED")
        else:
            conn.rollback(); print("DRY RUN — nothing written")
    except Exception:
        conn.rollback(); print("ROLLED BACK"); raise


if __name__ == "__main__":
    main()
