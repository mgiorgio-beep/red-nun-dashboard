"""
Management P&L — accrual basis, built from the sources that are actually right.

THE THREE GUARDRAILS (decided 2026-08-23, and the reason this module exists
rather than a pile of SELECTs):

  1. REVENUE COMES FROM THE DAILY SALES JOURNAL ENTRIES, NEVER FROM DEPOSITS.
     A deposit is money arriving, which is a balance-sheet event and lands days
     later in a lump. Revenue is what was sold. The two are related by a
     clearing account and are not interchangeable. Clearing accounts (Daily
     Sales:*, Cash/Credit Card Sales, Undeposited Funds) are typed Other
     Current Asset, so grouping by account TYPE excludes them automatically —
     but `assert_clearing_excluded()` proves it rather than trusting it.

  2. EACH DAY'S SALES JE IS GATED ON ITS PLUG. `Summary: Other` is the
     balancing figure when tenders and sales disagree. Over PLUG_THRESHOLD on
     a single day, that day enters the P&L FLAGGED and the month carries a
     banner naming the count and the total. Netting hides this: across both
     entities the net plug is under $3k while the gross on flagged days is over
     $21k, because over and short cancel.

  3. INHERITED DEFECTS PRINT ON THE FACE OF THE REPORT. Tips, unbooked Toast
     fees, and the absence of an inventory adjustment all distort these
     numbers. They appear as footnotes on every P&L until fixed. A wrong number
     with a label is honest; a wrong number alone is a trap.

ACCRUAL. Cost is recognised at INVOICE date from scanned_invoice_items, mapped
through gl_category_mapping. The bank transaction that later pays that invoice
is an AP settlement and is excluded here — register_routes enforces that it
cannot carry a P&L account at all. Bank rows are the P&L source only for things
with no invoice behind them: autopay utilities, card charges, bank fees.

Tax basis is the accountant's call and does not constrain these books.
"""
import logging
from datetime import date

from integrations.toast.data_store import get_connection
from routes.register_routes import (
    FNB_COGS_CATEGORIES, COGS_NON_FNB_CATEGORIES, _PL_ACCOUNT_TYPES,
)

logger = logging.getLogger(__name__)

# A single day's Summary: Other above this flags the day and the month.
PLUG_THRESHOLD = 50.00

# Accounts that carry LABOR. They are reported as their own line and folded
# into prime cost, so operating_expenses() must exclude them or the same wages
# are counted twice — once in prime cost, once in opex. Matched by name because
# both charts use these names and neither types them distinctly.
LABOR_ACCOUNT_NAMES = frozenset({
    "Payroll Expenses", "Payroll Taxes", "Payroll Fees", "Wages", "Wages-ERC",
    "Contract Labor", "Tip Wages", "Cash Tip Expense",
})

# Tip-payout channels. These settle the Tip Bank liability and must never sit
# on a labor account.
#
# THIS IS NO LONGER AN ADJUSTMENT. The P&L used to detect these rows and
# SUBTRACT them from labor, because they were miscoded to Payroll Expenses and
# Tip Wages. That was a report papering over a coding fault. classify_tip_
# settlement() in register_routes now codes them to Tip Bank at import, and the
# historical rows were recoded, so labor is simply the labor accounts.
#
# The hints stay as an AUDIT: a tip-channel row sitting on a labor account is
# now a FAILURE to be fixed, not a number to be quietly adjusted around.
#
# VENMO WAS HERE AND WAS WRONG — Mike pays bands and the trivia host through
# Venmo, exclusively. Including it swept $6,050 of March entertainment into
# "tip disbursements" and out of labor. A channel is not a purpose.
_TIP_PAYOUT_HINTS = ("7SHIFTS TI", "KICKFIN")

_TIP_CHANNEL_LABELS = {"7SHIFTS TI": "7shifts tip runs", "KICKFIN": "Kickfin"}

# Account types that belong on a P&L at all.
REVENUE_TYPES = frozenset({"Income", "Other Income"})
COGS_TYPES = frozenset({"Cost of Goods Sold"})
EXPENSE_TYPES = frozenset({"Expense", "Other Expense"})

# Everything else is balance sheet. Named explicitly so guardrail 1 is a
# statement about the data, not a hope about the query.
CLEARING_TYPES = frozenset({
    "Bank", "Other Current Asset", "Other Current Liability",
    "Accounts Receivable", "Accounts Payable", "Credit Card",
    "Fixed Asset", "Other Asset", "Long Term Liability", "Equity",
})


def _r2(x):
    return round(float(x or 0), 2)


# ─── GUARDRAIL 1 ─────────────────────────────────────────────────────────────

def assert_clearing_excluded(conn, location: str) -> list[str]:
    """Prove that no clearing account can reach the revenue section.

    Returns a list of violations; empty is the pass condition. This is the
    assertion guardrail 1 asks for: every tender and summary line in the sales
    journal must map to a balance-sheet account, so that selecting Income
    cannot pick up a deposit or a settlement.
    """
    violations = []
    rows = conn.execute(
        """
        SELECT m.journal_name, g.name AS gl_name, g.account_type
        FROM qb_line_mapping m
        JOIN gl_accounts g ON g.id = m.gl_account_id
        WHERE m.location = ?
          AND (m.journal_name LIKE 'Tenders:%'
               OR m.journal_name IN ('Summary: Tax', 'Summary: Tips',
                                     'Summary: Gift Card Sold'))
        """,
        (location,),
    ).fetchall()
    for r in rows:
        if r["account_type"] not in CLEARING_TYPES:
            violations.append(
                f"{r['journal_name']} -> {r['gl_name']} [{r['account_type']}] "
                f"is not a balance-sheet account"
            )
    return violations


# ─── GUARDRAIL 2 ─────────────────────────────────────────────────────────────

def plug_days(conn, location: str, start: str, end: str) -> dict:
    """Days whose sales JE needed a plug over the threshold.

    `Summary: Other` absorbs the difference when tenders do not agree with
    sales. A large one means the day's numbers are not trustworthy, and it must
    not disappear into a monthly total that nets to nearly nothing.
    """
    rows = conn.execute(
        """
        SELECT e.entry_date,
               COALESCE(li.credit, 0) - COALESCE(li.debit, 0) AS plug
        FROM qb_journal_line_items li
        JOIN qb_journal_entries e ON e.id = li.entry_id
        WHERE e.location = ? AND e.entry_date BETWEEN ? AND ?
          AND li.journal_name = 'Summary: Other'
        ORDER BY e.entry_date
        """,
        (location, start, end),
    ).fetchall()
    flagged = [{"date": r["entry_date"], "plug": _r2(r["plug"])}
               for r in rows if abs(float(r["plug"] or 0)) > PLUG_THRESHOLD]
    return {
        "threshold": PLUG_THRESHOLD,
        "days": flagged,
        "count": len(flagged),
        "gross": _r2(sum(abs(d["plug"]) for d in flagged)),
        "net": _r2(sum(float(r["plug"] or 0) for r in rows)),
        "banner": (
            f"{len(flagged)} day(s) with an unresolved plug totalling "
            f"${sum(abs(d['plug']) for d in flagged):,.2f} — these days are "
            f"included but their figures are not trustworthy"
        ) if flagged else None,
    }


# ─── REVENUE (guardrail 1 in practice) ───────────────────────────────────────

# ─── SALES TAX PAYABLE ROLL-FORWARD ──────────────────────────────────────────
#
# Sales Tax Payable is a pass-through: the daily sales JEs CREDIT it with tax
# collected, and DAVO DEBITS it when it remits to the state. DAVO pulls close to
# daily — 22-29 times a month — so the balance should sit near zero and the
# residual is just tax in transit.
#
# WHAT THE TWO DIRECTIONS MEAN, and why a growing balance either way matters:
#
#   growing CREDIT (collected > remitted)
#       Tax collected and not handed over. This is the state's money sitting in
#       the operating account, and it compounds quietly.
#
#   growing DEBIT (remitted > collected)
#       DAVO pulling more than we accrue. Either the lag unwinding after a
#       catch-up — harmless and self-correcting — or the DoorDash
#       marketplace-facilitator problem: DoorDash remits tax on its own orders
#       as the facilitator, and if those sales still carry tax in our accrual
#       we remit it a second time. That one does not self-correct; it just
#       keeps costing money.
#
# A single month proves nothing either way. The alarm needs BOTH a residual
# outside the transit band AND a balance that keeps growing in one direction.

# DAVO IS PENNY-EXACT. It reads the day's tax off the Toast integration and
# pulls that exact figure a few days later — verified across Dennis February
# 2026, where every accrual day matched a pull to the cent:
#
#     02-04 accrued 223.57 -> pulled 02-06 as 223.57
#     02-14 accrued 509.17 -> pulled 02-18 as 509.17
#
# So the audit MATCHES pulls to accrual days by exact amount rather than
# comparing fuzzy monthly totals. That turns a vague "the balance looks off"
# into two precise questions:
#
#   a tax day with no pull, older than the observed lag
#       -> collected and never remitted
#   a pull with no tax day
#       -> remitted with nothing behind it. This is what the DoorDash
#          marketplace-facilitator double-remit would look like: DoorDash
#          remits on its own orders, and if we remit the same tax again the
#          second payment has no accrual to match.
#
# The aggregate roll-forward is kept as the headline, but the matching is what
# makes the alarm worth acting on.

SALES_TAX_TRANSIT_DAYS = 5
# How far back a pull may reach for its accrual day. Observed lag is 2-5 days;
# this allows for a weekend plus a holiday.
SALES_TAX_MAX_LAG_DAYS = 10
# Consecutive months of growth in the same direction before it counts as a
# trend rather than timing.
SALES_TAX_TREND_MONTHS = 3


def _match_tax_pulls(conn, location: str, start: str, end: str, gl_id: int) -> dict:
    """Match each DAVO pull to the accrual day it is remitting, by exact amount.

    Greedy nearest-first within SALES_TAX_MAX_LAG_DAYS. Amounts are compared in
    integer cents — DAVO is exact, so anything that fails to match is a real
    finding rather than a rounding artefact.

    Edge effects are excluded deliberately: pulls early in the window are
    remitting accruals from before it, and accruals late in the window have not
    been pulled yet. Neither is a defect, and counting them would make every
    window look broken at both ends.
    """
    def cents(x):
        return int(round(float(x or 0) * 100))

    days = [{"date": r["d"], "cents": cents(r["t"]), "matched": None}
            for r in conn.execute(
                """SELECT e.entry_date AS d,
                          SUM(COALESCE(li.credit,0) - COALESCE(li.debit,0)) AS t
                   FROM qb_journal_line_items li
                   JOIN qb_journal_entries e ON e.id = li.entry_id
                   WHERE e.location = ? AND li.journal_name = 'Summary: Tax'
                     AND e.entry_date BETWEEN ? AND ?
                   GROUP BY 1 ORDER BY 1""", (location, start, end))
            if cents(r["t"]) > 0]

    pulls = [{"id": r["id"], "date": r["d"], "cents": cents(r["a"]), "matched": None}
             for r in conn.execute(
                 """SELECT m.id, m.entry_date AS d, -m.amount AS a
                    FROM manual_bank_entries m
                    JOIN bank_accounts ba ON ba.id = m.bank_account_id
                    WHERE ba.location = ? AND m.gl_account_id = ?
                      AND m.entry_date BETWEEN ? AND ?
                    ORDER BY m.entry_date""", (location, gl_id, start, end))]

    # PASS 1 — exact to the cent. This is the normal case: DAVO reads the day's
    # tax off Toast and pulls that figure.
    lags = []
    for p in pulls:
        if p["cents"] <= 0:
            continue                      # credits/reversals are not remittances
        pd = date.fromisoformat(p["date"])
        best = None
        for d in days:
            if d["matched"] is not None or d["cents"] != p["cents"]:
                continue
            lag = (pd - date.fromisoformat(d["date"])).days
            if 0 <= lag <= SALES_TAX_MAX_LAG_DAYS and (best is None or lag < best[0]):
                best = (lag, d)
        if best:
            best[1]["matched"] = p["id"]
            p["matched"] = best[1]["date"]
            lags.append(best[0])

    # PASS 2 — leftovers, matched by nearest amount within the lag window.
    #
    # Exact matching alone CASCADES: one day that differs by a few cents
    # orphans both its day and its pull, and the next pull then finds the wrong
    # day free. That manufactured a $1,819 "never remitted" figure on Dennis
    # out of days that plainly had a pull two days later, a few dollars light.
    # A near-match is a remittance with a variance, not a missing one, and the
    # variance is reported rather than alarmed on.
    variances = []
    for p in pulls:
        if p["matched"] is not None or p["cents"] <= 0:
            continue
        pd = date.fromisoformat(p["date"])
        best = None
        for d in days:
            if d["matched"] is not None:
                continue
            lag = (pd - date.fromisoformat(d["date"])).days
            if not (0 <= lag <= SALES_TAX_MAX_LAG_DAYS):
                continue
            gap = abs(d["cents"] - p["cents"])
            # Only pair things that are recognisably the same remittance.
            if gap > max(500, int(d["cents"] * 0.10)):
                continue
            if best is None or gap < best[0]:
                best = (gap, lag, d)
        if best:
            gap, lag, d = best
            d["matched"] = p["id"]
            p["matched"] = d["date"]
            lags.append(lag)
            variances.append({"date": d["date"], "pull_id": p["id"],
                              "accrued": _r2(d["cents"] / 100),
                              "remitted": _r2(p["cents"] / 100),
                              "variance": _r2((d["cents"] - p["cents"]) / 100)})

    win_end = date.fromisoformat(end)
    win_start = date.fromisoformat(start)
    # An accrual within the lag window of the end is still legitimately in
    # transit; a pull within the lag window of the start is settling an accrual
    # from before the window.
    unremitted = [d for d in days if d["matched"] is None
                  and (win_end - date.fromisoformat(d["date"])).days > SALES_TAX_MAX_LAG_DAYS]
    unmatched_pulls = [p for p in pulls if p["matched"] is None and p["cents"] > 0
                       and (date.fromisoformat(p["date"]) - win_start).days > SALES_TAX_MAX_LAG_DAYS]
    reversals = [p for p in pulls if p["cents"] < 0]

    return {
        "tax_days": len(days), "pulls": len([p for p in pulls if p["cents"] > 0]),
        "matched": len(lags),
        "exact": len(lags) - len(variances),
        "variances": variances,
        "variance_total": _r2(sum(v["variance"] for v in variances)),
        "match_rate": _r2(100 * len(lags) / len(days)) if days else None,
        "lag_min": min(lags) if lags else None,
        "lag_max": max(lags) if lags else None,
        "lag_avg": _r2(sum(lags) / len(lags)) if lags else None,
        "unremitted_days": [{"date": d["date"], "amount": _r2(d["cents"] / 100)}
                            for d in unremitted],
        "unremitted_total": _r2(sum(d["cents"] for d in unremitted) / 100),
        "unmatched_pulls": [{"id": p["id"], "date": p["date"],
                             "amount": _r2(p["cents"] / 100)} for p in unmatched_pulls],
        "unmatched_pull_total": _r2(sum(p["cents"] for p in unmatched_pulls) / 100),
        "reversals": [{"id": p["id"], "date": p["date"],
                       "amount": _r2(p["cents"] / 100)} for p in reversals],
    }


def sales_tax_rollforward(conn, location: str, end: str | None = None) -> dict:
    """Roll Sales Tax Payable forward over the window both sides cover.

    THE WINDOW MATTERS MORE THAN THE ARITHMETIC. Sales JEs run from 2025-04;
    imported statements start later and stop earlier. Comparing cumulative
    credits against cumulative debits over all time would show a huge unremitted
    balance that is nothing but missing statements — the same trap as computing
    a bottom line for a month with no bank rows. So the roll-forward runs only
    where BOTH sides have data, and says which window it used.
    """
    je = conn.execute(
        """SELECT MIN(entry_date), MAX(entry_date) FROM qb_journal_entries
           WHERE location = ?""", (location,)).fetchone()
    stmt = conn.execute(
        """SELECT MIN(u.period_start), MAX(u.period_end)
           FROM bank_statement_uploads u
           JOIN bank_accounts ba ON ba.id = u.bank_account_id
           WHERE ba.location = ?""", (location,)).fetchone()
    if not je[0] or not stmt[0]:
        return {"available": False,
                "reason": "no sales journal entries or no imported statements"}

    start = max(je[0], stmt[0])
    win_end = min(je[1], stmt[1])
    if end:
        win_end = min(win_end, end)
    if start > win_end:
        return {"available": False,
                "reason": "sales journal and statement coverage do not overlap"}

    gl = conn.execute(
        "SELECT id FROM gl_accounts WHERE name = 'Sales Tax Payable' "
        "AND location = ? AND active = 1", (location,)).fetchone()
    if not gl:
        return {"available": False, "reason": "no active Sales Tax Payable account"}

    # Credits: tax collected, from the sales journal.
    collected_rows = conn.execute(
        """SELECT SUBSTR(e.entry_date, 1, 7) AS m,
                  ROUND(SUM(COALESCE(li.credit,0) - COALESCE(li.debit,0)), 2) AS amt
           FROM qb_journal_line_items li
           JOIN qb_journal_entries e ON e.id = li.entry_id
           WHERE e.location = ? AND li.journal_name = 'Summary: Tax'
             AND e.entry_date BETWEEN ? AND ?
           GROUP BY 1""", (location, start, win_end)).fetchall()

    # Debits: remittances, taken from the ACCOUNT rather than the payee string.
    # This is the account's own roll-forward, so a remittance through some other
    # channel still counts and a DAVO row miscoded elsewhere correctly does not.
    remitted_rows = conn.execute(
        """SELECT SUBSTR(m.entry_date, 1, 7) AS m,
                  ROUND(SUM(-m.amount), 2) AS amt, COUNT(*) AS n
           FROM manual_bank_entries m
           JOIN bank_accounts ba ON ba.id = m.bank_account_id
           WHERE ba.location = ? AND m.gl_account_id = ?
             AND m.entry_date BETWEEN ? AND ?
           GROUP BY 1""", (location, gl["id"], start, win_end)).fetchall()

    coll = {r["m"]: float(r["amt"] or 0) for r in collected_rows}
    remit = {r["m"]: float(r["amt"] or 0) for r in remitted_rows}
    pulls = sum(r["n"] for r in remitted_rows)

    monthly, cum = [], 0.0
    for m in sorted(set(coll) | set(remit)):
        c_, r_ = _r2(coll.get(m, 0)), _r2(remit.get(m, 0))
        cum = _r2(cum + c_ - r_)
        monthly.append({"month": m, "collected": c_, "remitted": r_,
                        "residual": _r2(c_ - r_), "cumulative": cum})

    total_collected = _r2(sum(coll.values()))
    total_remitted = _r2(sum(remit.values()))
    residual = _r2(total_collected - total_remitted)

    days = max(1, (date.fromisoformat(win_end) - date.fromisoformat(start)).days + 1)
    avg_daily = _r2(total_collected / days)
    band = _r2(avg_daily * SALES_TAX_TRANSIT_DAYS)

    # A trend is growth in the SAME direction, in absolute terms, for several
    # months running. Oscillation around zero is DAVO's lag and is expected.
    trend = False
    tail = [m["cumulative"] for m in monthly][-(SALES_TAX_TREND_MONTHS + 1):]
    if len(tail) >= SALES_TAX_TREND_MONTHS + 1:
        rising = all(abs(tail[i + 1]) > abs(tail[i]) for i in range(len(tail) - 1))
        same_side = len({x > 0 for x in tail if x}) <= 1
        trend = rising and same_side

    within = abs(residual) <= band
    direction = ("balanced" if within
                 else ("credit" if residual > 0 else "debit"))

    match = _match_tax_pulls(conn, location, start, win_end, gl["id"])

    # The precise findings outrank the aggregate. DAVO is exact, so a day with
    # no pull or a pull with no day is a fact, not a tolerance question.
    messages = []
    if match["unremitted_days"]:
        n = len(match["unremitted_days"])
        oldest = match["unremitted_days"][0]["date"]
        messages.append(
            f"SALES TAX COLLECTED BUT NOT REMITTED — {n} business day(s) "
            f"totalling ${match['unremitted_total']:,.2f} have no matching DAVO "
            f"pull, the oldest being {oldest}. DAVO remits each day's tax "
            f"exactly, {match['lag_min']}-{match['lag_max']} days later, so a "
            f"day still unmatched well beyond that lag was not remitted. This "
            f"is the state's money.")
    if match["unmatched_pulls"]:
        n = len(match["unmatched_pulls"])
        # A repeated identical amount is a subscription, not a remittance.
        # DAVO's own monthly fee was landing on Sales Tax Payable this way:
        # $61.61 on 02-03, 03-03 and 04-06, with matching credits alongside.
        amounts = [p["amount"] for p in match["unmatched_pulls"]]
        recurring = sorted({a for a in amounts if amounts.count(a) > 1})
        if recurring:
            messages.append(
                f"DAVO'S OWN FEE IS ON THE TAX ACCOUNT — "
                f"{', '.join(f'${a:,.2f}' for a in recurring)} recurs monthly "
                f"among {n} pull(s) with no matching day's tax. A repeated "
                f"identical amount is a subscription charge, not a remittance. "
                f"It belongs on an expense account: on Sales Tax Payable it "
                f"understates the liability and hides a real cost.")
        else:
            messages.append(
                f"DAVO REMITTED ${match['unmatched_pull_total']:,.2f} WITH NO "
                f"MATCHING ACCRUAL — {n} pull(s) do not correspond to any day's "
                f"tax. DAVO takes its figures from Toast, so this should not "
                f"happen. Check whether DoorDash is remitting marketplace-"
                f"facilitator tax on orders we are also taxing, which would "
                f"mean the same tax is being paid twice.")
    if not messages and not within:
        # Everything matched but the totals still drift: that is window edge
        # effects, not a defect. Say so rather than alarming.
        side = "credit" if residual > 0 else "debit"
        messages.append(
            f"Sales Tax Payable shows a {side} residual of ${abs(residual):,.2f} "
            f"over {start}..{win_end}, but every accrual day matched a DAVO "
            f"pull to the penny. The residual is tax in transit across the "
            f"window edges, not a remittance failure.")

    alarm = bool(match["unremitted_days"] or match["unmatched_pulls"])

    return {
        "available": True,
        "window": {"start": start, "end": win_end, "days": days},
        "collected": total_collected, "remitted": total_remitted,
        "residual": residual, "pulls": pulls,
        "avg_daily_tax": avg_daily, "tolerance_band": band,
        "transit_days": SALES_TAX_TRANSIT_DAYS,
        "within_tolerance": within, "direction": direction,
        "growing": trend, "alarm": alarm,
        "matching": match,
        "monthly": monthly,
        "message": messages[0] if messages else None,
        "messages": messages,
    }


def _sales_tax_footnotes(conn, location: str, st: dict) -> list[str]:
    """Sales-tax notes for the P&L, only when something needs saying.

    Sales tax never touches the P&L — it is a liability pass-through — so this
    prints nothing when the account is behaving. A clean roll-forward is not
    news; an unremitted day is.
    """
    if not st.get("available"):
        return []
    notes = list(st.get("messages") or [])
    misrouted = sales_tax_misrouted_remittances(conn, location)
    if misrouted:
        total = sum(m["amount"] for m in misrouted)
        where = ", ".join(sorted({m["gl_name"] for m in misrouted}))
        notes.append(
            f"DAVO REMITTANCES ON THE WRONG ACCOUNT — {len(misrouted)} row(s) "
            f"totalling ${total:,.2f} sit on {where} instead of Sales Tax "
            f"Payable. They are real remittances, so the tax account looks "
            f"more unremitted than it is, and whichever account they landed on "
            f"is overstated.")
    return notes


def sales_tax_misrouted_remittances(conn, location: str) -> list[dict]:
    """DAVO rows NOT coded to Sales Tax Payable.

    A remittance sitting on some other account is invisible to the
    roll-forward above and inflates the apparent unremitted balance, so it is
    reported separately rather than silently swept in by payee-matching.
    """
    return [dict(r) for r in conn.execute(
        """SELECT m.id, m.entry_date, m.payee, ROUND(-m.amount, 2) AS amount,
                  COALESCE(g.name, '(uncoded)') AS gl_name, m.gl_status
           FROM manual_bank_entries m
           JOIN bank_accounts ba ON ba.id = m.bank_account_id
           LEFT JOIN gl_accounts g ON g.id = m.gl_account_id
           WHERE ba.location = ?
             AND UPPER(COALESCE(m.payee,'') || ' ' || COALESCE(m.memo,'')) LIKE '%DAVO%'
             AND (g.name IS NULL OR g.name <> 'Sales Tax Payable')
           ORDER BY m.entry_date""", (location,))]


def expense_coverage(conn, location: str, start: str, end: str) -> dict:
    """Is there enough bank data for the expense side to mean anything?

    Revenue comes from Toast and is always present. Operating expense and
    labor come from imported bank statements, and the statement backlog is
    still being loaded — Chatham March 2026 has ZERO bank rows, which yields
    full revenue against almost no cost and a fictitious profit.

    A P&L missing its expense side is not a slightly-off P&L, it is a
    misleading one. This is the check that stops it printing a bottom line as
    though it were real.
    """
    n = conn.execute(
        """SELECT COUNT(*) FROM manual_bank_entries m
           JOIN bank_accounts ba ON ba.id = m.bank_account_id
           WHERE ba.location = ? AND m.entry_date BETWEEN ? AND ?""",
        (location, start, end),
    ).fetchone()[0]
    ph = ",".join("?" * len(LABOR_ACCOUNT_NAMES))
    labor_rows = conn.execute(
        f"""SELECT COUNT(*) FROM manual_bank_entries m
            JOIN bank_accounts ba ON ba.id = m.bank_account_id
            JOIN gl_accounts g ON g.id = m.gl_account_id
            WHERE ba.location = ? AND m.entry_date BETWEEN ? AND ?
              AND g.name IN ({ph})""",
        (location, start, end, *sorted(LABOR_ACCOUNT_NAMES)),
    ).fetchone()[0]

    complete = n > 0 and labor_rows > 0
    warning = None
    if n == 0:
        warning = (
            "NO BANK ROWS for this period — the statement has not been "
            "imported. Operating expense and labor are MISSING, so the bottom "
            "line is revenue minus invoices only and is NOT a profit figure."
        )
    elif labor_rows == 0:
        warning = (
            f"NO LABOR ROWS for this period ({n} bank rows present, none on a "
            f"labor account). Prime cost excludes labor entirely and the "
            f"bottom line is overstated."
        )
    return {"bank_rows": n, "labor_rows": labor_rows,
            "expense_side_complete": complete, "warning": warning}


def revenue(conn, location: str, start: str, end: str) -> dict:
    """Revenue from the daily sales journal entries.

    Grouped by the GL account each journal line resolves to, filtered to Income
    types. Discounts are an Income-typed contra account carrying debits, so
    credit-minus-debit nets them off correctly without special-casing.
    """
    rows = conn.execute(
        """
        SELECT g.id AS gl_id, g.name AS gl_name, g.account_type,
               ROUND(SUM(COALESCE(li.credit, 0) - COALESCE(li.debit, 0)), 2) AS amount
        FROM qb_journal_line_items li
        JOIN qb_journal_entries e ON e.id = li.entry_id
        JOIN qb_line_mapping m ON m.location = e.location
                              AND m.journal_name = li.journal_name
        JOIN gl_accounts g ON g.id = m.gl_account_id
        WHERE e.location = ? AND e.entry_date BETWEEN ? AND ?
          AND g.account_type IN ('Income', 'Other Income')
        GROUP BY g.id, g.name, g.account_type
        ORDER BY amount DESC
        """,
        (location, start, end),
    ).fetchall()
    lines = [{"gl_account_id": r["gl_id"], "name": r["gl_name"],
              "amount": _r2(r["amount"]),
              "drill": {"source": "revenue", "key": r["gl_id"]}}
             for r in rows]
    gross = _r2(sum(l["amount"] for l in lines if l["amount"] > 0))
    contra = _r2(sum(l["amount"] for l in lines if l["amount"] < 0))
    return {"lines": lines, "gross_sales": gross, "contra": contra,
            "net_revenue": _r2(gross + contra)}


def journal_control_total(conn, location: str, start: str, end: str) -> dict:
    """Total debits and credits across the period's JEs, and the entry count.

    This is the reconciliation handle: the P&L is built from these entries, so
    the report states what it was built from and whether those entries balance.
    """
    r = conn.execute(
        """
        SELECT COUNT(DISTINCT e.id) AS entries,
               ROUND(SUM(COALESCE(li.debit, 0)), 2)  AS debits,
               ROUND(SUM(COALESCE(li.credit, 0)), 2) AS credits
        FROM qb_journal_line_items li
        JOIN qb_journal_entries e ON e.id = li.entry_id
        WHERE e.location = ? AND e.entry_date BETWEEN ? AND ?
        """,
        (location, start, end),
    ).fetchone()
    debits, credits = _r2(r["debits"]), _r2(r["credits"])
    return {"entries": r["entries"] or 0, "debits": debits, "credits": credits,
            "balanced": abs(round((debits - credits) * 100)) == 0}


# ─── COGS AND EXPENSE, ACCRUAL FROM INVOICES ─────────────────────────────────

def _invoice_costs(conn, location: str, start: str, end: str) -> list[dict]:
    """Confirmed invoice line items in the period, resolved to GL accounts.

    Recognised at INVOICE date — this is the accrual. The payment that settles
    the invoice is excluded by construction: it is never in this query, and
    register_routes refuses to give it a P&L account.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(ii.category_type), ''), 'UNKNOWN') AS category,
               g.id AS gl_id, g.name AS gl_name, g.account_type,
               cm.confidence, cm.note,
               ROUND(SUM(COALESCE(ii.total_price, 0)), 2) AS amount
        FROM scanned_invoice_items ii
        JOIN scanned_invoices si ON si.id = ii.invoice_id
        LEFT JOIN gl_category_mapping cm
               ON cm.location = si.location
              AND cm.category_type = COALESCE(NULLIF(TRIM(ii.category_type), ''), 'UNKNOWN')
        LEFT JOIN gl_accounts g ON g.id = cm.gl_account_id AND g.active = 1
        WHERE si.location = ? AND si.status = 'confirmed'
          AND si.invoice_date BETWEEN ? AND ?
        -- Group by the EXPRESSION, never the alias. `scanned_invoices` has its
        -- own `category` column, which shadows the alias in GROUP BY and
        -- silently splits each category across invoices. Subtotals still came
        -- out right, so the only symptom was fragmented line detail — a query
        -- that resolves cleanly and groups by the wrong thing.
        GROUP BY COALESCE(NULLIF(TRIM(ii.category_type), ''), 'UNKNOWN'),
                 g.id, g.name, g.account_type, cm.confidence, cm.note
        ORDER BY amount DESC
        """,
        (location, start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def cogs(conn, location: str, start: str, end: str) -> dict:
    """Cost of goods sold, with the food-cost denominator split out.

    TakeOut Supplies is COGS-typed in both charts and stays inside COGS, but is
    excluded from the F&B subtotal that food cost % is computed on — takeaway
    packaging is not food. Decision of 2026-08-23; see FNB_COGS_CATEGORIES.
    """
    rows = [r for r in _invoice_costs(conn, location, start, end)
            if r["account_type"] in COGS_TYPES]
    lines, fnb, non_fnb, approximate = [], 0.0, 0.0, []
    for r in rows:
        amt = _r2(r["amount"])
        lines.append({"category": r["category"], "name": r["gl_name"],
                      "amount": amt, "confidence": r["confidence"],
                      "drill": {"source": "cogs", "key": r["category"]}})
        if r["category"] in FNB_COGS_CATEGORIES:
            fnb += amt
        else:
            non_fnb += amt
        if r["confidence"] == "approximate":
            approximate.append({"category": r["category"], "amount": amt,
                                "note": r["note"]})
    return {"lines": lines, "fnb_subtotal": _r2(fnb),
            "non_fnb_subtotal": _r2(non_fnb), "total": _r2(fnb + non_fnb),
            "approximate": approximate}


def operating_expenses(conn, location: str, start: str, end: str) -> dict:
    """Operating expense from two disjoint sources, kept visibly separate.

    invoiced — expense-typed invoice lines, recognised at invoice date.
    banked   — bank rows with NO invoice behind them (autopay utilities, card
               charges, fees). Settlements cannot appear here: they are barred
               from carrying a P&L account.
    """
    invoiced = [
        {"category": r["category"], "name": r["gl_name"], "amount": _r2(r["amount"]),
         "drill": {"source": "opex_invoiced", "key": r["category"]}}
        for r in _invoice_costs(conn, location, start, end)
        if r["account_type"] in EXPENSE_TYPES
    ]
    # Labor accounts are excluded here — labor is its own line inside prime
    # cost, and leaving them in counts the same wages twice.
    ph = ",".join("?" * len(LABOR_ACCOUNT_NAMES))
    banked_rows = conn.execute(
        f"""
        SELECT g.name AS gl_name, g.account_type,
               ROUND(SUM(-m.amount), 2) AS amount
        FROM manual_bank_entries m
        JOIN bank_accounts ba ON ba.id = m.bank_account_id
        JOIN gl_accounts g ON g.id = m.gl_account_id AND g.active = 1
        WHERE ba.location = ? AND m.entry_date BETWEEN ? AND ?
          AND g.account_type IN ('Expense', 'Other Expense')
          AND g.name NOT IN ({ph})
        GROUP BY g.name, g.account_type
        ORDER BY amount DESC
        """,
        (location, start, end, *sorted(LABOR_ACCOUNT_NAMES)),
    ).fetchall()
    banked = [{"name": r["gl_name"], "amount": _r2(r["amount"]),
               "drill": {"source": "opex_banked", "key": r["gl_name"]}}
              for r in banked_rows]
    return {"invoiced": invoiced, "banked": banked,
            "invoiced_total": _r2(sum(x["amount"] for x in invoiced)),
            "banked_total": _r2(sum(x["amount"] for x in banked)),
            "total": _r2(sum(x["amount"] for x in invoiced)
                         + sum(x["amount"] for x in banked))}


def labor(conn, location: str, start: str, end: str) -> dict:
    """Labor cost, from the bank rows that actually paid it.

    Source choice matters and none of the three candidates is clean:

      bank rows      — what left the account. COMPLETE (payroll runs, taxes,
                       contract labor) and used here.
      payroll_checks — partial. Dennis March 2026 holds one pay run of two,
                       five checks against 249 shifts worked.
      Toast entries  — hourly wages only. No employer taxes, no salaried.

    The bank rows are the only complete picture, but they are contaminated:
    TIP DISBURSEMENTS are coded to Payroll Expenses and Tip Wages. Tips are
    collected into a liability (Tip Bank) and paying them out should DEBIT that
    liability, not hit an expense account. So the payouts are quantified
    separately here and reported — not silently reclassified, which is a coding
    decision, not a reporting one.
    """
    ph = ",".join("?" * len(LABOR_ACCOUNT_NAMES))
    rows = conn.execute(
        f"""
        SELECT g.name AS gl_name, m.payee, m.memo, -m.amount AS amount
        FROM manual_bank_entries m
        JOIN bank_accounts ba ON ba.id = m.bank_account_id
        JOIN gl_accounts g ON g.id = m.gl_account_id AND g.active = 1
        WHERE ba.location = ? AND m.entry_date BETWEEN ? AND ?
          AND g.name IN ({ph})
        """,
        (location, start, end, *sorted(LABOR_ACCOUNT_NAMES)),
    ).fetchall()

    by_account, total, tips_out = {}, 0.0, 0.0
    # Tip-channel rows found sitting on a LABOR account. This should now be
    # empty: they are coded to Tip Bank at import. Anything here is a coding
    # fault to fix, and it is reported rather than silently netted out.
    tip_channels: dict[str, float] = {}
    for r in rows:
        amt = float(r["amount"] or 0)
        total += amt
        by_account[r["gl_name"]] = _r2(by_account.get(r["gl_name"], 0) + amt)
        blob = f"{r['payee'] or ''} {r['memo'] or ''}".upper()
        hint = next((h for h in _TIP_PAYOUT_HINTS if h in blob), None)
        if hint or r["gl_name"] == "Tip Wages":
            tips_out += amt
            label = _TIP_CHANNEL_LABELS.get(hint, r["gl_name"] if not hint else hint)
            tip_channels[label] = _r2(tip_channels.get(label, 0) + amt)

    ymd_start, ymd_end = start.replace("-", ""), end.replace("-", "")
    toast = conn.execute(
        """
        SELECT COUNT(*) AS shifts,
               ROUND(SUM(COALESCE(regular_hours, 0) + COALESCE(overtime_hours, 0)), 1) AS hours,
               ROUND(SUM(COALESCE(total_pay, 0)), 2) AS wages
        FROM time_entries
        WHERE location = ? AND business_date BETWEEN ? AND ?
        """,
        (location, ymd_start, ymd_end),
    ).fetchone()
    checks = conn.execute(
        """
        SELECT COUNT(*) AS checks, ROUND(SUM(COALESCE(pc.gross_pay, 0)), 2) AS gross
        FROM payroll_checks pc
        LEFT JOIN payroll_runs pr ON pr.id = pc.payroll_run_id
        WHERE pc.location = ?
          AND COALESCE(pr.pay_date, pc.pay_period_end) BETWEEN ? AND ?
          AND (pc.voided IS NULL OR pc.voided = 0)
        """,
        (location, start, end),
    ).fetchone()

    return {
        "source": "bank_rows_on_labor_accounts",
        "by_account": by_account,
        # Same figures as by_account, carrying drill descriptors. by_account
        # stays because it is the shape the page already renders from.
        "accounts": [
            {"name": n, "amount": a,
             "drill": {"source": "labor", "key": n}}
            for n, a in sorted(by_account.items(), key=lambda kv: -kv[1])
        ],
        "tip_drill": {"source": "labor_tips", "key": "*"},
        "total": _r2(total),
        # Labor net of the tip disbursements that do not belong in it.
        # Labor is the labor accounts. No tip subtraction — tip payouts are
        # coded to Tip Bank now, so there is nothing in here to net out. These
        # two report a CODING FAULT when nonzero, and the footnote says so.
        "wages_excl_tip_payouts": _r2(total),
        "tip_channel_rows_on_labor": _r2(tips_out),
        "tip_channels": tip_channels,
        "comparison": {
            "toast_hourly_wages": _r2(toast["wages"]),
            "toast_hours": _r2(toast["hours"]),
            "toast_shifts": toast["shifts"] or 0,
            "payroll_checks": checks["checks"] or 0,
            "payroll_checks_gross": _r2(checks["gross"]),
        },
    }


# ─── GUARDRAIL 3 ─────────────────────────────────────────────────────────────

def footnotes(conn, location: str, start: str, end: str, parts: dict) -> list[str]:
    """Known defects, computed against THIS period, printed on the report."""
    notes = []

    tips = conn.execute(
        """SELECT ROUND(SUM(COALESCE(li.credit, 0) - COALESCE(li.debit, 0)), 2) AS t
           FROM qb_journal_line_items li
           JOIN qb_journal_entries e ON e.id = li.entry_id
           WHERE e.location = ? AND e.entry_date BETWEEN ? AND ?
             AND li.journal_name = 'Summary: Tips'""",
        (location, start, end),
    ).fetchone()["t"]
    if tips:
        net = parts["revenue"]["net_revenue"] or 1
        notes.append(
            f"TIPS: ${_r2(tips):,.2f} passed through in this period "
            f"({_r2(tips) / net * 100:.1f}% of net revenue). Booked to Tip Bank, "
            f"a balance-sheet account, so it is correctly EXCLUDED from revenue "
            f"here. Summing all JE credits instead would overstate revenue by "
            f"that amount."
        )

    notes.append(
        "TOAST FEES ARE NOT BOOKED. Processing fees are netted out of the "
        "settlement deposit and never reach an expense account, so operating "
        "expense is understated and net income is overstated by roughly the "
        "monthly Toast fee."
    )
    notes.append(
        "NO INVENTORY ADJUSTMENT. COGS here is PURCHASES, not cost of goods "
        "consumed. Without opening and closing counts, a month that stocks up "
        "looks expensive and a month that draws down looks cheap. Food cost % "
        "is therefore directionally right and precisely wrong."
    )

    lab = parts["labor"]
    cmp_ = lab["comparison"]
    # This footnote is now an EXCEPTION report, not a standing caveat. Tip
    # payouts code to Tip Bank at import, so labor should contain none of them.
    if lab["tip_channel_rows_on_labor"]:
        chans = ", ".join(
            f"{name} ${amt:,.2f}" for name, amt
            in sorted(lab["tip_channels"].items(), key=lambda kv: -kv[1])
        ) or "unattributed"
        notes.append(
            f"MISCODED TIP PAYOUTS IN LABOR — "
            f"${lab['tip_channel_rows_on_labor']:,.2f} of tip-channel rows "
            f"({chans}) are sitting on labor accounts. They settle the Tip Bank "
            f"liability and are not labor, so labor above is OVERSTATED by that "
            f"amount. These are coded to Tip Bank automatically on import, so "
            f"any row here predates that or was coded by hand — fix it in the "
            f"register rather than adjusting the report."
        )
    notes.append(
        f"LABOR SOURCE is the bank rows on labor accounts, the only complete "
        f"view. For comparison: Toast time entries show "
        f"${cmp_['toast_hourly_wages']:,.2f} over {cmp_['toast_hours']:,.1f} "
        f"hours ({cmp_['toast_shifts']} shifts) — hourly wages only, no employer "
        f"taxes or salaried staff — and payroll checks total "
        f"${cmp_['payroll_checks_gross']:,.2f} across {cmp_['payroll_checks']} "
        f"check(s), which is partial coverage for the period."
    )

    approx = parts["cogs"]["approximate"]
    if approx:
        total = sum(a["amount"] for a in approx)
        notes.append(
            f"APPROXIMATE CATEGORY MAPPINGS: ${total:,.2f} across "
            f"{', '.join(a['category'] for a in approx)}. Totals are right; "
            f"the split between beverage lines is a judgement call."
        )

    unmapped = conn.execute(
        """SELECT ROUND(SUM(COALESCE(ii.total_price, 0)), 2) AS amt
           FROM scanned_invoice_items ii
           JOIN scanned_invoices si ON si.id = ii.invoice_id
           LEFT JOIN gl_category_mapping cm
                  ON cm.location = si.location
                 AND cm.category_type = COALESCE(NULLIF(TRIM(ii.category_type), ''), 'UNKNOWN')
           WHERE si.location = ? AND si.status = 'confirmed'
             AND si.invoice_date BETWEEN ? AND ? AND cm.id IS NULL""",
        (location, start, end),
    ).fetchone()["amt"]
    if unmapped:
        notes.append(
            f"UNCODED INVOICE SPEND: ${_r2(unmapped):,.2f} on line items whose "
            f"category has no GL mapping. Excluded from COGS and expense above."
        )
    return notes


# ─── THE REPORT ──────────────────────────────────────────────────────────────

# ─── DRILL-DOWN ──────────────────────────────────────────────────────────────
#
# Every figure on the P&L has to be openable, and what opens must ADD UP to the
# figure. That is the contract: a line whose drill does not sum to it is a bug
# in one of the two, and the tests assert it per source type.
#
# The line's own `drill` descriptor names its source and key, so the UI hands
# back what the engine gave it rather than re-deriving how a line was built.

DRILL_SOURCES = ("revenue", "cogs", "opex_invoiced", "opex_banked",
                 "labor", "labor_tips")


def drill(conn, location: str, start: str, end: str,
          source: str, key: str) -> dict:
    """The rows behind one P&L line.

    Returns {"source", "key", "columns", "rows", "total", "count"}; `total` is
    the sum of the rows' amounts and must equal the line it came from.
    """
    if source not in DRILL_SOURCES:
        raise ValueError(f"unknown drill source: {source!r}")

    # ── Journal entry lines, behind a revenue account.
    if source == "revenue":
        rows = conn.execute(
            """
            SELECT e.entry_date AS date, li.journal_name AS detail,
                   ROUND(COALESCE(li.credit, 0) - COALESCE(li.debit, 0), 2) AS amount
            FROM qb_journal_line_items li
            JOIN qb_journal_entries e ON e.id = li.entry_id
            JOIN qb_line_mapping m ON m.location = e.location
                                  AND m.journal_name = li.journal_name
            WHERE e.location = ? AND e.entry_date BETWEEN ? AND ?
              AND m.gl_account_id = ?
            ORDER BY e.entry_date, li.journal_name
            """,
            (location, start, end, int(key)),
        ).fetchall()
        cols = ["date", "detail", "amount"]

    # ── Invoice line items, behind a COGS or invoiced-expense category.
    elif source in ("cogs", "opex_invoiced"):
        rows = conn.execute(
            """
            SELECT si.invoice_date AS date,
                   si.vendor_name AS vendor,
                   si.invoice_number AS reference,
                   ii.product_name AS detail,
                   ii.quantity AS qty,
                   ROUND(COALESCE(ii.total_price, 0), 2) AS amount
            FROM scanned_invoice_items ii
            JOIN scanned_invoices si ON si.id = ii.invoice_id
            WHERE si.location = ? AND si.status = 'confirmed'
              AND si.invoice_date BETWEEN ? AND ?
              AND COALESCE(NULLIF(TRIM(ii.category_type), ''), 'UNKNOWN') = ?
            ORDER BY si.invoice_date, si.vendor_name, ii.product_name
            """,
            (location, start, end, str(key).strip().upper()),
        ).fetchall()
        cols = ["date", "vendor", "reference", "detail", "qty", "amount"]

    # ── Bank rows, behind a banked expense or labor account.
    elif source in ("opex_banked", "labor"):
        rows = conn.execute(
            """
            SELECT m.entry_date AS date, m.payee AS detail, m.memo AS reference,
                   m.gl_status AS status, m.id AS row_id,
                   ROUND(-m.amount, 2) AS amount
            FROM manual_bank_entries m
            JOIN bank_accounts ba ON ba.id = m.bank_account_id
            JOIN gl_accounts g ON g.id = m.gl_account_id AND g.active = 1
            WHERE ba.location = ? AND m.entry_date BETWEEN ? AND ?
              AND g.name = ?
            ORDER BY m.entry_date, m.id
            """,
            (location, start, end, str(key)),
        ).fetchall()
        cols = ["date", "detail", "reference", "status", "amount"]

    # ── The tip-disbursement adjustment: the rows it nets out.
    else:  # labor_tips
        ph = ",".join("?" * len(LABOR_ACCOUNT_NAMES))
        candidates = conn.execute(
            f"""
            SELECT m.entry_date AS date, m.payee AS detail, m.memo AS reference,
                   g.name AS account, m.id AS row_id,
                   ROUND(-m.amount, 2) AS amount
            FROM manual_bank_entries m
            JOIN bank_accounts ba ON ba.id = m.bank_account_id
            JOIN gl_accounts g ON g.id = m.gl_account_id AND g.active = 1
            WHERE ba.location = ? AND m.entry_date BETWEEN ? AND ?
              AND g.name IN ({ph})
            ORDER BY m.entry_date, m.id
            """,
            (location, start, end, *sorted(LABOR_ACCOUNT_NAMES)),
        ).fetchall()
        # Same predicate labor() uses, so the two cannot disagree.
        rows = [r for r in candidates
                if r["account"] == "Tip Wages"
                or any(h in f"{r['detail'] or ''} {r['reference'] or ''}".upper()
                       for h in _TIP_PAYOUT_HINTS)]
        cols = ["date", "detail", "reference", "account", "amount"]

    out = [dict(r) for r in rows]
    return {"source": source, "key": key, "columns": cols, "rows": out,
            "count": len(out), "total": _r2(sum(r["amount"] or 0 for r in out))}


def build_profit_loss(location: str, start: str, end: str, conn=None) -> dict:
    """Assemble the management P&L for one entity and period.

    Dates are inclusive, YYYY-MM-DD.
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        violations = assert_clearing_excluded(conn, location)
        if violations:
            # Guardrail 1 is not advisory. If a clearing account can reach the
            # revenue section, every number below it is suspect, and the report
            # says so instead of printing a total.
            logger.error("Guardrail 1 violated for %s: %s", location, violations)

        rev = revenue(conn, location, start, end)
        cg = cogs(conn, location, start, end)
        opex = operating_expenses(conn, location, start, end)
        lab = labor(conn, location, start, end)
        plugs = plug_days(conn, location, start, end)
        control = journal_control_total(conn, location, start, end)
        coverage = expense_coverage(conn, location, start, end)
        # Entity-wide and cumulative, not period-specific: it is a statement
        # about the account's health, so it rides on every P&L for that entity
        # rather than only the month the drift happened in.
        sales_tax = sales_tax_rollforward(conn, location)

        net_rev = rev["net_revenue"]
        # Prime cost uses labor NET of tip disbursements. A tip handed to a
        # server is not a cost of running the restaurant — it is the settlement
        # of money held on their behalf.
        labor_cost = lab["wages_excl_tip_payouts"]
        # With no labor rows imported, labor is UNKNOWN, not zero. Reporting
        # 0.00 reads as a measured figure, and prime cost built on it is COGS
        # wearing a prime-cost label — Chatham March showed 39.65% that way.
        labor_known = coverage["labor_rows"] > 0
        prime = _r2(cg["total"] + labor_cost) if labor_known else None
        parts = {"revenue": rev, "cogs": cg, "labor": lab}

        def pct(x):
            return _r2(x / net_rev * 100) if net_rev else None

        return {
            "location": location,
            "period": {"start": start, "end": end},
            # No sales journal entries at all. Every figure below would be a
            # zero that LOOKS like a measured zero, so callers must say "no
            # data for this period" rather than render an empty statement.
            "has_sales_journal": control["entries"] > 0,
            "guardrails": {
                "clearing_excluded": not violations,
                "clearing_violations": violations,
                "plug": plugs,
                "journal_control": control,
                "expense_coverage": coverage,
                "sales_tax": sales_tax,
            },
            "revenue": rev,
            "cogs": {**cg,
                     "food_cost_pct": pct(cg["fnb_subtotal"]),
                     "total_cogs_pct": pct(cg["total"])},
            "labor": {**lab,
                      "available": labor_known,
                      "labor_cost": labor_cost if labor_known else None,
                      "labor_pct": pct(labor_cost) if labor_known else None},
            "prime_cost": prime,
            "prime_cost_pct": pct(prime) if labor_known else None,
            "operating_expenses": opex,
            # Withheld rather than printed when the expense side is missing —
            # a bottom line computed off half the costs is worse than none.
            "net_income": (_r2(net_rev - cg["total"] - labor_cost - opex["total"])
                           if coverage["expense_side_complete"] else None),
            "net_income_withheld_reason": (None if coverage["expense_side_complete"]
                                           else coverage["warning"]),
            "footnotes": ([coverage["warning"]] if coverage["warning"] else [])
                         + footnotes(conn, location, start, end, parts)
                         + _sales_tax_footnotes(conn, location, sales_tax),
        }
    finally:
        if own:
            conn.close()
