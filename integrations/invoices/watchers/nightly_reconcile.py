#!/usr/bin/env python3
"""
Nightly finance reconciliation (Jarvis) — runs 6pm ET via cron on the Beelink.

Sweeps for (1) payments that were made but never matched to an invoice
(especially QuickBooks payment notifications that were logged `no_match`
because the invoice had not been scanned yet), (2) suspected duplicate
invoices, and (3) invoice-vendor emails with no matching dashboard invoice
(possible missed imports).

Policy (user-chosen 2026-08-10): AUTO-APPLY only EXACT payment matches
(invoice# + amount + vendor all agree with a confirmed, unpaid invoice).
Everything ambiguous — duplicates, partials, missed invoices — is EMAILED
for manual review, never auto-changed.

Model: uses Claude Haiku only to write the summary email (cheap). All
reconciliation decisions are deterministic.

Usage:
    python3 nightly_reconcile.py            # live: apply safe matches + email
    python3 nightly_reconcile.py --dry-run  # print only, no writes, no email
"""
import os, sys, json, re, base64, datetime, urllib.request
sys.path.insert(0, "/opt/red-nun-dashboard")
sys.path.insert(0, "/opt/red-nun-dashboard/integrations/invoices/watchers")
from dotenv import load_dotenv
load_dotenv("/opt/red-nun-dashboard/.env")

from integrations.toast.data_store import get_connection
import email_receipt_poller as rp   # reuse Gmail auth, apply_payment, send_alert

DRY = "--dry-run" in sys.argv
AUDIT = "/opt/red-nun-dashboard/monitoring/receipt_poller_audit.jsonl"
STATE = "/opt/red-nun-dashboard/monitoring/nightly_reconcile_state.json"
HAIKU = "claude-haiku-4-5-20251001"
TODAY = datetime.date.today().isoformat()

def norm_vendor(n):
    n = (n or "").lower().strip()
    for s in [", inc.", ", inc", " inc.", " inc", ", llc", " llc", " corporation",
              " company", ", co.", " co."]:
        if n.endswith(s):
            n = n[:-len(s)].strip()
    n = n.replace(" and ", " & ")
    return re.sub(r"[.,\-]+", " ", n).strip()

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"resolved_msg_ids": []}

def save_state(st):
    if DRY: return
    json.dump(st, open(STATE, "w"), indent=2)

# ── Step 1: retroactive payment matching from the no_match audit log ──────────
def unmatched_payments():
    seen, out = {}, []
    if not os.path.exists(AUDIT):
        return out
    for line in open(AUDIT):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("event") != "no_match" or not e.get("amount"):
            continue
        mid = e.get("message_id")
        if mid:
            seen[mid] = e  # keep latest occurrence per email
    return list(seen.values())

def find_exact_invoice(conn, vendor, amount, invnum):
    """Return invoice id ONLY if a single confirmed, unpaid invoice matches
    invoice# + amount + vendor. Otherwise (None, candidate_list)."""
    rows = conn.execute(
        "SELECT id, vendor_name, invoice_number, total, COALESCE(balance,total) bal, payment_status "
        "FROM scanned_invoices WHERE status='confirmed' "
        "AND (payment_status IS NULL OR payment_status IN ('unpaid','partial'))"
    ).fetchall()
    tv = norm_vendor(vendor)
    cands = []
    for r in rows:
        if norm_vendor(r["vendor_name"]) != tv:
            continue
        if abs(float(r["bal"] or 0) - float(amount)) > 0.02:
            continue
        cands.append(r)
    exact = [r for r in cands if invnum and str(r["invoice_number"] or "").strip().lstrip("0") == str(invnum).strip().lstrip("0")]
    if len(exact) == 1:
        return exact[0]["id"], cands
    return None, cands

def reconcile_payments(conn, st):
    applied, flagged = [], []
    resolved = set(st.get("resolved_msg_ids", []))
    for e in unmatched_payments():
        mid = e.get("message_id")
        if mid in resolved:
            continue
        vendor, amount = e.get("vendor"), float(e.get("amount"))
        invnum = str(e.get("invoice_number") or "").strip()
        pdate = e.get("payment_date") or TODAY
        inv_id, cands = find_exact_invoice(conn, vendor, amount, invnum)
        if inv_id:
            ref = f"QB payment #{invnum}" if invnum else "QB payment"
            memo = "nightly reconcile: retroactively matched QuickBooks payment (was no_match)"
            if DRY:
                applied.append(f"[DRY] would mark inv {inv_id} PAID — {vendor} #{invnum} ${amount:.2f} ({pdate})")
            else:
                try:
                    rp.apply_payment(inv_id, amount, "QuickBooks", pdate, ref, memo)
                    applied.append(f"inv {inv_id} PAID — {vendor} #{invnum} ${amount:.2f} ({pdate})")
                    resolved.add(mid)
                except Exception as ex:
                    flagged.append(f"FAILED to apply {vendor} #{invnum} ${amount:.2f}: {ex}")
        elif cands:
            names = ", ".join(f"inv {c['id']}(#{c['invoice_number'] or '—'})" for c in cands)
            flagged.append(f"AMBIGUOUS payment {vendor} #{invnum} ${amount:.2f} → amount matches {len(cands)}: {names} (no exact invoice# match)")
        # no candidates at all → payment for an invoice not in the system; report sparingly
    st["resolved_msg_ids"] = sorted(resolved)
    return applied, flagged

# ── Step 2: suspected duplicate invoices ──────────────────────────────────────
def find_duplicates(conn):
    """Precise duplicate detection to avoid flagging legit RECURRING bills
    (same vendor + amount but distinct invoice numbers). A duplicate is:
      (a) an unpaid invoice sharing a non-empty invoice# with a PAID invoice, or
      (b) an unpaid invoice with NO invoice# whose vendor+total matches a PAID
          invoice (the classic re-scan-lost-the-number case, e.g. Fore&Aft 100817).
    """
    dups = []
    rows = conn.execute(
        "SELECT id, vendor_name, invoice_number, total, payment_status "
        "FROM scanned_invoices WHERE status='confirmed'"
    ).fetchall()
    by_num, paid_by_vt = {}, {}
    for r in rows:
        num = str(r["invoice_number"] or "").strip()
        if num:
            by_num.setdefault((norm_vendor(r["vendor_name"]), num.lstrip("0")), []).append(r)
        if r["payment_status"] == "paid":
            paid_by_vt.setdefault((norm_vendor(r["vendor_name"]), round(float(r["total"] or 0), 2)), []).append(r)
    for r in rows:
        if r["payment_status"] not in (None, "unpaid") or float(r["total"] or 0) <= 0:
            continue
        num = str(r["invoice_number"] or "").strip()
        if num:
            paid_twins = [t for t in by_num.get((norm_vendor(r["vendor_name"]), num.lstrip("0")), [])
                          if t["id"] != r["id"] and t["payment_status"] == "paid"]
            if paid_twins:
                dups.append(f"inv {r['id']} ({r['vendor_name']} #{num} ${r['total']:.2f}) shares its invoice# with PAID inv {paid_twins[0]['id']} — likely duplicate")
        else:
            twins = paid_by_vt.get((norm_vendor(r["vendor_name"]), round(float(r["total"] or 0), 2)))
            if twins:
                dups.append(f"inv {r['id']} ({r['vendor_name']} no-# ${r['total']:.2f}) matches PAID inv {twins[0]['id']} (#{twins[0]['invoice_number'] or '—'}) — likely duplicate scan that lost its number")
    return dups

# ── Step 3: invoice-vendor emails with no matching dashboard invoice ──────────
def missed_invoices():
    notes = []
    try:
        svc = rp.get_gmail_service()
    except Exception as ex:
        return [f"(missed-invoice sweep skipped: Gmail unavailable — {ex})"]
    try:
        q = "newer_than:3d -in:trash (from:noreply@vtinfo.com OR from:usfoods-notification@usfoods.com OR from:no-reply@valet.billfire.com OR from:support@cintas.com)"
        res = svc.users().messages().list(userId="me", q=q, maxResults=25).execute()
        msgs = res.get("messages", [])
        conn = get_connection()
        recent = conn.execute("SELECT COUNT(*) c FROM scanned_invoices WHERE created_at >= date('now','-3 day')").fetchone()["c"]
        conn.close()
        notes.append(f"{len(msgs)} invoice-vendor emails in last 3d; {recent} invoices imported in last 3d.")
        if len(msgs) > recent:
            notes.append(f"⚠ Possible shortfall: {len(msgs) - recent} more vendor emails than imported invoices — check for a missed import.")
    except Exception as ex:
        notes.append(f"(missed-invoice sweep error: {ex})")
    return notes

# ── Step 4: Haiku-written summary email ───────────────────────────────────────
def compose_and_send(applied, flagged, dups, missed):
    structured = {
        "date": TODAY,
        "auto_applied_payments": applied,
        "flagged_for_review": flagged,
        "suspected_duplicates": dups,
        "missed_invoice_check": missed,
    }
    body = None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        try:
            prompt = ("Write a concise plain-text nightly finance reconciliation email for a restaurant "
                      "owner (Mike). Use short sections and bullet lists. Lead with what was auto-applied, "
                      "then what needs his review. Be factual, no fluff. Data:\n" + json.dumps(structured, indent=2))
            req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                data=json.dumps({"model": HAIKU, "max_tokens": 1200,
                                 "messages": [{"role": "user", "content": prompt}]}).encode(),
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            r = json.load(urllib.request.urlopen(req, timeout=60))
            body = "".join(b.get("text", "") for b in r.get("content", []))
        except Exception as ex:
            body = None
    if not body:
        lines = [f"Nightly reconciliation — {TODAY}", "",
                 f"AUTO-APPLIED payments ({len(applied)}):"] + [f"  • {a}" for a in applied] + \
                ["", f"NEEDS REVIEW ({len(flagged)}):"] + [f"  • {f}" for f in flagged] + \
                ["", f"SUSPECTED DUPLICATES ({len(dups)}):"] + [f"  • {d}" for d in dups] + \
                ["", "MISSED-INVOICE CHECK:"] + [f"  • {m}" for m in missed]
        body = "\n".join(lines)
    subject = f"[Red Nun] Nightly reconcile {TODAY} — {len(applied)} applied, {len(flagged)+len(dups)} to review"
    if DRY:
        print("==== EMAIL (dry-run, not sent) ====\nSubject:", subject, "\n\n" + body)
    else:
        rp.send_alert(subject, body)

def main():
    conn = get_connection()
    st = load_state()
    applied, flagged = reconcile_payments(conn, st)
    dups = find_duplicates(conn)
    conn.close()
    missed = missed_invoices()
    save_state(st)
    compose_and_send(applied, flagged, dups, missed)
    print(f"DONE (dry-run={DRY}): applied={len(applied)} flagged={len(flagged)} dups={len(dups)}")

if __name__ == "__main__":
    main()
