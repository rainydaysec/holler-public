"""Rolling retention purge for completed (reviewed) sessions.

When a session is marked Done (`reviewed_at` set), holler keeps it around for a
grace period so it can still be reopened. After `purge_reviewed_days` elapse,
this sweep permanently removes it — both the DB rows (session + its turns +
tool_calls, via ON DELETE CASCADE) and the on-disk artifacts it left behind:

  - capture PNGs under the app's `captures/` dir (session + per-turn thumbs),
  - the Claude SDK transcript JSONL under `~/.claude/projects/<slug>/<id>.jsonl`.

Deletion is destructive and irreversible, so every unlink is *guarded*: a file
is only removed if it resolves to a path inside an allowed base directory
(captures dir or the Claude projects dir). A malformed `screenshot_path` in the
DB can therefore never cause deletion of an arbitrary file.

The sweep is invoked from the launch path (see runners/full.py). It is a read
(`Store.list_reviewed_purgeable`) followed by per-row worktree release (when a
WorktreeManager is supplied), `Store.delete`, and artifact deletion; one row
failing is logged and skipped, never aborting the sweep.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from holler.store import Store

log = logging.getLogger(__name__)


def _default_claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _unlink_guarded(target: Optional[str], base: Path) -> int:
    """Delete `target` iff it resolves to a regular file inside `base`.

    Returns 1 if a file was removed, else 0. Never raises for the caller — a
    per-file failure is logged and treated as "nothing removed" so one bad path
    doesn't abort the session's purge."""
    if not target:
        return 0
    try:
        path = Path(target).resolve()
        base_resolved = base.resolve()
    except OSError:
        return 0
    if not path.is_relative_to(base_resolved):
        # Outside the sandbox — refuse to touch it (defends against a malformed
        # or malicious screenshot_path in the DB).
        log.warning("retention: refusing to delete out-of-sandbox path %s", path)
        return 0
    try:
        if path.is_file():
            path.unlink()
            return 1
    except OSError:
        log.exception("retention: failed to unlink %s", path)
    return 0


def _purge_sdk_transcript(sdk_session_id: Optional[str], projects_dir: Path) -> int:
    """Remove the Claude SDK transcript JSONL for a session, wherever it landed.

    The JSONL lives at `<projects_dir>/<cwd-slug>/<sdk_session_id>.jsonl`, and
    the slug depends on the (possibly per-session worktree) cwd — so we glob by
    the UUID filename across every project dir rather than reconstruct the slug.
    Returns the number of files removed."""
    if not sdk_session_id:
        return 0
    try:
        base = projects_dir.resolve()
    except OSError:
        return 0
    if not base.is_dir():
        return 0
    removed = 0
    for match in base.glob(f"*/{sdk_session_id}.jsonl"):
        removed += _unlink_guarded(str(match), base)
    return removed


def purge_reviewed_sessions(
    store: Store,
    captures_dir: Path,
    cutoff_epoch: float,
    *,
    claude_projects_dir: Optional[Path] = None,
    worktree_manager=None,
) -> dict:
    """Purge every reviewed session marked Done before `cutoff_epoch`.

    Deletes each session's DB row (cascading to its turns/tool_calls), then its
    SDK transcript JSONL and any capture PNGs no other row still references.
    Returns `{"sessions": int, "files": int}` — sessions removed and on-disk
    files removed. Pinned/running sessions are exempt (enforced by the store
    query).

    `worktree_manager` (optional `WorktreeManager`): when given, a session
    with an active worktree has it released first (final_state='orphaned',
    never forced — dirty worktrees and unmerged branches are preserved). If
    the release fails, that session is skipped entirely (row kept) so the
    worktree is never left on disk with no DB row tracking it."""
    if claude_projects_dir is None:
        claude_projects_dir = _default_claude_projects_dir()

    candidates = store.list_reviewed_purgeable(cutoff_epoch)
    sessions_removed = 0
    files_removed = 0

    for sess in candidates:
        try:
            if getattr(sess, "worktree_state", "none") == "active":
                if worktree_manager is None:
                    log.warning(
                        "retention: session %s has an active worktree at %s but "
                        "no worktree manager was supplied; purging row anyway",
                        sess.id, getattr(sess, "worktree_path", ""),
                    )
                else:
                    try:
                        worktree_manager.release(
                            sess.id, force=False, final_state="orphaned",
                        )
                    except Exception:
                        log.exception(
                            "retention: could not release worktree for session "
                            "%s; keeping the session row so it stays tracked",
                            sess.id,
                        )
                        continue

            # Gather every on-disk artifact path this session references.
            thumb_paths = {sess.screenshot_path}
            for turn in store.get_turns(sess.id):
                thumb_paths.add(turn.screenshot_path)

            # Drop the rows first, then only unlink captures that no remaining
            # session / turn / evidence row still points at (shared captures,
            # e.g. an inbox turn's shot that an evidence row also carries).
            store.delete(sess.id)
            sessions_removed += 1

            removed_here = 0
            for p in thumb_paths:
                if not p:
                    continue
                try:
                    if store.is_screenshot_referenced(p):
                        continue
                except Exception:
                    log.exception("retention: reference check failed for %s", p)
                    continue  # when in doubt, keep the file
                removed_here += _unlink_guarded(p, captures_dir)
            removed_here += _purge_sdk_transcript(sess.sdk_session_id, claude_projects_dir)
            files_removed += removed_here
        except Exception:
            # One row failing must not abort the whole sweep. Log loud, move on.
            log.exception("retention: failed to purge session %s", sess.id)

    return {"sessions": sessions_removed, "files": files_removed}
