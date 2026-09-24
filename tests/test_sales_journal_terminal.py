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
