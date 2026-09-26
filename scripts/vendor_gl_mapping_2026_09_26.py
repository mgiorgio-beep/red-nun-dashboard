"""Per-vendor GL accounts for invoice lines with no real category, and three
bank-rule fixes. Mike approved 2026-09-26 ("yes to all, deposits to beer
cogs, fix the rules").

Invoice lines categorised NON_COGS / OTHER / TAX all resolved to "Other
Business Expenses". Each vendor now gets its own account in gl_vendor_mapping,
mostly copied from Mike's confirmed bank rules. Deposit returns go to Beer COGS
(as category DEPOSIT, so they sit inside food & beverage cost), US Foods fees to
Food COGS, and Chatham's 7shifts annual invoice to Prepaid Expenses: its bank
payment is already amortized (expense_amortization #1), so expensing the
invoice too counted it twice.

Bank rules that disagreed with the invoices are corrected, and the bank rows
they coded are recoded to match (all logged in gl_repair_log):
  Dennis  SPRAGUE OPERATIN   Electric       -> Gas              (natural gas)
  Dennis  DEPENDABLE RESTA   Daily Cleaning -> Kitchen Equipment (repairs)
  Chatham DEPENDABLE RESTA   R&M            -> Kitchen Equipment (same vendor, same account)
  Chatham GLANOLA NORTH      Building       -> Beer Line Cleaning

Idempotent: re-running changes nothing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.toast.data_store import get_connection  # noqa: E402
from routes.register_routes import init_register_tables, vendor_key  # noqa: E402

ACTOR = "mike-2026-09-26"
BOTH = ("chatham", "dennis")

# (vendor name prefix, account name, as_category, locations, note)
VENDORS = [
    ("UniFirst", "Linens", None, BOTH, "uniform / mat / towel service"),
    ("Cintas", "Linens", None, BOTH, "mat / towel / soap service"),
    ("The Caron Group", "Daily Cleaning", None, BOTH, "commercial cleaning"),
    ("Fore & Aft", "Landscaping", None, BOTH, "landscaping"),
    ("Bay State Sewage", "Grease Removal", None, BOTH, "grease trap pumping"),
    ("Sprague", "Gas", None, BOTH, "natural gas"),
    ("Acrisure", "Liability Insurance", None, BOTH, "insurance audit"),
    ("Harris Warren", "Kitchen Equipment", None, BOTH, "equipment PM contract"),
    ("Dependable Restaurant", "Kitchen Equipment", None, BOTH, "equipment repairs"),
    ("Barrows Waste", "Trash Removal", None, BOTH, "dumpsters"),
    ("Tiger Exchange", "Hood Cleaning", None, BOTH, "hood filter exchange"),
    ("Cozzini", "Knife Sharpening", None, BOTH, "knife service"),
    ("Glanola", "Beer Line Cleaning", None, BOTH, "LineMaster unit"),
    ("Oceanside", "Building Repairs", None, BOTH, "emergency hazmat cleanup"),
    ("J. W. Dubis", "Building Repairs", None, BOTH, "plumbing"),
    ("Hinckley", "Building Repairs", None, BOTH, "hardware"),
    ("Suburban Supply", "Chemicals", None, BOTH, "freight on chemical orders"),
    ("Bayside Tent", "Tents", None, BOTH, "tent / table rental"),
    ("Fire Equipment", "Fire Extinguishers", None, BOTH, "suppression inspection"),
    ("Chatham Chamber", "Dues & Subscriptions", None, BOTH, "chamber membership"),
    ("Google", "Office Supplies & Software", None, BOTH, "Workspace"),
    ("Anthropic", "Office Supplies & Software", None, BOTH, "API credits"),
    ("Affirmed Medical", "Kitchen Supplies", None, BOTH, "first aid"),
    ("7shifts", "Prepaid Expenses", None, ("chatham",),
     "annual invoice; the bank payment is amortized (expense_amortization #1)"),
    # Keg / container deposit returns: Beer COGS, inside food & beverage cost.
    ("L. Knife", "Beer COGS", "DEPOSIT", BOTH, "keg deposit returns"),
    ("Colonial Wholesale", "Beer COGS", "DEPOSIT", BOTH, "keg deposit returns"),
    ("Craft Collective", "Beer COGS", "DEPOSIT", BOTH, "keg deposit returns"),
    ("Atlantic Beverage", "Beer COGS", "DEPOSIT", BOTH, "keg deposit returns"),
    ("Martignetti", "Beer COGS", "DEPOSIT", BOTH, "container deposits"),
    ("Southern Glazer", "Beer COGS", "DEPOSIT", BOTH, "barrel / case deposits"),
    ("US Foods", "Food Costs -F&B", "FOOD", BOTH, "delivery fees / adjustments"),
]

# (location, rule pattern, from account, to account)
RULES = [
    ("dennis", "SPRAGUE OPERATIN", "Electric", "Gas"),
    ("dennis", "DEPENDABLE RESTA", "Daily Cleaning", "Kitchen Equipment"),
    ("dennis", "DEPENDABLE RESTA", "Repairs & Maintenance", "Kitchen Equipment"),  # rows only
    ("chatham", "DEPENDABLE RESTA", "Repairs & Maintenance", "Kitchen Equipment"),
    ("chatham", "GLANOLA NORTH", "Building", "Beer Line Cleaning"),
]


def gl_id(conn, location, name):
    r = conn.execute("SELECT id FROM gl_accounts WHERE location=? AND name=? AND active=1",
                     (location, name)).fetchall()
    if len(r) != 1:
        raise SystemExit(f"{location} account {name!r}: {len(r)} active matches")
    return r[0]["id"]


def log(conn, kind, table, target, old, new, rule, detail):
    conn.execute(
        """INSERT INTO gl_repair_log (kind, target_table, target_id, old_gl_account_id,
                                      new_gl_account_id, match_rule, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (kind, table, target, old, new, rule, f"{ACTOR}: {detail}"))


def main():
    init_register_tables()
    conn = get_connection()
    made = rules = rows = 0
    try:
        for name, acct, as_cat, locs, note in VENDORS:
            key = vendor_key(name)
            for loc in locs:
                gid = gl_id(conn, loc, acct)
                have = conn.execute("SELECT id FROM gl_vendor_mapping WHERE location=? AND vendor_key=?",
                                    (loc, key)).fetchone()
                if have:
                    continue
                cur = conn.execute(
                    """INSERT INTO gl_vendor_mapping
                           (location, vendor_key, gl_account_id, as_category, note, created_by)
                       VALUES (?, ?, ?, ?, ?, ?)""", (loc, key, gid, as_cat, note, ACTOR))
                log(conn, "vendor_map_create", "gl_vendor_mapping", cur.lastrowid, None, gid,
                    "approved proposal", f"{loc} {name} -> {acct}" + (f" as {as_cat}" if as_cat else ""))
                made += 1

        for loc, pattern, old_name, new_name in RULES:
            old_id, new_id = gl_id(conn, loc, old_name), gl_id(conn, loc, new_name)
            r = conn.execute("SELECT id, gl_account_id FROM gl_account_rules WHERE location=? AND pattern=?",
                             (loc, pattern)).fetchone()
            if r and r["gl_account_id"] == old_id:
                conn.execute("UPDATE gl_account_rules SET gl_account_id=? WHERE id=?", (new_id, r["id"]))
                log(conn, "rule_remap", "gl_account_rules", r["id"], old_id, new_id, "approved proposal",
                    f"{loc} rule {pattern!r} {old_name} -> {new_name}")
                rules += 1
            # The bank rows for this vendor still on the old account.
            like = "%" + pattern.split()[0] + "%"
            for m in conn.execute(
                    """SELECT m.id FROM manual_bank_entries m
                       JOIN bank_accounts ba ON ba.id = m.bank_account_id
                       WHERE ba.location=? AND UPPER(m.payee) LIKE ? AND m.gl_account_id=?""",
                    (loc, like, old_id)).fetchall():
                conn.execute("UPDATE manual_bank_entries SET gl_account_id=? WHERE id=?", (new_id, m["id"]))
                log(conn, "row_remap", "manual_bank_entries", m["id"], old_id, new_id, "approved proposal",
                    f"{loc} {pattern} bank row {old_name} -> {new_name}")
                rows += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"vendor mappings created: {made}; rules remapped: {rules}; bank rows recoded: {rows}")


if __name__ == "__main__":
    main()
