"""Per-session git worktree lifecycle for Holler.

Stage 1 of the concurrency model. When `per_session_worktrees=True` in
config, every Holler session that touches a shared git checkout (concurrent edits
etc.) gets its own git worktree at:

    <main_repo>/.claude/worktrees/holler-<session_id>-<slug>/

on a dedicated branch:

    holler/<session_id>-<slug>

PTTs for the same logical session reuse that worktree — Holler's existing
per-`sdk_session_id` asyncio.Lock serializes turns within a single logical
session. PTTs targeting different logical sessions get separate worktrees and
run fully in parallel.

The merge-back happens when the branch lands via plain git. That step detects worktree
context (`git rev-parse --git-dir` != `--git-common-dir`), acquires the
merge-back lock at `<appdata>/holler/merge.lock`, then runs the merge ritual
in the main checkout via `git -C <main>`. The landing step tears the
worktree down as the last step.

What this module is responsible for:
  - Creating worktrees on first acquire for a session
  - Returning the worktree path on subsequent acquires (idempotent lookup)
  - Reconciling DB state against `git worktree list --porcelain` on startup
  - Releasing worktrees (used by the landing path; can also be called
    directly for cleanup of orphans)

What this module is NOT responsible for:
  - The merge itself — that's the plain-git landing step. WorktreeManager only
    manages the worktree's *existence*; deciding when to merge belongs to the
    landing step, which the operator drives.
  - Branch naming policy beyond holler/<id>-<slug> — owned here.
  - Locking the merge step — that's the landing step + a separate file lock.

Concurrency: every git op is invoked as a subprocess via `_git`. The
operations we use (`git worktree add`, `git worktree list`, `git worktree
remove`, `git rev-parse`, `git branch`) are all safe to call concurrently
against the same .git/ — git serializes its own ref / worktree metadata
updates internally.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Branch / worktree dir prefix. Keeping it stable lets us tell our worktrees
# apart from operator-created ones (`ui-fixes`, `feature-x` etc. in
# .claude/worktrees/ — managed externally; we leave those alone).
_BRANCH_PREFIX = "holler/"
_DIR_PREFIX = "holler-"


def _slugify(text: str, *, max_len: int = 24) -> str:
    """Lowercase, hyphenate, strip noise. Empty input → 'untitled'.

    Mirrors the branch-naming convention used when landing. We don't drop filler
    words here — the title transcripts are short already and dropping words
    risks losing meaning at this length.
    """
    if not text:
        return "untitled"
    s = text.lower()
    s = re.sub(r"[^\w\s-]+", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    if len(s) > max_len:
        # Cut at a word boundary if we can.
        cut = s[:max_len]
        last_hyphen = cut.rfind("-")
        if last_hyphen >= max_len - 8:  # only trim if it doesn't gut the slug
            cut = cut[:last_hyphen]
        s = cut.strip("-")
    return s or "untitled"


@dataclass
class _WorktreeRef:
    """In-memory cache entry. Source of truth is still the DB."""
    path: Path
    branch: str


class WorktreeError(RuntimeError):
    """Wraps git command failures with a useful message + stderr."""


def detect_default_branch(repo: Path | str) -> str:
    """The repo's default branch name, resolved from git rather than assumed.

    Order: the `origin/HEAD` symref (what the remote — GitHub/GitLab — calls its
    default), then a remote-tracking `main`/`master`, then a local
    `main`/`master`, then "master" as the legacy last resort. Mirrors
    `deploy_gate.default_branch` so landing and fork-point logic agree.
    """
    def _ok(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            timeout=15, check=False,
        )

    try:
        p = _ok("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip().rsplit("/", 1)[-1]
        for prefix in ("refs/remotes/origin/", "refs/heads/"):
            for cand in ("main", "master"):
                if _ok("show-ref", "--verify", "--quiet", f"{prefix}{cand}").returncode == 0:
                    return cand
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "master"


class WorktreeManager:
    """Manage per-Holler-session git worktrees against a single main repo.

    Construct once per repo, share across the runner (`WorktreeRegistry` keeps
    one per repo when sessions target more than one project). Methods are
    thread-safe via `_lock` — the SDK session thread and the GUI thread can
    both call into the manager (the GUI calls `recover_on_startup` and the
    SDK thread calls `acquire`).

    `is_default`: this manager serves the default repo (`cfg.repo_cwd`), so it
    also owns sessions whose row carries no explicit `repo_cwd`. A project
    manager (`is_default=False`) owns only sessions that name its repo.

    `fork_from_default`: fork new worktrees from the repo's default branch
    (`origin/<default>` after a best-effort fetch) instead of the main
    checkout's HEAD. Project repos use this: their main checkout is the
    operator's live clone, routinely parked on an unrelated feature branch.
    """

    def __init__(
        self,
        main_repo_cwd: Path,
        store,
        *,
        is_default: bool = True,
        fork_from_default: bool = False,
    ) -> None:
        # `store` is `holler.store.Store`. Imported by-name (not by-type)
        # to keep this module light and circular-import-free.
        self._main = Path(main_repo_cwd).resolve()
        self._store = store
        self._is_default = is_default
        self._fork_from_default = fork_from_default
        self._lock = threading.Lock()
        # Cache `local_session_id → _WorktreeRef`. Loaded lazily from the DB
        # on first acquire; recover_on_startup primes it for known-active
        # sessions.
        self._cache: dict[int, _WorktreeRef] = {}
        # Verify the main repo *is* a git repo at construction time — fail
        # loud here rather than at the first acquire.
        try:
            self._git("rev-parse", "--show-toplevel", cwd=self._main)
        except WorktreeError as exc:
            raise WorktreeError(
                f"main_repo_cwd={self._main} is not a git checkout: {exc}"
            ) from exc

    # ──────────────────────────── public API ────────────────────────────

    def acquire(self, local_session_id: int, *, slug_hint: str = "") -> Path:
        """Return the worktree path for this session, creating it if needed.

        Idempotent: repeated calls for the same `local_session_id` return the
        same path without re-creating the worktree. Slug is derived once at
        creation time and frozen — re-naming a session does NOT rename its
        worktree branch / dir.
        """
        with self._lock:
            cached = self._cache.get(local_session_id)
            if cached is not None and cached.path.exists():
                return cached.path

            # Check the DB first — a worktree may have been created in a
            # prior Holler run and survived restart.
            sess = self._store.get_session(local_session_id)
            if sess is not None and sess.worktree_state == "active" and sess.worktree_path:
                p = Path(sess.worktree_path)
                if p.exists() and self._is_known_worktree(p):
                    self._cache[local_session_id] = _WorktreeRef(
                        path=p, branch=sess.worktree_branch,
                    )
                    return p
                # DB claims active but disk says no — flag orphaned and fall
                # through to create a fresh one.
                log.warning(
                    "session %d: DB worktree_path %s missing on disk — "
                    "marking orphaned and creating fresh worktree",
                    local_session_id, sess.worktree_path,
                )
                self._store.set_worktree(
                    local_session_id,
                    path="", branch="", state="orphaned",
                )

            # Drop stale .git/worktrees/ metadata for worktree dirs that were
            # deleted out from under git (operator `rm -rf`, crash). Without
            # this, re-creating at the same path fails with "missing but
            # already registered worktree", and the existing branch stays
            # "already used by worktree" so the fallback below fails too.
            self._prune()

            # A row flagged orphaned while its worktree is still registered
            # and on disk (another Holler build's startup recovery that
            # doesn't know about per-repo ownership clears path+branch): re-
            # adopt it by session id. Re-deriving the name from the title
            # would miss it once the title changed (auto-titling) and fork a
            # fresh worktree, stranding the session's work in the old one.
            if sess is not None and sess.worktree_state == "orphaned":
                own = self._find_own_worktree(local_session_id)
                if own is not None:
                    path, branch = own
                    log.info(
                        "session %d: re-adopting its orphan-flagged worktree at %s",
                        local_session_id, path,
                    )
                    self._store.set_worktree(
                        local_session_id, path=str(path), branch=branch, state="active",
                    )
                    self._cache[local_session_id] = _WorktreeRef(path=path, branch=branch)
                    return path

            # Fresh worktree. Derive name + branch.
            slug = _slugify(slug_hint or (sess.title if sess else ""))
            name = f"{_DIR_PREFIX}{local_session_id}-{slug}"
            branch = f"{_BRANCH_PREFIX}{local_session_id}-{slug}"
            worktree_dir = self._main / ".claude" / "worktrees" / name

            # Belt-and-suspenders: if the dir already exists but isn't a known
            # git worktree, refuse rather than blowing it away.
            if worktree_dir.exists() and not self._is_known_worktree(worktree_dir):
                raise WorktreeError(
                    f"path {worktree_dir} already exists and is not a known "
                    f"git worktree — refusing to overwrite. Move or remove it manually."
                )

            # `git worktree add -b <branch> <path> <fork>` creates the branch
            # and checks it out in the new worktree. The default repo forks
            # from HEAD, never a hardcoded "master" — the default branch may
            # be `main` (or anything), and `master` not existing made every
            # turn abort with "invalid reference: master". Project repos fork
            # from their detected default branch (see `_fork_point`).
            if worktree_dir.exists():
                # Path is a known worktree from a prior run that lost its DB
                # row — just adopt it.
                log.info(
                    "session %d: adopting pre-existing worktree at %s",
                    local_session_id, worktree_dir,
                )
            else:
                worktree_dir.parent.mkdir(parents=True, exist_ok=True)
                fork = self._fork_point()
                # A remote-tracking start point would make the new branch
                # track origin/<default>; --no-track keeps a stray bare
                # `git push` from ever targeting the shared default branch.
                track = [] if fork == "HEAD" else ["--no-track"]
                try:
                    self._git(
                        "worktree", "add",
                        *track,
                        "-b", branch,
                        str(worktree_dir),
                        fork,
                        cwd=self._main,
                    )
                except WorktreeError as exc:
                    # Branch may already exist from a previous aborted run
                    # whose worktree was deleted. Try again without -b, using
                    # the existing branch — git will check it out into the
                    # new path if it's not active elsewhere.
                    msg = str(exc).lower()
                    if "already exists" in msg or "already used by worktree" in msg:
                        log.info(
                            "session %d: branch %s already exists — attaching "
                            "worktree to existing branch",
                            local_session_id, branch,
                        )
                        self._git(
                            "worktree", "add",
                            str(worktree_dir),
                            branch,
                            cwd=self._main,
                        )
                    else:
                        raise

            # Stamp the DB and cache.
            self._store.set_worktree(
                local_session_id,
                path=str(worktree_dir),
                branch=branch,
                state="active",
            )
            self._cache[local_session_id] = _WorktreeRef(
                path=worktree_dir, branch=branch,
            )
            log.info(
                "session %d: acquired worktree at %s on %s",
                local_session_id, worktree_dir, branch,
            )
            return worktree_dir

    def release(
        self,
        local_session_id: int,
        *,
        force: bool = False,
        final_state: str = "merged",
    ) -> None:
        """Remove the worktree dir + delete the branch.

        force=True passes --force to `git worktree remove` (will throw away
        uncommitted changes in the worktree). Without force, refuses if the
        worktree has dirty state.

        final_state stamps the session's worktree_state after release — use
        'merged' for the happy path (branch landed via git), 'orphaned' for cleanup.
        """
        with self._lock:
            sess = self._store.get_session(local_session_id)
            if sess is None or not sess.worktree_path:
                log.debug(
                    "session %d: release() called but no worktree on record — no-op",
                    local_session_id,
                )
                return

            wt_path = Path(sess.worktree_path)
            branch = sess.worktree_branch

            if not wt_path.exists():
                # Dir vanished — clear git's registration so the branch is no
                # longer "checked out" there and `branch -d` can proceed.
                self._prune()
            else:
                args = ["worktree", "remove"]
                if force:
                    args.append("--force")
                args.append(str(wt_path))
                try:
                    self._git(*args, cwd=self._main)
                except WorktreeError as exc:
                    if force:
                        # Even --force failed — fall back to manual cleanup
                        # so we don't leave the row in an inconsistent state.
                        log.exception(
                            "session %d: git worktree remove --force failed; "
                            "leaving filesystem alone, marking orphaned. err=%s",
                            local_session_id, exc,
                        )
                        self._store.set_worktree(
                            local_session_id, path="", branch="", state="orphaned",
                        )
                        self._cache.pop(local_session_id, None)
                        raise
                    raise

            # Delete the branch. `-d` is the safe variant — refuses if not
            # merged. Use `-D` only when force=True (operator override).
            kept_branch = ""
            if branch:
                del_flag = "-D" if force else "-d"
                try:
                    self._git("branch", del_flag, branch, cwd=self._main)
                except WorktreeError as exc:
                    # If the branch was already pruned (e.g. during an earlier
                    # git landing) this is fine — log and continue.
                    if "not found" in str(exc).lower():
                        log.debug(
                            "session %d: branch %s already gone",
                            local_session_id, branch,
                        )
                    else:
                        # Typically `-d` refusing an unmerged branch. The
                        # worktree is gone but the work still lives on the
                        # branch — never claim 'merged' in that case. Record
                        # 'orphaned' and keep the branch name so the operator
                        # can find and land/delete it.
                        log.warning(
                            "session %d: branch %s delete failed (kept; marking "
                            "orphaned): %s",
                            local_session_id, branch, exc,
                        )
                        kept_branch = branch
                        final_state = "orphaned"

            self._store.set_worktree(
                local_session_id, path="", branch=kept_branch, state=final_state,
            )
            self._cache.pop(local_session_id, None)
            log.info(
                "session %d: released worktree (state=%s)",
                local_session_id, final_state,
            )

    def recover_on_startup(self) -> dict:
        """Reconcile DB worktree state against `git worktree list`.

        Called once at Holler launch (from runners/full.py) before any PTT
        can spawn a session. Walks every session with worktree_state='active'
        and checks the corresponding dir.

        Returns a small dict for logging: {checked, healthy, orphaned}.
        """
        with self._lock:
            checked = 0
            healthy = 0
            orphaned: list[int] = []

            # Build the set of currently-registered worktree paths once.
            registered = self._list_registered_worktrees()

            sessions = [
                s for s in self._store.list_sessions_with_active_worktree()
                if self.owns_session(s)
            ]
            for sess in sessions:
                checked += 1
                p = Path(sess.worktree_path)
                if (
                    p.exists()
                    and str(p.resolve()) in registered
                ):
                    # Reconciled — prime the cache so the next acquire is
                    # a free lookup.
                    self._cache[sess.id] = _WorktreeRef(
                        path=p, branch=sess.worktree_branch,
                    )
                    healthy += 1
                else:
                    log.warning(
                        "session %d: worktree at %s missing or unregistered — "
                        "marking orphaned (operator can clean up in Sessions window)",
                        sess.id, sess.worktree_path,
                    )
                    self._store.set_worktree(
                        sess.id, path="", branch="", state="orphaned",
                    )
                    orphaned.append(sess.id)

            # `git worktree prune` cleans up .git/worktrees/ metadata for
            # worktrees whose dirs are gone. Cheap; always run.
            try:
                self._git("worktree", "prune", cwd=self._main)
            except WorktreeError:
                # Non-fatal — we logged what we did to the DB already.
                log.exception("worktree prune failed during startup recovery")

            log.info(
                "WorktreeManager.recover_on_startup: checked=%d healthy=%d orphaned=%d",
                checked, healthy, len(orphaned),
            )
            return {"checked": checked, "healthy": healthy, "orphaned": orphaned}

    def list_orphans(self) -> list[int]:
        """Returns local_session_ids whose worktree_state is 'orphaned'.

        UI uses this to surface a cleanup affordance.
        """
        rows = self._store._conn().execute(  # noqa: SLF001 — Store-internal use
            "SELECT id FROM sessions WHERE worktree_state = 'orphaned'"
        ).fetchall()
        return [int(r["id"]) for r in rows]

    @property
    def main_repo(self) -> Path:
        return self._main

    def default_branch(self) -> str:
        return detect_default_branch(self._main)

    def owns_session(self, sess) -> bool:
        """True if `sess` (a `domain.Session`) belongs to this manager's repo.

        A row with no `repo_cwd` belongs to the default repo; otherwise the
        recorded path must resolve to this manager's main checkout. Recovery
        uses this so a project manager never orphans another repo's worktrees.
        """
        repo = getattr(sess, "repo_cwd", "") or ""
        if not repo:
            return self._is_default
        try:
            return Path(repo).resolve() == self._main
        except OSError:
            return False

    # ──────────────────────────── internals ─────────────────────────────

    def _fork_point(self) -> str:
        """Commit-ish new worktrees branch from.

        Default repo: HEAD (unchanged legacy behaviour). Project repo: the
        default branch's freshest known tip — `origin/<default>` after a
        best-effort fetch (offline is fine: we use whatever was fetched last),
        else the local `<default>` branch, else HEAD.
        """
        if not self._fork_from_default:
            return "HEAD"
        base = self.default_branch()
        try:
            self._git("fetch", "origin", base, "--quiet", cwd=self._main)
        except WorktreeError:
            log.info("fetch of origin/%s in %s failed — using last-known tip", base, self._main)
        for ref in (f"refs/remotes/origin/{base}", f"refs/heads/{base}"):
            try:
                self._git("show-ref", "--verify", "--quiet", ref, cwd=self._main)
                return ref
            except WorktreeError:
                continue
        return "HEAD"

    def _git(self, *args: str, cwd: Path) -> str:
        """Run a git command and return stdout. Raises WorktreeError on failure.

        We capture stderr separately so error messages are useful — the
        common failure modes (branch already exists, dir already used by
        another worktree) are detected by substring matching on stderr.
        """
        cmd = ["git", *args]
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeError(f"git {args[0]}... failed to launch: {exc}") from exc
        finally:
            elapsed_ms = int((time.time() - t0) * 1000)
            log.debug("git %s -> %dms (cwd=%s)", " ".join(args), elapsed_ms, cwd)
        if proc.returncode != 0:
            raise WorktreeError(
                f"git {args[0]}... exited {proc.returncode}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    def _prune(self) -> None:
        """`git worktree prune` — non-fatal; failure is logged only."""
        try:
            self._git("worktree", "prune", cwd=self._main)
        except WorktreeError:
            log.warning("git worktree prune failed", exc_info=True)

    def _is_known_worktree(self, path: Path) -> bool:
        """True if `path` is a worktree registered with the main repo."""
        registered = self._list_registered_worktrees()
        try:
            return str(path.resolve()) in registered
        except OSError:
            return False

    def _find_own_worktree(self, local_session_id: int) -> Optional[tuple[Path, str]]:
        """(path, branch) of a live registered worktree this manager created
        for `local_session_id` — `.claude/worktrees/holler-<sid>-*` on branch
        `holler/<sid>-*` — or None."""
        try:
            out = self._git("worktree", "list", "--porcelain", cwd=self._main)
        except WorktreeError:
            return None
        root = (self._main / ".claude" / "worktrees").resolve()
        dir_prefix = f"{_DIR_PREFIX}{local_session_id}-"
        ref_prefix = f"refs/heads/{_BRANCH_PREFIX}{local_session_id}-"
        for block in out.split("\n\n"):
            wt, ref = "", ""
            for line in block.splitlines():
                if line.startswith("worktree "):
                    wt = line[len("worktree "):].strip()
                elif line.startswith("branch "):
                    ref = line[len("branch "):].strip()
            if not wt or not ref.startswith(ref_prefix):
                continue
            p = Path(wt)
            try:
                rp = p.resolve()
            except OSError:
                continue
            if rp.parent == root and rp.name.startswith(dir_prefix) and rp.exists():
                return p, ref[len("refs/heads/"):]
        return None

    def _list_registered_worktrees(self) -> set[str]:
        """Resolved paths of every worktree git knows about (main + extra)."""
        try:
            out = self._git("worktree", "list", "--porcelain", cwd=self._main)
        except WorktreeError:
            return set()
        paths: set[str] = set()
        for line in out.splitlines():
            if line.startswith("worktree "):
                wp = line[len("worktree "):].strip()
                try:
                    paths.add(str(Path(wp).resolve()))
                except OSError:
                    paths.add(wp)
        return paths


class WorktreeRegistry:
    """One `WorktreeManager` per repo, routed by each session's recorded repo.

    Drop-in for a single `WorktreeManager` (same `acquire` / `release` /
    `recover_on_startup` / `list_orphans` surface), so `Session`, retention and
    the runner don't care how many repos are in play. A session row with an
    empty `repo_cwd` targets the default repo (`cfg.repo_cwd`) exactly as
    before; a row naming another repo (a configured project, e.g. myproject) gets
    a worktree under `<that repo>/.claude/worktrees/holler-<sid>-<slug>/`,
    forked from that repo's default branch.

    With no project sessions ever created, the only manager ever built is the
    default one, constructed eagerly with the legacy arguments — behaviour is
    identical to a bare `WorktreeManager(cfg.repo_cwd, store)`.
    """

    def __init__(self, main_repo_cwd: Path, store, manager_cls=None) -> None:
        self._store = store
        self._manager_cls = manager_cls or WorktreeManager
        self._lock = threading.Lock()
        self._default = self._manager_cls(main_repo_cwd, store)
        self._default_path = Path(main_repo_cwd).resolve()
        self._managers: dict[Path, WorktreeManager] = {self._default_path: self._default}

    # ----- routing -----

    @property
    def default_manager(self) -> WorktreeManager:
        return self._default

    def repo_for_session(self, local_session_id: int) -> Path:
        sess = self._store.get_session(local_session_id)
        repo = getattr(sess, "repo_cwd", "") if sess is not None else ""
        return Path(repo).resolve() if repo else self._default_path

    def manager_for(self, repo: Path | str) -> WorktreeManager:
        """The manager for `repo`, built on first use. Raises WorktreeError
        when `repo` isn't a git checkout (the turn must not silently fall
        back to some other repo)."""
        key = Path(repo).resolve()
        with self._lock:
            mgr = self._managers.get(key)
            if mgr is None:
                mgr = self._manager_cls(
                    key, self._store, is_default=False, fork_from_default=True,
                )
                self._managers[key] = mgr
            return mgr

    def manager_for_session(self, local_session_id: int) -> WorktreeManager:
        return self.manager_for(self.repo_for_session(local_session_id))

    # ----- WorktreeManager-compatible surface -----

    def acquire(self, local_session_id: int, *, slug_hint: str = "") -> Path:
        return self.manager_for_session(local_session_id).acquire(
            local_session_id, slug_hint=slug_hint,
        )

    def release(self, local_session_id: int, **kwargs) -> None:
        return self.manager_for_session(local_session_id).release(local_session_id, **kwargs)

    def recover_on_startup(self) -> dict:
        """Reconcile every repo that has an active session worktree.

        Each manager only reconciles the sessions it owns. A project repo that
        has vanished (or stopped being a git checkout) can't be reconciled —
        its active rows are flagged orphaned so the operator sees them.
        """
        repos = {self._default_path}
        for sess in self._store.list_sessions_with_active_worktree():
            repo = getattr(sess, "repo_cwd", "") or ""
            if repo:
                repos.add(Path(repo).resolve())
        total = {"checked": 0, "healthy": 0, "orphaned": []}
        for repo in sorted(repos, key=str):
            try:
                mgr = self._default if repo == self._default_path else self.manager_for(repo)
            except WorktreeError:
                log.warning("worktree recovery: %s is not a usable git repo", repo)
                for sess in self._store.list_sessions_with_active_worktree():
                    if Path(getattr(sess, "repo_cwd", "") or self._default_path).resolve() == repo:
                        self._store.set_worktree(sess.id, path="", branch="", state="orphaned")
                        total["checked"] += 1
                        total["orphaned"].append(sess.id)
                continue
            r = mgr.recover_on_startup()
            total["checked"] += r["checked"]
            total["healthy"] += r["healthy"]
            total["orphaned"].extend(r["orphaned"])
        return total

    def list_orphans(self) -> list[int]:
        return self._default.list_orphans()
