"""One switch in front of every write to QuickBooks Online.

Mike, 2026-09-24: 197 of Chatham's gl_accounts carry QBO ids copied from
Dennis's chart (e.g. Sales Tax Payable -> Red Buoy's Dues & Subscriptions,
Accounts Payable -> Food COGS). Anything pushed to Red Buoy by id would land
on the wrong account. Until Mike approves the rebuild of those ids from Red
Buoy's own chart, NO write may reach the Red Buoy (Chatham) company — the
dashboard's sales-journal push and every legacy script in this folder, most
of which default to Red Buoy's realm.

Reads are not affected. Set CHATHAM_QBO_WRITES_BLOCKED = False only after the
rebuild is approved and applied.

LIFTED 2026-09-26: Mike signed off every Chatham account id. The rebuild is
applied (gl_repair_log kind 'qbo_id_remap'); the last copied id, Tip Bank
175 (American Express CC in Red Buoy), now points at Red Buoy's Tip Bank 188.
The switch stays so it can be thrown again if the chart drifts.
"""
import os

CHATHAM_QBO_WRITES_BLOCKED = False
_RED_BUOY_DEFAULT_REALM = "123146237986854"   # the legacy scripts' hardcoded default


class QboWriteBlocked(RuntimeError):
    pass


def red_buoy_realms():
    return {r for r in (os.getenv("QB_REALM_ID_CHATHAM"), _RED_BUOY_DEFAULT_REALM) if r}


def check_write(realm_id, what="QBO write"):
    """Raise QboWriteBlocked if `realm_id` is Red Buoy's and writes are blocked."""
    if CHATHAM_QBO_WRITES_BLOCKED and str(realm_id or "") in red_buoy_realms():
        raise QboWriteBlocked(
            f"{what} to Red Buoy (Chatham) refused: Chatham's QBO account ids are being rebuilt from Red Buoy's "
            f"own chart and every Chatham push is blocked until Mike approves it "
            f"(integrations/quickbooks/push_guard.py)")
