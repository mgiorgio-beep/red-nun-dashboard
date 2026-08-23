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
        """Every transfer row in the register must agree with the classifier:
        coded to Building Rent, or uncoded and awaiting review."""
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
        rows = conn.execute(
            "SELECT id, bank_account_id, payee, memo, amount, gl_account_id "
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
                assert r["gl_account_id"] is None, (
                    f"row {r['id']} must stay uncoded for review ({reason}), "
                    f"is coded to {r['gl_account_id']}"
                )


# ── Reconciliation sign-off ──────────────────────────────────────────────────

class TestReconciliationSignOff:
    """A closed period records that the cleared rows tied exactly AND that a
    named person accepted the itemized remainder."""

    def test_every_loaded_period_is_reconciled(self, client, uploads):
        j = client.get("/api/bank-reconcile/reconciliations").get_json()
        closed = {(r["bank_account_id"], r["period_start"]) for r in j["reconciliations"]
                  if r["status"] == "reconciled"}
        for u in uploads:
            assert (u["bank_account_id"], u["period_start"]) in closed, (
                f"account {u['bank_account_id']} {u['period_start']} is not signed off"
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
                UNION SELECT gl_account_id FROM gl_category_mapping WHERE gl_account_id IS NOT NULL)
        """).fetchall()
        inactive = [r[0] for r in refd if conn.execute(
            "SELECT active FROM gl_accounts WHERE id = ?", (r[0],)).fetchone()[0] == 0]
        assert not inactive, f"referenced but inactive accounts: {inactive}"


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

    def test_unmapped_or_empty_category_resolves_to_none(self, conn):
        from routes.register_routes import _resolve_gl_for_category
        assert _resolve_gl_for_category(conn, "NOT_A_CATEGORY", "chatham") is None
        assert _resolve_gl_for_category(conn, "", "chatham") is None
        assert _resolve_gl_for_category(conn, None, "chatham") is None
        # No location means no entity, and therefore no defensible account.
        assert _resolve_gl_for_category(conn, "FOOD", None) is None
