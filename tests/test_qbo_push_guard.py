"""The switch in front of Red Buoy's (Chatham's) QBO writes. Blocked while its
account ids were rebuilt (Mike, 2026-09-24); lifted on his sign-off 2026-09-26.
The blocking tests throw the switch themselves so the mechanism stays covered."""
import pytest

from integrations.quickbooks import push_guard


def test_red_buoy_writes_pass_after_signoff(monkeypatch):
    monkeypatch.setenv("QB_REALM_ID_CHATHAM", "999000111")
    assert push_guard.CHATHAM_QBO_WRITES_BLOCKED is False
    push_guard.check_write("999000111", "POST journalentry")   # no raise


def test_red_buoy_writes_are_blocked(monkeypatch):
    monkeypatch.setattr(push_guard, "CHATHAM_QBO_WRITES_BLOCKED", True)
    monkeypatch.setenv("QB_REALM_ID_CHATHAM", "999000111")
    with pytest.raises(push_guard.QboWriteBlocked):
        push_guard.check_write("999000111", "POST journalentry")
    with pytest.raises(push_guard.QboWriteBlocked):      # the legacy scripts' hardcoded default
        push_guard.check_write("123146237986854", "POST deposit")


def test_dennis_writes_pass(monkeypatch):
    monkeypatch.setenv("QB_REALM_ID_CHATHAM", "999000111")
    push_guard.check_write("555000222", "POST account")   # no raise


def test_legacy_script_refuses_before_any_network_call(monkeypatch):
    monkeypatch.setattr(push_guard, "CHATHAM_QBO_WRITES_BLOCKED", True)
    import integrations.quickbooks.qb_push_payments as m
    monkeypatch.setattr(m, "REALM_ID", "123146237986854")
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: pytest.fail("reached the network"))
    with pytest.raises(push_guard.QboWriteBlocked):
        m.qbo_post("deposit", {}, {"access_token": "x"})
