"""
Bank register / reconciliation regression tests.

These run against the LIVE database, read-only (GET requests and SELECTs only).
That is deliberate: the whole point is to replay the real statements Cape Cod
Five actually issued and assert the tie-outs to the penny. A synthetic fixture
would only prove the arithmetic, not that *these books* still balance.

The three Dennis statements are the known-good corpus. Jan and Feb tie exactly
and must never stop doing so — if either breaks, something changed the register
query, the import, or the underlying rows.

March and Chatham January carry KNOWN, DOCUMENTED breaks and are asserted at
their exact current values. When a fix lands, these numbers change and the test
must be updated in the same commit. A test that says "March is off by
3246.49" is a fact about the books; a test that says "March doesn't tie" is
worth nothing.

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


def tie_out(client, upload):
    """(computed_cents, stated_cents, delta_cents) for one statement period."""
    r = client.get(
        f"/api/register/{upload['bank_account_id']}"
        f"?start={upload['period_start']}&end={upload['period_end']}"
    )
    assert r.status_code == 200, f"register query failed: {r.status_code}"
    s = r.get_json()["summary"]
    computed = (cents(upload["beginning_balance"])
                + cents(s["total_inflow"]) - cents(s["total_outflow"]))
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


class TestDennisTieOuts:
    """beginning + inflows - outflows == ending, per statement period.
    Periods are NOT calendar months — February runs 02/02-03/01."""

    def test_january_ties_exactly(self, client, uploads):
        u = _upload(uploads, DENNIS, "2026-01-01")
        computed, stated, delta = tie_out(client, u)
        assert cents(u["beginning_balance"]) == 949535
        assert stated == 2428014
        assert delta == 0, f"Dennis January must tie to the penny, off by {delta/100:.2f}"

    def test_february_ties_exactly(self, client, uploads):
        u = _upload(uploads, DENNIS, "2026-02-02")
        computed, stated, delta = tie_out(client, u)
        assert cents(u["beginning_balance"]) == 2428014
        assert stated == 3574663
        assert delta == 0, f"Dennis February must tie to the penny, off by {delta/100:.2f}"

    def test_march_is_short_by_the_known_duplicate_payroll(self, client, uploads):
        """KNOWN BREAK, asserted exactly.

        -3,246.49 = 3,189.03 of payroll checks 9689/9692/9693/9695 counted
        twice (once as payroll_checks rows, once as statement 'Check NNNN'
        lines) + 57.46 for Maya Jones, genuinely uncleared.

        After the dedupe pass merges those four, this must become -5746.
        Change the expected value in the same commit that runs the dedupe.
        """
        u = _upload(uploads, DENNIS, "2026-03-02")
        computed, stated, delta = tie_out(client, u)
        assert stated == 3216219
        assert delta == -324649, (
            f"Dennis March delta moved from the documented -3246.49 to "
            f"{delta/100:.2f}. If dedupe has run, expected -57.46."
        )


class TestChathamTieOut:
    def test_january_is_short_and_unexplained(self, client, uploads):
        """KNOWN BREAK, not yet diagnosed.

        Found 2026-08-22. The handover brief reported only Dennis March as
        broken and did not compute Chatham at all. Chatham is the account with
        one statement of coverage and the larger uncoded backlog, so this delta
        is unexplained rather than itemized. Do not 'fix' it by adjusting the
        register — find what it is first.
        """
        u = _upload(uploads, CHATHAM, "2026-01-01")
        computed, stated, delta = tie_out(client, u)
        assert stated == 2578504
        assert delta == -515148, (
            f"Chatham January delta moved from -5151.48 to {delta/100:.2f}"
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
