"""Multi-repo sessions (plan task 1.1).

A session can target a configured project repo (e.g. myproject) instead of
`cfg.repo_cwd`. These tests use two real temp git repos, each cloned from a
local bare "origin":

  - `holler`  — origin default branch `main` (the default repo, cfg.repo_cwd)
  - `myproject`  — origin default branch `trunk` (a project repo with a
                non-main/master default), whose live checkout is parked on an
                unrelated feature branch, like the operator's real clone.

Covered: config defaults keep behaviour identical with no projects; project
resolution; store round-trip + idempotent migration; per-repo worktree
managers (path, fork point, no upstream tracking); per-repo recovery and
release; Superintendent dispatch pinning workers to the project repo; and
`_branch_landed` checking the SESSION's repo on its own default branch.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from holler import config as config_mod
from holler import sdk_session
from holler.projects import (
    UnknownProjectError,
    project_name_for_repo,
    resolve_session_repo,
)
from holler.sdk_session import Session, _Job
from holler.store import Store
from holler.superintendent.dispatcher import SuperintendentDispatcher
from holler.worktree_manager import (
    WorktreeManager,
    WorktreeRegistry,
    detect_default_branch,
)


# ──────────────────────────── helpers / fixtures ────────────────────────────

_IDENT = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _git(*args, cwd) -> str:
    p = subprocess.run(
        ["git", *_IDENT, *args], cwd=str(cwd), capture_output=True, text=True, timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError(f"git {args} failed: {p.stderr}")
    return p.stdout.strip()


def _cloned_repo(tmp_path: Path, name: str, default: str) -> Path:
    origin = tmp_path / f"{name}-origin.git"
    _git("init", "--bare", "-b", default, str(origin), cwd=tmp_path)
    seed = tmp_path / f"{name}-seed"
    _git("init", "-b", default, str(seed), cwd=tmp_path)
    (seed / "README.md").write_text(f"{name}\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "origin", default, cwd=seed)
    clone = tmp_path / name
    _git("clone", str(origin), str(clone), cwd=tmp_path)
    return clone


@pytest.fixture
def holler_repo(tmp_path):
    return _cloned_repo(tmp_path, "holler", "main")


@pytest.fixture
def project_repo(tmp_path):
    repo = _cloned_repo(tmp_path, "myproject", "trunk")
    # The operator's live clone sits on an unrelated feature branch with a
    # commit that is NOT on the default branch.
    _git("checkout", "-b", "feature-x", cwd=repo)
    (repo / "wip.txt").write_text("operator wip\n")
    _git("add", "wip.txt", cwd=repo)
    _git("commit", "-m", "operator wip", cwd=repo)
    return repo


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "holler.db")
    yield s
    s.close()


def _cfg(holler_repo, project_repo=None, **kw):
    projects = {"myproject": str(project_repo)} if project_repo else {}
    return SimpleNamespace(
        repo_cwd=holler_repo, projects=projects, profile_projects={},
        superintendent_model="", claude_model="m", **kw,
    )


# ──────────────────────────── config / resolution ────────────────────────────


def test_default_config_has_no_projects(tmp_path):
    cfg = config_mod.load(tmp_path / "absent.toml")
    assert cfg.projects == {} and cfg.profile_projects == {}
    for profile in (None, "superintendent", "investigation", "ui-ux", "engineering"):
        assert resolve_session_repo(cfg, profile=profile) is None


def test_config_projects_round_trip_through_fallback_serializer(tmp_path, monkeypatch):
    cfg = config_mod.load(tmp_path / "absent.toml")
    cfg.projects = {"myproject": "/x/myproject", "two words": "/y"}
    cfg.profile_projects = {"superintendent": "myproject"}
    import builtins
    real_import = builtins.__import__

    def no_tomli_w(name, *a, **k):
        if name == "tomli_w":
            raise ImportError
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_tomli_w)
    path = tmp_path / "config.toml"
    config_mod.save(cfg, path)
    monkeypatch.setattr(builtins, "__import__", real_import)
    back = config_mod.load(path)
    assert back.projects == {"myproject": "/x/myproject", "two words": "/y"}
    assert back.profile_projects == {"superintendent": "myproject"}


def test_resolution_order_explicit_then_profile_then_default(holler_repo, project_repo):
    cfg = _cfg(holler_repo, project_repo)
    assert resolve_session_repo(cfg, profile="superintendent") is None
    cfg.profile_projects = {"superintendent": "myproject"}
    assert resolve_session_repo(cfg, profile="superintendent") == str(project_repo)
    assert resolve_session_repo(cfg, profile="ui-ux") is None
    # Explicit request wins; an unknown explicit project is an error, never a
    # silent fallback to the default repo.
    assert resolve_session_repo(cfg, project="myproject", profile="ui-ux") == str(project_repo)
    with pytest.raises(UnknownProjectError):
        resolve_session_repo(cfg, project="nope")
    # A profile mapped to an unconfigured name falls back (config slip).
    cfg.profile_projects = {"superintendent": "ghost"}
    assert resolve_session_repo(cfg, profile="superintendent") is None
    assert project_name_for_repo(cfg, str(project_repo)) == "myproject"


# ──────────────────────────── store ────────────────────────────


def test_session_records_its_repo(store, project_repo):
    a = store.create_session(model="m", transcript="t", screenshot_path=None)
    b = store.create_session(model="m", transcript="t", screenshot_path=None,
                             repo_cwd=str(project_repo))
    assert store.get_session(a).repo_cwd == ""
    assert store.get_session(b).repo_cwd == str(project_repo)


def test_v15_migration_is_idempotent_when_repo_cwd_already_exists(tmp_path):
    """Real installs already carry sessions.repo_cwd from an earlier upstream
    migration at user_version 14 — v15 must not try to add it twice."""
    db = tmp_path / "h.db"
    s = Store(db)
    s.close()
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version = 14")
    c.execute("ALTER TABLE superintendent_evidence DROP COLUMN project")
    c.commit()
    c.close()
    s = Store(db)  # re-runs v15 against a DB that already has repo_cwd
    eid = s.add_evidence(transcript="x", screenshot_path=None, inbox_session_id=None,
                         inbox_turn_id=None, project="myproject")
    assert s.get_evidence(eid)["project"] == "myproject"
    s.close()


def test_coalesce_never_merges_across_projects(store):
    inbox = store.ensure_superintendent_inbox()
    a = store.add_evidence(transcript="a", screenshot_path=None, inbox_session_id=inbox,
                           inbox_turn_id=None, project="")
    b = store.add_evidence(transcript="b", screenshot_path=None, inbox_session_id=inbox,
                           inbox_turn_id=None, project="myproject")
    c = store.add_evidence(transcript="c", screenshot_path=None, inbox_session_id=inbox,
                           inbox_turn_id=None, project="")
    row = store.claim_next_queued_evidence()
    assert int(row["id"]) == a
    assert store.coalesce_siblings(a, window_s=60) == 1
    assert store.get_evidence(b)["status"] == "queued"
    assert store.get_evidence(c)["status"] == "merged"


# ──────────────────────────── worktree managers ────────────────────────────


def test_detect_default_branch_per_repo(holler_repo, project_repo):
    assert detect_default_branch(holler_repo) == "main"
    assert detect_default_branch(project_repo) == "trunk"


def test_registry_default_session_matches_legacy_manager(holler_repo, store, tmp_path):
    """No project on the row → identical worktree to a bare WorktreeManager:
    same dir, same branch, forked from the main checkout's HEAD."""
    _git("checkout", "-b", "local-work", cwd=holler_repo)
    _git("commit", "--allow-empty", "-m", "local head", cwd=holler_repo)
    head = _git("rev-parse", "HEAD", cwd=holler_repo)

    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    sid = store.create_session(model="m", transcript="fix it", screenshot_path=None)
    wt = reg.acquire(sid)
    assert wt == holler_repo.resolve() / ".claude" / "worktrees" / f"holler-{sid}-fix-it"
    assert _git("rev-parse", "HEAD", cwd=wt) == head
    reg.release(sid, force=True)

    legacy = WorktreeManager(main_repo_cwd=holler_repo, store=store)
    assert legacy.acquire(sid) == wt
    assert _git("rev-parse", "HEAD", cwd=wt) == head


def test_project_session_gets_worktree_in_project_repo_off_default_branch(
    holler_repo, project_repo, store,
):
    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    sid = store.create_session(model="m", transcript="fix alerts", screenshot_path=None,
                               repo_cwd=str(project_repo))
    wt = reg.acquire(sid)
    assert wt == project_repo.resolve() / ".claude" / "worktrees" / f"holler-{sid}-fix-alerts"
    # Forked from origin/trunk, NOT the operator's parked feature branch.
    assert _git("rev-parse", "HEAD", cwd=wt) == _git("rev-parse", "origin/trunk", cwd=project_repo)
    assert not (wt / "wip.txt").exists()
    assert _git("branch", "--show-current", cwd=wt) == f"holler/{sid}-fix-alerts"
    # No upstream: a stray bare `git push` can't target the shared default branch.
    p = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
                       cwd=wt, capture_output=True, text=True)
    assert p.returncode != 0
    # The Holler repo is untouched.
    assert not (holler_repo / ".claude" / "worktrees").exists()
    # The operator's checkout is untouched.
    assert _git("branch", "--show-current", cwd=project_repo) == "feature-x"


def test_release_routes_to_project_repo(holler_repo, project_repo, store):
    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    sid = store.create_session(model="m", transcript="x", screenshot_path=None,
                               repo_cwd=str(project_repo))
    wt = reg.acquire(sid)
    reg.release(sid)
    assert not wt.exists()
    assert _git("branch", "--list", f"holler/{sid}-x", cwd=project_repo) == ""
    assert store.get_session(sid).worktree_state == "merged"


def test_recovery_is_per_repo(holler_repo, project_repo, store):
    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    a = store.create_session(model="m", transcript="a", screenshot_path=None)
    b = store.create_session(model="m", transcript="b", screenshot_path=None,
                             repo_cwd=str(project_repo))
    reg.acquire(a)
    wt_b = reg.acquire(b)

    fresh = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    r = fresh.recover_on_startup()
    assert (r["checked"], r["healthy"], r["orphaned"]) == (2, 2, [])

    # Kill only the project worktree on disk: only that session is orphaned.
    subprocess.run(["rm", "-rf", str(wt_b)], check=True)
    r = WorktreeRegistry(main_repo_cwd=holler_repo, store=store).recover_on_startup()
    assert r["orphaned"] == [b]
    assert store.get_session(a).worktree_state == "active"


def test_legacy_single_manager_does_not_orphan_project_sessions(holler_repo, project_repo, store):
    """Before per-repo ownership, the default manager reconciled EVERY active
    row against its own `git worktree list` — a project worktree would be
    flagged orphaned on every launch."""
    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    b = store.create_session(model="m", transcript="b", screenshot_path=None,
                             repo_cwd=str(project_repo))
    reg.acquire(b)
    r = WorktreeManager(main_repo_cwd=holler_repo, store=store).recover_on_startup()
    assert r["checked"] == 0
    assert store.get_session(b).worktree_state == "active"


# ──────────────────────────── Session cwd routing ────────────────────────────


class _FakeClient:
    def __init__(self, options=None):
        self.options = options

    async def connect(self):
        pass

    async def query(self, gen):
        async for _ in gen:
            pass

    async def receive_response(self):
        return
        yield  # noqa

    async def receive_messages(self):  # turn_router's single pump
        return
        yield  # noqa

    async def disconnect(self):
        pass


def _captured_cwd(monkeypatch, sess, sid) -> str:
    captured: dict = {}

    def _opts(**kw):
        captured.update(kw)
        return kw

    monkeypatch.setattr(sdk_session, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(sdk_session, "ClaudeAgentOptions", _opts)
    asyncio.run(sess._execute(_Job(id=1, image_b64=None, transcript="hi",
                                   submitted_at=0.0, local_session_id=sid)))
    return captured["cwd"]


def test_session_runs_in_project_worktree(monkeypatch, tmp_path, holler_repo, project_repo, store):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    sid = store.create_session(model="m", transcript="go", screenshot_path=None,
                               repo_cwd=str(project_repo))
    sess = Session(repo_cwd=str(holler_repo), model="m", system_prompt_append="",
                   on_event=lambda e: None, worktree_manager=reg)
    cwd = _captured_cwd(monkeypatch, sess, sid)
    assert Path(cwd).is_relative_to(project_repo.resolve() / ".claude" / "worktrees")


def test_shared_checkout_mode_uses_session_repo(monkeypatch, tmp_path, holler_repo, project_repo):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repos = {5: str(project_repo), 6: ""}
    sess = Session(repo_cwd=str(holler_repo), model="m", system_prompt_append="",
                   on_event=lambda e: None,
                   session_repo_resolver=lambda sid: repos.get(sid) or None)
    assert _captured_cwd(monkeypatch, sess, 5) == str(project_repo)
    assert _captured_cwd(monkeypatch, sess, 6) == str(holler_repo)


# ──────────────────────────── Superintendent dispatch ────────────────────────


class _RecStore:
    def __init__(self, sess=None):
        self.created: list[dict] = []
        self.statuses: list[tuple] = []
        self._sess = sess

    def create_session(self, **kw):
        self.created.append(kw)
        return 9

    def add_turn(self, **kw):
        return 1

    def set_evidence_status(self, eid, status, **kw):
        self.statuses.append((eid, status, kw))

    def get_session(self, sid):
        return self._sess


class _RecSession:
    def __init__(self):
        self.submits = []

    def submit(self, *a, **kw):
        self.submits.append(kw)


def _row(**kw):
    base = {"id": 3, "transcript": "alerts page is empty", "mode": "deploy",
            "screenshot_paths": None, "screenshot_path": None, "error": "",
            "project": ""}
    base.update(kw)
    return base


def _disp(cfg, store):
    d = SuperintendentDispatcher(store, _RecSession(), {"cfg": cfg})
    d._build_worker_append = lambda mode="deploy": "APPEND"
    return d


def test_dispatch_without_projects_is_unchanged(holler_repo):
    store = _RecStore()
    _disp(_cfg(holler_repo), store)._spawn_worker(_row())
    kw = store.created[0]
    assert kw["repo_cwd"] is None
    assert kw["transcript"].endswith("Run the full 6-step pipeline to completion and deploy."
                                     "\n\nEvidence: alerts page is empty")
    assert "worktree of the" not in kw["transcript"]


def test_dispatch_pins_worker_to_profile_project(holler_repo, project_repo):
    cfg = _cfg(holler_repo, project_repo)
    cfg.profile_projects = {"superintendent": "myproject"}
    store = _RecStore()
    _disp(cfg, store)._spawn_worker(_row())
    kw = store.created[0]
    assert kw["repo_cwd"] == str(project_repo)
    assert "worktree of the myproject repo" in kw["transcript"]
    assert "ship it exactly as your profile prescribes for myproject" in kw["transcript"]


def test_dispatch_evidence_project_overrides_and_unknown_fails(holler_repo, project_repo):
    cfg = _cfg(holler_repo, project_repo)
    store = _RecStore()
    _disp(cfg, store)._spawn_worker(_row(project="myproject"))
    assert store.created[0]["repo_cwd"] == str(project_repo)
    with pytest.raises(UnknownProjectError):
        _disp(cfg, _RecStore())._spawn_worker(_row(project="ghost"))


def test_mr_result_line_is_its_own_terminal_state(holler_repo):
    turn = SimpleNamespace(ended_at=1.0, is_error=False, final_text=(
        "Fixed.\nSUPERINTENDENT RESULT: DONE + MR OPENED — x — https://gitlab/x/-/merge_requests/1"))
    store = _RecStore()
    store.get_turns = lambda sid: [turn]
    d = _disp(_cfg(holler_repo), store)
    assert d._worker_terminal_state(9) == "mr_opened"
    store.active_evidence = lambda: [{**_row(), "status": "working", "dispatch_session_id": 9,
                                      "updated_at": 0, "dispatched_at": 0}]
    d._branch_landed = lambda sid: (_ for _ in ()).throw(AssertionError("no git needed"))
    d._reconcile()
    assert store.statuses[-1][1] == "mr_opened" and store.statuses[-1][2]["error"] == ""


# ──────────────────────────── _branch_landed ────────────────────────────


def _project_session(project_repo, branch, created):
    return SimpleNamespace(repo_cwd=str(project_repo), worktree_branch=branch,
                           created_at=created, worktree_path="", sdk_session_id=None)


def test_branch_landed_checks_session_repo_gitlab_merge_commit(holler_repo, project_repo):
    """GitLab MR merge on the project's `trunk` — cfg.repo_cwd (Holler) has no
    such commit, so the old repo_cwd-only check said False forever."""
    created = time.time() - 5
    branch = "holler/9-fix-alerts"
    _git("checkout", "-b", branch, "origin/trunk", cwd=project_repo)
    _git("commit", "--allow-empty", "-m", "fix alerts", cwd=project_repo)
    _git("checkout", "-b", "merge-tmp", "origin/trunk", cwd=project_repo)
    _git("merge", "--no-ff", branch, "-m",
         f"Merge branch '{branch}' into 'trunk'", cwd=project_repo)
    d = _disp(_cfg(holler_repo, project_repo), _RecStore(_project_session(project_repo, branch, created)))
    assert d._branch_landed(9) is False  # not pushed yet
    _git("push", "origin", "HEAD:trunk", cwd=project_repo)
    assert d._branch_landed(9) is True


def test_branch_landed_fast_forward_merge_detected_by_ancestry(holler_repo, project_repo):
    created = time.time() - 5
    branch = "holler/9-ff"
    _git("checkout", "-b", branch, "origin/trunk", cwd=project_repo)
    _git("commit", "--allow-empty", "-m", "a plain commit message", cwd=project_repo)
    d = _disp(_cfg(holler_repo, project_repo), _RecStore(_project_session(project_repo, branch, created)))
    assert d._branch_landed(9) is False
    _git("push", "origin", f"{branch}:trunk", cwd=project_repo)
    assert d._branch_landed(9) is True


def test_branch_landed_ignores_untouched_branch_and_stale_history(holler_repo, project_repo):
    """A branch with no worker commits sits on its fork point (trivially an
    ancestor), and an old same-named merge predates the session: neither is
    proof this worker shipped."""
    branch = "holler/9-old"
    _git("checkout", "-b", branch, "origin/trunk", cwd=project_repo)
    _git("commit", "--allow-empty", "-m", f"Merge branch '{branch}' into 'trunk'",
         cwd=project_repo)
    _git("push", "origin", f"{branch}:trunk", cwd=project_repo)
    created = time.time() + 3600  # session started after all of that history
    d = _disp(_cfg(holler_repo, project_repo), _RecStore(_project_session(project_repo, branch, created)))
    assert d._branch_landed(9) is False


def test_branch_landed_ignores_untouched_branch_forked_from_a_fresh_commit(
    tmp_path, holler_repo, project_repo,
):
    """Another worker landed seconds before this session started and this
    worker made NO commits: the branch tip is that fresh commit — inside the
    session-start window and an ancestor of origin — yet nothing shipped."""
    other = tmp_path / "other"
    _git("clone", str(tmp_path / "myproject-origin.git"), str(other), cwd=tmp_path)
    _git("commit", "--allow-empty", "-m", "someone else's fix", cwd=other)
    _git("push", "origin", "HEAD:trunk", cwd=other)
    _git("fetch", "origin", cwd=project_repo)
    created = time.time()
    branch = "holler/9-nothing"
    _git("branch", "--no-track", branch, "origin/trunk", cwd=project_repo)
    d = _disp(_cfg(holler_repo, project_repo),
              _RecStore(_project_session(project_repo, branch, created)))
    assert d._branch_landed(9) is False
    # Once the worker commits and that commit reaches origin, it counts.
    _git("checkout", branch, cwd=project_repo)
    _git("commit", "--allow-empty", "-m", "the worker's fix", cwd=project_repo)
    _git("push", "origin", f"{branch}:trunk", cwd=project_repo)
    assert d._branch_landed(9) is True


def test_untouched_worker_without_result_line_is_not_recorded_deployed(
    tmp_path, holler_repo, store,
):
    """End to end through _reconcile on the default repo: a worker forked from
    a fresh HEAD that ends cleanly with no result line and no commits is
    `failed`, not `deployed`."""
    _git("commit", "--allow-empty", "-m", "a land a moment ago", cwd=holler_repo)
    _git("push", "origin", "HEAD:main", cwd=holler_repo)
    sid = store.create_session(model="m", transcript="fix it", screenshot_path=None)
    WorktreeRegistry(main_repo_cwd=holler_repo, store=store).acquire(sid)
    turn = SimpleNamespace(ended_at=1.0, is_error=False,
                           final_text="Verified, awaiting approval.")
    rec = _RecStore(store.get_session(sid))
    rec.get_turns = lambda _sid: [turn]
    rec.active_evidence = lambda: [{**_row(), "status": "working", "dispatch_session_id": sid,
                                    "updated_at": 0, "dispatched_at": 0}]
    _disp(_cfg(holler_repo), rec)._reconcile()
    assert rec.statuses[-1][1] == "failed"


def test_project_prompt_never_claims_a_worktree_in_shared_checkout_mode(holler_repo, project_repo):
    """per_session_worktrees off (or the registry failed to start): the
    Session has no worktree manager, so the project worker's cwd is the
    operator's own clone. The prompt must say so, not promise a worktree."""
    cfg = _cfg(holler_repo, project_repo)
    cfg.profile_projects = {"superintendent": "myproject"}
    store = _RecStore()
    d = _disp(cfg, store)
    d._session.worktree_manager = None
    d._spawn_worker(_row())
    text = store.created[0]["transcript"]
    assert "Holler-managed git worktree" not in text
    assert "shared checkout of the myproject repo, NOT a Holler-managed worktree" in text
    # Worktree mode on → the managed-worktree wording.
    store2 = _RecStore()
    d2 = _disp(cfg, store2)
    d2._session.worktree_manager = object()
    d2._spawn_worker(_row())
    assert "Holler-managed git worktree of the myproject repo" in store2.created[0]["transcript"]


def test_orphan_flagged_live_worktree_is_readopted_after_retitle(holler_repo, project_repo, store):
    """A Holler build without per-repo ownership (e.g. the previous release
    sharing this DB) reconciles a project worktree against the Holler repo and
    flags it orphaned, clearing path + branch. If the session was auto-retitled
    meanwhile, re-deriving the name would fork a fresh worktree and strand the
    work. The next acquire must find the session's own worktree by id."""
    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    sid = store.create_session(model="m", transcript="fix alerts", screenshot_path=None,
                               repo_cwd=str(project_repo))
    wt = reg.acquire(sid)
    (wt / "work.txt").write_text("uncommitted worker progress\n")
    store.set_worktree(sid, path="", branch="", state="orphaned")  # the other build
    store.set_title(sid, "Alerts page shows empty state on 500", auto=True)

    again = WorktreeRegistry(main_repo_cwd=holler_repo, store=store).acquire(sid)
    assert again == wt
    assert (again / "work.txt").exists()
    s = store.get_session(sid)
    assert (s.worktree_state, s.worktree_branch) == ("active", f"holler/{sid}-fix-alerts")


def test_fresh_session_never_adopts_a_stale_same_id_worktree(holler_repo, store):
    """Only a row that HAD a worktree (orphaned) is re-adopted by id; a brand
    new row must not inherit a leftover holler-<id>-* from an older DB."""
    reg = WorktreeRegistry(main_repo_cwd=holler_repo, store=store)
    sid = store.create_session(model="m", transcript="old work", screenshot_path=None)
    old = reg.acquire(sid)
    store.set_worktree(sid, path="", branch="", state="none")
    store.set_title(sid, "something new", auto=True)
    fresh = WorktreeRegistry(main_repo_cwd=holler_repo, store=store).acquire(sid)
    assert fresh != old and fresh.name == f"holler-{sid}-something-new"
