"""Sales journals that QBO already holds stay settled (Mike, 2026-09-24).

MarginEdge posted the daily sales JEs through Chatham 5/07 and Dennis 5/03;
one dashboard JE (Dennis 8/20) is posted. A rebuild must not flip either
back to 'ready', and nothing on or before the MarginEdge cutoff may push.
Synthetic DB; always runnable.
"""
import sqlite3

import pytest


@pytest.fixture
def sj(tmp_path, monkeypatch):
    from reports import sales_journal as mod
    path = str(tmp_path / "sj.db")

    def conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(mod, "get_connection", conn)
    # sales_journal loads .env at import: strip every QBO credential so a
    # push that gets past the guards can never reach a real company.
    import os
    for k in [k for k in os.environ if k.startswith(("QB_CLIENT", "QB_REALM"))]:
        monkeypatch.delenv(k)
    mod.init_sales_journal_tables()
    # In production the register's migrations add these; the push reads them.
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS gl_accounts (id INTEGER PRIMARY KEY, name TEXT, account_type TEXT, "
              "location TEXT, active INTEGER DEFAULT 1, qbo_id TEXT)")
    cols = [r[1] for r in c.execute("PRAGMA table_info(qb_line_mapping)")]
    if "gl_account_id" not in cols:
        c.execute("ALTER TABLE qb_line_mapping ADD COLUMN gl_account_id INTEGER")
    c.commit()
    return mod, conn


def _entry(loc, day, status="ready"):
    return {"entry_type": "sales_journal", "location": loc, "entry_date": day, "je_name": f"X{day}",
            "total_debits": 10.0, "total_credits": 10.0, "balanced": True, "status": status,
            "line_items": [{"journal_name": "Tenders: Cash", "qbo_account": "Cash", "debit": 10.0, "mapped": True},
                           {"journal_name": "Gross Sales: Food", "qbo_account": "Food", "credit": 10.0, "mapped": True}]}


@pytest.mark.parametrize("held", ["superseded_me", "posted"])
def test_a_rebuild_leaves_a_settled_day_alone(sj, held):
    mod, conn = sj
    eid = mod.persist_journal_entry(_entry("chatham", "2026-04-02"))
    c = conn()
    c.execute("UPDATE qb_journal_entries SET status=? WHERE id=?", (held, eid))
    c.commit()
    again = _entry("chatham", "2026-04-02")
    again["total_debits"] = again["total_credits"] = 999.0
    assert mod.persist_journal_entry(again) == eid
    row = conn().execute("SELECT status, total_debits FROM qb_journal_entries WHERE id=?", (eid,)).fetchone()
    assert (row["status"], row["total_debits"]) == (held, 10.0)
    assert conn().execute("SELECT COUNT(*) FROM qb_journal_line_items WHERE entry_id=?", (eid,)).fetchone()[0] == 2


def test_a_ready_day_still_rebuilds(sj):
    mod, conn = sj
    eid = mod.persist_journal_entry(_entry("dennis", "2026-06-02"))
    again = _entry("dennis", "2026-06-02", status="needs_attention")
    mod.persist_journal_entry(again)
    assert conn().execute("SELECT status FROM qb_journal_entries WHERE id=?", (eid,)).fetchone()[0] == "needs_attention"


@pytest.mark.parametrize("loc,day,blocked", [("chatham", "2026-05-07", True), ("chatham", "2026-05-08", False),
                                             ("dennis", "2026-05-03", True), ("dennis", "2026-05-04", False)])
def test_nothing_on_or_before_the_marginedge_cutoff_pushes(sj, loc, day, blocked):
    mod, conn = sj
    eid = mod.persist_journal_entry(_entry(loc, day))
    r = mod.push_to_qbo(eid)
    assert r["success"] is False                      # credentials stripped in the fixture
    assert ("MarginEdge" in r["error"]) is blocked


def _map(conn, loc, jname, gl_id, qbo_id, atype="Income"):
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS gl_accounts (id INTEGER PRIMARY KEY, name TEXT, account_type TEXT, "
              "location TEXT, active INTEGER DEFAULT 1, qbo_id TEXT)")
    c.execute("INSERT OR REPLACE INTO gl_accounts (id, name, account_type, location, active, qbo_id) VALUES (?,?,?,?,1,?)",
              (gl_id, jname, atype, loc, qbo_id))
    c.execute("INSERT INTO qb_line_mapping (location, journal_name, qbo_account, gl_account_id) VALUES (?,?,?,?)",
              (loc, jname, qbo_id, gl_id))
    c.commit()


def test_an_entry_built_with_outdated_ids_is_refused(sj):
    """Chatham's QBO ids were rebuilt 2026-09-24; an entry built before that
    carries the old ids and must be rebuilt, never pushed."""
    mod, conn = sj
    e = _entry("dennis", "2026-06-02")
    e["line_items"][1]["qbo_account"] = "OLD-49"          # built when the account carried an old id
    eid = mod.persist_journal_entry(e)
    _map(conn, "dennis", "Gross Sales: Food", 900, "49")
    r = mod.push_to_qbo(eid)
    assert r["success"] is False and "outdated QBO account ids" in r["error"]


def test_an_entry_with_current_ids_passes_the_guard(sj):
    mod, conn = sj
    e = _entry("dennis", "2026-06-03")
    e["line_items"][1]["qbo_account"] = "49"
    eid = mod.persist_journal_entry(e)
    _map(conn, "dennis", "Gross Sales: Food", 901, "49")
    r = mod.push_to_qbo(eid)
    assert "outdated" not in r["error"]                    # stops later, at the stripped credentials
