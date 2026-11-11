"""Lane-separated concurrency in sdk_session.

Superintendent workers hold an SDK turn slot for their entire pipeline run
(up to 45 minutes). Before lanes, they shared one semaphore with operator
PTT turns — 10 workers could occupy every slot and the operator's own
sessions queued behind them. These tests pin the fix: background jobs at
cap must never delay an interactive job, and lanes still cap themselves.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from pathlib import Path

# Stub out PySide6 / claude_agent_sdk so sdk_session imports without deps.
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

    class _QTimer:
        def __init__(self, *a, **kw):
            self.timeout = _Sig()

        def setSingleShot(self, *_a):
            pass

        def setInterval(self, *_a):
            pass

        def isActive(self):
            return False

        def start(self, *_a):
            pass

        def stop(self):
            pass

    qtcore.Signal = lambda *a, **kw: _Sig()
    qtcore.Slot = lambda *a, **kw: (lambda f: f)
    qtcore.QObject = _QObject
    qtcore.QTimer = _QTimer
    pyside.QtCore = qtcore
    sys.modules["PySide6"] = pyside
    sys.modules["PySide6.QtCore"] = qtcore

if "claude_agent_sdk" not in sys.modules:
    sdk = types.ModuleType("claude_agent_sdk")
    for name in (
        "AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
        "HookMatcher", "RateLimitEvent", "ResultMessage", "SystemMessage",
        "TextBlock", "ThinkingBlock", "ToolUseBlock", "UserMessage",
    ):
        # Permissive __init__: HookMatcher is instantiated at sdk_session import
        # time (Mac lint-hook wiring), so the stub must accept its kwargs.
        setattr(sdk, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    sys.modules["claude_agent_sdk"] = sdk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holler.sdk_session import Session, _Job  # noqa: E402


def _make_session(events: list, concurrency: int = 1) -> Session:
    return Session(
        repo_cwd=".",
        model="claude-opus-4-7",
        system_prompt_append="",
        on_event=events.append,
        concurrency=concurrency,
    )


def test_submit_lane_lands_on_job(monkeypatch):
    events: list = []
    sess = _make_session(events)
    sess.start()
    try:
        seen: list[str] = []
        done = threading.Event()

        async def _fake_execute(job):
            seen.append(job.lane)
            done.set()

        monkeypatch.setattr(sess, "_execute", _fake_execute)
        sess.submit(None, "hi", lane="background")
        assert done.wait(timeout=5)
        assert seen == ["background"]
    finally:
        sess.stop()


def test_bogus_lane_falls_back_to_interactive(monkeypatch):
    events: list = []
    sess = _make_session(events)
    sess.start()
    try:
        seen: list[str] = []
        done = threading.Event()

        async def _fake_execute(job):
            seen.append(job.lane)
            done.set()

        monkeypatch.setattr(sess, "_execute", _fake_execute)
        sess.submit(None, "hi", lane="nonsense")
        assert done.wait(timeout=5)
        assert seen == ["interactive"]
    finally:
        sess.stop()


def test_background_at_cap_does_not_block_interactive(monkeypatch):
    """With concurrency=1: one background job holds its lane's only slot;
    an interactive job submitted afterwards must still run immediately."""
    events: list = []
    sess = _make_session(events, concurrency=1)
    sess.start()
    try:
        release_bg = asyncio.Event()
        interactive_ran = threading.Event()
        bg_started = threading.Event()

        async def _fake_execute(job):
            if job.lane == "background":
                bg_started.set()
                await release_bg.wait()
            else:
                interactive_ran.set()

        monkeypatch.setattr(sess, "_execute", _fake_execute)

        sess.submit(None, "worker turn", lane="background")
        assert bg_started.wait(timeout=5), "background job never started"

        sess.submit(None, "operator ptt", lane="interactive")
        assert interactive_ran.wait(timeout=5), (
            "interactive job queued behind a background job — lanes are "
            "sharing a semaphore again"
        )

        sess._loop.call_soon_threadsafe(release_bg.set)
    finally:
        sess.stop()


def test_same_lane_still_respects_cap(monkeypatch):
    """With concurrency=1, a second background job must wait for the first."""
    events: list = []
    sess = _make_session(events, concurrency=1)
    sess.start()
    try:
        release_first = asyncio.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        order: list[str] = []

        async def _fake_execute(job):
            order.append(job.transcript)
            if job.transcript == "first":
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()

        monkeypatch.setattr(sess, "_execute", _fake_execute)

        sess.submit(None, "first", lane="background")
        assert first_started.wait(timeout=5)
        sess.submit(None, "second", lane="background")
        # Give the loop a beat — second must NOT start while first holds the slot.
        assert not second_started.wait(timeout=0.5), (
            "second background job ran while first held the only slot — "
            "the lane cap is broken"
        )
        sess._loop.call_soon_threadsafe(release_first.set)
        assert second_started.wait(timeout=5)
        assert order == ["first", "second"]
    finally:
        sess.stop()
