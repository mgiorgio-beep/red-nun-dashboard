"""
One vendor screen (UI plan Phase 5, 2026-10-15).

Management > Vendors (the vendors table: contact, category) and Pay Bills > Vendor
Bill Pay Setup (vendor_bill_pay, keyed by the vendor's name as text) were two screens
with two different lists. These endpoints feed the single Vendors screen:

  GET  /api/vendor-directory           every vendor: vendors-table rows plus any
                                       invoice vendor name that doesn't fold onto one,
                                       with contact fields; for admin / accountant also
                                       the Bill Pay summary (managers are refused bill
                                       pay data elsewhere, so it isn't sent to them)
  PUT  /api/vendor-directory/contact   save contact / category; creates the vendors
                                       row when the vendor so far only existed on
                                       invoices or in Bill Pay

Bill Pay settings themselves are still saved by /api/billpay/vendors/<name>, and a
rename still goes through /api/billpay/vendors/<name>/rename (which now renames the
vendors row too). Settings are matched to a vendor case- and punctuation-blind
(norm()), so "TIGER EXCHANGE INC." settings belong to "Tiger Exchange Inc.".
"""
import re

from flask import Blueprint, jsonify, request, session

from integrations.toast.data_store import get_connection
from routes.auth_routes import login_required

vendor_directory_bp = Blueprint("vendor_directory", __name__)

CONTACT_FIELDS = ("category", "contact_name", "email", "phone", "address", "account_number", "notes")


def norm(n):
    """Same folding /api/billpay/vendors uses (suffixes, punctuation, &/and)."""
    n = (n or "").lower().strip()
    for s in [", inc.", ", inc", " inc.", " inc", ", llc", " llc", ", corp", " corp",
              " corporation", " company", " homegrown"]:
        if n.endswith(s):
            n = n[:-len(s)].strip()
    n = n.replace(" and ", " & ")
    n = re.sub(r"[.,\-]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n.replace("foodservice", "food service")


def _can_see_bill_pay():
    return session.get("role") in ("admin", "accountant")


@vendor_directory_bp.route("/api/vendor-directory")
@login_required
def vendor_directory():
    show_bp = _can_see_bill_pay()
    conn = get_connection()
    try:
        vendors = [dict(r) for r in conn.execute("SELECT * FROM vendors")]
        inv = {r["vendor_name"]: dict(r) for r in conn.execute(
            """SELECT vendor_name, COUNT(*) AS invoices, MAX(invoice_date) AS last_invoice,
                      ROUND(SUM(CASE WHEN COALESCE(payment_status, '') != 'paid'
                                     AND COALESCE(balance, total) > 0
                                     THEN COALESCE(balance, total) ELSE 0 END), 2) AS open_total
               FROM scanned_invoices WHERE status = 'confirmed' AND vendor_name IS NOT NULL
               GROUP BY vendor_name""")}
        bp = {}
        if show_bp:
            for r in conn.execute("SELECT * FROM vendor_bill_pay"):
                bp.setdefault(norm(r["vendor_name"]), dict(r))
    finally:
        conn.close()

    rows, by_norm = [], {}
    for v in vendors:
        row = {"vendor_id": v["id"], "name": v["name"], "in_vendor_list": True,
               "active": bool(v.get("active", 1)), "spellings": []}
        row.update({f: v.get(f) for f in CONTACT_FIELDS})
        rows.append(row)
        by_norm.setdefault(norm(v["name"]), row)
    for name in sorted(inv, key=lambda n: -inv[n]["invoices"]):   # most-used spelling leads
        row = by_norm.get(norm(name))
        if row is None:
            row = {"vendor_id": None, "name": name, "in_vendor_list": False, "active": True, "spellings": []}
            row.update({f: None for f in CONTACT_FIELDS})
            rows.append(row)
            by_norm[norm(name)] = row
        row["spellings"].append(name)

    for row in rows:
        names = row["spellings"] or [row["name"]]
        row["invoices"] = sum(inv.get(n, {}).get("invoices", 0) for n in names)
        row["last_invoice"] = max((inv.get(n, {}).get("last_invoice") or "" for n in names), default="") or None
        if show_bp:
            b = bp.get(norm(row["name"])) or {}
            row["bill_pay"] = {
                "settings_name": b.get("vendor_name"),   # the name its settings are saved under
                "enabled": bool(b.get("bill_pay_enabled")),
                "portal": bool(b.get("portal_pay_enabled")),
                "auto_pay": bool(b.get("auto_pay")),
                "remit": ", ".join(x for x in (b.get("remit_address_1"), b.get("remit_city"), b.get("remit_state")) if x),
                "terms": b.get("payment_term_type"),
                "open_total": round(sum(inv.get(n, {}).get("open_total") or 0 for n in names), 2),
            }
    rows.sort(key=lambda r: r["name"].lower())
    return jsonify({"vendors": rows, "bill_pay_visible": show_bp})


@vendor_directory_bp.route("/api/vendor-directory/contact", methods=["PUT"])
@login_required
def save_contact():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    vid = data.get("vendor_id")
    if not name and not vid:
        return jsonify({"error": "name or vendor_id required"}), 400
    vals = {f: (str(data[f]).strip() or None) if data.get(f) is not None else None
            for f in CONTACT_FIELDS if f in data}
    conn = get_connection()
    try:
        row = None
        if vid:
            row = conn.execute("SELECT id FROM vendors WHERE id = ?", (vid,)).fetchone()
        if row is None and name:
            row = conn.execute("SELECT id FROM vendors WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if row is None:
            if not name:
                return jsonify({"error": "vendor not found"}), 404
            cols = ["name"] + list(vals)
            cur = conn.execute(f"INSERT INTO vendors ({', '.join(cols)}, active) VALUES "
                               f"({', '.join('?' * len(cols))}, 1)", (name, *vals.values()))
            vid, created = cur.lastrowid, True
        else:
            vid, created = row["id"], False
            sets = [f"{f} = ?" for f in vals]
            params = list(vals.values())
            if "active" in data:
                sets.append("active = ?")
                params.append(1 if data["active"] else 0)
            if sets:
                conn.execute(f"UPDATE vendors SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP "
                             f"WHERE id = ?", (*params, vid))
        conn.commit()
        return jsonify({"status": "ok", "vendor_id": vid, "created": created})
    finally:
        conn.close()
