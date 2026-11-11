"""Regression tests for runtime/SDK fixes (catalogue group B).

No real claude CLI is launched: SDK clients/options are monkeypatched fakes.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holler import sdk_session  # noqa: E402
from holler.sdk_session import Session, SessionEvent, _Job, _PoolEntry  # noqa: E402


def _session(events, **kw) -> Session:
    return Session(
        repo_cwd=kw.pop("repo_cwd", "."),
        model="claude-opus-4-8",
        system_prompt_append="",
        on_event=events.append,
        **kw,
    )


class _FakeClient:
    def __init__(self, options=None):
        self.options = options

    async def connect(self):
        pass

    async def query(self, gen):
        async for _ in gen:
            pass

    async def receive_messages(self):
        return
        yield  # noqa

    async def disconnect(self):
        pass


def _run_execute_capture_opts(monkeypatch, sess, job) -> dict:
    captured: dict = {}

    def _opts(**kw):
        captured.update(kw)
        return kw

    monkeypatch.setattr(sdk_session, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(sdk_session, "ClaudeAgentOptions", _opts)
    asyncio.run(sess._execute(job))
    return captured


# ── B1: max_buffer_size ─────────────────────────────────────────────────────


def test_options_set_generous_max_buffer_size(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    sess = _session([], repo_cwd=str(tmp_path))
    opts = _run_execute_capture_opts(
        monkeypatch, sess, _Job(id=1, image_b64=None, transcript="hi", submitted_at=0.0),
    )
    assert opts["max_buffer_size"] >= 32 * 1024 * 1024


# ── B10: lint hook dedupe ───────────────────────────────────────────────────


def _write_lint_settings(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PostToolUse": [{
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{"type": "command", "command": "python3 /x/.claude/hooks/lint_on_edit.py"}],
    }]}}))


def test_in_process_lint_hook_used_when_settings_lack_it(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sess = _session([], repo_cwd=str(tmp_path))
    opts = _run_execute_capture_opts(
        monkeypatch, sess, _Job(id=1, image_b64=None, transcript="hi", submitted_at=0.0),
    )
    assert opts.get("hooks") is sdk_session._LINT_HOOKS
    # User settings are never disabled (skills / CLAUDE.md parity).
    assert opts.get("setting_sources") is None


@pytest.mark.parametrize("where", ["user", "project", "local"])
def test_in_process_lint_hook_skipped_when_settings_register_it(monkeypatch, tmp_path, where):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = {
        "user": home / ".claude" / "settings.json",
        "project": repo / ".claude" / "settings.json",
        "local": repo / ".claude" / "settings.local.json",
    }[where]
    _write_lint_settings(target)
    sess = _session([], repo_cwd=str(repo))
    opts = _run_execute_capture_opts(
        monkeypatch, sess, _Job(id=1, image_b64=None, transcript="hi", submitted_at=0.0),
    )
    assert "hooks" not in opts
    assert opts.get("setting_sources") is None


# ── B2: cancelled queued job must not stay the merge target ─────────────────


def test_cancel_queued_job_then_submit_is_not_merged_into_dead_job(monkeypatch):
    events: list[SessionEvent] = []
    sess = _session(events, concurrency=4)
    sess.start()
    try:
        release_a = threading.Event()
        a_started = threading.Event()
        executed: list[int] = []

        async def _fake_execute(job):
            sess._release_pending_slot(job)  # what the real _execute does first
            executed.append(job.id)
            if job.transcript == "A":
                a_started.set()
                while not release_a.is_set():
                    await asyncio.sleep(0.01)

        monkeypatch.setattr(sess, "_execute", _fake_execute)

        job_a = sess.submit(None, "A", resume_session_id="S")
        assert a_started.wait(5)
        # B queues behind A on the per-session resume lock → it's the merge target.
        job_b = sess.submit(None, "B", resume_session_id="S")
        assert sess._pending_by_sid.get("S").id == job_b
        assert sess.cancel_job(job_b)
        deadline = time.time() + 5
        while job_b in sess._tasks_by_job and time.time() < deadline:
            time.sleep(0.01)
        assert "S" not in sess._pending_by_sid

        job_c = sess.submit(None, "C", resume_session_id="S")
        assert job_c != job_b, "new PTT was merged into the cancelled job"
        assert not [e for e in events if e.kind == "merged_in"]
        release_a.set()
        deadline = time.time() + 5
        while job_c not in executed and time.time() < deadline:
            time.sleep(0.01)
        assert executed == [job_a, job_c]
    finally:
        sess.stop()


def test_turns_on_same_local_session_without_sdk_id_are_serialized(monkeypatch):
    """A resumed session whose sdk id hasn't attached yet goes out with
    resume_session_id=None; two quick turns into it must still run one at a
    time (same worktree, same transcript), not concurrently."""
    sess = _session([], concurrency=4)
    sess.start()
    try:
        release_first = threading.Event()
        first_started = threading.Event()
        running: list[str] = []
        overlap: list[bool] = []

        async def _fake_execute(job):
            sess._release_pending_slot(job)
            overlap.append(bool(running))
            running.append(job.transcript)
            if job.transcript == "first":
                first_started.set()
                while not release_first.is_set():
                    await asyncio.sleep(0.01)
            running.remove(job.transcript)

        monkeypatch.setattr(sess, "_execute", _fake_execute)

        sess.submit(None, "first", local_session_id=7)
        assert first_started.wait(5)
        second = sess.submit(None, "second", local_session_id=7)
        time.sleep(0.1)
        assert running == ["first"], "second turn ran concurrently with the first"
        release_first.set()
        deadline = time.time() + 5
        while second in sess._tasks_by_job and time.time() < deadline:
            time.sleep(0.01)
        assert overlap == [False, False]
        assert not sess._resume_locks and not sess._resume_lock_refs
    finally:
        sess.stop()


def test_task_cancelled_before_running_releases_merge_slot():
    """A task cancelled before its coroutine runs never enters
    _run_job_ephemeral; _done_cb must still release the slot."""
    sess = _session([])
    job = _Job(id=9, image_b64=None, transcript="x", submitted_at=0.0, resume_session_id="S")
    sess._pending_by_sid["S"] = job

    async def _go():
        sess._semaphores = {"interactive": asyncio.Semaphore(1)}
        await sess._spawn(job)
        sess._tasks_by_job[9].cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert "S" not in sess._pending_by_sid
    assert job.fired


# ── B11: reaper pops all victims before awaiting disconnect ─────────────────


def test_reaper_pops_victims_before_disconnecting():
    sess = _session([])
    seen_pool_sizes: list[int] = []

    class _C:
        async def disconnect(self):
            seen_pool_sizes.append(len(sess._client_pool))
            await asyncio.sleep(0)

    old = time.monotonic() - sdk_session._POOL_IDLE_TIMEOUT_S - 10
    for k in ("a", "b"):
        sess._client_pool[k] = _PoolEntry(client=_C(), model="m", append="", cwd=".", last_used=old)
    sess._client_pool["fresh"] = _PoolEntry(
        client=_C(), model="m", append="", cwd=".", last_used=time.monotonic(),
    )

    async def _go():
        busy = asyncio.Lock()
        await busy.acquire()
        sess._resume_locks["b"] = busy  # in-flight turn → must be deferred
        await sess._reap_idle_once()

    asyncio.run(_go())
    assert set(sess._client_pool) == {"b", "fresh"}
    assert seen_pool_sizes == [2]  # "a" already removed when its disconnect ran


# ── B12: replay excludes the in-flight turn ─────────────────────────────────


def _turn(id_, seq, text, final=""):
    from holler.domain import Turn
    return Turn(id=id_, session_id=1, seq=seq, transcript=text, screenshot_path=None,
                final_text=final, duration_ms=0, started_at=0.0, ended_at=0.0,
                is_error=False, job_id=None)


class _ReplayStore:
    def __init__(self, turns):
        self.turns = turns
        self.tool_call_ids = None

    def get_turns(self, sid):
        return list(self.turns)

    def get_tool_calls_for_turns(self, ids):
        self.tool_call_ids = ids
        return {}


def test_replay_excludes_current_and_later_turns():
    from holler.replay import load_replay_from_store
    store = _ReplayStore([
        _turn(10, 1, "first question", "first answer"),
        _turn(11, 2, "CURRENT REQUEST"),
        _turn(12, 3, "MERGED EXTRA"),
    ])
    text = load_replay_from_store(store, 1, 11)
    assert "first question" in text
    assert "CURRENT REQUEST" not in text and "MERGED EXTRA" not in text
    assert store.tool_call_ids == [10]
    # First turn of a session: nothing prior → no replay at all.
    assert load_replay_from_store(store, 1, 10) == ""
    # Legacy call without a turn id still replays everything.
    assert "CURRENT REQUEST" in load_replay_from_store(store, 1)


def test_session_passes_current_turn_to_replay_loader():
    calls = []
    sess = _session([], replay_loader=lambda sid, before_turn_id=None: calls.append((sid, before_turn_id)) or "")
    job = _Job(id=1, image_b64=None, transcript="x", submitted_at=0.0,
               local_session_id=5, local_turn_id=42)
    sess._call_replay_loader(job)
    assert calls == [(5, 42)]
    legacy = []
    sess2 = _session([], replay_loader=lambda sid: legacy.append(sid) or "")
    sess2._call_replay_loader(job)
    assert legacy == [5]


# ── B5: screenshot dedup only on live pooled client reuse ───────────────────


def test_will_reuse_client_conservative():
    sess = _session([])
    assert not sess.will_reuse_client(None)
    assert not sess.will_reuse_client("S")  # no pooled client → fresh subprocess
    sess._client_pool["S"] = _PoolEntry(
        client=object(), model="claude-opus-4-8", append="", cwd=".", last_used=time.monotonic(),
    )
    assert sess.will_reuse_client("S", system_prompt_append_override="")
    assert not sess.will_reuse_client("S", model_override="claude-sonnet-5", system_prompt_append_override="")
    assert not sess.will_reuse_client("S", system_prompt_append_override="profile changed")
    sess._resume_lock_refs["S"] = 1  # a turn queued/in flight could evict it
    assert not sess.will_reuse_client("S", system_prompt_append_override="")
    sess._resume_lock_refs.clear()
    sess._client_pool["S"].last_used = time.monotonic() - sdk_session._POOL_IDLE_TIMEOUT_S + 5
    assert not sess.will_reuse_client("S", system_prompt_append_override="")  # about to be reaped


def test_screen_unchanged_requires_reuse():
    full = pytest.importorskip("holler.runners.full")
    state: dict = {}
    reuse = {"v": False}
    fake = SimpleNamespace(will_reuse_client=lambda *a, **k: reuse["v"])
    kw = dict(resume_session_id="S", model_override=None, append_override="")
    assert not full._screen_unchanged_for_turn(state, fake, 1, "fp", **kw)  # first
    assert not full._screen_unchanged_for_turn(state, fake, 1, "fp", **kw)  # same fp, fresh client
    reuse["v"] = True
    assert full._screen_unchanged_for_turn(state, fake, 1, "fp", **kw)
    assert not full._screen_unchanged_for_turn(state, fake, 1, "fp2", **kw)
    assert not full._screen_unchanged_for_turn(state, fake, None, "fp2", **kw)


# ── B4: selected session continues without sdk_session_id ───────────────────


class _RoutingStore:
    def __init__(self, sessions):
        self.sessions = {s.id: s for s in sessions}
        self.turns: list = []
        self.created: list = []

    def list_sessions(self, limit=50):
        return []  # active session not among "recent" candidates

    def get_session(self, sid):
        return self.sessions.get(sid)

    def add_turn(self, session_id, transcript, screenshot_path, job_id):
        self.turns.append(session_id)
        return 100 + len(self.turns)

    def create_session(self, **kw):
        self.created.append(kw)
        return 999


def test_selected_session_without_sdk_id_is_continued():
    full = pytest.importorskip("holler.runners.full")
    row = SimpleNamespace(id=7, title="old", sdk_session_id=None, archived=False, profile=None)
    store = _RoutingStore([row])
    state = {"active_session_id": 7, "persistent_store": store}
    sid, tid, resume, text = full._resolve_routing(
        store, state, "do it", screenshot_path=None, effective_model="m", notice=lambda m: None,
    )
    assert (sid, resume, text) == (7, None, "do it")
    assert store.created == [] and store.turns == [7]


def test_selected_session_with_sdk_id_outside_top50_resumes():
    full = pytest.importorskip("holler.runners.full")
    row = SimpleNamespace(id=7, title="old", sdk_session_id="sdk-7", archived=False, profile=None)
    store = _RoutingStore([row])
    state = {"active_session_id": 7, "persistent_store": store}
    sid, _tid, resume, _ = full._resolve_routing(
        store, state, "go", screenshot_path=None, effective_model="m", notice=lambda m: None,
    )
    assert (sid, resume) == (7, "sdk-7")


def test_deleted_selected_session_starts_new():
    full = pytest.importorskip("holler.runners.full")
    store = _RoutingStore([])
    state = {"active_session_id": 7, "persistent_store": store}
    sid, _tid, resume, _ = full._resolve_routing(
        store, state, "go", screenshot_path=None, effective_model="m", notice=lambda m: None,
    )
    assert sid == 999 and resume is None and len(store.created) == 1


# ── B13 / B14 / B6 helpers ──────────────────────────────────────────────────


def test_closing_thread_db_closes_even_on_error():
    full = pytest.importorskip("holler.runners.full")
    closed = []
    store = SimpleNamespace(close_thread_connection=lambda: closed.append(1))

    def boom():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        full._closing_thread_db(store, boom)()
    assert closed == [1]
    assert full._closing_thread_db(store, lambda a: a * 2)(3) == 6
    assert closed == [1, 1]


def test_purge_receives_worktree_manager():
    full = pytest.importorskip("holler.runners.full")
    got = {}

    def purge_new(store, captures, cutoff, *, claude_projects_dir=None, worktree_manager=None):
        got["wm"] = worktree_manager
        return {"sessions": 0, "files": 0}

    wm = object()
    full._call_purge_reviewed(purge_new, None, Path("."), 0.0, wm)
    assert got["wm"] is wm


def test_swap_worktree_manager_on_repo_change():
    full = pytest.importorskip("holler.runners.full")
    sess = _session([], worktree_manager="OLD")
    holder = {"mgr": "OLD"}

    class _Mgr:
        def __init__(self, main_repo_cwd, store):
            if "bad" in str(main_repo_cwd):
                raise RuntimeError("not a git checkout")
            self.main = main_repo_cwd

    msg = full._swap_worktree_manager(sess, holder, Path("/bad"), None, manager_cls=_Mgr)
    assert sess.worktree_manager == "OLD" and holder["mgr"] == "OLD" and "Couldn't use" in msg
    full._swap_worktree_manager(sess, holder, Path("/new"), None, manager_cls=_Mgr)
    assert sess.worktree_manager.main == Path("/new") and holder["mgr"] is sess.worktree_manager


# ── B8: second terminal event ignored ───────────────────────────────────────


def test_second_terminal_event_is_ignored(monkeypatch):
    from holler import jobs
    monkeypatch.setattr(jobs._state, "load", lambda: {})
    store = jobs.JobStore()
    persisted = []
    monkeypatch.setattr(store, "_persist_job_completion", lambda job: persisted.append(job.status))
    store.apply_event(SessionEvent(1, "submitted", {"transcript": "hi"}))
    store.apply_event(SessionEvent(1, "result", {"is_error": False, "result": "ok"}))
    store.apply_event(SessionEvent(1, "error", {"error": "task ended without terminal event"}))
    store.apply_event(SessionEvent(1, "result", {"is_error": False, "result": "again"}))
    job = store.get(1)
    assert job.status == "done" and job.final_text == "ok" and not job.error
    assert persisted == ["done"]
