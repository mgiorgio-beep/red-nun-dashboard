"""
Apply Mike's vendor decisions (2026-10-15) before the vendor screens merge (UI plan
Phase 5). DRY RUN unless --apply. Decisions came from scripts/vendor_alignment.py.

Touches ONLY the vendors table and one duplicate vendor_bill_pay row. Invoices, payments,
the register, GL coding, the P&L and Bill Pay settings keep their names: several of
those look a vendor up by its exact invoice spelling (check printing, auto-pay), so
nothing they key on is renamed.

  1. Add vendors that exist on invoices / in Bill Pay but not in the vendors table;
     copy phone, email, account # from their Bill Pay setting where the new row is blank.
  2. Correct three vendors-table names to the spelling the invoices / Bill Pay use.
  3. Fold the old, disabled "Bay State Sewage Disposal Inc" Bill Pay setting into the
     LLC one (fill the LLC's blanks from it, then remove it) — no invoices use it.
  4. Mark MarginEdge (cancelled) and the internal-transfer row inactive.
Left alone, waiting on Mike: Sprague and UniFirst account numbers (the two lists
disagree). Hinckley Home Center stays off the list (one-off).

  cd /opt/red-nun-dashboard && venv/bin/python3 scripts/vendor_merge_2026_10.py          # show
  venv/bin/python3 scripts/vendor_merge_2026_10.py --apply                               # do it

--apply first backs up the database with SQLite's backup API (a plain cp of the live
WAL database is torn) to /opt/backups/, then makes every change in one transaction.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import DB_PATH, get_connection  # noqa: E402

# Exact names as the invoices / Bill Pay spell them. A name ending in "*" is matched
# by prefix (the alignment report truncated it).
NEW_VENDORS = [
    "Fore & Aft, Inc.", "Bay State Sewage Disposal, LLC", "Acrisure East Insurance Services, LLC",
    "Harris Warren Commercial Kitchens", "Barrows Waste Systems, LLC", "Glanola North America",
    "Tiger Exchange Inc.", "7shifts (US), Corp", "Oceanside, Inc.", "Toast Inc.",
    "Chatham Chamber of Commerce*", "Google LLC", "Anthropic, PBC",
]
RENAMES = [   # vendors-table name -> the spelling invoices / Bill Pay use
    ("Fire Equipment Inc", "Fire Equipment Incorporated"),
    ("J.W. Dubis & Sins, INc", "J.W. Dubis & Sons, Inc."),
    ("Affirmed Medical Service", "Affirmed Medical"),
]
BP_FOLD = ("Bay State Sewage Disposal Inc", "Bay State Sewage Disposal, LLC")
INACTIVE = ["MarginEdge, Inc", "Red Nun Bar & Grill- Internal Transfer"]
BP_FIELDS = ("payment_recipient", "remit_address_1", "remit_address_2", "remit_city", "remit_state",
             "remit_zip", "account_number", "contact_email", "phone", "notes")


def resolve_invoice_name(conn, name):
    if not name.endswith("*"):
        return name
    rows = conn.execute("SELECT DISTINCT vendor_name FROM scanned_invoices WHERE vendor_name LIKE ? ",
                        (name[:-1] + "%",)).fetchall()
    if len(rows) != 1:
        raise SystemExit(f"'{name}' matches {len(rows)} invoice names — expected exactly one")
    return rows[0][0]


def plan(conn):
    steps, problems = [], []
    vnames = {r["name"].lower(): r for r in conn.execute("SELECT * FROM vendors")}
    bp = {r["vendor_name"].lower(): dict(r) for r in conn.execute("SELECT * FROM vendor_bill_pay")}

    for raw in NEW_VENDORS:
        name = resolve_invoice_name(conn, raw)
        if name.lower() in vnames:
            steps.append(("skip", f"already in vendors: {name}", None))
            continue
        b = bp.get(name.lower()) or {}
        fields = {"name": name, "phone": b.get("phone"), "email": b.get("contact_email"),
                  "account_number": b.get("account_number")}
        extra = ", ".join(f"{k} {v}" for k, v in fields.items() if k != "name" and v)
        steps.append(("add", f"add vendor: {name}" + (f"  ({extra} from Bill Pay)" if extra else ""), fields))

    taken = set(vnames)
    for old, new in RENAMES:
        if old.lower() not in vnames and new.lower() in vnames:
            steps.append(("skip", f"already renamed: {new}", None))
        elif old.lower() not in vnames:
            problems.append(f"rename: '{old}' isn't in the vendors table")
        elif new.lower() in taken and new.lower() != old.lower():
            problems.append(f"rename: '{new}' already exists in the vendors table")
        else:
            steps.append(("rename", f"rename vendor: '{old}' -> '{new}'", (vnames[old.lower()]["id"], new)))
            taken.add(new.lower())

    old, keep = BP_FOLD
    if old.lower() in bp:
        if keep.lower() not in bp:
            problems.append(f"fold: '{keep}' has no Bill Pay setting to fold into")
        elif conn.execute("SELECT 1 FROM scanned_invoices WHERE vendor_name = ? COLLATE NOCASE LIMIT 1",
                          (old,)).fetchone():
            problems.append(f"fold: invoices still use '{old}' — not removing its Bill Pay setting")
        else:
            o, k = bp[old.lower()], bp[keep.lower()]
            fill = {f: o[f] for f in BP_FIELDS if o.get(f) and not k.get(f)}
            steps.append(("fold", f"Bill Pay: remove disabled '{old}'"
                          + (f", first copying {', '.join(fill)} to '{keep}'" if fill else " (nothing to copy)"),
                          (o["id"], k["id"], fill)))
    else:
        steps.append(("skip", f"no Bill Pay setting named '{old}'", None))

    for name in INACTIVE:
        r = vnames.get(name.lower())
        if not r:
            problems.append(f"inactive: '{name}' isn't in the vendors table")
        elif not r["active"]:
            steps.append(("skip", f"already inactive: {name}", None))
        else:
            steps.append(("inactive", f"mark inactive: {name}", r["id"]))
    return steps, problems


def backup():
    folder = os.getenv("BACKUP_DIR", "/opt/backups")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"toast_data_{datetime.now():%Y%m%d_%H%M}.db")
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(path)
    src.backup(dst)
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    src.close(); dst.close()
    if ok != "ok":
        raise SystemExit(f"backup {path} failed its integrity check: {ok}")
    return path


def main():
    ap = argparse.ArgumentParser(description="Apply the 2026-10-15 vendor decisions (dry run unless --apply).")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = get_connection()
    steps, problems = plan(conn)
    for kind, text, _ in steps:
        print(("   " if kind == "skip" else " * ") + text)
    if problems:
        print("\nNOT applying — fix these first:\n  " + "\n  ".join(problems))
        sys.exit(1)
    todo = [s for s in steps if s[0] != "skip"]
    print(f"\n{len(todo)} change(s).")
    if not args.apply:
        print("Dry run — nothing changed. Re-run with --apply to make these changes.")
        return
    conn.close()

    path = backup()
    print(f"Backed up to {path} (integrity ok). Older backups are left for you to prune.")
    conn = get_connection()
    try:
        cur = conn.cursor()   # one transaction: commit at the end, roll back on any error
        for kind, _, data in todo:
            if kind == "add":
                cur.execute("INSERT INTO vendors (name, phone, email, account_number, active) VALUES (?,?,?,?,1)",
                            (data["name"], data["phone"], data["email"], data["account_number"]))
            elif kind == "rename":
                cur.execute("UPDATE vendors SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (data[1], data[0]))
            elif kind == "fold":
                old_id, keep_id, fill = data
                for f, v in fill.items():
                    cur.execute(f"UPDATE vendor_bill_pay SET {f} = ? WHERE id = ?", (v, keep_id))
                cur.execute("DELETE FROM vendor_bill_pay WHERE id = ?", (old_id,))
            elif kind == "inactive":
                cur.execute("UPDATE vendors SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (data,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"Applied {len(todo)} change(s).")


if __name__ == "__main__":
    main()
