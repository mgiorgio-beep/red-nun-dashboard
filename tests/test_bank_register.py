"""
Bank register / reconciliation regression tests.

These run against the LIVE database, read-only (GET requests and SELECTs only).
That is deliberate: the whole point is to replay the real statements Cape Cod
Five actually issued and assert the tie-outs to the penny. A synthetic fixture
would only prove the arithmetic, not that *these books* still balance.

THE PASS CONDITION IS CLEARED-ONLY: a statement reflects only what the bank has
processed, so the tie-out is

    statement.beginning + cleared inflows - cleared outflows == statement.ending

All four covered statements satisfy this exactly. Uncleared rows are outstanding
items — checks written but not yet cashed — and are asserted separately, at
exact values, so a change is visible. A test that says "March doesn't tie" is
worth nothing; a test that says "March has 3 outstanding items netting -57.46"
is a fact about the books.

All comparisons are in INTEGER CENTS. The premise is "ties to the penny", and
float addition over ~200 rows does not.

Run:  venv/bin/python3 -m pytest tests/test_bank_register.py -v
"""
import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.toast.data_store import get_connection, DB_PATH  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.path.exists(DB_PATH),
    reason=f"live database not present at {DB_PATH}",
)


def cents(x):
    """Money -> integer cents. Never compare these as floats."""
    return int(round(float(x or 0) * 100))


@pytest.fixture(scope="module")
def conn():
    c = get_connection()
    yield c
    c.close()


@pytest.fixture(scope="module")
def client():
    from web.server import app
    app.config["TESTING"] = True
    with app.test_client() as cl:
        with cl.session_transaction() as s:
            s["user_id"] = 1
            s["role"] = "admin"
            s["username"] = "pytest"
        yield cl


@pytest.fixture(scope="module")
def uploads(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM bank_statement_uploads ORDER BY bank_account_id, period_start"
    )]


def register(client, upload):
    r = client.get(
        f"/api/register/{upload['bank_account_id']}"
        f"?start={upload['period_start']}&end={upload['period_end']}"
    )
    assert r.status_code == 200, f"register query failed: {r.status_code}"
    return r.get_json()


def tie_out(client, upload):
    """(computed, stated, delta) in integer cents for one statement period.

    THE PASS CONDITION IS CLEARED-ONLY:

        statement.beginning_balance + cleared inflows - cleared outflows
            == statement.ending_balance

    A bank statement only ever reflects transactions the bank has processed.
    Summing ALL register rows counts checks that were written but had not
    cleared by the period end, and reports a break that is really just timing.

    The handover brief's §10 health-check snippet makes exactly that mistake —
    it uses summary.total_inflow/total_outflow over every row — which is why
    Chatham January looked like an unexplained -5,151.48 shortfall. It is 9
    outstanding items, and the account ties to the penny.
    """
    j = register(client, upload)
    clr_in = sum(r["inflow"] for r in j["rows"] if r["cleared"])
    clr_out = sum(r["outflow"] for r in j["rows"] if r["cleared"])
    computed = cents(upload["beginning_balance"]) + cents(clr_in) - cents(clr_out)
    stated = cents(upload["ending_balance"])
    return computed, stated, computed - stated


# ── Statement continuity (brief §5c) ─────────────────────────────────────────

class TestStatementContinuity:
    """One check that detects a missing month, an overlap, a re-upload, and a
    mis-parsed balance. It is also the only validation of the anchors
    themselves — 'balances are facts, not settings' is a claim about parser
    output, and continuity is what makes it safe to rely on."""

    def test_consecutive_statements_chain_balances(self, uploads):
        by_acct = {}
        for u in uploads:
            by_acct.setdefault(u["bank_account_id"], []).append(u)
        checked = 0
        for acct, ups in by_acct.items():
            ups.sort(key=lambda u: u["period_start"])
            for prev, nxt in zip(ups, ups[1:]):
                assert cents(prev["ending_balance"]) == cents(nxt["beginning_balance"]), (
                    f"account {acct}: {prev['period_end']} ends at "
                    f"{prev['ending_balance']} but {nxt['period_start']} begins at "
                    f"{nxt['beginning_balance']}"
                )
                checked += 1
        assert checked >= 2, "expected at least two consecutive Dennis pairs to check"

    def test_consecutive_statements_have_no_date_gap(self, uploads):
        by_acct = {}
        for u in uploads:
            by_acct.setdefault(u["bank_account_id"], []).append(u)
        for acct, ups in by_acct.items():
            ups.sort(key=lambda u: u["period_start"])
            for prev, nxt in zip(ups, ups[1:]):
                end = date.fromisoformat(prev["period_end"])
                start = date.fromisoformat(nxt["period_start"])
                assert end + timedelta(days=1) == start, (
                    f"account {acct}: gap or overlap between {prev['period_end']} "
                    f"and {nxt['period_start']}"
                )

    def test_every_statement_has_both_anchors(self, uploads):
        for u in uploads:
            assert u["beginning_balance"] is not None, f"upload {u['id']} has no beginning_balance"
            assert u["ending_balance"] is not None, f"upload {u['id']} has no ending_balance"
            assert u["period_start"] and u["period_end"], f"upload {u['id']} has no period"


class TestStatementUniqueness:
    """bank_statement_uploads was append-only: a re-upload created a second row
    for the same period and re-imported every line."""

    def test_unique_index_exists(self, conn):
        idx = [dict(r) for r in conn.execute("PRAGMA index_list(bank_statement_uploads)")]
        uq = [i for i in idx if i["name"] == "uq_bsu_account_period"]
        assert uq, "uq_bsu_account_period is missing"
        assert uq[0]["unique"] == 1, "uq_bsu_account_period exists but is not UNIQUE"

    def test_no_duplicate_account_periods(self, conn):
        dupes = conn.execute(
            "SELECT bank_account_id, period_start, period_end, COUNT(*) n "
            "FROM bank_statement_uploads "
            "GROUP BY bank_account_id, period_start, period_end HAVING n > 1"
        ).fetchall()
        assert not dupes, f"duplicate statement periods: {[dict(d) for d in dupes]}"


# ── Tie-outs: the actual test of the books ───────────────────────────────────

DENNIS = 2
CHATHAM = 1


def _upload(uploads, acct, period_start):
    for u in uploads:
        if u["bank_account_id"] == acct and u["period_start"] == period_start:
            return u
    pytest.skip(f"statement not loaded: account {acct} starting {period_start}")


class TestEveryStatementTiesExactly:
    """The real reconciliation test, and it passes on every statement we hold.

    statement.beginning + cleared flows == statement.ending, to the penny,
    for all four covered periods. Any nonzero delta here is a genuine break:
    a missing transaction, a wrong amount, or a row cleared that should not be.
    """

    def test_all_statements_tie_to_the_penny(self, client, uploads):
        assert uploads, "no statements loaded"
        breaks = []
        for u in uploads:
            _, _, delta = tie_out(client, u)
            if delta != 0:
                breaks.append(
                    f"account {u['bank_account_id']} "
                    f"{u['period_start']}..{u['period_end']} off by {delta/100:,.2f}"
                )
        assert not breaks, "statements no longer tie: " + "; ".join(breaks)

    @pytest.mark.parametrize("acct,period,begin,end", [
        (CHATHAM, "2026-01-01", 3401357, 2578504),
        (DENNIS,  "2026-01-01",  949535, 2428014),
        (DENNIS,  "2026-02-02", 2428014, 3574663),
        (DENNIS,  "2026-03-02", 3574663, 3216219),
    ])
    def test_each_statements_anchors_are_unchanged(self, client, uploads,
                                                   acct, period, begin, end):
        """The anchors are facts printed on the statement. If one of these
        moves, the parser or the upload changed — not the books."""
        u = _upload(uploads, acct, period)
        assert cents(u["beginning_balance"]) == begin
        assert cents(u["ending_balance"]) == end
        computed, stated, delta = tie_out(client, u)
        assert delta == 0, f"{period} off by {delta/100:,.2f}"


class TestOutstandingItems:
    """book - bank == outstanding. These are timing differences, not errors:
    checks written but not yet cashed, deposits in transit.

    Asserted at exact values so a change is visible. When reconciling items are
    persisted and carried forward, these become the seed data.
    """

    def test_book_minus_bank_equals_outstanding(self, client, uploads):
        for u in uploads:
            s = register(client, u)["summary"]
            assert (cents(s["book_balance"]) - cents(s["bank_balance"])
                    == cents(s["outstanding_net"])), (
                f"identity broken for account {u['bank_account_id']} {u['period_start']}"
            )

    def test_reconciliation_figures_ignore_the_display_filter(self, client, uploads):
        """book/bank/outstanding are the reconciliation; `cleared` is a display
        control. Toggling the filter must not move them."""
        u = uploads[-1]
        base = None
        for f in ("all", "cleared", "uncleared"):
            r = client.get(
                f"/api/register/{u['bank_account_id']}"
                f"?start={u['period_start']}&end={u['period_end']}&cleared={f}"
            ).get_json()["summary"]
            trio = (r["book_balance"], r["bank_balance"], r["outstanding_net"])
            if base is None:
                base = trio
            assert trio == base, f"cleared={f} moved the reconciliation figures"

    def test_dennis_march_has_one_outstanding_check(self, client, uploads):
        """-57.46 is Maya Jones, written in March, uncleared at 03/31. This is
        THE worked example for why `delta == 0` is the wrong pass condition."""
        u = _upload(uploads, DENNIS, "2026-03-02")
        s = register(client, u)["summary"]
        assert cents(s["outstanding_net"]) == -5746
        assert s["outstanding_count"] == 3

    def test_chatham_january_outstanding(self, client, uploads):
        """Previously mis-reported as an unexplained -5,151.48 break. It is 9
        outstanding items; the statement itself ties exactly."""
        u = _upload(uploads, CHATHAM, "2026-01-01")
        s = register(client, u)["summary"]
        assert cents(s["outstanding_net"]) == -515148
        assert s["outstanding_count"] == 9
        _, _, delta = tie_out(client, u)
        assert delta == 0, "Chatham January must tie on cleared rows"


class TestMarchMerges:
    # The four March duplicates, as MERGED — identified by payroll_checks.id,
    # deliberately not by check_number. See TestCheckNumberIsNotAKey.
    MARCH_MERGES = {22: 360.98, 24: 978.20, 25: 645.98, 28: 1203.87}

    def test_march_duplicate_payroll_checks_are_merged(self, conn):
        """The four March payroll checks must be cleared book rows with no
        statement twin left behind, and each must still carry the amount it
        was matched on. Guards against a re-import recreating the duplicates.
        """
        for pid, amount in self.MARCH_MERGES.items():
            r = conn.execute(
                "SELECT check_number, employee_name, net_pay, cleared, cleared_date, "
                "pay_period_start FROM payroll_checks WHERE id = ?", (pid,)
            ).fetchone()
            assert r, f"payroll_check #{pid} is gone"
            assert cents(r["net_pay"]) == cents(amount), (
                f"payroll_check #{pid} net_pay moved from {amount} to {r['net_pay']}"
            )
            assert r["cleared"] == 1, f"payroll_check #{pid} is no longer cleared"
            assert r["cleared_date"], f"payroll_check #{pid} has no cleared_date"
            assert r["pay_period_start"] == "2026-03-02", (
                f"payroll_check #{pid} is not the March 02-15 pay period"
            )

        audited = conn.execute(
            "SELECT COUNT(*) FROM register_merge_audit "
            "WHERE target_source = 'payroll_check' AND target_id IN (22,24,25,28) "
            "AND reversed_at IS NULL"
        ).fetchone()[0]
        assert audited == 4, f"expected 4 audited March merges, found {audited}"


class TestCheckNumberIsNotAKey:
    """payroll_checks.check_number does NOT identify the physical check that
    cleared the bank, and must never be used as a join key.

    Found 2026-08-22 while verifying the March dedupe. The March statement
    lines read 'Check 9689 / 9692 / 9693 / 9695' and cleared 03/23-03/24. The
    payroll_checks rows bearing those exact numbers belong to the
    2026-05-25..06-07 pay period, are different employees, and carry different
    amounts. The March checks were recorded in the dashboard as 2011-2017 —
    its own sequence — so the number the bank printed was never stored.

    This refutes the handover brief's §7 job 034, which proposes backfilling
    check_number and matching statement lines on it. Doing that would have
    matched a March statement line to a June payroll check. Amount + date is
    the reliable signal here; the check number is actively misleading.
    """

    def test_no_statement_check_line_agrees_with_payroll_check_number(self, conn):
        """Documents the scale of the mismatch. If this ever starts finding
        agreements, the numbering has been reconciled and check-number matching
        can be reconsidered — deliberately, not by accident."""
        import re
        rows = conn.execute(
            "SELECT payee, amount FROM manual_bank_entries "
            "WHERE bank_account_id = ? AND payee LIKE 'Check %'", (DENNIS,)
        ).fetchall()
        assert rows, "no statement check lines found for Dennis"
        agree = 0
        for r in rows:
            num = re.sub(r"[^0-9]", "", r["payee"] or "")
            if not num:
                continue
            p = conn.execute(
                "SELECT net_pay FROM payroll_checks WHERE check_number = ?", (num,)
            ).fetchone()
            if p and cents(p["net_pay"]) == abs(cents(r["amount"])):
                agree += 1
        assert agree == 0, (
            f"{agree} of {len(rows)} statement check lines now agree with "
            f"payroll_checks.check_number on both number and amount. That is a "
            f"CHANGE from the documented 0 — re-evaluate whether check-number "
            f"matching is safe before relying on it."
        )

    def test_the_969x_rows_are_a_later_pay_period(self, conn):
        """The specific collision, pinned so it can't be silently 'fixed' by
        renumbering without someone reading this."""
        for num in ("9689", "9692", "9693"):
            r = conn.execute(
                "SELECT pay_period_start FROM payroll_checks WHERE check_number = ?",
                (num,)
            ).fetchone()
            if r:
                assert r["pay_period_start"] == "2026-05-25", (
                    f"check_number {num} moved to pay period "
                    f"{r['pay_period_start']} — the documented collision with "
                    f"the March statement has changed shape"
                )


# ── Query-shape guards the tie-outs depend on ────────────────────────────────

class TestRegisterQueryContract:
    def test_end_date_is_inclusive(self, client, conn, uploads):
        """The brief flags 02/02-03/01 as where an off-by-one would hide: if
        `end` were exclusive, the last day's rows would silently vanish and the
        delta would look like a data problem.

        Picks a date inside the Dennis February period that actually carries
        rows, so this guard always runs rather than skipping."""
        u = _upload(uploads, DENNIS, "2026-02-02")
        row = conn.execute(
            "SELECT entry_date, COUNT(*) n FROM manual_bank_entries "
            "WHERE bank_account_id = ? AND entry_date BETWEEN ? AND ? "
            "GROUP BY entry_date ORDER BY n DESC LIMIT 1",
            (DENNIS, u["period_start"], u["period_end"]),
        ).fetchone()
        assert row, "Dennis February has no manual entries at all — unexpected"
        day, n = row["entry_date"], row["n"]

        r = client.get(f"/api/register/{DENNIS}?start={day}&end={day}")
        rows = r.get_json()["rows"]
        assert len(rows) >= n, (
            f"{n} manual rows exist on {day} but a single-day query returned "
            f"{len(rows)} — `end` is being treated as exclusive"
        )
        assert all(x["date"] == day for x in rows), (
            f"single-day query for {day} returned other dates: "
            f"{sorted({x['date'] for x in rows})}"
        )

    def test_summary_reports_uncleared_count(self, client, uploads):
        u = _upload(uploads, DENNIS, "2026-03-02")
        s = client.get(
            f"/api/register/{DENNIS}?start={u['period_start']}&end={u['period_end']}"
        ).get_json()["summary"]
        assert "uncleared_count" in s
        assert "unassigned_count" in s


class TestMergeAudit:
    """dedupe_register() is the only irreversible operation in this path: it
    stamps the book row cleared and DELETEs the statement row. Every one of
    those deletions must leave a restorable trail."""

    def test_audit_table_exists(self, conn):
        t = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='register_merge_audit'"
        ).fetchone()
        assert t, "register_merge_audit is missing — do not run dedupe without it"

    def test_every_audit_row_is_restorable(self, conn):
        bad = conn.execute(
            "SELECT id FROM register_merge_audit "
            "WHERE deleted_entry_json IS NULL OR deleted_entry_json = ''"
        ).fetchall()
        assert not bad, (
            f"{len(bad)} merges recorded without the deleted row's payload — "
            f"those cannot be undone: ids {[b[0] for b in bad]}"
        )

    def test_every_audit_row_identifies_both_sides(self, conn):
        bad = conn.execute(
            "SELECT id FROM register_merge_audit "
            "WHERE target_source IS NULL OR target_id IS NULL "
            "OR deleted_entry_id IS NULL OR match_rule IS NULL"
        ).fetchall()
        assert not bad, f"incomplete audit rows: {[b[0] for b in bad]}"

    def test_no_merged_statement_row_still_exists(self, conn):
        """A deleted entry id must not reappear in manual_bank_entries — that
        would mean a re-import recreated a row we already merged away."""
        back = conn.execute(
            "SELECT a.deleted_entry_id FROM register_merge_audit a "
            "JOIN manual_bank_entries m ON m.id = a.deleted_entry_id "
            "WHERE a.reversed_at IS NULL"
        ).fetchall()
        assert not back, (
            f"statement rows {[b[0] for b in back]} were merged away but exist "
            f"again — a re-import has recreated merged duplicates"
        )


class TestProvenanceInvariant:
    """gl_status must never be 'confirmed' unless a human set it. Nothing may
    learn a rule from a suggested row (brief §2.4)."""

    TABLES = ("manual_bank_entries", "vendor_payments", "payroll_checks", "bank_deposits")

    def test_no_coded_row_lacks_provenance(self, conn):
        for t in self.TABLES:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE gl_account_id IS NOT NULL "
                f"AND gl_status IS NULL"
            ).fetchone()[0]
            assert n == 0, f"{t}: {n} coded rows carry no provenance"

    def test_no_provenance_without_a_coding(self, conn):
        for t in self.TABLES:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE gl_status IS NOT NULL "
                f"AND gl_account_id IS NULL"
            ).fetchone()[0]
            assert n == 0, f"{t}: {n} rows carry provenance but no GL account"

    def test_gl_status_values_are_known(self, conn):
        for t in self.TABLES:
            bad = conn.execute(
                f"SELECT DISTINCT gl_status FROM {t} WHERE gl_status IS NOT NULL "
                f"AND gl_status NOT IN ('confirmed','suggested')"
            ).fetchall()
            assert not bad, f"{t}: unexpected gl_status values {[b[0] for b in bad]}"

    def test_rule_learning_is_gated_on_confirmed(self, conn):
        from routes.register_routes import _may_learn_rule_from
        sug = conn.execute(
            "SELECT id FROM manual_bank_entries WHERE gl_status='suggested' LIMIT 1"
        ).fetchone()
        if sug:
            assert not _may_learn_rule_from(conn, "manual_bank_entries", sug["id"])
        conf = conn.execute(
            "SELECT id FROM manual_bank_entries WHERE gl_status='confirmed' LIMIT 1"
        ).fetchone()
        if conf:
            assert _may_learn_rule_from(conn, "manual_bank_entries", conf["id"])


# ── Transfer classifier ──────────────────────────────────────────────────────

class TestTransferClassifier:
    """Direction and counterparty both decide the answer, so a blanket
    'any transfer is rent' rule is wrong. Only 2757 -> 5087 outflows are rent.

    2757 Red Nun Public House (Dennis restaurant)
    5975 Red Buoy Inc         (Chatham restaurant)
    5087 Red Nun Realty LLC   (owns the Dennis Port property)
    """

    def _c(self):
        from routes.register_routes import classify_transfer
        return classify_transfer

    def test_restaurant_to_realty_outflow_is_rent(self):
        for desc in ("Transfer from x2757 to x5087",
                     "Transfer to DDA Acct No. Acct Ending 5087"):
            gl, reason = self._c()(desc, -1805.73, "2757")
            assert gl == "Building Rent", f"{desc!r} -> {gl} ({reason})"

    def test_variable_rent_amounts_still_classify(self):
        for amt in (-1000.00, -2000.00, -2500.00, -4000.00):
            gl, _ = self._c()("Transfer from x2757 to x5087", amt, "2757")
            assert gl == "Building Rent", f"{amt} should still be rent"

    def test_transfer_to_chatham_is_not_rent(self):
        """5975 is the other restaurant — this is the intercompany loan."""
        gl, reason = self._c()(
            "Transfer from x2757 to x5975 Loan repayment", -2000.00, "2757")
        assert gl is None
        assert "5975" in reason

    def test_inflow_from_realty_is_not_rent(self):
        """Rent does not flow backward."""
        gl, reason = self._c()(
            "Transfer from x5087 to x2757 Rasmussen", 1750.00, "2757")
        assert gl is None
        assert "backward" in reason

    def test_unknown_counterparty_is_not_rent(self):
        gl, reason = self._c()("Transfer from x5975 to x1239", -1500.00, "5975")
        assert gl is None and reason

    def test_non_transfers_are_ignored_entirely(self):
        gl, reason = self._c()("DBT CRD 1321 NORTH STATION GARAGE", -18.00, "2757")
        assert gl is None and reason is None

    def test_live_transfer_rows_match_the_classifier(self, conn):
        """Every transfer row must agree with the classifier.

        "Flag for review" means a HUMAN should look at it and decide — so a
        human-confirmed coding on a flagged row is the process working, not a
        violation. Row 1122 ("Transfer from x2757 to x5975 / Loan repayment")
        was flagged as the intercompany loan and Mike coded it to Loan to Red
        Buoy Inc.; that is the right answer, and an earlier version of this
        test failed it for existing.

        What must never happen is the machine guessing: a flagged row may not
        carry a machine coding, and must never be Building Rent, which is the
        specific wrong answer this classifier exists to prevent.
        """
        from routes.register_routes import classify_transfer
        acct = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, account_last4, location FROM bank_accounts")}
        # Building Rent exists PER ENTITY (both charts have one), so resolve it
        # against the row's own location — not globally.
        rent_by_loc = {
            r["location"]: r["id"] for r in conn.execute(
                "SELECT id, location FROM gl_accounts "
                "WHERE name = 'Building Rent' AND active = 1")
        }
        assert rent_by_loc, "no active Building Rent account in any chart"
        rent_ids = set(rent_by_loc.values())
        rows = conn.execute(
            "SELECT id, bank_account_id, payee, memo, amount, gl_account_id, gl_status "
            "FROM manual_bank_entries WHERE UPPER(COALESCE(payee,'')) LIKE '%TRANSFER%'"
        ).fetchall()
        assert rows, "no transfer rows found"
        for r in rows:
            a = acct.get(r["bank_account_id"], {})
            desc = (r["payee"] or "") + " " + (r["memo"] or "")
            name, reason = classify_transfer(
                desc, r["amount"], a.get("account_last4") or "")
            if name == "Building Rent":
                expected = rent_by_loc.get(a.get("location"))
                assert r["gl_account_id"] == expected, (
                    f"row {r['id']} ({a.get('location')}) should be Building Rent "
                    f"{expected}, is {r['gl_account_id']}"
                )
            elif reason:
                if r["gl_account_id"] is None:
                    continue                      # still awaiting review — fine
                assert r["gl_status"] == "confirmed", (
                    f"row {r['id']} was flagged for review ({reason}) but carries "
                    f"a machine coding to {r['gl_account_id']} — a flagged "
                    f"transfer may only be coded by a human"
                )
                assert r["gl_account_id"] not in rent_ids, (
                    f"row {r['id']} was flagged as NOT rent ({reason}) but is "
                    f"coded to Building Rent"
                )


# ── Reconciliation sign-off ──────────────────────────────────────────────────

class TestReconciliationSignOff:
    """A closed period records that the cleared rows tied exactly AND that a
    named person accepted the itemized remainder."""

    def test_unreconciled_periods_are_only_the_most_recent(self, client, uploads):
        """A freshly imported statement is not yet signed off, and that is
        ordinary workflow — Mike imports, then reconciles. What must not happen
        is an OLD period being skipped and left behind while later ones close.

        So the invariant is ordering, not completeness: per account, every
        unreconciled period must be more recent than every reconciled one.
        """
        j = client.get("/api/bank-reconcile/reconciliations").get_json()
        closed = {(r["bank_account_id"], r["period_start"]) for r in j["reconciliations"]
                  if r["status"] == "reconciled"}
        by_acct = {}
        for u in uploads:
            by_acct.setdefault(u["bank_account_id"], []).append(u["period_start"])
        for acct, periods in by_acct.items():
            done = sorted(p for p in periods if (acct, p) in closed)
            open_ = sorted(p for p in periods if (acct, p) not in closed)
            if not done or not open_:
                continue
            assert min(open_) > max(done), (
                f"account {acct}: period {min(open_)} is unreconciled but "
                f"{max(done)} is closed — an earlier period was skipped"
            )

    def test_closed_periods_tie_exactly_and_name_a_signer(self, client):
        j = client.get("/api/bank-reconcile/reconciliations").get_json()
        for r in j["reconciliations"]:
            assert cents(r["delta"]) == 0, (
                f"rec {r['id']} closed with a nonzero delta {r['delta']}"
            )
            assert r["closed_by"], f"rec {r['id']} has no signer"
            assert r["closed_at"], f"rec {r['id']} has no close timestamp"

    def test_outstanding_is_fully_itemized(self, client):
        """outstanding_net must equal the sum of the snapshot items. This is
        the 'fully itemized' half of the pass condition — an unexplained
        remainder is exactly what must not be signable."""
        j = client.get("/api/bank-reconcile/reconciliations").get_json()
        for r in j["reconciliations"]:
            item_sum = sum(cents(i["amount"]) for i in r["items"])
            assert item_sum == cents(r["outstanding_net"]), (
                f"rec {r['id']}: items sum to {item_sum} but outstanding_net is "
                f"{cents(r['outstanding_net'])}"
            )

    def test_snapshot_is_a_copy_not_a_view(self, conn):
        """Items must carry their own amount/date/payee so the record survives
        later edits to the underlying row."""
        n = conn.execute(
            "SELECT COUNT(*) FROM bank_reconciliation_items "
            "WHERE amount IS NULL OR entry_date IS NULL"
        ).fetchone()[0]
        assert n == 0, f"{n} snapshot items are missing their frozen values"

    def test_march_outstanding_is_maya_jones(self, client):
        j = client.get("/api/bank-reconcile/reconciliations?account_id=2").get_json()
        march = [r for r in j["reconciliations"] if r["period_start"] == "2026-03-02"]
        assert march, "Dennis March is not reconciled"
        nonzero = [i for i in march[0]["items"] if cents(i["amount"]) != 0]
        assert len(nonzero) == 1, f"expected one nonzero outstanding item, got {nonzero}"
        assert cents(nonzero[0]["amount"]) == -5746
        assert "Maya" in (nonzero[0]["payee"] or "")

    def test_close_refuses_a_period_that_does_not_tie(self, client, monkeypatch):
        """A signature must never paper over an unexplained delta."""
        import routes.bank_reconcile_routes as brr
        real = brr._reconciliation_state
        monkeypatch.setattr(
            brr, "_reconciliation_state",
            lambda conn, up: {**real(conn, up), "ties": False, "delta": -123.45},
        )
        u = client.get("/api/bank-reconcile/uploads").get_json()["uploads"][0]
        r = client.post("/api/bank-reconcile/reconciliation/close",
                        json={"upload_id": u["id"]})
        assert r.status_code == 409
        assert "-123.45" in r.get_json()["error"]

    def test_preview_writes_nothing(self, client, conn, uploads):
        before = conn.execute("SELECT COUNT(*) FROM bank_reconciliations").fetchone()[0]
        for u in uploads:
            client.get(f"/api/bank-reconcile/reconciliation/preview?upload_id={u['id']}")
        after = conn.execute("SELECT COUNT(*) FROM bank_reconciliations").fetchone()[0]
        assert before == after, "preview created reconciliation rows"


# ── GL account integrity ─────────────────────────────────────────────────────

class TestGlAccountGuard:
    """The PUT validated existence only, which is how 225 rows ended up coded
    to retired accounts. It must also require active + location-compatible.

    All three cases below are rejected BEFORE any write, so exercising them
    against the live database changes nothing.
    """

    def _a_dennis_row(self, conn):
        return conn.execute(
            "SELECT m.id FROM manual_bank_entries m "
            "JOIN bank_accounts ba ON ba.id = m.bank_account_id "
            "WHERE ba.location = 'dennis' LIMIT 1"
        ).fetchone()["id"]

    def test_rejects_inactive_account(self, client, conn):
        bad = conn.execute("SELECT id FROM gl_accounts WHERE active = 0 LIMIT 1").fetchone()
        if not bad:
            pytest.skip("no inactive accounts to test with")
        rid = self._a_dennis_row(conn)
        before = conn.execute(
            "SELECT gl_account_id FROM manual_bank_entries WHERE id = ?", (rid,)
        ).fetchone()["gl_account_id"]
        r = client.put("/api/register/row/gl-account", json={
            "source": "manual", "id": rid, "gl_account_id": bad["id"], "create_rule": False})
        assert r.status_code == 409
        assert "inactive" in r.get_json()["error"]
        after = conn.execute(
            "SELECT gl_account_id FROM manual_bank_entries WHERE id = ?", (rid,)
        ).fetchone()["gl_account_id"]
        assert after == before, "a rejected PUT still wrote to the row"

    def test_rejects_wrong_location_account(self, client, conn):
        bad = conn.execute(
            "SELECT id FROM gl_accounts WHERE active = 1 AND location = 'chatham' LIMIT 1"
        ).fetchone()
        if not bad:
            pytest.skip("no active chatham accounts to test with")
        rid = self._a_dennis_row(conn)
        r = client.put("/api/register/row/gl-account", json={
            "source": "manual", "id": rid, "gl_account_id": bad["id"], "create_rule": False})
        assert r.status_code == 409
        assert "chatham" in r.get_json()["error"]

    def test_rejects_missing_account(self, client, conn):
        rid = self._a_dennis_row(conn)
        r = client.put("/api/register/row/gl-account", json={
            "source": "manual", "id": rid, "gl_account_id": 999999, "create_rule": False})
        assert r.status_code == 404


class TestNoOrphanedGlReferences:
    """No row and no rule may point at an inactive or cross-entity account.

    This is the real deliverable of the 2026-08-22 repair — the 225 broken rows
    were the symptom. An orphaned reference is invisible in the UI (it used to
    render as an empty box) and silently wrong in any P&L built off the GL, so
    it has to be structurally impossible rather than periodically cleaned up.

    There are no allowed exceptions. The last two — the Apple Card rows coded
    to "Owner's Pay & Personal Expenses" (413, Chatham) — were closed on
    2026-08-23 by reactivating 413. That was the chart-of-accounts call the
    repair could not make on its own: an owner's personal card paid from the
    business account IS a draw, so an Equity target is correct, and a register
    row coding to a balance-sheet account is normal (draws, transfers, AP).
    """

    SOURCES = [
        ("manual_bank_entries", "bank_account_id"),
        ("vendor_payments", "bank_account_id"),
        ("payroll_checks", "bank_account_id"),
        ("bank_deposits", "bank_account_id"),
    ]

    def test_no_row_points_at_an_invalid_account(self, conn):
        bad = []
        for table, acct_col in self.SOURCES:
            for r in conn.execute(f"""
                SELECT t.id, t.gl_account_id, g.name, g.active, g.location AS gl_loc,
                       ba.location AS row_loc
                FROM {table} t
                JOIN gl_accounts g ON g.id = t.gl_account_id
                LEFT JOIN bank_accounts ba ON ba.id = t.{acct_col}
                WHERE t.gl_account_id IS NOT NULL
                  AND (g.active = 0
                       OR (g.location IS NOT NULL AND ba.location IS NOT NULL
                           AND g.location <> ba.location))
            """):
                bad.append(f"{table}#{r['id']} -> {r['gl_account_id']} "
                           f"'{r['name']}' (active={r['active']}, "
                           f"{r['gl_loc']} vs row {r['row_loc']})")
        assert not bad, f"{len(bad)} orphaned GL references: " + "; ".join(bad[:10])

    def test_no_rule_points_at_an_invalid_account(self, conn):
        bad = []
        for r in conn.execute("""
            SELECT r.id, r.pattern, r.location AS rule_loc, r.gl_account_id,
                   g.name, g.active, g.location AS gl_loc
            FROM gl_account_rules r
            LEFT JOIN gl_accounts g ON g.id = r.gl_account_id
            WHERE g.id IS NULL OR g.active = 0
               OR (g.location IS NOT NULL AND r.location IS NOT NULL
                   AND g.location <> r.location)
        """):
            bad.append(f"rule#{r['id']} {r['pattern']!r} -> {r['gl_account_id']} "
                       f"'{r['name']}'")
        assert not bad, f"{len(bad)} orphaned rules: " + "; ".join(bad[:10])

    def test_every_entity_has_a_usable_pl_chart(self, conn):
        """Chatham once had 44 active accounts, ALL balance-sheet — there was
        nothing to code an expense to. An entity with no active expense
        accounts cannot be coded at all, which is a silent dead end."""
        for (loc,) in conn.execute(
                "SELECT DISTINCT location FROM bank_accounts WHERE location IS NOT NULL"):
            n = conn.execute(
                "SELECT COUNT(*) FROM gl_accounts WHERE location = ? AND active = 1 "
                "AND account_type IN ('Expense','Cost of Goods Sold')", (loc,)
            ).fetchone()[0]
            assert n > 0, f"{loc} has no active expense/COGS accounts — nothing to code to"

    def test_replace_mode_cannot_deactivate_a_referenced_account(self, conn):
        """The guard is a SQL predicate in import_balance_sheet's replace pass.
        Assert the invariant it protects: every account referenced by a row or
        a rule is active."""
        refd = conn.execute("""
            SELECT DISTINCT gl_account_id FROM (
                SELECT gl_account_id FROM manual_bank_entries WHERE gl_account_id IS NOT NULL
                UNION SELECT gl_account_id FROM vendor_payments WHERE gl_account_id IS NOT NULL
                UNION SELECT gl_account_id FROM payroll_checks WHERE gl_account_id IS NOT NULL
                UNION SELECT gl_account_id FROM bank_deposits WHERE gl_account_id IS NOT NULL
                UNION SELECT gl_account_id FROM gl_account_rules WHERE gl_account_id IS NOT NULL
                UNION SELECT gl_account_id FROM gl_category_mapping WHERE gl_account_id IS NOT NULL
                UNION SELECT gl_account_id FROM qb_line_mapping WHERE gl_account_id IS NOT NULL)
        """).fetchall()
        inactive = [r[0] for r in refd if conn.execute(
            "SELECT active FROM gl_accounts WHERE id = ?", (r[0],)).fetchone()[0] == 0]
        assert not inactive, f"referenced but inactive accounts: {inactive}"


class TestEveryWritePathValidatesGl:
    """One validator, applied on every door that sets gl_account_id.

    The PUT endpoint checked active + entity. The IMPORT auto-coder did not,
    and a Dennis statement import coded 114 rows to Chatham accounts — Daily
    Sales:Doordash, Linens, Payroll Expenses among them. The rules were never
    at fault: every one was correctly scoped. The import called
    _find_gl_account_for_description() WITHOUT a location, which considers
    rules from both charts and returns whichever pattern is longest.

    A guard on one door is not a guard. These tests knock on each one.
    """

    def test_validator_rejects_the_three_failure_modes(self, conn):
        from routes.register_routes import (
            validate_gl_account, GL_PROBLEM_MISSING, GL_PROBLEM_INACTIVE,
            GL_PROBLEM_WRONG_ENTITY)
        assert validate_gl_account(conn, 99999999, "dennis")[1] == GL_PROBLEM_MISSING
        assert validate_gl_account(conn, None, "dennis")[1] == GL_PROBLEM_MISSING

        inactive = conn.execute(
            "SELECT id, location FROM gl_accounts WHERE active = 0 LIMIT 1").fetchone()
        if inactive:
            assert validate_gl_account(
                conn, inactive["id"], inactive["location"])[1] == GL_PROBLEM_INACTIVE

        chatham = conn.execute(
            "SELECT id FROM gl_accounts WHERE location = 'chatham' AND active = 1 "
            "LIMIT 1").fetchone()
        assert validate_gl_account(
            conn, chatham["id"], "dennis")[1] == GL_PROBLEM_WRONG_ENTITY
        assert validate_gl_account(conn, chatham["id"], "chatham")[1] is None

    def test_machine_wrapper_drops_an_invalid_coding(self, conn):
        """Automatic coders leave the row uncoded rather than write a coding a
        human would be refused. Uncoded is visible; wrong is not."""
        from routes.register_routes import resolve_gl_for_location
        chatham = conn.execute(
            "SELECT id FROM gl_accounts WHERE location = 'chatham' AND active = 1 "
            "LIMIT 1").fetchone()["id"]
        assert resolve_gl_for_location(conn, chatham, "dennis") is None
        assert resolve_gl_for_location(conn, chatham, "chatham") == chatham
        assert resolve_gl_for_location(conn, None, "dennis") is None

    def test_rule_lookup_is_scoped_to_the_entity(self, conn):
        """The actual bug. An unscoped lookup can return the other chart's
        account; a scoped one cannot."""
        from routes.register_routes import _find_gl_account_for_description
        rule = conn.execute("""
            SELECT r.pattern, r.location, g.location AS gl_loc
            FROM gl_account_rules r JOIN gl_accounts g ON g.id = r.gl_account_id
            WHERE r.location IS NOT NULL AND g.location IS NOT NULL LIMIT 1
        """).fetchone()
        if not rule:
            pytest.skip("no location-scoped rules present")
        other = "dennis" if rule["location"] == "chatham" else "chatham"
        got = _find_gl_account_for_description(conn, rule["pattern"], other)
        if got is not None:
            loc = conn.execute("SELECT location FROM gl_accounts WHERE id = ?",
                               (got,)).fetchone()["location"]
            assert loc in (other, None), \
                f"scoped lookup for {other} returned a {loc} account"

    def test_import_path_never_produces_a_cross_entity_coding(self, conn):
        """THE REGRESSION TEST. Drive the real import coder — the same function
        the import loop calls — with descriptions that match rules from BOTH
        charts, for each entity, and assert nothing invalid comes out."""
        from routes.bank_reconcile_routes import resolve_import_gl
        from routes.register_routes import validate_gl_account

        patterns = [r["pattern"] for r in conn.execute(
            "SELECT DISTINCT pattern FROM gl_account_rules "
            "WHERE pattern IS NOT NULL AND TRIM(pattern) <> ''")]
        assert patterns, "no rules to exercise"

        accounts = {r["location"]: r["account_last4"] for r in conn.execute(
            "SELECT location, account_last4 FROM bank_accounts "
            "WHERE location IS NOT NULL")}
        assert len(accounts) >= 2, "expected both entities to have a bank account"

        # Descriptions that exercise the classifiers too, not just the rules.
        extra = ["Transfer from x2757 to x5975 Loan repayment",
                 "Transfer from x2757 to x5087",
                 "PAYMENT VENMO", "PAYMENT VENMO 350",
                 "DBT CRD 1335 SOMETHING UNRECOGNISED"]

        checked = 0
        for location, last4 in accounts.items():
            for desc in patterns + extra:
                for signed in (-350.00, -1234.56, 987.65):
                    gl_id = resolve_import_gl(
                        conn, {"description": desc, "memo": ""},
                        signed, location, last4)
                    checked += 1
                    if gl_id is None:
                        continue
                    row, problem = validate_gl_account(conn, gl_id, location)
                    assert problem is None, (
                        f"import coder produced an invalid coding for a "
                        f"{location} row: {desc!r} -> #{gl_id} "
                        f"'{row['name'] if row else '?'}' ({problem})")
        assert checked > 50, f"only {checked} import decisions exercised"

    def test_backfill_refuses_to_cross_entities_when_unscoped(self, conn):
        """A location-specific account must never be sprayed across both
        entities by an unscoped backfill."""
        from routes.register_routes import _backfill_unassigned_for_pattern
        chatham = conn.execute(
            "SELECT id FROM gl_accounts WHERE location = 'chatham' AND active = 1 "
            "LIMIT 1").fetchone()["id"]
        n = _backfill_unassigned_for_pattern(
            conn, "ZZZ_NO_SUCH_PATTERN_ZZZ", chatham, None)
        assert n == 0, "unscoped backfill of a Chatham account was allowed"

    def test_backfill_refuses_a_cross_entity_target(self, conn):
        from routes.register_routes import _backfill_unassigned_for_pattern
        chatham = conn.execute(
            "SELECT id FROM gl_accounts WHERE location = 'chatham' AND active = 1 "
            "LIMIT 1").fetchone()["id"]
        n = _backfill_unassigned_for_pattern(
            conn, "ZZZ_NO_SUCH_PATTERN_ZZZ", chatham, "dennis")
        assert n == 0, "backfill wrote a Chatham account onto Dennis rows"

    def test_no_row_anywhere_is_cross_entity_or_inactive(self, conn):
        """The state assertion, across every register-source table."""
        bad = []
        for table in ("manual_bank_entries", "vendor_payments",
                      "payroll_checks", "bank_deposits"):
            for r in conn.execute(f"""
                SELECT t.id, g.name, g.active, g.location AS gl_loc,
                       ba.location AS row_loc
                FROM {table} t
                JOIN gl_accounts g ON g.id = t.gl_account_id
                LEFT JOIN bank_accounts ba ON ba.id = t.bank_account_id
                WHERE t.gl_account_id IS NOT NULL
                  AND (g.active = 0
                       OR (g.location IS NOT NULL AND ba.location IS NOT NULL
                           AND g.location <> ba.location))
            """):
                bad.append(f"{table}#{r['id']} -> '{r['name']}' "
                           f"({r['gl_loc']} on a {r['row_loc']} row, "
                           f"active={r['active']})")
        assert not bad, f"{len(bad)} invalid codings: " + "; ".join(bad[:10])


class TestPostImportAudit:
    """The invariants run automatically at import, not when someone remembers
    to run pytest.

    The guard that would have caught the 114 cross-entity codings already
    existed as an assertion in this file. Nothing ran it between the April
    import and someone noticing the wrong accounts on screen. An audit that
    waits for a human to invoke it is documentation, not a control.
    """

    def test_audit_reports_every_invariant(self, conn):
        from routes.register_routes import audit_register_invariants
        a = audit_register_invariants(conn)
        names = {c["name"] for c in a["checks"]}
        assert names == {"row_codings", "rules", "category_mappings",
                         "journal_mappings", "settlements"}
        for c in a["checks"]:
            assert set(c) >= {"name", "ok", "count", "summary", "detail"}

    def test_audit_is_currently_clean(self, conn):
        from routes.register_routes import audit_register_invariants
        a = audit_register_invariants(conn)
        assert a["ok"], "; ".join(
            f"{c['name']}={c['count']}" for c in a["checks"] if not c["ok"])

    def test_audit_detects_a_cross_entity_coding(self, conn):
        """The audit must FAIL on the exact defect it exists for — proving it
        passes is not proving it works. Injected and rolled back."""
        from routes.register_routes import audit_register_invariants
        row = conn.execute("""
            SELECT m.id, m.gl_account_id FROM manual_bank_entries m
            JOIN bank_accounts ba ON ba.id = m.bank_account_id
            WHERE ba.location = 'dennis' LIMIT 1
        """).fetchone()
        chatham = conn.execute(
            "SELECT id FROM gl_accounts WHERE location = 'chatham' AND active = 1 "
            "LIMIT 1").fetchone()["id"]
        try:
            conn.execute("UPDATE manual_bank_entries SET gl_account_id = ? WHERE id = ?",
                         (chatham, row["id"]))
            a = audit_register_invariants(conn)
            assert not a["ok"], "audit passed a Dennis row coded to a Chatham account"
            failed = [c for c in a["checks"] if not c["ok"]]
            assert [c["name"] for c in failed] == ["row_codings"]
            assert failed[0]["detail"][0]["problem"] == "wrong_entity"
            assert failed[0]["detail"][0]["row_location"] == "dennis"
        finally:
            conn.execute("UPDATE manual_bank_entries SET gl_account_id = ? WHERE id = ?",
                         (row["gl_account_id"], row["id"]))
            conn.commit()
        assert audit_register_invariants(conn)["ok"], "cleanup failed to restore"

    def test_audit_detects_an_inactive_coding(self, conn):
        from routes.register_routes import audit_register_invariants
        row = conn.execute(
            "SELECT id, gl_account_id FROM manual_bank_entries LIMIT 1").fetchone()
        dead = conn.execute(
            "SELECT id FROM gl_accounts WHERE active = 0 LIMIT 1").fetchone()
        if not dead:
            pytest.skip("no inactive accounts to test with")
        try:
            conn.execute("UPDATE manual_bank_entries SET gl_account_id = ? WHERE id = ?",
                         (dead["id"], row["id"]))
            a = audit_register_invariants(conn)
            assert not a["ok"]
            problems = {d["problem"] for c in a["checks"] if not c["ok"]
                        for d in c["detail"]}
            assert "inactive" in problems
        finally:
            conn.execute("UPDATE manual_bank_entries SET gl_account_id = ? WHERE id = ?",
                         (row["gl_account_id"], row["id"]))
            conn.commit()

    def test_location_scope_narrows_the_row_check(self, conn):
        from routes.register_routes import audit_register_invariants
        for loc in ("chatham", "dennis"):
            a = audit_register_invariants(conn, location=loc)
            assert a["location"] == loc
            for d in next(c for c in a["checks"] if c["name"] == "row_codings")["detail"]:
                assert d["row_location"] == loc

    def test_import_response_carries_the_audit(self, client, conn):
        """The hook, end to end: the audit result rides back in the same
        response as the import summary, where the parse summary shows.

        Imports zero rows against an existing upload — the audit runs
        regardless, which is the point being tested.
        """
        upload = conn.execute(
            "SELECT id FROM bank_statement_uploads ORDER BY id DESC LIMIT 1").fetchone()
        if not upload:
            pytest.skip("no statement uploads present")
        r = client.post("/api/bank-reconcile/import",
                        json={"upload_id": upload["id"], "indexes": [],
                              "also_clear_matches": False})
        assert r.status_code == 200, r.get_data(as_text=True)
        j = r.get_json()
        assert j["inserted"] == 0
        assert "audit" in j, "import response does not carry the audit"
        assert j["audit"] is not None
        assert "checks" in j["audit"] and j["audit"]["checks"]
        assert j["audit"]["ok"] is True, (
            "post-import audit failed: "
            + "; ".join(f"{c['name']}={c['count']}"
                        for c in j["audit"]["checks"] if not c["ok"]))


class TestVenmoIsNeverTips:
    """Standing rule (Mike, 2026-08-25): VENMO IS NEVER TIPS.

    He pays bands and the trivia host through Venmo, exclusively, at both
    locations. Treating the channel as a tip payout swept $6,050 of March
    entertainment spend into "tip disbursements", which then netted straight
    out of labor.

    Venmo earns a deterministic classifier precisely BECAUSE the channel has
    one purpose. That is not a general licence to rule payment channels —
    PayPal carries mixed traffic and stays unruled.
    """

    def test_venmo_outflow_is_band_pay(self):
        from routes.register_routes import classify_venmo
        name, reason = classify_venmo("PAYMENT VENMO", -400.00)
        assert name == "Bands" and reason

    def test_exactly_350_is_the_trivia_host(self):
        from routes.register_routes import classify_venmo, TRIVIA_RATE
        assert TRIVIA_RATE == 350.00
        name, _ = classify_venmo("PAYMENT VENMO", -TRIVIA_RATE)
        assert name == "Trivia"
        # Near misses are band pay, not trivia — the rate is exact.
        assert classify_venmo("PAYMENT VENMO", -349.99)[0] == "Bands"
        assert classify_venmo("PAYMENT VENMO", -351.00)[0] == "Bands"

    def test_venmo_inflow_is_not_classified(self):
        """Money coming IN over Venmo has no settled treatment."""
        assert classify_venmo_name("PAYMENT VENMO", 400.00) is None

    def test_non_venmo_is_ignored_entirely(self):
        from routes.register_routes import classify_venmo
        assert classify_venmo("PAYMENT PAYPAL", -400.00) == (None, None)
        assert classify_venmo("DBT CRD 1335 SYSCO", -400.00) == (None, None)

    def test_paypal_the_channel_stays_unruled(self, conn):
        """PayPal carries mixed traffic, so no rule may claim the CHANNEL.

        A bare 'PAYPAL' -> Travel rule existed (id 404, from rebuild-030) and
        was deleted on 2026-08-25: Dennis PayPal rows are Small Equipment and
        Dues & Subscriptions, not travel, so the channel rule was wrong on its
        own data. A rule naming a specific merchant WITHIN the channel —
        'PAYPAL UBER' — is fine, because that is a merchant, not a channel.
        """
        bare = conn.execute("""
            SELECT id, location, pattern FROM gl_account_rules
            WHERE TRIM(UPPER(pattern)) IN ('PAYPAL', 'PAYPAL ', 'PPAL')
        """).fetchall()
        assert not bare, ("PayPal the channel must stay unruled, found: "
                          + "; ".join(f"#{r['id']} {r['location']} {r['pattern']!r}"
                                      for r in bare))

    def test_no_venmo_row_sits_on_a_tip_or_payroll_account(self, conn):
        """The history sweep, asserted. Any Venmo row on Tip Wages or Payroll
        Expenses is miscoded by definition."""
        bad = conn.execute("""
            SELECT m.id, ba.location, g.name, m.gl_status
            FROM manual_bank_entries m
            JOIN bank_accounts ba ON ba.id = m.bank_account_id
            JOIN gl_accounts g ON g.id = m.gl_account_id
            WHERE UPPER(COALESCE(m.payee,'') || ' ' || COALESCE(m.memo,'')) LIKE '%VENMO%'
              AND g.name IN ('Tip Wages', 'Payroll Expenses')
        """).fetchall()
        assert not bad, ("Venmo rows still coded as tips/payroll: "
                         + "; ".join(f"#{r['id']} {r['location']} {r['name']}" for r in bad))

    def test_venmo_codings_are_suggested_not_confirmed(self, conn):
        """Machine codings from the classifier must stay suggested so an
        oddball amount surfaces for review instead of becoming band pay."""
        bad = conn.execute("""
            SELECT m.id FROM manual_bank_entries m
            JOIN gl_accounts g ON g.id = m.gl_account_id
            WHERE UPPER(COALESCE(m.payee,'') || ' ' || COALESCE(m.memo,'')) LIKE '%VENMO%'
              AND g.name IN ('Bands', 'Trivia')
              AND m.gl_source = 'rule' AND m.gl_status = 'confirmed'
        """).fetchall()
        assert not bad, f"machine-coded Venmo rows marked confirmed: {[r[0] for r in bad]}"


def classify_venmo_name(desc, amount):
    from routes.register_routes import classify_venmo
    return classify_venmo(desc, amount)[0]


class TestProfitLossSnapshot:
    """EXACT figures, frozen. These prove the ENGINE is deterministic.

    They run against tests/fixtures/pl_snapshot.json, not the live database.
    Live figures move whenever a bank row is coded in the register — that is
    ordinary bookkeeping, not a regression, and pinning it made this suite go
    red on Mike's normal work. If one of these fails, the ENGINE changed:
    decide whether that was intended BEFORE regenerating the fixture with
    tests/fixtures/regenerate_pl_snapshot.py.
    """

    @pytest.fixture(scope="class")
    def snap(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "pl_snapshot.json")
        if not os.path.exists(path):
            pytest.skip("pl_snapshot.json not generated")
        with open(path) as fh:
            return json.load(fh)

    @pytest.fixture(scope="class")
    def dennis(self, snap):
        return snap["dennis_2026-03"]["pl"]

    def test_dennis_march_revenue(self, dennis):
        assert dennis["guardrails"]["journal_control"]["entries"] == 22
        assert cents(dennis["guardrails"]["journal_control"]["debits"]) == cents(127757.91)
        assert cents(dennis["revenue"]["gross_sales"]) == cents(104616.00)
        assert cents(dennis["revenue"]["contra"]) == cents(-7912.28)
        assert cents(dennis["revenue"]["net_revenue"]) == cents(96703.72)

    def test_dennis_march_cogs_and_food_cost(self, dennis):
        assert cents(dennis["cogs"]["fnb_subtotal"]) == cents(29765.93)
        assert cents(dennis["cogs"]["non_fnb_subtotal"]) == cents(706.35)
        assert cents(dennis["cogs"]["total"]) == cents(30472.28)
        assert dennis["cogs"]["food_cost_pct"] == 30.78

    def test_dennis_march_labor_and_prime(self, dennis):
        lab = dennis["labor"]
        assert cents(lab["total"] - lab["tip_disbursements"]) == cents(lab["labor_cost"])
        assert cents(dennis["cogs"]["total"] + lab["labor_cost"]) == cents(dennis["prime_cost"])

    def test_venmo_is_not_counted_as_a_tip_channel(self, snap):
        """The standing rule, asserted on frozen data. Venmo is band and
        trivia-host pay at both locations; treating the channel as a tip
        channel swept $6,050 of March entertainment out of labor."""
        for key in snap["_meta"]["periods"]:
            lab = snap[key]["pl"]["labor"]
            for channel in lab.get("tip_channels", {}):
                assert "VENMO" not in channel.upper(), \
                    f"{key}: Venmo counted as a tip channel"
            for note in snap[key]["pl"]["footnotes"]:
                if "TIP PAYOUTS" in note:
                    assert "Venmo" not in note, \
                        f"{key}: footnote still claims Venmo is a tip payout"

    def test_every_snapshot_drill_sums_to_its_line(self, snap):
        """The drill contract, on frozen data: what a line opens must add up
        to the line."""
        for key in snap["_meta"]["periods"]:
            entry = snap[key]
            pl, drills = entry["pl"], entry["drills"]
            checked = 0
            groups = ([("revenue", pl["revenue"]["lines"]),
                       ("cogs", pl["cogs"]["lines"]),
                       ("opex_invoiced", pl["operating_expenses"]["invoiced"]),
                       ("opex_banked", pl["operating_expenses"]["banked"]),
                       ("labor", pl["labor"].get("accounts", []))])
            for _name, lines in groups:
                for line in lines:
                    d = line["drill"]
                    got = drills[f"{d['source']}:{d['key']}"]
                    assert cents(got["total"]) == cents(line["amount"]), (
                        f"{key} {d['source']}:{d['key']} — line "
                        f"{line['amount']} vs drill {got['total']}")
                    checked += 1
            assert checked > 0, f"{key}: no drillable lines in the snapshot"

    def test_snapshot_is_not_silently_empty(self, snap):
        assert snap["_meta"]["periods"], "snapshot has no periods"
        for key in snap["_meta"]["periods"]:
            assert snap[key]["pl"]["has_sales_journal"] is True
            assert snap[key]["drills"], f"{key}: no drills captured"


class TestProfitLossInvariants:
    """INVARIANTS against the LIVE database. No exact figures here.

    These must hold no matter how far Mike has got through coding the
    register. A failure means the books broke, not that the numbers moved.
    """

    LOC, START, END = "dennis", "2026-03-01", "2026-03-31"

    @pytest.fixture(scope="class")
    def pl(self, conn):
        from reports.profit_loss import build_profit_loss
        return build_profit_loss(self.LOC, self.START, self.END, conn=conn)

    def test_journal_entries_balance(self, pl):
        c = pl["guardrails"]["journal_control"]
        assert c["entries"] > 0
        assert c["balanced"], f"JEs out of balance: {c['debits']} vs {c['credits']}"

    def test_guardrail_1_no_clearing_account_reaches_revenue(self, pl):
        assert pl["guardrails"]["clearing_excluded"], \
            f"clearing leak: {pl['guardrails']['clearing_violations']}"

    def test_revenue_lines_are_income_only(self, conn, pl):
        for line in pl["revenue"]["lines"]:
            t = conn.execute("SELECT account_type FROM gl_accounts WHERE id = ?",
                             (line["gl_account_id"],)).fetchone()[0]
            assert t in ("Income", "Other Income"), \
                f"{line['name']} is {t}, not income"

    def test_tips_never_reach_revenue(self, pl):
        for line in pl["revenue"]["lines"]:
            assert "tip" not in line["name"].lower(), \
                f"a tip account reached revenue: {line['name']}"

    def test_food_cost_percent_stays_in_a_sane_band(self, pl):
        """Outside 25-40% means a mapping or double-count problem, not a
        restaurant problem."""
        fc = pl["cogs"]["food_cost_pct"]
        assert fc is not None and 25.0 <= fc <= 40.0, \
            f"food cost {fc}% is outside the sane band"

    def test_cogs_splits_into_its_subtotals(self, pl):
        assert cents(pl["cogs"]["fnb_subtotal"] + pl["cogs"]["non_fnb_subtotal"]) \
            == cents(pl["cogs"]["total"])

    def test_takeout_supplies_stays_out_of_food_cost(self, pl):
        names = {l["name"] for l in pl["cogs"]["lines"]
                 if l["category"] == "TOGO_SUPPLIES"}
        assert names <= {"TakeOut Supplies"}

    def test_cogs_lines_are_not_fragmented(self, pl):
        seen = [(l["category"], l["name"]) for l in pl["cogs"]["lines"]]
        assert len(seen) == len(set(seen)), f"duplicate COGS lines: {seen}"

    def test_labor_is_not_double_counted(self, pl):
        from reports.profit_loss import LABOR_ACCOUNT_NAMES
        leaked = [x["name"] for x in pl["operating_expenses"]["banked"]
                  if x["name"] in LABOR_ACCOUNT_NAMES]
        assert not leaked, f"labor accounts leaked into opex: {leaked}"

    def test_labor_nets_out_tip_disbursements(self, pl):
        lab = pl["labor"]
        assert cents(lab["total"] - lab["tip_disbursements"]) == cents(lab["labor_cost"])

    def test_venmo_is_never_a_tip_channel(self, pl):
        """Standing rule: Mike pays bands and the trivia host by Venmo,
        exclusively. It is never a tip payout."""
        for channel in pl["labor"].get("tip_channels", {}):
            assert "VENMO" not in channel.upper()

    def test_prime_cost_is_cogs_plus_labor(self, pl):
        assert cents(pl["cogs"]["total"] + pl["labor"]["labor_cost"]) \
            == cents(pl["prime_cost"])
        assert 45.0 <= pl["prime_cost_pct"] <= 75.0, \
            f"prime cost {pl['prime_cost_pct']}% is outside any plausible band"

    def test_net_income_is_the_sum_of_its_parts(self, pl):
        if pl["net_income"] is None:
            pytest.skip("net income withheld for this period")
        expected = (pl["revenue"]["net_revenue"] - pl["cogs"]["total"]
                    - pl["labor"]["labor_cost"] - pl["operating_expenses"]["total"])
        assert cents(pl["net_income"]) == cents(expected)

    def test_guardrail_3_inherited_defects_are_printed(self, pl):
        blob = " ".join(pl["footnotes"]).upper()
        for required in ("TIPS", "TOAST FEES", "INVENTORY ADJUSTMENT", "LABOR SOURCE"):
            assert required in blob, f"missing footnote: {required}"

    def test_net_income_is_withheld_when_expenses_are_missing(self, conn):
        from reports.profit_loss import build_profit_loss
        pl = build_profit_loss("chatham", "2026-03-01", "2026-03-31", conn=conn)
        if pl["guardrails"]["expense_coverage"]["expense_side_complete"]:
            pytest.skip("Chatham March statement has since been imported")
        assert pl["net_income"] is None
        assert pl["net_income_withheld_reason"]
        assert pl["revenue"]["net_revenue"] > 0

    def test_settlements_do_not_appear_as_expense(self, conn):
        from routes.register_routes import audit_settlement_codings
        assert not audit_settlement_codings(conn)


class TestDrillDownSumsToItsLine:
    """The drill contract on the LIVE database: the number on a line must
    equal the sum of the rows the line opens.

    One test per SOURCE TYPE, as asked: journal entry lines behind revenue,
    invoice line items behind COGS, and bank rows behind a banked expense.
    """

    LOC, START, END = "dennis", "2026-03-01", "2026-03-31"

    @pytest.fixture(scope="class")
    def pl(self, conn):
        from reports.profit_loss import build_profit_loss
        return build_profit_loss(self.LOC, self.START, self.END, conn=conn)

    def _check(self, conn, line):
        from reports.profit_loss import drill
        d = line["drill"]
        got = drill(conn, self.LOC, self.START, self.END, d["source"], d["key"])
        assert cents(got["total"]) == cents(line["amount"]), (
            f"{d['source']}:{d['key']} — line {line['amount']} "
            f"vs drill {got['total']} over {got['count']} rows")
        return got

    def test_journal_entry_lines_sum_to_a_revenue_line(self, conn, pl):
        lines = pl["revenue"]["lines"]
        assert lines, "no revenue lines to drill"
        got = self._check(conn, lines[0])
        assert got["count"] > 0 and "detail" in got["columns"]

    def test_invoice_items_sum_to_a_cogs_line(self, conn, pl):
        lines = pl["cogs"]["lines"]
        assert lines, "no COGS lines to drill"
        got = self._check(conn, lines[0])
        assert got["count"] > 0 and "vendor" in got["columns"]

    def test_bank_rows_sum_to_a_banked_expense_line(self, conn, pl):
        lines = pl["operating_expenses"]["banked"]
        if not lines:
            pytest.skip("no banked expense lines in this period")
        got = self._check(conn, lines[0])
        assert got["count"] > 0 and "status" in got["columns"]

    def test_every_line_on_the_statement_drills_correctly(self, conn, pl):
        """Not just one per type — all of them."""
        checked = 0
        for group in (pl["revenue"]["lines"], pl["cogs"]["lines"],
                      pl["operating_expenses"]["invoiced"],
                      pl["operating_expenses"]["banked"],
                      pl["labor"].get("accounts", [])):
            for line in group:
                self._check(conn, line)
                checked += 1
        assert checked >= 10, f"only {checked} lines drilled"

    def test_tip_adjustment_drills_to_its_rows(self, conn, pl):
        from reports.profit_loss import drill
        if not pl["labor"]["tip_disbursements"]:
            pytest.skip("no tip disbursements in this period")
        d = pl["labor"]["tip_drill"]
        got = drill(conn, self.LOC, self.START, self.END, d["source"], d["key"])
        assert cents(got["total"]) == cents(pl["labor"]["tip_disbursements"])


class TestProfitLossEndpoint:
    """The API door onto the P&L engine, and the page that consumes it."""

    URL = "/api/reports/profit-loss"

    def test_returns_the_full_report(self, client):
        r = client.get(f"{self.URL}?location=dennis&start=2026-03-01&end=2026-03-31")
        assert r.status_code == 200
        j = r.get_json()
        assert j["location"] == "dennis" and j["location_label"] == "Dennis Port"
        assert j["period"] == {"start": "2026-03-01", "end": "2026-03-31"}
        assert j["has_sales_journal"] is True
        assert j["guardrails"]["journal_control"]["entries"] == 22
        assert cents(j["revenue"]["net_revenue"]) == cents(96703.72)
        assert j["footnotes"]

    def test_payload_carries_every_field_the_page_renders(self, client):
        """The page reads these by name. A rename in the engine that this
        misses shows up as a blank section rather than an error, so assert the
        contract explicitly."""
        j = client.get(f"{self.URL}?location=dennis"
                       "&start=2026-03-01&end=2026-03-31").get_json()
        for k in ("has_sales_journal", "location_label", "period", "revenue",
                  "cogs", "labor", "prime_cost", "prime_cost_pct",
                  "operating_expenses", "net_income",
                  "net_income_withheld_reason", "footnotes", "guardrails"):
            assert k in j, f"missing top-level key: {k}"
        for k in ("clearing_excluded", "clearing_violations", "plug",
                  "journal_control", "expense_coverage"):
            assert k in j["guardrails"], f"missing guardrail: {k}"
        assert {"lines", "net_revenue", "gross_sales", "contra"} <= set(j["revenue"])
        assert {"lines", "fnb_subtotal", "non_fnb_subtotal", "total",
                "food_cost_pct", "total_cogs_pct"} <= set(j["cogs"])
        assert {"by_account", "labor_cost", "labor_pct", "tip_disbursements",
                "total"} <= set(j["labor"])
        assert {"invoiced", "banked", "total"} <= set(j["operating_expenses"])
        for line in j["cogs"]["lines"]:
            assert {"category", "name", "amount", "confidence"} <= set(line)
        for line in j["operating_expenses"]["banked"]:
            assert {"name", "amount"} <= set(line)
        assert {"threshold", "count", "banner"} <= set(j["guardrails"]["plug"])

    def test_month_with_no_journal_entries_says_so(self, client):
        """Rendering zeros would look like a measured zero."""
        j = client.get(f"{self.URL}?location=dennis"
                       "&start=2024-01-01&end=2024-01-31").get_json()
        assert j["has_sales_journal"] is False
        assert j["guardrails"]["journal_control"]["entries"] == 0

    def test_withheld_net_income_carries_its_reason(self, client, conn):
        j = client.get(f"{self.URL}?location=chatham"
                       "&start=2026-03-01&end=2026-03-31").get_json()
        if j["net_income"] is not None:
            pytest.skip("Chatham March statement has since been imported")
        assert j["net_income_withheld_reason"], \
            "a withheld bottom line must explain itself"

    def test_labor_is_unknown_not_zero_when_uncoded(self, client):
        """Chatham March has no labor rows. Reporting 0.00 would read as a
        measured figure, and prime cost built on it is COGS wearing a
        prime-cost label — it showed 39.65% that way."""
        j = client.get(f"{self.URL}?location=chatham"
                       "&start=2026-03-01&end=2026-03-31").get_json()
        if j["guardrails"]["expense_coverage"]["labor_rows"]:
            pytest.skip("Chatham March labor has since been coded")
        assert j["labor"]["available"] is False
        assert j["labor"]["labor_cost"] is None
        assert j["labor"]["labor_pct"] is None
        assert j["prime_cost"] is None
        assert j["prime_cost_pct"] is None

    def test_labor_is_present_when_coded(self, client):
        j = client.get(f"{self.URL}?location=dennis"
                       "&start=2026-03-01&end=2026-03-31").get_json()
        assert j["labor"]["available"] is True
        assert j["labor"]["labor_cost"] is not None
        assert j["prime_cost"] is not None

    def test_rejects_a_bad_location(self, client):
        r = client.get(f"{self.URL}?location=bogus")
        assert r.status_code == 400
        assert "chatham" in r.get_json()["error"]

    def test_rejects_malformed_and_inverted_dates(self, client):
        assert client.get(f"{self.URL}?location=dennis&start=nope").status_code == 400
        r = client.get(f"{self.URL}?location=dennis&start=2026-05-01&end=2026-04-01")
        assert r.status_code == 400

    def test_defaults_to_the_current_month(self, client):
        r = client.get(self.URL)
        assert r.status_code == 200
        assert r.get_json()["location"] == "dennis"

    def test_requires_auth(self):
        """Same posture as the other register routes — no anonymous access."""
        from web.server import app
        app.config["TESTING"] = True
        with app.test_client() as anon:
            r = anon.get(f"{self.URL}?location=dennis")
        assert r.status_code in (301, 302, 401, 403), \
            f"unauthenticated request returned {r.status_code}"

    def test_drill_endpoint_returns_rows_that_sum_to_the_line(self, client):
        pl = client.get(f"{self.URL}?location=dennis"
                        "&start=2026-03-01&end=2026-03-31").get_json()
        line = pl["cogs"]["lines"][0]
        d = line["drill"]
        r = client.get(f"{self.URL}/drill?location=dennis"
                       f"&start=2026-03-01&end=2026-03-31"
                       f"&source={d['source']}&key={d['key']}")
        assert r.status_code == 200
        j = r.get_json()
        assert cents(j["total"]) == cents(line["amount"])
        assert j["count"] == len(j["rows"]) and j["rows"]
        assert set(j["columns"]) <= set(j["rows"][0].keys())

    def test_every_line_carries_a_drill_descriptor(self, client):
        pl = client.get(f"{self.URL}?location=dennis"
                        "&start=2026-03-01&end=2026-03-31").get_json()
        for group in (pl["revenue"]["lines"], pl["cogs"]["lines"],
                      pl["operating_expenses"]["invoiced"],
                      pl["operating_expenses"]["banked"],
                      pl["labor"].get("accounts", [])):
            for line in group:
                assert "drill" in line, f"line without a drill descriptor: {line}"
                assert {"source", "key"} == set(line["drill"])

    def test_drill_rejects_an_unknown_source(self, client):
        r = client.get(f"{self.URL}/drill?location=dennis&source=nonsense&key=1")
        assert r.status_code == 400

    def test_drill_requires_a_key(self, client):
        r = client.get(f"{self.URL}/drill?location=dennis&source=cogs")
        assert r.status_code == 400

    def test_drill_requires_auth(self):
        from web.server import app
        app.config["TESTING"] = True
        with app.test_client() as anon:
            r = anon.get(f"{self.URL}/drill?location=dennis&source=cogs&key=FOOD")
        assert r.status_code in (301, 302, 401, 403)

    def test_page_is_served_and_linked_in_the_sidebar(self, client):
        assert client.get("/profit-loss").status_code == 200
        sidebar = client.get("/static/sidebar.js").get_data(as_text=True)
        assert "/profit-loss" in sidebar, "P&L is not linked in the Accounting menu"


class TestSettlementIsNotAnExpense:
    """Accrual invariant: a row that settles an invoice may not carry a P&L
    account.

    These books recognise cost at INVOICE date from the invoice's own line
    items. The bank transaction that pays it is an AP settlement:

        invoice confirmed :  Dr Food COGS         Cr Accounts Payable
        payment clears    :  Dr Accounts Payable  Cr Bank

    Coding the payment to Food COGS as well books the cost twice. That was
    live, not theoretical: 9 of the 12 coded vendor_payments were on a P&L
    account while linked to confirmed invoices, and every one of them was a
    double count of real money.

    Bank rows with NO invoice behind them — autopay utilities, card charges,
    bank fees — are the legitimate P&L source and are untouched.
    """

    PL_TYPES = {"Income", "Other Income", "Expense", "Other Expense",
                "Cost of Goods Sold"}

    def test_no_settlement_is_coded_to_a_pl_account(self, conn):
        from routes.register_routes import audit_settlement_codings
        bad = audit_settlement_codings(conn)
        assert not bad, (
            f"{len(bad)} settlements double-counted as expense: "
            + "; ".join(f"{b['table']}#{b['id']} -> {b['gl_name']} ({b['evidence']})"
                        for b in bad[:10]))

    def test_both_entities_have_an_ap_account(self, conn):
        """Without one there is nowhere for a settlement to go, so the coder
        would fall back to an expense account and silently double count."""
        from routes.register_routes import _resolve_ap_account
        for loc in ("chatham", "dennis"):
            assert _resolve_ap_account(conn, loc), \
                f"{loc} has no active Accounts Payable account"

    def test_vendor_payment_with_invoices_is_detected(self, conn):
        """The detector must actually fire on real data, or the guard is
        decorative."""
        from routes.register_routes import settlement_evidence
        row = conn.execute("""
            SELECT vp.id FROM vendor_payments vp
            JOIN ap_payment_invoices api ON api.payment_id = vp.ap_payment_id
            LIMIT 1
        """).fetchone()
        if not row:
            pytest.skip("no invoice-linked vendor payments present")
        ev = settlement_evidence(conn, "vendor_payments", row["id"])
        assert ev and ev["kind"] == "direct" and ev["invoices"] > 0

    def test_payment_without_invoices_is_not_a_settlement(self, conn):
        """The converse matters just as much: a bank row with no invoice must
        stay freely codeable, or every utility bill becomes unclassifiable."""
        from routes.register_routes import settlement_evidence
        row = conn.execute("""
            SELECT vp.id FROM vendor_payments vp
            WHERE NOT EXISTS (SELECT 1 FROM ap_payment_invoices api
                              WHERE api.payment_id = vp.ap_payment_id)
              AND NOT EXISTS (SELECT 1 FROM vendor_payment_invoices vpi
                              WHERE vpi.payment_id = vp.id)
            LIMIT 1
        """).fetchone()
        if not row:
            pytest.skip("every vendor payment has invoices attached")
        assert settlement_evidence(conn, "vendor_payments", row["id"]) is None

    def test_api_refuses_a_pl_account_on_a_settlement(self, client, conn):
        """End-to-end: the endpoint returns 409 and names the AP account."""
        row = conn.execute("""
            SELECT vp.id, vp.location FROM vendor_payments vp
            JOIN ap_payment_invoices api ON api.payment_id = vp.ap_payment_id
            WHERE vp.location IS NOT NULL LIMIT 1
        """).fetchone()
        if not row:
            pytest.skip("no invoice-linked vendor payments present")
        cogs = conn.execute(
            "SELECT id FROM gl_accounts WHERE location = ? AND active = 1 "
            "AND account_type = 'Cost of Goods Sold' LIMIT 1", (row["location"],)
        ).fetchone()
        assert cogs, f"no COGS account for {row['location']}"

        before = conn.execute("SELECT gl_account_id FROM vendor_payments WHERE id = ?",
                              (row["id"],)).fetchone()[0]
        r = client.put("/api/register/row/gl-account", json={
            "source": "bill_pay", "id": row["id"],
            "gl_account_id": cogs["id"], "create_rule": False})
        assert r.status_code == 409, f"expected refusal, got {r.status_code}"
        body = r.get_json()
        assert body.get("reason") == "settlement_not_expense"
        assert body.get("suggested_gl_account_id"), "refusal must name the AP account"

        after = conn.execute("SELECT gl_account_id FROM vendor_payments WHERE id = ?",
                             (row["id"],)).fetchone()[0]
        assert after == before, "a refused coding must not have been written"

    def test_ap_account_is_still_accepted_on_a_settlement(self, client, conn):
        """The guard blocks P&L accounts, not all coding. Re-applying the AP
        account a settlement already carries must succeed."""
        from routes.register_routes import _resolve_ap_account
        row = conn.execute("""
            SELECT vp.id, vp.location, vp.gl_account_id FROM vendor_payments vp
            JOIN ap_payment_invoices api ON api.payment_id = vp.ap_payment_id
            WHERE vp.location IS NOT NULL AND vp.gl_account_id IS NOT NULL LIMIT 1
        """).fetchone()
        if not row:
            pytest.skip("no coded invoice-linked vendor payments present")
        ap = _resolve_ap_account(conn, row["location"])
        r = client.put("/api/register/row/gl-account", json={
            "source": "bill_pay", "id": row["id"],
            "gl_account_id": ap, "create_rule": False})
        assert r.status_code == 200, f"AP coding refused: {r.get_json()}"


class TestSalesJournalMapping:
    """qb_line_mapping resolves into gl_accounts, per entity, by id.

    It used to key on a raw QBO account id — a second account namespace — and
    for Chatham those ids were copied from Dennis, where the same numbers name
    different accounts. 16 of 20 lines resolved SILENTLY to the wrong account
    and 4 to retired ones. The worst was structural, not cosmetic: every credit
    card tender pointed at "Food Sales", an Income account, so a clearing debit
    would have landed in revenue and double-counted it.

    Revenue on the P&L is built from these journal entries, so this mapping is
    load-bearing for every income number we report.
    """

    # Lines whose account must be BALANCE SHEET. A tender is money moving
    # between clearing accounts; it is not income, and this is the assertion
    # that would have caught the Chatham breakage on day one.
    CLEARING_LINES = [
        "Tenders: Cash", "Tenders: Credit", "Tenders: Visa",
        "Tenders: Mastercard", "Tenders: Amex", "Tenders: Discover",
        "Tenders: Other", "Tenders: Gift Card",
        "Summary: Tax", "Summary: Tips", "Summary: Gift Card Sold",
    ]
    BALANCE_SHEET_TYPES = {
        "Bank", "Other Current Asset", "Other Current Liability",
        "Accounts Receivable", "Accounts Payable", "Credit Card",
        "Fixed Asset", "Other Asset", "Long Term Liability", "Equity",
    }
    REVENUE_LINES = [
        "Gross Sales: Food", "Gross Sales: Beer", "Gross Sales: Wine",
        "Gross Sales: Liquor", "Gross Sales: NA Beverage", "Discounts: Total",
    ]

    def test_every_line_resolves_onto_the_spine(self, conn):
        """All 40 lines, both entities, no exceptions. Chatham's
        Tenders: House Account was the last gap — closed 2026-08-23 by
        reactivating House Accounts Receivable (343), the same call as 413:
        the account was right, it was merely switched off."""
        unmapped = [(r["location"], r["journal_name"]) for r in conn.execute(
            "SELECT location, journal_name FROM qb_line_mapping WHERE gl_account_id IS NULL")]
        assert not unmapped, f"unmapped journal lines: {sorted(unmapped)}"

    def test_no_mapping_points_at_an_invalid_account(self, conn):
        from routes.register_routes import audit_qb_line_mapping
        bad = audit_qb_line_mapping(conn)
        assert not bad, (
            f"{len(bad)} invalid journal-line mappings: "
            + "; ".join(f"{b['location']}/{b['journal_name']} ({b['problem']})" for b in bad[:10]))

    def test_tenders_and_summaries_are_balance_sheet_not_income(self, conn):
        """The Chatham regression, asserted directly. A tender debit on an
        Income account inflates revenue by the full amount tendered."""
        bad = []
        for loc in ("chatham", "dennis"):
            for jname in self.CLEARING_LINES:
                r = conn.execute("""
                    SELECT g.name, g.account_type FROM qb_line_mapping m
                    JOIN gl_accounts g ON g.id = m.gl_account_id
                    WHERE m.location = ? AND m.journal_name = ?
                """, (loc, jname)).fetchone()
                if not r:
                    continue  # unmapped is covered by its own test
                if r["account_type"] not in self.BALANCE_SHEET_TYPES:
                    bad.append(f"{loc}/{jname} -> {r['name']} [{r['account_type']}]")
        assert not bad, "clearing lines mapped to non-balance-sheet accounts: " + "; ".join(bad)

    def test_gross_sales_lines_are_income(self, conn):
        bad = []
        for loc in ("chatham", "dennis"):
            for jname in self.REVENUE_LINES:
                r = conn.execute("""
                    SELECT g.name, g.account_type FROM qb_line_mapping m
                    JOIN gl_accounts g ON g.id = m.gl_account_id
                    WHERE m.location = ? AND m.journal_name = ?
                """, (loc, jname)).fetchone()
                assert r, f"{loc}/{jname} is unmapped"
                if r["account_type"] != "Income":
                    bad.append(f"{loc}/{jname} -> {r['name']} [{r['account_type']}]")
        assert not bad, "revenue lines not mapped to Income: " + "; ".join(bad)

    def test_entities_never_share_a_gl_account(self, conn):
        shared = conn.execute("""
            SELECT gl_account_id, COUNT(DISTINCT location) n FROM qb_line_mapping
            WHERE gl_account_id IS NOT NULL GROUP BY gl_account_id HAVING n > 1
        """).fetchall()
        assert not shared, f"journal mappings shared across entities: {[dict(s) for s in shared]}"

    def test_each_line_maps_to_a_distinct_role_not_a_catch_all(self, conn):
        """Chatham had Discounts and Tenders: Other both on qbo 52, and
        Non-Grat and Summary: Other both on 212. Collapsing distinct roles onto
        one account is how a plug hides."""
        for loc in ("chatham", "dennis"):
            for a, b in [("Discounts: Total", "Tenders: Other"),
                         ("Summary: Non-Grat", "Summary: Other")]:
                ra = conn.execute("SELECT gl_account_id FROM qb_line_mapping "
                                  "WHERE location=? AND journal_name=?", (loc, a)).fetchone()
                rb = conn.execute("SELECT gl_account_id FROM qb_line_mapping "
                                  "WHERE location=? AND journal_name=?", (loc, b)).fetchone()
                if ra and rb and ra[0] and rb[0]:
                    assert ra[0] != rb[0], f"{loc}: {a!r} and {b!r} share account {ra[0]}"

    def test_journal_entries_still_balance(self, conn):
        """Remapping changes which account a line hits, never its amount.
        Dennis March is the acceptance-test month."""
        r = conn.execute("""
            SELECT ROUND(SUM(COALESCE(li.debit, 0)), 2)  AS dr,
                   ROUND(SUM(COALESCE(li.credit, 0)), 2) AS cr,
                   COUNT(DISTINCT e.id) AS n
            FROM qb_journal_line_items li
            JOIN qb_journal_entries e ON e.id = li.entry_id
            WHERE e.location = 'dennis' AND SUBSTR(e.entry_date, 1, 7) = '2026-03'
        """).fetchone()
        assert r["n"] == 22, f"expected 22 Dennis March JEs, got {r['n']}"
        assert cents(r["dr"]) == cents(r["cr"]), \
            f"Dennis March out of balance: debits {r['dr']} vs credits {r['cr']}"


class TestInvoiceCategoryMapping:
    """Invoice category_type resolves into gl_accounts, per entity, by ID.

    This is the mapping COGS is built on, so it gets the same treatment as the
    register codings: it points at an account ID, the account must be active
    and belong to the same entity, and nothing resolves a GL account by name at
    runtime. The old hardcoded name dict is what this replaced — a name is not
    an identity, and the 225-row orphaning is what happens when it is treated
    as one.
    """

    EXPECTED_CATEGORIES = {
        "FOOD", "LIQUOR", "BEER", "WINE", "NA_BEVERAGES", "NON_COGS",
        "TOGO_SUPPLIES", "DR_SUPPLIES", "KITCHEN_SUPPLIES",
        "OTHER", "TAX", "DEPOSIT", "LIQUOR_WINE", "LIQUOR_WINE_BEER",
    }

    def test_both_entities_are_fully_mapped(self, conn):
        for loc in ("chatham", "dennis"):
            got = {r[0] for r in conn.execute(
                "SELECT category_type FROM gl_category_mapping WHERE location = ?", (loc,))}
            missing = self.EXPECTED_CATEGORIES - got
            assert not missing, f"{loc} is missing category mappings: {sorted(missing)}"

    def test_no_mapping_points_at_an_invalid_account(self, conn):
        from routes.register_routes import audit_gl_category_mapping
        bad = audit_gl_category_mapping(conn)
        assert not bad, (
            f"{len(bad)} invalid category mappings: "
            + "; ".join(f"{b['location']}/{b['category_type']} -> "
                        f"{b['gl_account_id']} ({b['problem']})" for b in bad[:10])
        )

    def test_entities_never_share_a_gl_account(self, conn):
        """The two COAs are separate by design. If one account id ever served
        both locations, a Dennis invoice would post into Chatham's books."""
        shared = conn.execute("""
            SELECT gl_account_id, COUNT(DISTINCT location) n
            FROM gl_category_mapping GROUP BY gl_account_id HAVING n > 1
        """).fetchall()
        assert not shared, f"category mappings shared across entities: {[dict(s) for s in shared]}"

    def test_unknown_is_never_mapped(self, conn):
        """An uncategorised line must stay visibly uncoded. Mapping UNKNOWN to
        a real account would bury unclassified spend inside a legitimate one."""
        n = conn.execute(
            "SELECT COUNT(*) FROM gl_category_mapping WHERE category_type = 'UNKNOWN'"
        ).fetchone()[0]
        assert n == 0, "UNKNOWN must not be mapped — it has to surface as uncoded"

    def test_every_confirmed_invoice_line_resolves(self, conn):
        """Coverage, in dollars. A category present in the data but absent from
        the mapping is silently-dropped COGS, so assert on spend, not on rows."""
        from routes.register_routes import _resolve_gl_for_category
        unresolved = {}
        for r in conn.execute("""
            SELECT si.location AS loc,
                   COALESCE(NULLIF(TRIM(ii.category_type), ''), 'UNKNOWN') AS cat,
                   SUM(COALESCE(ii.total_price, 0)) AS amt
            FROM scanned_invoice_items ii
            JOIN scanned_invoices si ON si.id = ii.invoice_id
            WHERE si.status = 'confirmed' AND si.location IS NOT NULL
            GROUP BY 1, 2
        """):
            if not _resolve_gl_for_category(conn, r["cat"], r["loc"]):
                unresolved[f"{r['loc']}/{r['cat']}"] = round(r["amt"] or 0, 2)
        assert not unresolved, f"confirmed invoice spend with no GL mapping: {unresolved}"

    def test_cogs_categories_land_on_cogs_accounts(self, conn):
        """Food/beverage categories must reach a Cost of Goods Sold account, or
        food cost % is computed off the wrong side of the P&L."""
        from routes.register_routes import _resolve_gl_for_category
        for loc in ("chatham", "dennis"):
            for cat in ("FOOD", "LIQUOR", "BEER", "WINE", "NA_BEVERAGES"):
                gl_id = _resolve_gl_for_category(conn, cat, loc)
                assert gl_id, f"{loc}/{cat} does not resolve"
                t = conn.execute(
                    "SELECT account_type FROM gl_accounts WHERE id = ?", (gl_id,)
                ).fetchone()[0]
                assert t == "Cost of Goods Sold", f"{loc}/{cat} -> {t}, expected COGS"

    def test_resolver_refuses_a_cross_entity_lookup(self, conn):
        """Chatham's Food account must never be reachable from a Dennis row."""
        from routes.register_routes import _resolve_gl_for_category
        chatham_food = _resolve_gl_for_category(conn, "FOOD", "chatham")
        dennis_food = _resolve_gl_for_category(conn, "FOOD", "dennis")
        assert chatham_food and dennis_food
        assert chatham_food != dennis_food, (
            "both entities resolved FOOD to the same account id — the charts are "
            "supposed to be separate"
        )

    def test_food_cost_denominator_excludes_takeout_supplies(self, conn):
        """Decision of 2026-08-23. TakeOut Supplies is COGS-typed in both
        charts and stays that way (retyping is the accountant's call), so it
        sits inside COGS — but takeout packaging is not food, and including it
        inflated Dennis March food cost by 4.6%. It gets its own line and stays
        out of the denominator."""
        from routes.register_routes import (
            FNB_COGS_CATEGORIES, COGS_NON_FNB_CATEGORIES, _resolve_gl_for_category)

        assert "TOGO_SUPPLIES" not in FNB_COGS_CATEGORIES
        assert "TOGO_SUPPLIES" in COGS_NON_FNB_CATEGORIES
        assert not (FNB_COGS_CATEGORIES & COGS_NON_FNB_CATEGORIES), \
            "a category cannot be both in and out of the food cost denominator"

        # Every category in either set must actually land on a COGS account —
        # otherwise the split is being applied to the wrong side of the P&L.
        for loc in ("chatham", "dennis"):
            for cat in FNB_COGS_CATEGORIES | COGS_NON_FNB_CATEGORIES:
                gl_id = _resolve_gl_for_category(conn, cat, loc)
                assert gl_id, f"{loc}/{cat} does not resolve"
                t = conn.execute(
                    "SELECT account_type FROM gl_accounts WHERE id = ?", (gl_id,)
                ).fetchone()[0]
                assert t == "Cost of Goods Sold", f"{loc}/{cat} -> {t}, expected COGS"

    def test_approximate_categories_are_flagged_for_the_pl_footnote(self, conn):
        """The five judgement-call mappings must stay marked, in both entities.
        The flag is what turns a wrong-ish number into a labelled one."""
        expected = {"OTHER", "TAX", "DEPOSIT", "LIQUOR_WINE", "LIQUOR_WINE_BEER"}
        for loc in ("chatham", "dennis"):
            got = {r[0] for r in conn.execute(
                "SELECT category_type FROM gl_category_mapping "
                "WHERE location = ? AND confidence = 'approximate'", (loc,))}
            assert got == expected, f"{loc} approximate set drifted: {sorted(got)}"
            # An approximate mapping without a note is just an unexplained guess.
            missing = [r[0] for r in conn.execute(
                "SELECT category_type FROM gl_category_mapping "
                "WHERE location = ? AND confidence = 'approximate' "
                "AND (note IS NULL OR TRIM(note) = '')", (loc,))]
            assert not missing, f"{loc} approximate mappings with no note: {missing}"

    def test_unmapped_or_empty_category_resolves_to_none(self, conn):
        from routes.register_routes import _resolve_gl_for_category
        assert _resolve_gl_for_category(conn, "NOT_A_CATEGORY", "chatham") is None
        assert _resolve_gl_for_category(conn, "", "chatham") is None
        assert _resolve_gl_for_category(conn, None, "chatham") is None
        # No location means no entity, and therefore no defensible account.
        assert _resolve_gl_for_category(conn, "FOOD", None) is None
