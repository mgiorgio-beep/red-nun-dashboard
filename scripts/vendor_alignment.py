"""
Line up the two vendor lists before merging the vendor screens (UI plan Phase 5). READ-ONLY.

  Management > Vendors        = the vendors table (contacts, category; ~52 rows)
  Pay Bills > Vendor Bill Pay = vendors table names + every confirmed-invoice vendor
                                name that doesn't fuzzy-match one, with settings in
                                vendor_bill_pay keyed by the name as text

Mike, 2026-10-08: one vendor screen, merged after lining these up; show him any names
that need a decision first. This prints them, grouped:

  A. Invoice names not in the vendors table, each with the nearest vendors-table name
     -> decide: same vendor (alias of X) / new vendor / one-off (leave)
  B. Bill Pay settings saved under a name that isn't in the vendors table
  C. Fields both lists hold that disagree (account #, phone, email)
  D. vendors-table rows with no invoices at all (retire?)

Nothing is changed. Run on Chatham:
  cd /opt/red-nun-dashboard && venv/bin/python3 scripts/vendor_alignment.py
  venv/bin/python3 scripts/vendor_alignment.py --csv /tmp/vendor_alignment.csv
"""
import argparse
import csv
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import get_connection  # noqa: E402


def norm(n):
    """Same normalisation /api/billpay/vendors uses to fold invoice-name variants."""
    n = (n or "").lower().strip()
    for s in [", inc.", ", inc", " inc.", " inc", ", llc", " llc", ", corp", " corp",
              " corporation", " company", " homegrown"]:
        if n.endswith(s):
            n = n[:-len(s)].strip()
    n = n.replace(" and ", " & ")
    n = re.sub(r"[.,\-]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n.replace("foodservice", "food service")


def digits(s):
    return re.sub(r"\D", "", s or "")


def main():
    ap = argparse.ArgumentParser(description="Line up the two vendor lists (read-only).")
    ap.add_argument("--csv", help="also write the decision list (section A) to this CSV")
    args = ap.parse_args()

    conn = get_connection()
    vendors = [dict(r) for r in conn.execute("SELECT * FROM vendors ORDER BY name")]
    bp = {r["vendor_name"]: dict(r) for r in conn.execute("SELECT * FROM vendor_bill_pay")}
    inv = {r["vendor_name"]: dict(r) for r in conn.execute(
        """SELECT vendor_name, COUNT(*) AS n, MAX(invoice_date) AS last, ROUND(SUM(total), 2) AS spend,
                  ROUND(SUM(CASE WHEN COALESCE(payment_status, '') != 'paid' THEN COALESCE(balance, total) ELSE 0 END), 2) AS open
           FROM scanned_invoices WHERE status = 'confirmed' AND vendor_name IS NOT NULL
           GROUP BY vendor_name""")}
    conn.close()

    vnames = [v["name"] for v in vendors]
    vnorm = {norm(n): n for n in vnames}

    def nearest(name):
        m = difflib.get_close_matches(norm(name), list(vnorm), n=1, cutoff=0.5)
        return vnorm[m[0]] if m else ""

    # invoice names folded onto a vendors-table name the way Bill Pay folds them
    folded = {}
    for name in inv:
        folded.setdefault(vnorm.get(norm(name)), []).append(name)

    print(f"vendors table: {len(vendors)} rows · invoice vendor names: {len(inv)} · "
          f"bill pay settings: {len(bp)} rows\n")

    # A. invoice names with no vendors-table match, one decision per normalised name
    groups = {}
    for name, d in inv.items():
        if norm(name) in vnorm:
            continue
        g = groups.setdefault(norm(name), {"names": [], "n": 0, "last": "", "spend": 0.0, "open": 0.0, "bp": False})
        g["names"].append(name)
        g["n"] += d["n"]
        g["last"] = max(g["last"], d["last"] or "")
        g["spend"] += d["spend"] or 0
        g["open"] += d["open"] or 0
        g["bp"] = g["bp"] or name in bp
    a_rows = []
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["spend"]):
        name = max(g["names"], key=lambda n: inv[n]["n"])   # the spelling used most
        a_rows.append((name, g, nearest(name), g["bp"]))
    print(f"A. {len(a_rows)} vendor(s) on invoices but not in the vendors table — decide each:")
    print("   same vendor as the suggestion (alias) / a new vendor / one-off (leave as is)\n")
    for name, g, near, has_bp in a_rows:
        print(f"   {name[:38]:38} {g['n']:>4} inv  last {g['last'] or '?':10}  ${g['spend']:>10,.2f}"
              f"  open ${g['open']:>9,.2f}  {'[bill pay set up] ' if has_bp else ''}"
              f"{'-> like: ' + near if near else ''}")
        others = sorted(set(g["names"]) - {name})
        if others:
            print(f"      also spelled: {', '.join(others)}")

    # also: invoice spellings Bill Pay already folds onto one vendor (FYI, no decision)
    multi = {k: v for k, v in folded.items() if k and len(v) > 1}
    if multi:
        print(f"\n   FYI — spellings already treated as one vendor ({len(multi)}):")
        for k, v in sorted(multi.items()):
            print(f"     {k}: {', '.join(sorted(v))}")

    # B. bill pay settings under a name the vendors table doesn't have
    b_rows = [(n, nearest(n)) for n in sorted(bp) if norm(n) not in vnorm]
    print(f"\nB. {len(b_rows)} Bill Pay setting(s) saved under a name not in the vendors table:")
    for n, near in b_rows:
        r = bp[n]
        print(f"   {n[:38]:38} bill pay {'on ' if r.get('bill_pay_enabled') else 'off'}  "
              f"terms {r.get('payment_term_type') or '-'}  {'-> like: ' + near if near else ''}")

    # C. fields both lists hold that disagree
    print("\nC. Disagreements between the two lists (same vendor):")
    c_count = 0
    for v in vendors:
        b = bp.get(v["name"])
        if not b:
            continue
        for label, va, vb, same in (
            ("account #", v.get("account_number"), b.get("account_number"), lambda x, y: x.strip().lower() == y.strip().lower()),
            ("phone", v.get("phone"), b.get("phone"), lambda x, y: digits(x)[-10:] == digits(y)[-10:]),
            ("email", v.get("email"), b.get("contact_email"), lambda x, y: x.strip().lower() == y.strip().lower()),
        ):
            if va and vb and not same(str(va), str(vb)):
                c_count += 1
                print(f"   {v['name'][:30]:30} {label:9}  Vendors: {va}   |   Bill Pay: {vb}")
    if not c_count:
        print("   none")

    # D. vendors-table rows with no invoices under any spelling
    d_rows = [v for v in vendors if not folded.get(v["name"])]
    print(f"\nD. {len(d_rows)} vendors-table row(s) with no confirmed invoices under a matching name "
          f"(retire, or an alias from A?):")
    for v in d_rows:
        print(f"   {v['name'][:38]:38} {v.get('category') or '':12} {'(inactive)' if not v.get('active', 1) else ''}")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["invoice_vendor_name", "also_spelled", "invoices", "last_invoice", "spend", "open",
                        "bill_pay_set_up", "nearest_vendor", "decision (alias of X / new / one-off)"])
            for name, g, near, has_bp in a_rows:
                w.writerow([name, "; ".join(sorted(set(g["names"]) - {name})), g["n"], g["last"],
                            round(g["spend"], 2), round(g["open"], 2), "yes" if has_bp else "", near, ""])
        print(f"\nWrote {args.csv} — fill in the last column.")


if __name__ == "__main__":
    main()
