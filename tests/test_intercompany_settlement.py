"""
Intercompany settlement end to end (brief 8H, acceptance 15).

Runs against a COPY of the database (it writes): set DB_PATH to a scratch copy
made with `sqlite3 /var/lib/rednun/toast_data.db ".backup /tmp/x.db"`. Refuses to
run against the live file.

  DB_PATH=/tmp/x.db venv/bin/python3 -m pytest -q tests/test_intercompany_settlement.py

Scenario: Dennis lends Chatham 2 cases of fries, Chatham sends Dennis 7 cases of
Tito's (a power buy), Chatham brings 1 case of fries back. The month is booked,
the tie-out passes; Reconcile pays the rest with one check from Dennis; the
tie-out still passes; an entry injected on one side only makes it FAIL.
"""
import os
import uuid

import pytest

LIVE = '/var/lib/rednun/toast_data.db'
DB = os.environ.get('DB_PATH', '')
pytestmark = pytest.mark.skipif(not DB or os.path.realpath(DB) == os.path.realpath(LIVE) or not os.path.exists(DB),
                                reason='needs DB_PATH pointing at a scratch copy (never the live DB)')

FRIES, TITOS = 783, 619


@pytest.fixture(scope='module')
def env():
    import reports.move_notify as N
    N.queue = lambda *a, **k: None                       # no emails from tests
    from integrations.toast.data_store import get_connection
    from reports import moves as M
    from reports import intercompany as IC
    conn = get_connection()
    IC.ensure_settlement_tables(conn)
    for t in ('settlement_lines', 'intercompany_settlements', 'inventory_transfers'):
        conn.execute(f'DELETE FROM {t}')
    conn.execute("DELETE FROM qb_journal_entries WHERE entry_type LIKE 'intercompany%'")
    conn.commit()
    return conn, M, IC


def _log(conn, M, src, pid, qty, unit='case'):
    actor = {'person': 'test', 'token_id': None, 'role': 'owner', 'home': src, 'via': 'web'}
    r = M.log_direct(conn, 'transfer', str(uuid.uuid4()), {'from': src, 'product_id': pid, 'qty': qty, 'unit': unit}, actor)
    assert r['status'] == 'logged', r
    return conn.execute('SELECT * FROM inventory_transfers WHERE id = ?', (r['id'],)).fetchone()


def test_settlement_flow(env):
    conn, M, IC = env
    lend = _log(conn, M, 'dennis', FRIES, 2)
    power = _log(conn, M, 'chatham', TITOS, 7)
    back = _log(conn, M, 'chatham', FRIES, 1)
    assert lend['total_cost'] and power['total_cost']
    # the case going back is a return, at what the borrowed cases cost
    assert back['is_settlement'] == 1
    assert back['total_cost'] == pytest.approx(lend['total_cost'] / 2, abs=0.01)

    items = {tuple(i['key']): i for i in IC.open_items(conn)}
    fries = next(i for k, i in items.items() if FRIES in k)
    titos = next(i for k, i in items.items() if TITOS in k)
    assert fries['owed_by'] == 'chatham' and fries['abs_qty'] == pytest.approx(lend['qty_base'] / 2)
    assert titos['owed_by'] == 'dennis' and titos['abs_cost'] == pytest.approx(power['total_cost'])

    # book the month: move the test rows into last month first
    conn.execute("UPDATE inventory_transfers SET business_date = '20260915'")
    conn.commit()
    ents = IC.build_month_entries(conn, '2026-09')
    assert set(ents) == {'chatham', 'dennis'}
    for e in ents.values():
        assert e['balanced'] and e['status'] == 'ready', e
    t = IC.tie_out(conn)
    assert t['ok'], t['problems']
    owed = round(titos['abs_cost'] - fries['abs_cost'], 2)
    assert t['chatham'] == pytest.approx(owed) and t['dennis'] == pytest.approx(-owed)

    # keep the fries open (coming back); pay the Tito's only
    pv = IC.preview_reconcile(conn, keep_keys=[fries['key']])
    assert pv['payer'] == 'dennis' and pv['amount'] == pytest.approx(titos['abs_cost'])
    with pytest.raises(ValueError):
        IC.reconcile(conn, [fries['key']], 'test', pv['amount'] + 1)        # stale amount refused
    s = IC.reconcile(conn, [fries['key']], 'test', pv['amount'])
    chk = conn.execute('SELECT * FROM manual_checks WHERE id = ?', (s['manual_check_id'],)).fetchone()
    assert chk['location'] == 'dennis' and chk['payee_name'] == 'Red Buoy Inc.' and chk['amount'] == pytest.approx(pv['amount'])
    regs = {r['bank_account_id']: r for r in conn.execute(
        'SELECT * FROM manual_bank_entries WHERE id IN (?, ?)', (s['payer_register_id'], s['payee_register_id']))}
    assert regs[2]['amount'] == pytest.approx(-pv['amount'])    # Dennis's bank: check out
    assert regs[1]['amount'] == pytest.approx(pv['amount'])     # Chatham's bank: deposit expected
    assert len(s['entries']) == 2 and all(e['status'] == 'ready' for e in s['entries'])
    left = IC.open_items(conn)
    assert [tuple(i['key']) for i in left] == [tuple(fries['key'])]

    t = IC.tie_out(conn)
    assert t['ok'], t['problems']

    # a one-sided entry must make it fail, loudly
    conn.execute("DELETE FROM qb_journal_entries WHERE id = ?", (s['payee_je_id'],))
    conn.commit()
    t = IC.tie_out(conn)
    assert not t['ok'] and t['headline'].startswith('INTERCOMPANY OUT OF BALANCE')
    assert any('only at dennis' in p for p in t['problems'])
