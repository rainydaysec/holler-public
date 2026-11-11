"""Per-session aliveness markers for slash-command coordination.

Holler can have many sessions active concurrently. Some of them (Canvas edit
sessions specifically) need to coordinate so they don't clobber each other's
work in a shared git checkout. A coordinating slash command could write a
project-local lockfile, but it also needs a way to
detect when a lock is *stale* — held by a session whose Holler process is
gone, or whose Holler is still running but the session itself is archived.

The mechanism: Holler writes one tiny marker file per live session into
`~/.holler/locks/sess-<id>.alive`. The slash command reads the lockfile,
extracts the holding session's id, and checks for the marker. Marker
present → lock is real; marker absent → lock is stale, can be cleared.

The marker dir is wiped on Holler startup because any marker from a prior
run is by definition stale (the SDK subprocess for that session died with
Holler). The slash commands consult only what's on disk *right now*, so a
fresh wipe + per-session re-marking is sufficient.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


def locks_dir() -> Path:
    """`~/.holler/locks/` — same shape on Windows / macOS / Linux."""
    return Path.home() / ".holler" / "locks"


def marker_path(session_id: int | str) -> Path:
    return locks_dir() / f"sess-{session_id}.alive"


def wipe_all() -> None:
    """Remove every marker. Called once at Holler startup since markers from
    a previous run are stale by construction (their owning SDK clients died
    with the previous process)."""
    d = locks_dir()
    try:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.exception("failed to wipe stale session locks at %s", d)


def mark_alive(session_id: int | str) -> None:
    """Touch the marker file for this session. Idempotent — repeated calls
    on the same id are no-ops. Safe to call from any thread."""
    if session_id is None:
        return
    try:
        d = locks_dir()
        d.mkdir(parents=True, exist_ok=True)
        marker_path(session_id).touch(exist_ok=True)
    except Exception:
        log.exception("failed to mark session %s alive", session_id)


def clear(session_id: int | str) -> None:
    """Remove the marker for a session. Called when the operator archives or
    explicitly ends a session — frees any coordination lock a slash command may
    be holding on its behalf."""
    if session_id is None:
        return
    try:
        marker_path(session_id).unlink(missing_ok=True)
    except Exception:
        log.exception("failed to clear session %s marker", session_id)


def is_alive(session_id: int | str) -> bool:
    """True if the marker file exists. The slash command runs the equivalent
    check directly from PowerShell — this helper is for Python callers."""
    if session_id is None:
        return False
    try:
        return marker_path(session_id).exists()
    except Exception:
        return False
