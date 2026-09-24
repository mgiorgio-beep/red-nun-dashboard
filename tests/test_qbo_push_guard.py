"""No write may reach Red Buoy's (Chatham's) QBO company while its account ids
are being rebuilt (Mike, 2026-09-24). Reads and Dennis are unaffected."""
import pytest

from integrations.quickbooks import push_guard


def test_red_buoy_writes_are_blocked(monkeypatch):
    monkeypatch.setenv("QB_REALM_ID_CHATHAM", "999000111")
    with pytest.raises(push_guard.QboWriteBlocked):
        push_guard.check_write("999000111", "POST journalentry")
    with pytest.raises(push_guard.QboWriteBlocked):      # the legacy scripts' hardcoded default
        push_guard.check_write("123146237986854", "POST deposit")


def test_dennis_writes_pass(monkeypatch):
    monkeypatch.setenv("QB_REALM_ID_CHATHAM", "999000111")
    push_guard.check_write("555000222", "POST account")   # no raise


def test_legacy_script_refuses_before_any_network_call(monkeypatch):
    import integrations.quickbooks.qb_push_payments as m
    monkeypatch.setattr(m, "REALM_ID", "123146237986854")
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: pytest.fail("reached the network"))
    with pytest.raises(push_guard.QboWriteBlocked):
        m.qbo_post("deposit", {}, {"access_token": "x"})
