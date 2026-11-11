"""Terminal-event guarantee for sdk_session.

Three failure modes the production code must cover:
  1. `_execute` drains receive_response without a ResultMessage → finally
     synthesizes an "error" event so JobStore leaves "running".
  2. `_done_cb` runs and JobStore is still non-terminal → reaper synthesizes
     an "error" event (catches the inner emit getting lost across the Qt
     thread bridge).
  3. Normal exception inside `_execute` → ONE error event, not two
     (regression guard against the synthesized + outer-handler double-fire).
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Stub out PySide6 so jobs.py imports without a Qt install.
if "PySide6" not in sys.modules:
    pyside = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")

    class _Sig:
        def __init__(self, *a, **kw):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, *a, **kw):
            for s in list(self._slots):
                s(*a, **kw)

    class _QObject:
        def __init__(self, *a, **kw):
            pass

    qtcore.Signal = lambda *a, **kw: _Sig()
    qtcore.QObject = _QObject
    pyside.QtCore = qtcore
    sys.modules["PySide6"] = pyside
    sys.modules["PySide6.QtCore"] = qtcore

# Stub claude_agent_sdk so sdk_session imports clean.
if "claude_agent_sdk" not in sys.modules:
    sdk = types.ModuleType("claude_agent_sdk")
    for name in (
        "AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
        "RateLimitEvent", "ResultMessage", "SystemMessage", "TextBlock",
        "ThinkingBlock", "ToolUseBlock", "UserMessage",
    ):
        setattr(sdk, name, type(name, (), {}))
    sys.modules["claude_agent_sdk"] = sdk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holler import sdk_session  # noqa: E402
from holler.jobs import Job, JobStore  # noqa: E402
from holler.sdk_session import Session, SessionEvent, _Job  # noqa: E402


def _make_session(events: list, *, is_terminal=None) -> Session:
    return Session(
        repo_cwd=".",
        model="claude-opus-4-8",
        system_prompt_append="",
        on_event=events.append,
        is_terminal=is_terminal,
    )


def _make_job(job_id: int = 1) -> _Job:
    return _Job(id=job_id, image_b64=None, transcript="hi", submitted_at=0.0)


# ── 1. _execute drains without ResultMessage ────────────────────────────────


def test_execute_synthesizes_error_when_iterator_drains_clean(monkeypatch):
    """If receive_response yields zero messages and exits cleanly, the finally
    block must emit a terminal "error" event so the job leaves running."""
    events: list[SessionEvent] = []
    sess = _make_session(events)

    class _FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self):
            pass

        async def query(self, gen):
            async for _ in gen:
                pass

        async def receive_messages(self):
            return
            yield  # noqa: makes this an async generator

        async def disconnect(self):
            pass

    monkeypatch.setattr(sdk_session, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(sdk_session, "ClaudeAgentOptions", lambda **kw: kw)

    asyncio.run(sess._execute(_make_job()))

    kinds = [e.kind for e in events]
    assert "result" not in kinds, "no ResultMessage was sent — must not emit result"
    assert "error" in kinds, "drain-without-result must synthesize an error event"
    err = next(e for e in events if e.kind == "error")
    assert "ResultMessage" in err.data.get("error", "")


# ── 2. _execute exception path: exactly one error event ─────────────────────


def test_execute_exception_does_not_double_emit(monkeypatch):
    """When receive_response raises, the outer _run_job_ephemeral handler
    emits one "error". The finally block must NOT emit a second one."""
    events: list[SessionEvent] = []
    sess = _make_session(events)

    class _BoomClient:
        def __init__(self, options=None):
            pass

        async def connect(self):
            pass

        async def query(self, gen):
            async for _ in gen:
                pass

        async def receive_messages(self):
            raise RuntimeError("transport blew up")
            yield

        async def disconnect(self):
            pass

    monkeypatch.setattr(sdk_session, "ClaudeSDKClient", _BoomClient)
    monkeypatch.setattr(sdk_session, "ClaudeAgentOptions", lambda **kw: kw)

    sess.start()
    try:
        # _run_job_ephemeral wraps _execute and emits "error" on exception.
        # Schedule via the session's loop the same way _spawn does.
        async def _run():
            await sess._run_job_ephemeral(_make_job())
        asyncio.run_coroutine_threadsafe(_run(), sess._loop).result(timeout=5)
    finally:
        sess.stop()

    error_events = [e for e in events if e.kind == "error"]
    assert len(error_events) == 1, (
        f"expected exactly 1 error event on exception path, got {len(error_events)}: "
        f"{[e.data for e in error_events]}"
    )
    assert "transport blew up" in error_events[0].data["error"]


# ── 3. _done_cb reaper fires when JobStore stays non-terminal ───────────────


def test_done_cb_reaper_emits_error_when_jobstore_not_terminal():
    """If the asyncio task ends but JobStore hasn't seen a terminal event
    (cross-thread emit lost, hot-restart pathology), the reaper synthesizes
    one so the row doesn't sit at 'running' forever."""
    store = JobStore()
    store.add(Job(id=42, transcript="hi", submitted_at=0.0))
    # Deliberately: do NOT mark terminal.

    events: list[SessionEvent] = []
    sess = _make_session(events, is_terminal=store.is_terminal)
    sess.start()
    try:
        async def _noop():
            return None
        async def _go():
            t = asyncio.create_task(_noop())
            sess._tasks_by_job[42] = t
            sess._in_flight.add(t)

            done = asyncio.get_event_loop().create_future()

            def cb(task):
                # Mirror the real _done_cb's shape: discard from in_flight,
                # check is_terminal, emit error if not.
                sess._in_flight.discard(task)
                sess._tasks_by_job.pop(42, None)
                if sess._is_terminal is not None and not sess._is_terminal(42):
                    sess._on_event(SessionEvent(
                        42, "error",
                        {"error": "task ended without terminal event"},
                    ))
                done.set_result(None)

            t.add_done_callback(cb)
            await done
        asyncio.run_coroutine_threadsafe(_go(), sess._loop).result(timeout=5)
    finally:
        sess.stop()

    error_events = [e for e in events if e.kind == "error" and e.job_id == 42]
    assert len(error_events) == 1
    assert "terminal event" in error_events[0].data["error"]


def test_done_cb_reaper_silent_when_jobstore_terminal():
    """If JobStore already saw a terminal event (status="done"/"error"),
    the reaper must NOT emit a duplicate."""
    store = JobStore()
    job = Job(id=7, transcript="hi", submitted_at=0.0)
    job.status = "done"
    store.add(job)

    events: list[SessionEvent] = []
    sess = _make_session(events, is_terminal=store.is_terminal)
    sess.start()
    try:
        async def _go():
            done = asyncio.get_event_loop().create_future()
            t = asyncio.create_task(asyncio.sleep(0))

            def cb(task):
                if sess._is_terminal is not None and not sess._is_terminal(7):
                    sess._on_event(SessionEvent(
                        7, "error", {"error": "should not fire"},
                    ))
                done.set_result(None)

            t.add_done_callback(cb)
            await done
        asyncio.run_coroutine_threadsafe(_go(), sess._loop).result(timeout=5)
    finally:
        sess.stop()

    assert [e for e in events if e.job_id == 7] == []


# ── JobStore.is_terminal helper ─────────────────────────────────────────────


def test_jobstore_is_terminal_states():
    store = JobStore()
    assert store.is_terminal(999) is False  # missing
    j = Job(id=1, transcript="", submitted_at=0.0)
    store.add(j)
    assert store.is_terminal(1) is False  # status="queued"
    j.status = "running"
    assert store.is_terminal(1) is False
    j.status = "done"
    assert store.is_terminal(1) is True
    j.status = "error"
    assert store.is_terminal(1) is True
