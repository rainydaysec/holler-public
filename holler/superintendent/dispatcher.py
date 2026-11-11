"""SuperintendentDispatcher — drains the evidence ledger into worker sessions.

A daemon thread (mirrors swarm/auto_dispatcher.py) that, every tick:
  1. reconciles in-flight workers — marks finished ones deployed/failed and
     frees their concurrency slot; reaps wedged ones past the timeout.
  2. fills open slots — atomically claims the oldest queued evidence row and
     spawns a real Holler session (visible green in the left rail) carrying the
     6-step `superintendent` profile plus the last-wins deploy-authority
     override.

Workers run in isolated per-session git worktrees (the Session's worktree
manager acquires one for any submit with a local_session_id), so parallel
workers can't clobber each other. Deploy is serialized separately by
deploy_gate.py.

Everything here runs off the GUI thread — identical to the normal PTT dispatch
path (`_dispatch_job`), which also calls Store writes + Session.submit from a
daemon thread. The Session's on_event marshals rail updates to Qt internally.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_TICK_S = 5.0
_BOOT_GRACE_S = 8.0
# A dump that lands within this window of a just-filed chain (same inbox + lane)
# is a straggler of that chain — a trailing PTT that finished transcribing a beat
# after the operator filed — so it folds into the same worker instead of spawning
# a second, redundant session. Kept well under a deliberate new-report gap.
_COALESCE_WINDOW_S = 20.0
# A worker gets this long to run analysis -> ... -> deploy in its single turn.
# Past it, we assume it wedged, mark the item failed, and free the slot.
_WORKER_TIMEOUT_S = 45 * 60
# Stamped into `error` when an AUP-refused item is requeued, so a second
# refusal fails for real instead of looping forever.
_AUP_RETRY_MARKER = "aup-retried"


# Distinguishes "this Session impl has no worktree_manager attribute" (assume
# managed, the production default) from "worktree mode is off" (None).
_UNKNOWN = object()


class SuperintendentDispatcher:
    def __init__(self, persistent_store: Any, session: Any, state: dict) -> None:
        self._store = persistent_store
        self._session = session
        self._state = state
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._hot_cap_mtime: Optional[float] = None
        self._hot_cap_value: Optional[int] = None

    # ----- lifecycle -----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="superintendent-dispatcher", daemon=True,
        )
        self._thread.start()
        log.info("superintendent dispatcher started")

    def stop(self, join_timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    # ----- config knobs (read live each tick) -----

    def _cfg(self):
        return self._state.get("cfg")

    def _enabled(self) -> bool:
        cfg = self._cfg()
        return bool(getattr(cfg, "superintendent_enabled", True)) if cfg else False

    def _max_workers(self) -> int:
        # Hot-read from config.toml (mtime-cached) so the operator can raise
        # the cap with a file edit and have it apply on the next tick — the
        # in-memory state cfg only refreshes on a settings-dialog save.
        hot = self._hot_max_workers()
        if hot is not None:
            configured = hot
        else:
            cfg = self._cfg()
            try:
                configured = max(1, int(getattr(cfg, "superintendent_max_workers", 10)))
            except Exception:
                configured = 10
        # Never put more workers in flight than the Session's background lane
        # can actually run. A worker over the lane size would sit queued on the
        # semaphore while its `dispatched_at` wedge clock runs, and get reaped
        # as "wedged" without ever having started.
        lane = self._lane_capacity()
        return min(configured, lane) if lane else configured

    def _lane_capacity(self) -> Optional[int]:
        """Background-lane size of the live Session (its concurrency), falling
        back to cfg.concurrency_cap. None when neither is known."""
        for src, attr in (
            (self._session, "lane_capacity"),
            (self._session, "_concurrency"),
            (self._cfg(), "concurrency_cap"),
        ):
            v = getattr(src, attr, None) if src is not None else None
            if callable(v):
                try:
                    v = v("background")
                except Exception:
                    v = None
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                return v
        return None

    _MAX_WORKERS_RE = re.compile(
        r"^[ \t]*superintendent_max_workers[ \t]*=[ \t]*(\d+)", re.MULTILINE
    )

    def _hot_max_workers(self) -> Optional[int]:
        try:
            from holler.config import _config_path_default
            path = _config_path_default()
            mtime = path.stat().st_mtime
            if mtime != self._hot_cap_mtime:
                m = self._MAX_WORKERS_RE.search(path.read_text(encoding="utf-8"))
                self._hot_cap_value = max(1, int(m.group(1))) if m else None
                self._hot_cap_mtime = mtime
            return self._hot_cap_value
        except Exception:
            return None

    # ----- loop -----

    def _run(self) -> None:
        self._stop.wait(_BOOT_GRACE_S)
        try:
            self._requeue_aup_refusals()
        except Exception:
            log.exception("superintendent AUP-refusal backfill raised; continuing")
        while not self._stop.is_set():
            try:
                if self._enabled():
                    self._autocommit_stale_draft()
                    self._reconcile()
                    self._fill_slots()
            except Exception:
                log.exception("superintendent dispatcher tick raised; continuing")
            self._stop.wait(_TICK_S)

    # ----- draft auto-commit -----

    def _autocommit_stale_draft(self) -> None:
        """Flush an idle draft so a missed commit cue never strands evidence."""
        from holler.superintendent import drafts as _drafts

        lock = self._state.get("superintendent_draft_lock")
        if lock is None:
            return
        with lock:
            # Chain mode armed via the inbox Chain bar = the operator explicitly
            # took control of when to file — never auto-commit under them.
            if self._state.get("superintendent_chain_armed"):
                return
            draft = self._state.get("superintendent_draft")
            if not _drafts.draft_is_stale(draft):
                return
            inbox_sid = self._state.get("superintendent_inbox_sid")
            turn_ids = list(draft.get("turn_ids", []))
            try:
                eid = _drafts.flush_draft(
                    self._store,
                    inbox_sid if isinstance(inbox_sid, int) else None,
                    draft,
                )
                self._state["superintendent_draft"] = None
                for tid in turn_ids:
                    try:
                        self._store.update_turn(
                            tid, ended_at=time.time(),
                            final_text=f"Evidence #{eid} filed automatically after the draft sat idle → queued.",
                        )
                    except Exception:
                        pass
                log.info("superintendent draft auto-committed as evidence %s", eid)
            except Exception:
                log.exception("superintendent draft auto-commit failed")

    # ----- reconcile in-flight workers -----

    def _reconcile(self) -> None:
        for row in self._store.active_evidence():
            sid = row["dispatch_session_id"]
            eid = int(row["id"])
            if not isinstance(sid, int):
                # Claimed (dispatching) but the spawn never stamped a session —
                # a crash between claim and submit. Fail it so it frees the slot.
                if row["status"] == "dispatching" and self._age(row["updated_at"]) > 120:
                    self._store.set_evidence_status(
                        eid, "failed", error="dispatch never completed",
                    )
                continue
            mode = self._mode_for(row)
            state = self._worker_terminal_state(sid, mode)
            if state is not None:
                if state == "failed" and self._should_aup_retry(row, sid):
                    self._store.set_evidence_status(eid, "queued", error=_AUP_RETRY_MARKER)
                    log.warning(
                        "superintendent item %s refused by AUP classifier — "
                        "requeued once with sanitized prompt", eid,
                    )
                    continue
                wt = self._worktree_path(sid)
                if mode == "investigation":
                    # IR never deploys, so the worker's terminal verdict is
                    # authoritative — there is no "landed anyway" to reconcile.
                    if state == "reported":
                        err = ""
                    elif _AUP_RETRY_MARKER in (row["error"] or ""):
                        from holler.superintendent.sanitize import is_aup_refusal
                        err = (
                            "AUP refusal persisted after sanitized retry"
                            if is_aup_refusal(self._last_final_text(sid))
                            else "worker turn ended in error (after AUP retry)"
                        )
                    else:
                        err = "worker turn ended in error"
                elif state == "failed" and self._branch_landed(sid):
                    # The turn errored/timed out AFTER the fix landed on the
                    # default branch (e.g. rate limit during the deploy watch).
                    # The work shipped — don't let the ledger lie about it.
                    state, err = "deployed", ""
                    log.info(
                        "superintendent item %s: turn errored but merge is on "
                        "the default branch — recorded deployed", eid,
                    )
                elif state == "deployed" and not self._has_done_marker(sid):
                    # Turn ended cleanly but WITHOUT the mandatory result line
                    # (e.g. the worker stopped at "verified, awaiting
                    # approval"). A clean exit is not proof of a land — only
                    # record deployed when the merge is actually there.
                    if self._branch_landed(sid):
                        err = ""
                    else:
                        state = "failed"
                        err = "worker ended without a result line and no landed merge was found"
                elif state in ("deployed", "mr_opened"):
                    err = ""
                elif _AUP_RETRY_MARKER in (row["error"] or ""):
                    from holler.superintendent.sanitize import is_aup_refusal
                    err = (
                        "AUP refusal persisted after sanitized retry"
                        if is_aup_refusal(self._last_final_text(sid))
                        else "worker turn ended in error (after AUP retry)"
                    )
                else:
                    err = "worker turn ended in error"
                self._store.set_evidence_status(
                    eid, state, worktree_path=wt or None, error=err,
                )
                log.info("superintendent item %s -> %s (session %s)", eid, state, sid)
                continue
            # Wedge reaper — actually interrupts the worker's in-flight job
            # (frees the slot AND the session), then records the truth: if the
            # merge already landed on master the fix shipped, so the item is
            # deployed, not failed.
            if row["status"] == "working" and self._age(row["dispatched_at"]) > _WORKER_TIMEOUT_S:
                try:
                    self._cancel_session(sid)
                except Exception:
                    log.exception("failed to cancel wedged worker session %s", sid)
                if mode != "investigation" and self._branch_landed(sid):
                    self._store.set_evidence_status(eid, "deployed", error="")
                    log.warning(
                        "superintendent item %s reaped past timeout but merge "
                        "landed — recorded deployed (session %s)", eid, sid,
                    )
                else:
                    self._store.set_evidence_status(
                        eid, "failed",
                        error=f"worker exceeded {_WORKER_TIMEOUT_S // 60}m timeout",
                    )
                    log.warning("superintendent item %s reaped (session %s wedged)", eid, sid)

    def _last_final_text(self, sid: int) -> str:
        try:
            turns = self._store.get_turns(sid)
        except Exception:
            return ""
        if not turns:
            return ""
        return getattr(turns[-1], "final_text", "") or ""

    def _has_done_marker(self, sid: int) -> bool:
        return "SUPERINTENDENT RESULT: DONE + PUSHED" in self._last_final_text(sid)

    def _should_aup_retry(self, row, sid: int) -> bool:
        from holler.superintendent.sanitize import is_aup_refusal

        if _AUP_RETRY_MARKER in (row["error"] or ""):
            return False
        return is_aup_refusal(self._last_final_text(sid))

    def _requeue_aup_refusals(self) -> None:
        """One-shot startup backfill: rescue items that failed on an API-side
        AUP classifier refusal before prompt sanitization existed. Each gets
        exactly one sanitized retry (the marker blocks a second requeue)."""
        from holler.superintendent.sanitize import is_aup_refusal

        for row in self._store.list_evidence(statuses=["failed"]):
            if _AUP_RETRY_MARKER in (row["error"] or ""):
                continue
            sid = row["dispatch_session_id"]
            if not isinstance(sid, int):
                continue
            if not is_aup_refusal(self._last_final_text(sid)):
                continue
            eid = int(row["id"])
            self._store.set_evidence_status(eid, "queued", error=_AUP_RETRY_MARKER)
            log.warning(
                "superintendent item %s (past AUP refusal) requeued with "
                "sanitized prompt", eid,
            )

    def _worker_terminal_state(self, sid: int, mode: str = "deploy") -> Optional[str]:
        """Terminal status | None(still running) for a worker session.

        deploy lane:        'deployed' | 'mr_opened' | 'failed'
        investigation lane: 'reported' | 'failed'

        'mr_opened' is the fallback outcome on a merge-request repo (Canvas):
        the deploy gate normally opens the MR *and* lands it, so a worker that
        reports only an open MR hit the gate's exit 3 (the operator set
        `superintendent_ship_policy = "merge_request"`) or exit 4 (the default
        branch is protected). The change is reviewable but has NOT deployed, so
        it is deliberately not recorded as deployed.

        The worker runs its whole pipeline in ONE turn and gets no follow-up
        PTT, so a turn that has ended is terminal for the item. The mandatory
        final result line is the primary verdict; is_error is only the fallback
        when the worker never emitted it.
        """
        try:
            turns = self._store.get_turns(sid)
        except Exception:
            return None
        if not turns:
            return None
        last = turns[-1]
        ended = getattr(last, "ended_at", 0) or 0
        if ended <= 0:
            return None
        final = getattr(last, "final_text", "") or ""
        if mode == "investigation":
            if "IR RESULT: COMPLETE" in final:
                return "reported"
            if "IR RESULT: BLOCKED" in final or "IR RESULT: NOT DONE" in final:
                return "failed"
            return "failed" if getattr(last, "is_error", False) else "reported"
        if "SUPERINTENDENT RESULT: DONE + PUSHED" in final:
            return "deployed"
        if "SUPERINTENDENT RESULT: DONE + MR OPENED" in final:
            return "mr_opened"
        if "SUPERINTENDENT RESULT: NOT DONE" in final:
            return "failed"
        return "failed" if getattr(last, "is_error", False) else "deployed"

    def _branch_landed(self, sid: int) -> bool:
        """True when this worker's branch is merged into its repo's
        origin/<default branch>.

        Checks the SESSION's repo (a project repo such as Canvas, recorded on
        the session row) — not blindly cfg.repo_cwd — and that repo's own
        default branch on its own origin (GitHub or GitLab), resolved with the
        gate's helper so the two never disagree. Two independent proofs:

          1. a commit on origin/<default> whose message names the branch —
             the gate's `superintendent: land holler/<sid>-…` and GitLab's
             `Merge branch 'holler/<sid>-…' into 'main'` both match;
          2. the branch tip is an ancestor of origin/<default> (fast-forward
             or rebase-free MR merges), counted only when that tip was
             committed after the session started AND that tip is not the
             branch's fork point (its reflog's creation entry) — a branch with
             no worker commits sits on its fork point, which is trivially an
             ancestor and is often only minutes old.

        Both are bounded to history after the session was created, so an old
        `holler/<n>-…` branch from another Holler install can't masquerade.
        """
        import subprocess

        from holler.superintendent.deploy_gate import default_branch

        sess = None
        try:
            sess = self._store.get_session(sid)
        except Exception:
            sess = None
        repo = (getattr(sess, "repo_cwd", "") or "") if sess is not None else ""
        if not repo:
            cfg = self._cfg()
            repo = str(getattr(cfg, "repo_cwd", "")) if cfg else ""
        if not repo:
            return False
        branch = (getattr(sess, "worktree_branch", "") or "") if sess is not None else ""
        try:
            created = float(getattr(sess, "created_at", 0) or 0) if sess is not None else 0.0
        except (TypeError, ValueError):
            created = 0.0
        since = int(created) - 300 if created > 0 else 0  # clock-skew slack

        def _git(*args, timeout=30):
            return subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout,
            )

        try:
            _git("fetch", "origin", "-q", timeout=60)
            base = default_branch(repo)
            target = f"origin/{base}"
            needle = branch if branch else f"holler/{sid}-"
            args = ["log", target, "--oneline", "-1", "--fixed-strings", "--grep", needle]
            if since:
                args.insert(2, f"--since=@{since}")
            p = _git(*args)
            if p.returncode == 0 and (p.stdout or "").strip():
                return True
            if branch and since:
                tip = _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
                sha = (tip.stdout or "").strip()
                if tip.returncode == 0 and sha:
                    ct = _git("show", "-s", "--format=%ct", sha)
                    committed = int((ct.stdout or "0").strip() or 0)
                    if committed >= since and self._worker_committed(_git, branch, sha, created):
                        anc = _git("merge-base", "--is-ancestor", sha, target)
                        return anc.returncode == 0
            return False
        except Exception:
            return False

    @staticmethod
    def _worker_committed(git, branch: str, tip: str, created: float) -> bool:
        """True when the branch tip is a commit the worker made, not the
        commit the branch was forked from.

        An untouched branch sits on its fork point, which is trivially an
        ancestor of the default branch — and that fork point is routinely a
        commit only minutes old (another worker's land, the operator's last
        commit), so a commit-time window can't tell the two apart. The
        branch's reflog can: its oldest entry is the creation ("branch:
        Created from …") sha. Without a reflog, fall back to a strict
        committed-after-session-start check (no clock-skew slack).
        """
        log_p = git("reflog", "show", "--format=%H", f"refs/heads/{branch}")
        shas = [s for s in (log_p.stdout or "").split() if s] if log_p.returncode == 0 else []
        if shas:
            return shas[-1] != tip
        ct = git("show", "-s", "--format=%ct", tip)
        try:
            committed = int((ct.stdout or "0").strip() or 0)
        except ValueError:
            return False
        return created > 0 and committed > created

    def _worktree_path(self, sid: int) -> str:
        try:
            sess = self._store.get_session(sid)
            return getattr(sess, "worktree_path", "") or ""
        except Exception:
            return ""

    def _cancel_session(self, sid: int) -> None:
        # Cancels the worker's in-flight asyncio task by local session id —
        # the real interrupt that stops a wedged worker from burning for hours.
        cancel_local = getattr(self._session, "cancel_local_session", None)
        if callable(cancel_local):
            try:
                if cancel_local(sid):
                    return
            except Exception:
                pass
        # Legacy fallback for Session impls without cancel_local_session.
        try:
            sess = self._store.get_session(sid)
            sdk_id = getattr(sess, "sdk_session_id", None)
        except Exception:
            sdk_id = None
        cancel = getattr(self._session, "cancel_session", None)
        if callable(cancel) and sdk_id:
            try:
                cancel(sdk_id)
            except Exception:
                pass

    # ----- fill open worker slots -----

    def _fill_slots(self) -> None:
        max_workers = self._max_workers()
        while not self._stop.is_set():
            active = len(self._store.active_evidence())
            if active >= max_workers:
                return
            row = self._store.claim_next_queued_evidence()
            if row is None:
                return
            try:
                merged = self._store.coalesce_siblings(
                    int(row["id"]), window_s=_COALESCE_WINDOW_S,
                )
                if merged:
                    refreshed = self._store.get_evidence(int(row["id"]))
                    if refreshed is not None:
                        row = refreshed
                    log.info(
                        "superintendent coalesced %d straggler(s) into evidence %s",
                        merged, int(row["id"]),
                    )
            except Exception:
                log.exception(
                    "superintendent coalesce failed for %s; dispatching as-is",
                    int(row["id"]),
                )
            try:
                self._spawn_worker(row)
            except Exception as e:
                log.exception("superintendent spawn failed for item %s", row["id"])
                self._store.set_evidence_status(
                    int(row["id"]), "failed", error=f"spawn error: {e}",
                )

    def _spawn_worker(self, row) -> None:
        eid = int(row["id"])
        transcript = row["transcript"] or ""
        mode = self._mode_for(row)
        shots = self._shots_for(row)
        primary = shots[0] if shots else None
        cfg = self._cfg()
        # Superintendent workers can run on a model chosen independently of the
        # interactive badge (empty override = inherit the global claude_model).
        # Read live from cfg each spawn so a selection applies to the next worker.
        model = ""
        if cfg:
            model = getattr(cfg, "superintendent_model", "") or getattr(cfg, "claude_model", "")
        # Only a real superintendent_model choice overrides the Session's
        # default; inheriting claude_model passes None so the live badge wins.
        model_override = (getattr(cfg, "superintendent_model", "") or None) if cfg else None

        profile = "investigation" if mode == "investigation" else "superintendent"

        # Which repo the worker works in: the evidence row's explicit project,
        # else the profile's default project, else cfg.repo_cwd (legacy). The
        # path is stamped on the session row, so the Session's worktree
        # registry cuts the worker's worktree from THAT repo — a Canvas worker
        # starts inside a managed Canvas worktree instead of the Holler repo.
        # An unknown explicit project raises → the item fails visibly.
        from holler.projects import resolve_project_name, resolve_session_repo

        requested = self._project_for(row)
        repo = resolve_session_repo(cfg, project=requested, profile=profile)
        project = resolve_project_name(cfg, project=requested, profile=profile) if repo else ""
        # The "you are in a managed worktree" promise only holds when the live
        # Session actually cuts worktrees (per_session_worktrees on, and the
        # registry came up). In shared-checkout mode a project worker's cwd is
        # the operator's own clone of that repo — telling it otherwise makes
        # it commit straight onto whatever branch the operator has parked.
        managed = getattr(self._session, "worktree_manager", _UNKNOWN) is not None
        first_prompt = self._compose_prompt(
            eid, transcript, len(shots), mode, project=project,
            managed_worktree=managed,
        )

        sid = self._store.create_session(
            model=model,
            transcript=first_prompt,
            screenshot_path=primary,
            profile=profile,
            title=self._title_for(transcript, eid, mode),
            repo_cwd=repo,
        )
        turn_id = self._store.add_turn(
            session_id=sid,
            transcript=first_prompt,
            screenshot_path=primary,
            job_id=None,
        )

        append = self._build_worker_append(mode)
        image_b64 = self._load_image_b64(primary)
        extra_images = [b for b in (self._load_image_b64(s) for s in shots[1:]) if b]

        self._session.submit(
            image_b64,
            first_prompt,
            local_session_id=sid,
            local_turn_id=turn_id,
            system_prompt_append_override=append,
            extra_images=extra_images,
            model_override=model_override,
            # Background lane: workers hold their concurrency slot for the
            # whole 6-step turn (up to 45 min) — keep them out of the
            # operator's interactive pool so PTTs never queue behind them.
            lane="background",
        )
        self._store.set_evidence_status(eid, "working", dispatch_session_id=sid)
        log.info("superintendent item %s dispatched -> worker session %s", eid, sid)

    # ----- helpers -----

    @staticmethod
    def _age(ts) -> float:
        try:
            return time.time() - float(ts or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _mode_for(row) -> str:
        """Evidence lane for a row — 'investigation' (IR) or 'deploy' (default).

        Guards the pre-`mode` schema: any row without a recognised value (old
        DBs, NULLs) falls back to the deploy remediation lane.
        """
        try:
            m = row["mode"]
        except Exception:
            m = None
        return m if m in ("deploy", "investigation") else "deploy"

    @staticmethod
    def _project_for(row) -> str:
        """Per-request project override on an evidence row ('' = none).
        Guards the pre-v15 schema (no `project` column)."""
        try:
            p = row["project"]
        except (KeyError, IndexError):
            return ""
        return p.strip() if isinstance(p, str) else ""

    @staticmethod
    def _title_for(transcript: str, eid: int, mode: str = "deploy") -> str:
        tag = "Investigate" if mode == "investigation" else "Deploy"
        head = (transcript or "").strip().split("\n", 1)[0][:52]
        return f"[{tag} #{eid}] {head}" if head else f"[{tag} #{eid}]"

    @staticmethod
    def _compose_prompt(
        eid: int, transcript: str, n_shots: int = 1, mode: str = "deploy",
        *, project: str = "", managed_worktree: bool = True,
    ) -> str:
        from holler.superintendent.sanitize import paraphrase_note, sanitize_evidence_text

        # The API-side safety classifier refuses requests containing certain
        # literal phrases regardless of framing — send the paraphrased words,
        # keep the raw transcript only in the local ledger.
        clean, changed = sanitize_evidence_text(transcript)
        note = f"\n\n{paraphrase_note()}" if changed else ""
        # Project workers start inside a Holler-managed worktree of that repo.
        # Say so, so they never improvise their own clone or /tmp worktree.
        if not project:
            where = ""
        elif managed_worktree:
            where = (
                f" Your cwd is a Holler-managed git worktree of the {project} repo, "
                f"branched from its default branch — work and commit only there; "
                f"do not create other clones or worktrees."
            )
        else:
            where = (
                f" Your cwd is the operator's shared checkout of the {project} "
                f"repo, NOT a Holler-managed worktree — do not edit or commit "
                f"in it. First create your own git worktree on a new branch off "
                f"its default branch (as your profile's fallback describes) and "
                f"work and commit only there."
            )

        if mode == "investigation":
            shot_note = ""
            if n_shots > 1:
                shot_note = (
                    f" The operator chained {n_shots} screenshots — treat them as "
                    f"facets of the SAME incident."
                )
            return (
                f"[IR evidence #{eid}] The operator captured this while looking at "
                f"their project. This is an incident to investigate — a bug/gap "
                f"report about the product's own UI or pipeline.{shot_note}{where} "
                f"Run your 4-step IR protocol: analyse to root cause, then report "
                f"back in AT MOST 3-4 sentences. Do NOT deploy.\n\nEvidence: {clean}{note}".strip()
            )

        # Default repo keeps the legacy wording; a project ships per the
        # profile's policy for that repo (Canvas: branch + merge request).
        ship = (
            f"ship it exactly as your profile prescribes for {project}"
            if project else "deploy"
        )
        shot_note = ""
        if n_shots > 1:
            shot_note = (
                f" The operator attached {n_shots} screenshots — they belong to "
                f"the SAME issue, treat them together."
            )
        return (
            f"[Superintendent evidence #{eid}] The operator captured this while "
            f"looking at their project. This is a bug/gap report about the "
            f"product's own UI or pipeline.{shot_note}{where} Run the full "
            f"6-step pipeline to completion and {ship}.\n\nEvidence: {clean}{note}".strip()
        )

    @staticmethod
    def _shots_for(row) -> list:
        """Ordered screenshot paths for an evidence row — the JSON array when
        present, else the single legacy path."""
        import json

        try:
            raw = row["screenshot_paths"]
        except Exception:
            raw = None
        if raw:
            try:
                paths = json.loads(raw)
                if isinstance(paths, list):
                    return [p for p in paths if p]
            except Exception:
                pass
        single = row["screenshot_path"]
        return [single] if single else []

    def _build_worker_append(self, mode: str = "deploy") -> str:
        from holler.sysprompt import build_append
        from holler.superintendent.override import authority_override_for_profile

        # Override is appended AFTER build_append's output (which ends with the
        # operator's personal rules) so it is the true last-wins layer. Keyed by
        # profile so interactive continuations (runners/full.py) re-derive the
        # same authority from the session row — see authority_override_for_profile.
        profile = "investigation" if mode == "investigation" else "superintendent"
        return build_append(profile=profile) + authority_override_for_profile(profile)

    @staticmethod
    def _load_image_b64(screenshot_path: Optional[str]) -> Optional[str]:
        if not screenshot_path:
            return None
        try:
            p = Path(screenshot_path)
            if not p.exists():
                return None
            return base64.b64encode(p.read_bytes()).decode("ascii")
        except Exception:
            log.exception("failed to load evidence screenshot %s", screenshot_path)
            return None
