"""
Bank lock on payments: a payment the BANK has already cleared cannot be
voided or deleted by an ordinary click.

2026-10-05: a void aimed at Martignetti AP #512/#513 landed on
vendor_payments #512/#513 instead — two Performance Foodservice ACHs from
7/22 ($3,328.57 Chatham, $4,880.26 Dennis) that had cleared the bank months
earlier and sat inside signed-off reconciliations. The void went through
silently: five invoices reopened, and Chatham's September register opened
$3,328.57 above the statement because the money the bank shows leaving had
vanished from the books.

The rule: if the bank says the money moved (cleared = 1) or the row is part
of a signed-off reconciliation (reconciliation_id set), the void / delete is
refused with 409 and a plain explanation naming the payment. A caller that
really means it (the bank reversed the payment) resends with
{"force": true}; the forced change is logged.

Every void/delete route for vendor_payments, ap_payments (through their
vendor_payments mirror) and payroll_checks calls bank_lock() first.
"""
import logging

logger = logging.getLogger(__name__)


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def bank_lock(conn, table: str, where: str, params: tuple) -> list[dict]:
    """Rows in `table` matching `where` that the bank has cleared or that
    sit in a signed-off reconciliation. Empty list = free to change."""
    cols = _cols(conn, table)
    conds = []
    if "cleared" in cols:
        conds.append("COALESCE(cleared, 0) = 1")
    if "reconciliation_id" in cols:
        conds.append("reconciliation_id IS NOT NULL")
    if not conds:
        return []
    label = {"vendor_payments": "vendor AS who, payment_total AS amount, payment_date AS dt",
             "payroll_checks": "employee_name AS who, net_pay AS amount, pay_period_end AS dt"
             }.get(table, "NULL AS who, NULL AS amount, NULL AS dt")
    extra = ", cleared_date" if "cleared_date" in cols else ", NULL AS cleared_date"
    extra += ", reconciliation_id" if "reconciliation_id" in cols else ", NULL AS reconciliation_id"
    rows = conn.execute(
        f"SELECT id, {label}{extra} FROM {table} "
        f"WHERE ({where}) AND ({' OR '.join(conds)})", params).fetchall()
    return [dict(r) for r in rows]


def lock_response(locked: list[dict], action: str, table: str):
    """(body, 409) explaining why the change was refused."""
    parts = []
    for r in locked:
        amt = r.get("amount")
        amt_s = f"${float(amt):,.2f}" if amt is not None else ""
        bits = [f"#{r['id']}", (r.get("who") or "").strip(), amt_s]
        if r.get("cleared_date"):
            bits.append(f"cleared the bank {r['cleared_date']}")
        if r.get("reconciliation_id"):
            bits.append(f"in signed-off reconciliation #{r['reconciliation_id']}")
        parts.append(" ".join(b for b in bits if b))
    msg = (f"Refused to {action}: the bank has already cleared this payment — "
           + "; ".join(parts)
           + ". Changing it would erase money the bank shows leaving and alter a "
             "reconciled month. If the bank actually reversed it, confirm to force.")
    return {"error": msg, "needs_force": True, "locked": locked,
            "table": table}, 409


def check_or_refuse(conn, table, where, params, action, force, who=None):
    """None when the change may proceed; otherwise a (body, 409) tuple.
    A forced change on a locked row is logged loudly."""
    locked = bank_lock(conn, table, where, params)
    if not locked:
        return None
    if force:
        logger.warning("FORCED %s on bank-cleared %s rows %s by %s",
                       action, table, [r["id"] for r in locked], who or "?")
        return None
    return lock_response(locked, action, table)
