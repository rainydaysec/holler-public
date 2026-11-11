"""Atomic JSON state for things that should outlive `config.toml` round-trips.

Lives at `%LOCALAPPDATA%\\holler\\state.json`. Schema (extensible — unknown
keys are preserved on round-trip):

    {
        "panel":   {"x": int, "y": int, "w": int, "h": int},
        "sessions_window": {"x": int, "y": int, "w": int, "h": int},
        "sessions_splitter": [int, int],  # rail width / detail width
        "sessions_filter": "active",       # last-used filter chip
        "sessions_sort": "recent",         # last-used sort key
        "sessions_recents": [int, int, ...],  # MRU session-id stack (max 8)
        "model_recent": ["claude-opus-4-8", ...],
    }
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def _state_path() -> Path:
    from holler.appdir import app_data_dir
    return app_data_dir() / "state.json"


def load() -> dict[str, Any]:
    """Return parsed state. Empty dict on missing/corrupted file."""
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("state.json unreadable; ignoring", exc_info=True)
        return {}


# Serializes every load-merge-write cycle in this process. The debounced
# Timer thread and synchronous callers (shutdown flush, GUI) used to race:
# both wrote the same `state.json.tmp` path and could interleave
# load/update/save, dropping one side's keys.
_io_lock = threading.RLock()


def save(state: dict[str, Any]) -> None:
    """Atomic write via unique temp file + replace."""
    path = _state_path()
    with _io_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, indent=2))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def merged_save(updates: dict[str, Any]) -> None:
    """Load current state, shallow-merge updates, write back.

    Synchronous. Use for shutdown-style flushes where the disk write must
    complete before the caller returns. Hot paths (selection change, rate
    limit transition) should prefer ``merged_save_debounced``.
    """
    with _io_lock:
        current = load()
        current.update(updates)
        save(current)


# Coalesced write machinery: callers accumulate updates into a single in-memory
# dict; a daemon Timer flushes once activity stops. This collapses bursts of
# selection / sort / rate-limit writes into a single disk fsync.
_pending_updates: dict[str, Any] = {}
_pending_lock = threading.Lock()
_flush_timer: Optional[threading.Timer] = None


def _flush_pending() -> None:
    global _flush_timer
    with _pending_lock:
        u = dict(_pending_updates)
        _pending_updates.clear()
        _flush_timer = None
    if u:
        try:
            merged_save(u)
        except Exception:
            log.exception("state debounced flush failed")


def merged_save_debounced(updates: dict[str, Any], *, delay: float = 1.0) -> None:
    """Schedule an atomic write of ``updates`` after ``delay`` seconds of
    quiet. Subsequent calls within the window merge into the pending batch
    and reset the timer. Safe to call from any thread.
    """
    global _flush_timer
    with _pending_lock:
        _pending_updates.update(updates)
        if _flush_timer is not None:
            try:
                _flush_timer.cancel()
            except Exception:
                pass
        t = threading.Timer(delay, _flush_pending)
        t.daemon = True
        _flush_timer = t
        t.start()


def flush_pending() -> None:
    """Force an immediate write of any pending debounced updates. Call from
    app shutdown so the operator's last selection / sort / rate-limit state
    survives a quick quit."""
    global _flush_timer
    with _pending_lock:
        if _flush_timer is not None:
            try:
                _flush_timer.cancel()
            except Exception:
                pass
            _flush_timer = None
    _flush_pending()
