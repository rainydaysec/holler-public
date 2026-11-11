"""holler/accounts.py — saving and swapping Claude Code logins.

The Keychain is faked with a dict so tests never touch the real login."""

import json

import pytest

from holler import accounts


@pytest.fixture
def env(tmp_path, monkeypatch):
    kc: dict = {}
    monkeypatch.setattr(accounts.sys, "platform", "darwin")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HOLLER_ACCOUNTS_DISABLED", raising=False)
    monkeypatch.setattr(accounts, "claude_json_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(accounts, "_index_path", lambda: tmp_path / "accounts.json")
    monkeypatch.setattr(accounts.getpass, "getuser", lambda: "me")

    def read(service, account=None):
        if account is None:
            return next((v for (s, _), v in kc.items() if s == service), None)
        return kc.get((service, account))

    monkeypatch.setattr(accounts, "_kc_read", read)
    monkeypatch.setattr(accounts, "_kc_write", lambda s, a, v: kc.__setitem__((s, a), v))
    monkeypatch.setattr(accounts, "_kc_delete", lambda s, a: kc.pop((s, a), None))

    def login(email, token):
        # What `claude auth login` leaves behind.
        kc[(accounts.LIVE_SERVICE, "me")] = json.dumps({"claudeAiOauth": {"accessToken": token}})
        path = tmp_path / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {"other": 1}
        data["oauthAccount"] = {"emailAddress": email, "organizationName": f"{email} org"}
        path.write_text(json.dumps(data))

    return kc, login, tmp_path


def live_token(kc):
    return json.loads(kc[(accounts.LIVE_SERVICE, "me")])["claudeAiOauth"]["accessToken"]


def test_current_reads_claude_json(env):
    _, login, _ = env
    assert accounts.current() is None
    login("a@work.com", "t1")
    assert accounts.current() == accounts.Account("a@work.com", "a@work.com org")


def test_remember_then_switch_back_and_forth(env):
    kc, login, tmp = env
    login("a@work.com", "t-work")
    accounts.remember_current()
    login("b@gmail.com", "t-home")  # added via Terminal
    accounts.remember_current()
    assert [a.email for a in accounts.saved()] == ["a@work.com", "b@gmail.com"]

    accounts.switch_to("a@work.com")
    assert live_token(kc) == "t-work"
    assert accounts.current().email == "a@work.com"
    # Rest of ~/.claude.json survives the swap.
    assert json.loads((tmp / ".claude.json").read_text())["other"] == 1

    accounts.switch_to("b@gmail.com")
    assert live_token(kc) == "t-home"


def test_switch_saves_outgoing_refreshed_token(env):
    kc, login, _ = env
    login("a@work.com", "t-work")
    accounts.remember_current()
    login("b@gmail.com", "t-home")
    accounts.remember_current()
    # CLI refreshed b's token after the last save.
    kc[(accounts.LIVE_SERVICE, "me")] = json.dumps({"claudeAiOauth": {"accessToken": "t-home-2"}})
    accounts.switch_to("a@work.com")
    accounts.switch_to("b@gmail.com")
    assert live_token(kc) == "t-home-2"


def test_switch_unknown_account_raises_and_changes_nothing(env):
    kc, login, _ = env
    login("a@work.com", "t-work")
    with pytest.raises(accounts.AccountError):
        accounts.switch_to("nobody@x.com")
    assert live_token(kc) == "t-work"


def test_forget_drops_saved_copy_only(env):
    kc, login, _ = env
    login("a@work.com", "t-work")
    accounts.remember_current()
    accounts.forget("a@work.com")
    assert accounts.saved() == []
    assert (accounts.SAVED_SERVICE, "a@work.com") not in kc
    assert live_token(kc) == "t-work"


def test_unsupported_with_custom_config_dir(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/x")
    assert not accounts.supported()
    with pytest.raises(accounts.AccountError):
        accounts.switch_to("a@work.com")
