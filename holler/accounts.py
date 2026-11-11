"""Switch which Claude account Holler (and Claude Code) is signed in as.

Claude Code keeps ONE active login on macOS: the OAuth tokens in the Keychain
item `Claude Code-credentials`, plus the account's profile (`oauthAccount`) in
`~/.claude.json`. Holler spawns that same CLI, so whoever the CLI is signed in
as is who Holler works as.

To make switching one click, Holler saves a copy of each account it sees in its
own Keychain item (`Holler Claude account`, one per email) and swaps the saved
copy into the live slot on demand. Adding an account is a normal
`claude auth login` in Terminal; Holler notices the new login and saves it.

Refresh tokens rotate, so the ACTIVE account's saved copy goes stale as the CLI
refreshes. `remember_current()` re-saves it — called before every switch and
periodically by the UI — so switching back never lands on a dead token.

The swap is global: Claude Code in Terminal follows it too. Already-running CLI
processes keep the old login in memory until they restart, which is why the UI
restarts Holler after a switch.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from holler.appdir import app_data_dir

log = logging.getLogger(__name__)

LIVE_SERVICE = "Claude Code-credentials"
SAVED_SERVICE = "Holler Claude account"
_TIMEOUT = 10.0
_lock = threading.Lock()


class AccountError(RuntimeError):
    """A switch/save failed; the message is safe to show the operator."""


@dataclass(frozen=True)
class Account:
    email: str
    org: str = ""

    @property
    def label(self) -> str:
        return self.email


# ---------------------------------------------------------------- environment

def supported() -> bool:
    """Only the default macOS Keychain layout is handled. A custom
    CLAUDE_CONFIG_DIR changes the CLI's Keychain item name, so stay out."""
    return (
        sys.platform == "darwin"
        and not os.environ.get("CLAUDE_CONFIG_DIR")
        and not os.environ.get("HOLLER_ACCOUNTS_DISABLED")  # the test suite
    )


def claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def _index_path() -> Path:
    return app_data_dir() / "accounts.json"


# ------------------------------------------------------------------- keychain

def _kc_read(service: str, account: Optional[str] = None) -> Optional[str]:
    cmd = ["security", "find-generic-password", "-s", service]
    if account is not None:
        cmd += ["-a", account]
    try:
        out = subprocess.run(cmd + ["-w"], capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("keychain read %s failed: %s", service, e)
        return None
    return out.stdout.rstrip("\n") if out.returncode == 0 and out.stdout.strip() else None


def _kc_write(service: str, account: str, secret: str) -> None:
    # Fed through `security -i` on stdin, hex-encoded, so the token never
    # appears in a process listing or needs shell quoting. `-U` updates in
    # place, which keeps the item's access list (Claude Code reads it through
    # this same `security` tool).
    line = (
        f"add-generic-password -U -a {shlex.quote(account)} -s {shlex.quote(service)} "
        f"-X {secret.encode('utf-8').hex()}\n"
    )
    try:
        out = subprocess.run(["security", "-i"], input=line, capture_output=True,
                             text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise AccountError(f"Couldn't write to the Keychain: {e}") from e
    if out.returncode != 0 or "error" in (out.stderr or "").lower():
        raise AccountError(f"Couldn't write to the Keychain: {(out.stderr or '').strip()}")


def _kc_delete(service: str, account: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, timeout=_TIMEOUT)


# ------------------------------------------------------------- ~/.claude.json

def _read_claude_json() -> dict:
    try:
        return json.loads(claude_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_oauth_account(profile: dict) -> None:
    # Read-modify-write as late as possible: running CLIs rewrite this file.
    path = claude_json_path()
    data = _read_claude_json()
    data["oauthAccount"] = profile
    tmp = path.with_name(path.name + ".holler-tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, path.stat().st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _account_from_profile(profile: dict) -> Optional[Account]:
    email = (profile or {}).get("emailAddress")
    return Account(email, profile.get("organizationName") or "") if email else None


# -------------------------------------------------------------------- index

def _read_index() -> list[dict]:
    try:
        rows = json.loads(_index_path().read_text(encoding="utf-8"))
        return [r for r in rows if isinstance(r, dict) and r.get("email")]
    except (OSError, ValueError, TypeError):
        return []


def _write_index(rows: list[dict]) -> None:
    path = _index_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------- API

def current() -> Optional[Account]:
    """Who Claude Code is signed in as right now (None when signed out)."""
    return _account_from_profile(_read_claude_json().get("oauthAccount") or {})


def saved() -> list[Account]:
    """Every account Holler can switch to, in the order first seen."""
    return [Account(r["email"], r.get("org") or "") for r in _read_index()]


def remember_current() -> Optional[Account]:
    """Save (or refresh) the active login so it can be switched back to."""
    if not supported():
        return None
    with _lock:
        profile = _read_claude_json().get("oauthAccount") or {}
        acct = _account_from_profile(profile)
        creds = _kc_read(LIVE_SERVICE, getpass.getuser()) or _kc_read(LIVE_SERVICE)
        if acct is None or not creds:
            return None
        blob = json.dumps({"credentials": creds, "oauthAccount": profile})
        if _kc_read(SAVED_SERVICE, acct.email) != blob:
            _kc_write(SAVED_SERVICE, acct.email, blob)
        rows = _read_index()
        for r in rows:
            if r["email"].lower() == acct.email.lower():
                r["org"] = acct.org
                break
        else:
            rows.append({"email": acct.email, "org": acct.org, "saved_at": time.time()})
        _write_index(rows)
        return acct


def switch_to(email: str) -> Account:
    """Make `email` the active Claude Code login. Raises AccountError."""
    if not supported():
        raise AccountError("Account switching needs macOS and the default Claude Code config.")
    cur = current()
    if cur and cur.email.lower() == email.lower():
        return cur
    remember_current()  # keep the outgoing account's newest refresh token
    with _lock:
        raw = _kc_read(SAVED_SERVICE, email)
        if not raw:
            raise AccountError(f"No saved sign-in for {email}. Add it again.")
        try:
            blob = json.loads(raw)
            creds, profile = blob["credentials"], blob["oauthAccount"]
        except (ValueError, KeyError, TypeError) as e:
            raise AccountError(f"The saved sign-in for {email} is damaged. Add it again.") from e
        _kc_write(LIVE_SERVICE, getpass.getuser(), creds)
        _write_oauth_account(profile)
    log.info("switched Claude account to %s", email)
    return _account_from_profile(profile) or Account(email)


def forget(email: str) -> None:
    """Drop Holler's saved copy (never signs the live account out)."""
    with _lock:
        _kc_delete(SAVED_SERVICE, email)
        _write_index([r for r in _read_index() if r["email"].lower() != email.lower()])


def login_in_terminal(cli: Optional[str], email: str = "") -> None:
    """Open Terminal on `claude auth login`. The browser flow there replaces
    the live login; the UI then calls remember_current() once it sees it."""
    remember_current()
    cmd = f"{shlex.quote(cli or 'claude')} auth login"
    if email:
        cmd += f" --email {shlex.quote(email)}"
    script = (
        'tell application "Terminal"\n'
        f"  do script {json.dumps(cmd)}\n"
        "  activate\n"
        "end tell"
    )
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise AccountError(f"Couldn't open Terminal: {e}") from e
