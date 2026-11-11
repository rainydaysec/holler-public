"""Session.stop() must leave no SDK subprocess behind.

Regression: after a restart a bundled `claude` CLI (a Superintendent worker,
mid-turn) survived holler with PPID 1. The SDK's close() can take ~10s on a
CLI that ignores stdin EOF; stop() timed out first, the loop cancelled the
close, and the SDK dropped the process from its atexit kill list. These tests
use fake clients/processes — the real CLI is never launched.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path

if "claude_agent_sdk" not in sys.modules:
    sdk = types.ModuleType("claude_agent_sdk")
    for name in (
        "AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
        "HookMatcher", "RateLimitEvent", "ResultMessage", "SystemMessage",
        "TextBlock", "ThinkingBlock", "ToolUseBlock", "UserMessage",
    ):
        setattr(sdk, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    sys.modules["claude_agent_sdk"] = sdk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holler import sdk_session  # noqa: E402
from holler.sdk_session import Session, _PoolEntry  # noqa: E402


class _FakeProc:
    """Stands in for the anyio Process behind a transport."""

    def __init__(self, ignores_term: bool = False):
        self.returncode = None
        self.ignores_term = ignores_term
        self.signals: list[str] = []
        self._exited: asyncio.Event | None = None

    def _ev(self) -> asyncio.Event:
        if self._exited is None:
            self._exited = asyncio.Event()
        return self._exited

    def _exit(self, code: int) -> None:
        self.returncode = code
        if self._exited is not None:
            self._exited.set()

    def terminate(self):
        self.signals.append("term")
        if not self.ignores_term:
            self._exit(-15)

    def kill(self):
        self.signals.append("kill")
        self._exit(-9)

    async def wait(self):
        if self.returncode is None:
            await self._ev().wait()
        return self.returncode


class _FakeClient:
    """Pooled/in-flight client whose disconnect() hangs like a CLI mid-turn
    that ignores stdin EOF — until its process dies."""

    def __init__(self, proc: _FakeProc, hang: bool = True):
        self._transport = types.SimpleNamespace(_process=proc)
        self.proc = proc
        self.hang = hang
        self.disconnects = 0

    async def disconnect(self):
        self.disconnects += 1
        if self.hang:
            await self.proc.wait()
        else:
            self.proc._exit(0)
        self._transport = None


def _session() -> Session:
    return Session(repo_cwd=".", model="m", system_prompt_append="", on_event=lambda e: None)


def _run_on(sess: Session, coro):
    return asyncio.run_coroutine_threadsafe(coro, sess._loop).result(timeout=5)


def test_stop_disconnects_pooled_and_inflight_clients_and_kills_stragglers(monkeypatch):
    sess = _session()
    sess.start()

    pooled_ok = _FakeClient(_FakeProc(), hang=False)
    pooled_stuck = _FakeClient(_FakeProc(ignores_term=True))
    inflight = _FakeClient(_FakeProc())
    started = threading.Event()

    async def _install():
        now = time.monotonic()
        for key, c in (("a", pooled_ok), ("b", pooled_stuck)):
            sess._client_pool[key] = _PoolEntry(client=c, model="m", append="", cwd=".", last_used=now)

    _run_on(sess, _install())

    # An in-flight turn: holds `inflight` until cancelled, then disconnects it
    # from its finally (the real _execute's shape).
    async def _fake_execute(job):
        sess._active_clients.add(inflight)
        try:
            started.set()
            await asyncio.sleep(3600)
        finally:
            try:
                await inflight.disconnect()
            finally:
                sess._active_clients.discard(inflight)

    monkeypatch.setattr(sess, "_execute", _fake_execute)
    sess.submit(None, "hi", lane="background")
    assert started.wait(timeout=5)

    t0 = time.monotonic()
    sess.stop()
    elapsed = time.monotonic() - t0

    assert elapsed < 3.5, f"stop() hung for {elapsed:.1f}s"
    assert pooled_ok.disconnects == 1
    assert pooled_stuck.disconnects == 1
    assert inflight.disconnects == 1
    # Every subprocess is dead; the stuck ones were signalled, the one that
    # TERM didn't stop was killed.
    for c in (pooled_ok, pooled_stuck, inflight):
        assert c.proc.returncode is not None
    assert pooled_ok.proc.signals == []
    assert inflight.proc.signals == ["term"]
    assert pooled_stuck.proc.signals == ["term", "kill"]
    assert sess._client_pool == {}


def test_stop_reaps_foreign_sdk_children_only_when_asked(monkeypatch):
    """AutoTitler's clients run on the Session loop but aren't Session-owned;
    the app composer asks stop() to sweep the SDK's live-process list too."""
    foreign = _FakeProc(ignores_term=True)
    monkeypatch.setattr(sdk_session, "_sdk_live_processes", lambda: [foreign])

    sess = _session()
    sess.start()
    sess.stop()
    assert foreign.signals == [] and foreign.returncode is None

    sess2 = _session()
    sess2.start()
    sess2.stop(reap_all_sdk_children=True)
    assert foreign.signals == ["term", "kill"]
    assert foreign.returncode is not None


def test_execute_tracks_inflight_client_until_disposed(monkeypatch):
    """_execute registers its client so stop() can find the subprocess of a
    turn that is mid-flight (it's not in the pool until the turn ends)."""
    sess = _session()
    seen_during_turn: list[bool] = []
    created: list = []

    class _Client:
        def __init__(self, options=None):
            created.append(self)

        async def connect(self):
            pass

        async def query(self, gen):
            async for _ in gen:
                pass

        async def receive_messages(self):
            seen_during_turn.append(self in sess._active_clients)
            return
            yield  # noqa: async generator

        async def disconnect(self):
            seen_during_turn.append(self in sess._active_clients)

    monkeypatch.setattr(sdk_session, "ClaudeSDKClient", _Client)
    monkeypatch.setattr(sdk_session, "ClaudeAgentOptions", lambda **kw: kw)
    job = sdk_session._Job(id=1, image_b64=None, transcript="hi", submitted_at=0.0)
    asyncio.run(sess._execute(job))

    assert seen_during_turn == [True, True]
    assert sess._active_clients == set()
