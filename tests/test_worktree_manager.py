"""Tests for the per-session worktree lifecycle (Stage 1).

These tests stand up a real git repository in tmp_path so we can verify
WorktreeManager actually creates / removes git worktrees instead of mocking
the git layer (the bug surface lives in the git interaction, not the Python
glue).

Covers:
  - Store migration v8 lands the worktree columns with the right defaults
  - Store.set_worktree / list_sessions_with_active_worktree round-trip
  - WorktreeManager.acquire is idempotent within and across instances
  - WorktreeManager.acquire creates the expected path + branch
  - WorktreeManager.release tears down both worktree dir and branch
  - WorktreeManager.recover_on_startup re-attaches healthy worktrees and
    marks dead ones as 'orphaned' rather than fighting with disk state
  - _slugify edge cases (empty, all-punctuation, long input, word-boundary cut)
"""

from __future__ import annotations

import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

# PySide6 isn't imported by store.py or worktree_manager.py, so no stubbing
# needed for these tests.

from holler.store import Store
from holler.worktree_manager import (
    WorktreeError,
    WorktreeManager,
    _slugify,
)


# ──────────────────────────── fixtures ────────────────────────────────


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "holler.db")
    yield db
    db.close()


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo on `master` with one initial commit.

    Why a real repo: WorktreeManager shells out to git for every op. Mocking
    subprocess would hide the failure modes (branch name collisions, dir
    pre-existence, etc.) that we specifically want to catch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # Init + first commit. -b master so we don't depend on the user's
    # init.defaultBranch.
    _run(["git", "init", "-b", "master"], cwd=repo)
    # Local-only identity so commits work in CI / clean machines.
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)
    return repo


@pytest.fixture
def git_repo_main(tmp_path):
    """A real git repo whose default branch is `main`, not `master`.

    Regression guard: `acquire` used to hardcode `master` as the fork point, so
    every worktree creation blew up with `invalid reference: master` on the
    (now default) `main`-branch repos — aborting every turn. The other fixture
    pins `master`, so only this one exercises the bug.
    """
    repo = tmp_path / "repo_main"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)
    return repo


@pytest.fixture
def mgr(git_repo, store):
    return WorktreeManager(main_repo_cwd=git_repo, store=store)


def _run(cmd: list[str], cwd: Path) -> str:
    """Shell helper for fixtures. Raises with stderr on failure."""
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=False, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {proc.stderr.strip()}")
    return proc.stdout


# ──────────────────────────── _slugify ────────────────────────────────


def test_slugify_empty():
    assert _slugify("") == "untitled"
    assert _slugify("   ") == "untitled"


def test_slugify_basic():
    assert _slugify("Fix Trust Center") == "fix-trust-center"


def test_slugify_punctuation_collapses():
    assert _slugify("Fix: trust-center  ASAP!!") == "fix-trust-center-asap"


def test_slugify_caps_length_at_24_with_word_boundary():
    # The slug logic prefers cutting at a hyphen if one exists late enough
    # in the cut window. Verify that cut + re-strip behavior.
    s = _slugify("refactor the alert fanout pipeline cleaner")
    assert len(s) <= 24
    # Word-boundary cut means no dangling partial word.
    assert not s.endswith("clea")


def test_slugify_unicode_strips():
    # Non-ASCII characters get filtered. "café" → "caf".
    assert _slugify("café update") in {"caf-update", "café-update"}
    # Either is acceptable depending on \w handling; the important thing is
    # we get a non-empty slug that's filesystem-safe.


# ──────────────────────────── store migration v8 ──────────────────────


def test_v8_migration_adds_worktree_columns(store):
    sid = store.create_session(model="opus", transcript="hello", screenshot_path=None)
    sess = store.get_session(sid)
    assert sess is not None
    # Defaults: no worktree on a fresh session.
    assert sess.worktree_path == ""
    assert sess.worktree_branch == ""
    assert sess.worktree_state == "none"


def test_set_worktree_round_trip(store):
    sid = store.create_session(model="opus", transcript="hello", screenshot_path=None)
    store.set_worktree(sid, path="/some/path", branch="holler/1-foo", state="active")
    sess = store.get_session(sid)
    assert sess.worktree_path == "/some/path"
    assert sess.worktree_branch == "holler/1-foo"
    assert sess.worktree_state == "active"


def test_list_sessions_with_active_worktree_filters_correctly(store):
    a = store.create_session(model="opus", transcript="a", screenshot_path=None)
    b = store.create_session(model="opus", transcript="b", screenshot_path=None)
    c = store.create_session(model="opus", transcript="c", screenshot_path=None)
    store.set_worktree(a, path="/wt/a", branch="holler/a-x", state="active")
    store.set_worktree(b, path="", branch="", state="merged")
    # c stays in default 'none' state.

    active = store.list_sessions_with_active_worktree()
    assert [s.id for s in active] == [a]


# ──────────────────────────── WorktreeManager.acquire ─────────────────


def test_acquire_creates_worktree_and_branch(mgr, store, git_repo):
    sid = store.create_session(model="opus", transcript="Trust center fix", screenshot_path=None)
    path = mgr.acquire(sid)
    assert path.exists()
    assert path.is_dir()
    # Dir should live under .claude/worktrees with our prefix.
    assert ".claude" in str(path)
    assert "worktrees" in str(path)
    assert path.name.startswith("holler-")
    # README.md from the seed commit should be present.
    assert (path / "README.md").exists()
    # DB row reflects the new state.
    sess = store.get_session(sid)
    assert sess.worktree_state == "active"
    assert sess.worktree_path == str(path)
    assert sess.worktree_branch.startswith("holler/")


def test_acquire_works_on_main_branch_repo(git_repo_main, store):
    """Regression: acquire must not assume the default branch is `master`.

    Forks the session branch off HEAD, so a `main`-only repo (no `master` ref)
    creates its worktree instead of aborting with `invalid reference: master`.
    """
    mgr = WorktreeManager(main_repo_cwd=git_repo_main, store=store)
    sid = store.create_session(model="opus", transcript="main branch", screenshot_path=None)
    path = mgr.acquire(sid)
    assert path.exists()
    assert (path / "README.md").exists()
    sess = store.get_session(sid)
    assert sess.worktree_state == "active"
    assert sess.worktree_branch.startswith("holler/")


def test_acquire_is_idempotent_same_path(mgr, store):
    sid = store.create_session(model="opus", transcript="something", screenshot_path=None)
    p1 = mgr.acquire(sid)
    p2 = mgr.acquire(sid)
    assert p1 == p2


def test_acquire_survives_new_manager_instance(git_repo, store):
    """A new WorktreeManager (e.g. after Holler restart) should adopt the
    existing worktree rather than creating a duplicate."""
    sid = store.create_session(model="opus", transcript="resume me", screenshot_path=None)
    mgr1 = WorktreeManager(main_repo_cwd=git_repo, store=store)
    p1 = mgr1.acquire(sid)
    # Simulate Holler restart — fresh manager, same store + repo.
    mgr2 = WorktreeManager(main_repo_cwd=git_repo, store=store)
    p2 = mgr2.acquire(sid)
    assert p1 == p2


def test_acquire_different_sessions_get_distinct_worktrees(mgr, store):
    s1 = store.create_session(model="opus", transcript="first", screenshot_path=None)
    s2 = store.create_session(model="opus", transcript="second", screenshot_path=None)
    p1 = mgr.acquire(s1)
    p2 = mgr.acquire(s2)
    assert p1 != p2
    assert p1.exists()
    assert p2.exists()


# ──────────────────────────── WorktreeManager.release ─────────────────


def test_release_removes_worktree_dir_and_branch(mgr, store, git_repo):
    sid = store.create_session(model="opus", transcript="ephemeral", screenshot_path=None)
    path = mgr.acquire(sid)
    branch = store.get_session(sid).worktree_branch

    mgr.release(sid)

    assert not path.exists()
    sess = store.get_session(sid)
    assert sess.worktree_state == "merged"
    assert sess.worktree_path == ""
    assert sess.worktree_branch == ""
    # Branch gone from the main repo.
    out = _run(["git", "branch", "--list", branch], cwd=git_repo)
    assert branch not in out


def test_release_with_uncommitted_changes_refuses_without_force(mgr, store):
    sid = store.create_session(model="opus", transcript="dirty", screenshot_path=None)
    path = mgr.acquire(sid)
    # Make the worktree dirty.
    (path / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(WorktreeError):
        mgr.release(sid)

    # Path still there, DB still active — release must be all-or-nothing.
    assert path.exists()
    assert store.get_session(sid).worktree_state == "active"


def test_release_force_drops_dirty_worktree(mgr, store):
    sid = store.create_session(model="opus", transcript="dirty force", screenshot_path=None)
    path = mgr.acquire(sid)
    (path / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    mgr.release(sid, force=True)
    assert not path.exists()
    assert store.get_session(sid).worktree_state == "merged"


# ──────────────────────────── recover_on_startup ──────────────────────


def test_recover_on_startup_with_no_active_worktrees(mgr):
    result = mgr.recover_on_startup()
    assert result == {"checked": 0, "healthy": 0, "orphaned": []}


def test_recover_on_startup_keeps_healthy_worktrees(mgr, store):
    sid = store.create_session(model="opus", transcript="healthy", screenshot_path=None)
    mgr.acquire(sid)

    result = mgr.recover_on_startup()
    assert result["checked"] == 1
    assert result["healthy"] == 1
    assert result["orphaned"] == []
    assert store.get_session(sid).worktree_state == "active"


def test_recover_on_startup_flags_orphans(git_repo, store):
    sid = store.create_session(model="opus", transcript="will-be-orphan", screenshot_path=None)
    # Stamp the DB with a worktree that doesn't exist on disk — simulates
    # Holler crashing after creating the row but before the worktree was
    # registered, or operator manually deleting the worktree dir.
    store.set_worktree(
        sid,
        path=str(git_repo / ".claude" / "worktrees" / "ghost"),
        branch="holler/0-ghost",
        state="active",
    )
    mgr = WorktreeManager(main_repo_cwd=git_repo, store=store)
    result = mgr.recover_on_startup()
    assert result["checked"] == 1
    assert result["healthy"] == 0
    assert result["orphaned"] == [sid]
    assert store.get_session(sid).worktree_state == "orphaned"


def test_list_orphans_returns_orphaned_ids(mgr, store):
    a = store.create_session(model="opus", transcript="a", screenshot_path=None)
    b = store.create_session(model="opus", transcript="b", screenshot_path=None)
    c = store.create_session(model="opus", transcript="c", screenshot_path=None)
    store.set_worktree(a, path="", branch="", state="orphaned")
    store.set_worktree(b, path="/some/wt", branch="holler/b-x", state="active")
    store.set_worktree(c, path="", branch="", state="orphaned")

    ids = mgr.list_orphans()
    assert set(ids) == {a, c}


# ──────────────────────────── construction guard ──────────────────────


def test_constructor_rejects_non_git_dir(tmp_path, store):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    with pytest.raises(WorktreeError):
        WorktreeManager(main_repo_cwd=not_a_repo, store=store)


# ──────────────────────────── regression: A4 / A6 ─────────────────────


def test_reacquire_after_worktree_dir_deleted(mgr, store, git_repo):
    """A4: the worktree dir is rm -rf'd (still registered in .git/worktrees).
    A fresh manager (cache empty) must prune stale metadata and re-create at
    the same path instead of failing with "missing but already registered"."""
    import shutil

    sid = store.create_session(model="opus", transcript="gone dir", screenshot_path=None)
    path = mgr.acquire(sid)
    branch = store.get_session(sid).worktree_branch
    shutil.rmtree(path)

    mgr2 = WorktreeManager(main_repo_cwd=git_repo, store=store)
    again = mgr2.acquire(sid)

    assert again == path
    assert again.exists()
    sess = store.get_session(sid)
    assert sess.worktree_state == "active"
    assert sess.worktree_branch == branch


def test_release_unmerged_branch_marks_orphaned_and_keeps_branch(mgr, store, git_repo):
    """A6: `git branch -d` refuses an unmerged branch — release must not
    record 'merged', and the branch (holding the work) must survive."""
    sid = store.create_session(model="opus", transcript="unmerged", screenshot_path=None)
    path = mgr.acquire(sid)
    branch = store.get_session(sid).worktree_branch
    (path / "work.txt").write_text("work\n", encoding="utf-8")
    _run(["git", "add", "work.txt"], cwd=path)
    _run(["git", "commit", "-m", "work"], cwd=path)

    mgr.release(sid)

    assert not path.exists()
    sess = store.get_session(sid)
    assert sess.worktree_state == "orphaned"
    assert sess.worktree_path == ""
    assert sess.worktree_branch == branch
    out = _run(["git", "branch", "--list", branch], cwd=git_repo)
    assert branch in out
