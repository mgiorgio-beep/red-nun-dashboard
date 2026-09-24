"""
Vendors that serve exactly one entity (Mike, 2026-09-24).

    Fore & Aft       = Chatham landscaping only. Dennis has no landscaping.
    Nickerson        = Chatham trash only.
    Barrows Waste    = Dennis trash only.

Enforced in two places:
  * Invoice intake — integrations/invoices/ingest_guard.py rule 5 holds an
    invoice filed on the other entity (every Fore & Aft bill that ever landed
    on Dennis was entered on the wrong entity).
  * Bank coding — routes/bank_reconcile_routes.resolve_import_gl. On the
    owner's account the line takes the vendor's expense account; on the OTHER
    entity's account it is an intercompany payment (one entity paid the
    other's bill), so it takes that bank's loan account, never an expense —
    the cost belongs in the owner's P&L through the owner's invoice.
"""
import re

# (pattern over upper-cased vendor / statement text, owning location,
#  expense account on the owner's chart, why)
VENDOR_ENTITY = [
    (re.compile(r"FORE\s*(?:&|AND)\s*AFT"), "chatham", "Landscaping",
     "Fore & Aft is Chatham's landscaper only; Dennis has no landscaping"),
    (re.compile(r"\bNICKERSON\b"), "chatham", "Trash Removal",
     "Nickerson is Chatham's trash hauler only"),
    (re.compile(r"\bBARROWS\b"), "dennis", "Trash Removal",
     "Barrows Waste is Dennis's trash hauler only"),
]

# The account each entity books when its bank pays the other entity's bill.
INTERCOMPANY_LOAN = {
    "chatham": "Loan to Red Nun Dennisport",
    "dennis": "Loan to Red Buoy Inc.",
}


def owner_for_text(text):
    """(owner_location, expense_account_name, why) for a single-entity vendor
    named in `text`, else None."""
    t = (text or "").upper()
    for pat, owner, account, why in VENDOR_ENTITY:
        if pat.search(t):
            return owner, account, why
    return None


def bank_account_name(text, bank_location, signed):
    """The account a statement line naming a single-entity vendor takes on
    `bank_location`'s books, with the reason. (None, None) when no rule
    applies (not such a vendor, or an inflow — refunds are left to a human).
    """
    hit = owner_for_text(text)
    if not hit or (signed or 0) >= 0 or not bank_location:
        return None, None
    owner, account, why = hit
    if owner == bank_location:
        return account, why
    return (INTERCOMPANY_LOAN[bank_location],
            f"{why}: {bank_location.title()}'s bank paid {owner.title()}'s bill — intercompany; "
            f"pair it with {owner.title()}'s Bill Pay row")
