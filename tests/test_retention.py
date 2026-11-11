"""Tests for the rolling retention purge of reviewed (Done) sessions.

These stand up a real Store on a tmp SQLite DB plus real capture PNGs and a
real Claude-projects JSONL on disk, so we verify the purge actually deletes the
right rows AND the right files — the bug surface is the filesystem side-effect,
not the Python glue, so nothing here is mocked.

Covers:
  - a reviewed session older than the cutoff is purged: DB row gone, its capture
    PNGs gone, its SDK transcript JSONL gone, and its turns/tool_calls cascade
  - a recently-reviewed session (inside the grace window) is retained
  - an un-reviewed (still active) session is never touched
  - a pinned reviewed session is exempt even when past the cutoff
  - a running reviewed session is exempt
  - the sandbox guard: a screenshot_path pointing outside captures_dir is
    refused (file left intact) while the DB row still purges
"""

from __future__ import annotations

import time
from pathlib import Path

from holler.store import Store
from holler.retention import purge_reviewed_sessions


DAY = 86400.0


def _mk_png(captures_dir: Path, name: str) -> str:
    p = captures_dir / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    return str(p)


def _mk_jsonl(projects_dir: Path, slug: str, sdk_id: str) -> Path:
    d = projects_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sdk_id}.jsonl"
    p.write_text('{"turn": 1}\n', encoding="utf-8")
    return p


def _set_reviewed(
    store: Store, session_id: int, reviewed_at: float, status: str = "idle"
) -> None:
    """Directly stamp reviewed_at (the public mark_reviewed only sets 'now').

    Also pins status — a genuinely completed session is 'idle', but add_turn
    leaves it 'running', so callers set the realistic terminal state here."""
    c = store._conn()
    with c:
        c.execute(
            "UPDATE sessions SET reviewed_at = ?, status = ? WHERE id = ?",
            (reviewed_at, status, session_id),
        )


def _make_env(tmp: Path):
    captures = tmp / "captures"
    captures.mkdir()
    projects = tmp / "projects"
    projects.mkdir()
    store = Store(tmp / "holler.db")
    return store, captures, projects


def test_old_reviewed_session_is_fully_purged(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()

    thumb = _mk_png(captures, "sess.png")
    sid = store.create_session(
        model="m", transcript="hi", screenshot_path=thumb, status="idle"
    )
    turn_thumb = _mk_png(captures, "turn.png")
    store.add_turn(session_id=sid, transcript="t", screenshot_path=turn_thumb, job_id=None)
    store.attach_sdk_session_id(sid, "uuid-old")
    jsonl = _mk_jsonl(projects, "some-project", "uuid-old")
    _set_reviewed(store, sid, now - 5 * DAY)

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )

    assert result["sessions"] == 1, result
    assert result["files"] == 3, result  # session png + turn png + jsonl
    assert store.get_session(sid) is None
    assert store.get_turns(sid) == []
    assert not Path(thumb).exists()
    assert not Path(turn_thumb).exists()
    assert not jsonl.exists()


def test_recent_reviewed_session_is_retained(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    thumb = _mk_png(captures, "recent.png")
    sid = store.create_session(model="m", transcript="hi", screenshot_path=thumb)
    _set_reviewed(store, sid, now - 1 * DAY)  # inside the 3-day grace window

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )

    assert result["sessions"] == 0
    assert store.get_session(sid) is not None
    assert Path(thumb).exists()


def test_unreviewed_session_is_never_touched(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    thumb = _mk_png(captures, "active.png")
    sid = store.create_session(model="m", transcript="hi", screenshot_path=thumb)
    # never marked reviewed -> reviewed_at stays NULL

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )

    assert result["sessions"] == 0
    assert store.get_session(sid) is not None
    assert Path(thumb).exists()


def test_pinned_reviewed_session_is_exempt(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    sid = store.create_session(model="m", transcript="hi", screenshot_path=None)
    _set_reviewed(store, sid, now - 10 * DAY)
    with store._conn() as c:
        c.execute("UPDATE sessions SET pinned = 1 WHERE id = ?", (sid,))

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )

    assert result["sessions"] == 0
    assert store.get_session(sid) is not None


def test_running_reviewed_session_is_exempt(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    sid = store.create_session(
        model="m", transcript="hi", screenshot_path=None, status="running"
    )
    _set_reviewed(store, sid, now - 10 * DAY, status="running")

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )

    assert result["sessions"] == 0
    assert store.get_session(sid) is not None


def test_out_of_sandbox_path_is_refused_but_row_purges(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    # A screenshot_path OUTSIDE captures_dir must never be deleted.
    outsider = tmp_path / "important.txt"
    outsider.write_text("do not delete", encoding="utf-8")
    sid = store.create_session(
        model="m", transcript="hi", screenshot_path=str(outsider)
    )
    _set_reviewed(store, sid, now - 5 * DAY)

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )

    assert result["sessions"] == 1
    assert result["files"] == 0  # refused to delete the out-of-sandbox file
    assert outsider.exists()  # still there
    assert store.get_session(sid) is None  # DB row still purged


# ──────────────── regression: A5 (worktrees) / A7 (shared captures) ────────


class _FakeWM:
    def __init__(self, store, fail=False):
        self.store = store
        self.fail = fail
        self.calls = []

    def release(self, sid, *, force=False, final_state="merged"):
        self.calls.append((sid, force, final_state))
        if self.fail:
            raise RuntimeError("dirty worktree")
        self.store.set_worktree(sid, path="", branch="", state=final_state)


def _active_worktree_session(store, tmp: Path) -> int:
    sid = store.create_session(model="m", transcript="wt", screenshot_path=None)
    store.set_worktree(sid, path=str(tmp / "wt"), branch="holler/x", state="active")
    return sid


def test_purge_releases_active_worktree_as_orphaned(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    sid = _active_worktree_session(store, tmp_path)
    _set_reviewed(store, sid, now - 5 * DAY)
    wm = _FakeWM(store)

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects,
        worktree_manager=wm,
    )

    assert wm.calls == [(sid, False, "orphaned")]  # never forced
    assert result["sessions"] == 1
    assert store.get_session(sid) is None


def test_purge_keeps_row_when_worktree_release_fails(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    thumb = _mk_png(captures, "wt.png")
    sid = _active_worktree_session(store, tmp_path)
    with store._conn() as c:
        c.execute("UPDATE sessions SET screenshot_path = ? WHERE id = ?", (thumb, sid))
    _set_reviewed(store, sid, now - 5 * DAY)

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects,
        worktree_manager=_FakeWM(store, fail=True),
    )

    assert result["sessions"] == 0
    sess = store.get_session(sid)
    assert sess is not None and sess.worktree_state == "active"
    assert Path(thumb).exists()


def test_purge_without_worktree_manager_still_works(tmp_path: Path) -> None:
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    sid = _active_worktree_session(store, tmp_path)
    _set_reviewed(store, sid, now - 5 * DAY)

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )
    assert result["sessions"] == 1


def test_shared_capture_is_not_unlinked(tmp_path: Path) -> None:
    """A7: a capture still referenced by another session, a turn of another
    session, or a superintendent_evidence row must survive the purge."""
    store, captures, projects = _make_env(tmp_path)
    now = time.time()
    shared_sess = _mk_png(captures, "shared_sess.png")
    shared_turn = _mk_png(captures, "shared_turn.png")
    shared_ev = _mk_png(captures, "shared_ev.png")
    shared_ev_multi = _mk_png(captures, "shared_ev_multi.png")
    own = _mk_png(captures, "own.png")

    doomed = store.create_session(model="m", transcript="x", screenshot_path=shared_sess)
    for p in (shared_turn, shared_ev, shared_ev_multi, own):
        store.add_turn(session_id=doomed, transcript="t", screenshot_path=p, job_id=None)
    _set_reviewed(store, doomed, now - 5 * DAY)

    keeper = store.create_session(model="m", transcript="y", screenshot_path=shared_sess)
    store.add_turn(session_id=keeper, transcript="t", screenshot_path=shared_turn, job_id=None)
    store.add_evidence(transcript="e", screenshot_path=shared_ev,
                       inbox_session_id=None, inbox_turn_id=None)
    store.add_evidence_multi(transcript="e2", screenshot_paths=["/elsewhere.png", shared_ev_multi],
                             inbox_session_id=None)

    result = purge_reviewed_sessions(
        store, captures, now - 3 * DAY, claude_projects_dir=projects
    )

    assert result["sessions"] == 1
    assert result["files"] == 1
    assert not Path(own).exists()
    for p in (shared_sess, shared_turn, shared_ev, shared_ev_multi):
        assert Path(p).exists(), p


if __name__ == "__main__":
    import tempfile
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
                print(f"PASS  {fn.__name__}")
            except Exception:
                failed += 1
                print(f"FAIL  {fn.__name__}")
                traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
