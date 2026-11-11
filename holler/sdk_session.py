"""Pooled ClaudeSDKClient session manager.

Each `submit()` schedules an independent asyncio.Task. On execute we:
  - look up a pooled ClaudeSDKClient keyed by `sdk_session_id`; if present and
    options (model / append / cwd) match, reuse it (no `connect()` cost)
  - otherwise spin up a fresh client and `connect()` it
  - send one streaming-input image+text turn via `query()`
  - a per-client TurnRouter (holler/turn_router.py) reads every message the
    client streams and resolves the turn's ResultMessage; it also turns
    background-agent completions into agent-driven follow-up turns
  - on success: install/refresh the pool entry (DO NOT disconnect — the next
    turn for this session will pick up the live client)
  - on failure/cancel/drain-without-terminal: evict + disconnect

Concurrency is capped by `asyncio.Semaphore` (default 8). Per-`sdk_session_id`
`asyncio.Lock` serializes turns AND mediates between the turn handler and the
idle reaper (a periodic task that disposes clients idle > pool_idle_timeout).

The dedicated asyncio thread + `Session.start()/stop()` lifecycle is preserved
from the prior architecture; only the per-job execution model changed.

Cold-start cost saved by pooling: ~800ms-5s per resume turn on Windows
(Node.js CLI subprocess spawn + handshake).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)

from holler import claude_cli
from holler.lint_hook import post_tool_use_hook
from holler.turn_router import PumpClosed, TurnRouter

log = logging.getLogger(__name__)


# Advisory lint hook. It runs the project's own linter on the edited file and
# feeds findings back as non-blocking additionalContext; see
# holler/lint_hook.py.
#
# SDK 0.1.81: `setting_sources=None` (the default; holler leaves it unset)
# passes no --setting-sources flag, so the CLI loads ALL
# filesystem settings — user (~/.claude/settings.json), project and local —
# which is what gives holler sessions parity with a terminal `claude` run
# (user skills, CLAUDE.md, hooks). If one of those settings files already
# registers the lint_on_edit PostToolUse hook, registering the in-process
# copy as well makes every Edit/Write lint twice and inject duplicate
# findings. So the in-process hook is only a fallback for machines whose
# settings don't already carry it; see `_hooks_for_cwd`.
_LINT_HOOKS = {
    "PostToolUse": [
        HookMatcher(matcher="Edit|Write|MultiEdit", hooks=[post_tool_use_hook]),
    ],
}

# Substring identifying the settings-registered lint hook command
# (e.g. "python3 ~/.claude/hooks/lint_on_edit.py").
_LINT_HOOK_MARKER = "lint_on_edit"

# Generous stdout line buffer for the CLI transport. The SDK default (1MB)
# kills the session with "JSON message exceeded maximum buffer size" whenever
# a single message (big tool result, image-bearing replay, long Read) exceeds
# it.
_MAX_BUFFER_SIZE = 32 * 1024 * 1024


def _settings_file_has_lint_hook(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    hooks = (data or {}).get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False
    for matcher in hooks.get("PostToolUse") or []:
        if not isinstance(matcher, dict):
            continue
        for h in matcher.get("hooks") or []:
            if isinstance(h, dict) and _LINT_HOOK_MARKER in str(h.get("command", "")):
                return True
    return False


def _settings_register_lint_hook(cwd: Optional[str]) -> bool:
    """True if user/project/local settings the CLI will load (setting_sources
    =None) already register the lint_on_edit PostToolUse hook."""
    candidates = [Path.home() / ".claude" / "settings.json"]
    if cwd:
        base = Path(cwd)
        candidates.append(base / ".claude" / "settings.json")
        candidates.append(base / ".claude" / "settings.local.json")
    return any(_settings_file_has_lint_hook(p) for p in candidates)


# Prompt-cache lifetime for every request a session makes, main conversation and
# subagents alike. Claude Code defaults to 1 hour on a subscription, which bills
# each cache write at 2x input; 5 minutes bills it at 1.25x and only costs a
# full re-write after a pause longer than 5 minutes. Measured on the operator's
# own transcripts (`python -m holler.cost_audit`, the "prompt-cache TTL" line)
# 5 minutes was ~7% cheaper. It changes billing only, never what the model sees.
_PROMPT_CACHE_TTL = "5m"
_PROMPT_CACHE_TTL_VARS = (
    "CLAUDE_CODE_PROMPT_CACHE_TTL",
    "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
)


def _prompt_cache_env() -> dict[str, str]:
    """Env that pins the prompt-cache TTL, unless the operator set it already.

    A value already in Holler's own environment wins, so exporting either
    variable before launch (e.g. ``=1h``) overrides this without a code change.
    """
    return {
        var: os.environ.get(var) or _PROMPT_CACHE_TTL
        for var in _PROMPT_CACHE_TTL_VARS
    }


def _hooks_for_cwd(cwd: Optional[str]) -> Optional[dict]:
    """In-process hooks to register for a turn — None when settings already
    supply the lint hook (avoid running it twice)."""
    if _settings_register_lint_hook(cwd):
        return None
    return _LINT_HOOKS


_DEFAULT_CONCURRENCY = 8


def _extract_usage(usage) -> dict:
    """Pull token counts from an SDK usage object (dict or attribute style)."""
    def _g(name: str) -> int:
        v = None
        if isinstance(usage, dict):
            v = usage.get(name)
        else:
            v = getattr(usage, name, None)
        try:
            return int(v) if v is not None else 0
        except Exception:
            return 0
    return {
        "input_tokens": _g("input_tokens"),
        "output_tokens": _g("output_tokens"),
        "cache_creation_input_tokens": _g("cache_creation_input_tokens"),
        "cache_read_input_tokens": _g("cache_read_input_tokens"),
    }


@dataclass
class SessionEvent:
    job_id: int
    # "submitted" | "started" | "session_init" | "text_delta" | "tool_use"
    # | "tool_result" | "thinking" | "result" | "error"
    # | "followup" (an agent-driven turn opened by the CLI itself)
    # | "agent_launched" | "agent_started" | "agent_progress"
    # | "agent_activity" | "agent_done" (subagents; see turn_router)
    # ("session_rotated" is deprecated; never emitted under the ephemeral model.)
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


EventCallback = Callable[[SessionEvent], None]


@dataclass
class _PoolEntry:
    """One pooled, connected ClaudeSDKClient ready for the next turn on a session."""
    client: Any  # ClaudeSDKClient — typed Any to avoid SDK type leak in repr
    model: str
    append: str
    cwd: str
    last_used: float
    # True once a replay block has been injected for this client. Replay is
    # one-shot per process spawn: the first submit for a fresh subprocess
    # rebuilds context; subsequent turns reuse the warm in-process model
    # state and do NOT replay (would be redundant and double-billed).
    replayed_in_process: bool = False
    # The client's single message reader (see holler/turn_router.py).
    router: Optional[TurnRouter] = None

    def idle_for(self, now: float) -> float:
        last = self.last_used
        if self.router is not None:
            last = max(last, self.router.last_message_at)
        return now - last


# Reap pooled clients idle longer than this. Long enough to absorb a typical
# "think, edit, talk again" cycle; short enough that abandoned sessions don't
# pile up Node subprocesses in the background.
_POOL_IDLE_TIMEOUT_S = 600.0  # 10 minutes
_POOL_REAP_INTERVAL_S = 60.0
# A client whose subagents are still running is not reaped — unless it has
# streamed nothing at all for this long (a wedged agent).
_AGENT_STALL_S = 1800.0

# Shutdown budget (Session.stop). The SDK's own close() waits up to 5s for the
# CLI to exit on stdin EOF, then 5s more after SIGTERM — and a CLI mid-turn
# (e.g. a Superintendent worker) ignores EOF until its turn ends. stop() used
# to outlive its timeout on such a close, the loop then cancelled it, and the
# SDK dropped the process from its atexit kill list on the way out: the `claude`
# CLI survived holler as an orphan (PPID 1). So stop() gives clients a short
# graceful window, then terminates — and finally kills — whatever is left.
_SHUTDOWN_GRACE_S = 1.2
_SHUTDOWN_TERM_S = 0.5
_SHUTDOWN_SETTLE_S = 0.3


def _client_process(client: Any) -> Any:
    """The CLI subprocess behind a ClaudeSDKClient, or None (not connected /
    already disconnected / unknown SDK internals)."""
    for transport in (
        getattr(client, "_transport", None),
        getattr(getattr(client, "_query", None), "transport", None),
    ):
        proc = getattr(transport, "_process", None)
        if proc is not None:
            return proc
    return None


def _sdk_live_processes() -> list:
    """Every CLI subprocess the SDK currently tracks in this process (it keeps
    them for its own atexit SIGTERM). Empty if the SDK internals moved."""
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli
    except Exception:
        return []
    try:
        return list(getattr(subprocess_cli, "_ACTIVE_CHILDREN", ()) or ())
    except Exception:
        return []


async def _terminate_processes(procs: list, wait_s: float) -> int:
    """SIGTERM every still-running process, wait up to `wait_s` for them to
    exit, then SIGKILL the stragglers. Returns how many needed a signal."""
    alive = [p for p in procs if getattr(p, "returncode", 0) is None]
    if not alive:
        return 0
    for p in alive:
        try:
            p.terminate()
        except Exception:
            pass

    async def _wait(p) -> None:
        try:
            await p.wait()
        except Exception:
            pass

    waiters = [asyncio.ensure_future(_wait(p)) for p in alive]
    await asyncio.wait(waiters, timeout=wait_s)
    killed = [p for p in alive if getattr(p, "returncode", 0) is None]
    for p in killed:
        try:
            p.kill()
        except Exception:
            pass
    if killed:
        await asyncio.wait(waiters, timeout=_SHUTDOWN_SETTLE_S)
        log.warning("force-killed %d SDK subprocess(es) on shutdown", len(killed))
    for w in waiters:
        if not w.done():
            w.cancel()
    return len(alive)


@dataclass
class _Job:
    id: int
    image_b64: Optional[str]
    transcript: str
    submitted_at: float
    model_override: Optional[str] = None
    resume_session_id: Optional[str] = None
    # Phase 2 correlation IDs — set by the dispatcher before submit().
    local_session_id: Optional[int] = None
    local_turn_id: Optional[int] = None
    # V3 "smart batch" merge: when rapid PTTs target a busy session, additional
    # (image_b64, transcript, local_turn_id) tuples accumulate here. They fire
    # as a single multi-content SDK call so Claude reasons across them at once.
    extra_content: list[dict] = field(default_factory=list)
    # Once the SDK call has started, no more merges are accepted for this Job.
    fired: bool = False
    # Optional per-job system_prompt_append override. When set, used instead
    # of `Session._system_prompt_append`. Lets the dispatcher inject a profile-
    # specific append for new sessions without mutating shared state.
    system_prompt_append_override: Optional[str] = None
    # Concurrency lane: "interactive" (operator-driven) or "background"
    # (Superintendent workers). Each lane has its own semaphore so background
    # workers can't starve the operator's sessions.
    lane: str = "interactive"


class Session:
    def __init__(
        self,
        repo_cwd: str,
        model: str,
        system_prompt_append: str,
        on_event: EventCallback,
        concurrency: int = _DEFAULT_CONCURRENCY,
        is_terminal: Optional[Callable[[int], bool]] = None,
        worktree_manager: Optional[Any] = None,
        replay_loader: Optional[Callable[[int], str]] = None,
        session_repo_resolver: Optional[Callable[[int], Optional[str]]] = None,
    ):
        self._repo_cwd = repo_cwd
        # Shared-checkout mode only (no worktree manager): maps a local session
        # id to the project repo recorded on its row ('' / None = repo_cwd).
        # In worktree mode the WorktreeRegistry does this routing itself.
        self._session_repo_resolver = session_repo_resolver
        self._model = model  # default model — read at submit-time, hot-mutable
        self._system_prompt_append = system_prompt_append
        self._on_event = on_event
        self._concurrency = max(1, concurrency)
        # Stage 1: if set, sessions with a local_session_id get their own
        # git worktree at acquire time. Absent → fall back to shared cwd
        # (legacy behavior). Type-hinted as Any to avoid a circular import;
        # actual type is `holler.worktree_manager.WorktreeManager`.
        self._worktree_manager = worktree_manager
        # Optional readback into JobStore — used by _done_cb to detect tasks
        # that returned without ever delivering a terminal SessionEvent (Qt
        # cross-thread bridge race, hot-restart edge cases). Wired by the
        # composer; absent in standalone Session use, in which case the
        # reaper short-circuits.
        self._is_terminal = is_terminal
        # Pure callable that builds a "prior conversation" replay block from
        # Holler's SQLite store, given a local_session_id. Called whenever a
        # fresh SDK subprocess is spawned for a session that already has
        # turns on disk — we prepend the returned text as the model's first
        # input so it picks up context from the actual transcript instead of
        # relying on the SDK's internal JSONL (which may be missing after a
        # Holler restart or worktree teardown). Returns "" for new sessions
        # with no prior turns. Wired by the composer; None = no replay.
        self._replay_loader = replay_loader
        self._replay_loader_takes_turn: Optional[bool] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._in_flight: set[asyncio.Task] = set()
        self._tasks_by_job: dict[int, asyncio.Task] = {}
        self._local_sid_by_job: dict[int, int] = {}
        self._resume_locks: dict[str, asyncio.Lock] = {}
        self._resume_lock_refs: dict[str, int] = {}  # ref counts for prune
        # Persistent ClaudeSDKClient pool keyed by sdk_session_id. First turn
        # of a fresh session creates a client; once session_init delivers the
        # sid we install the connected client here. Subsequent turns reuse
        # the cached client, skipping the 1.5-5s Node CLI cold start.
        self._client_pool: dict[str, _PoolEntry] = {}
        # Clients a running turn is using right now (fresh or borrowed from
        # the pool). Lets stop() find the subprocess of a turn that is
        # mid-flight — the pool only holds clients between turns.
        self._active_clients: set[Any] = set()
        # Latest (client, router) per Holler session row — how the UI reaches
        # a session's subagents (stop_agent) and agent-driven turns (cancel).
        self._routers_by_local_sid: dict[int, tuple[Any, TurnRouter]] = {}
        self._reaper_task: Optional[asyncio.Task] = None

        # V3 "smart batch": pending non-fired Job per sdk_session_id. New PTTs
        # into the same session merge into this Job's extra_content instead of
        # creating another SDK call.
        self._pending_by_sid: dict[str, _Job] = {}
        self._merge_lock = threading.Lock()

        self._next_job_id = 1
        self._id_lock = threading.Lock()
        self._stopping = threading.Event()

    # ----- thread-safe knobs -----

    def set_default_model(self, model: str) -> None:
        """Mutate the default model used when a job has no per-job override.

        Atomic in CPython (single attribute assignment); each new job reads it
        once when constructing ClaudeAgentOptions.
        """
        self._model = model

    def set_repo_cwd(self, cwd: str) -> None:
        self._repo_cwd = cwd

    @property
    def worktree_manager(self) -> Optional[Any]:
        return self._worktree_manager

    def set_worktree_manager(self, manager: Optional[Any]) -> None:
        """Swap the per-session worktree manager (e.g. after repo_cwd changes
        in Settings). Takes effect for the next turn's acquire; pooled clients
        bound to old cwds are dropped automatically because the pool entry's
        cwd no longer matches."""
        self._worktree_manager = manager

    def set_system_prompt_append(self, text: str) -> None:
        self._system_prompt_append = text

    # ----- lifecycle -----

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def _runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            # One pool per lane. Interactive turns (operator PTTs) never wait
            # behind background workers (Superintendent), and vice versa.
            self._semaphores = {
                "interactive": asyncio.Semaphore(self._concurrency),
                "background": asyncio.Semaphore(self._concurrency),
            }
            # Schedule the idle-pool reaper on this loop so it runs alongside
            # turn tasks. Self-cancels in stop() via the _stopping event.
            self._reaper_task = self._loop.create_task(self._reap_idle_clients())
            ready.set()
            try:
                self._loop.run_forever()
            finally:
                # Cancel anything still in flight; gather to drain cancellations.
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self._loop.close()

        self._thread = threading.Thread(target=_runner, name="holler-sdk", daemon=True)
        self._thread.start()
        ready.wait(timeout=5.0)

    def stop(self, reap_all_sdk_children: bool = False) -> None:
        """Cancel every turn, disconnect every client and make sure each CLI
        subprocess is gone, within ~2s (see _SHUTDOWN_GRACE_S).

        `reap_all_sdk_children`: also terminate SDK subprocesses this Session
        didn't spawn itself but that run on its loop (AutoTitler attaches to
        it). Only the app composer passes True — it owns the whole process.
        """
        if self._loop is None:
            return
        self._stopping.set()

        async def _cancel_all():
            await self._shutdown(reap_all_sdk_children)

        budget = _SHUTDOWN_GRACE_S + _SHUTDOWN_TERM_S + 2 * _SHUTDOWN_SETTLE_S + 1.0
        try:
            asyncio.run_coroutine_threadsafe(_cancel_all(), self._loop).result(timeout=budget)
        except Exception:
            log.exception("error cancelling in-flight jobs")

        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _owned_processes(self, include_foreign: bool) -> list:
        clients = [e.client for e in self._client_pool.values()]
        clients.extend(self._active_clients)
        procs = [p for p in (_client_process(c) for c in clients) if p is not None]
        if include_foreign:
            procs.extend(_sdk_live_processes())
        return procs

    async def _shutdown(self, include_foreign: bool) -> None:
        # Snapshot subprocesses FIRST: a client's disconnect() forgets its
        # process (and the SDK drops it from its own kill list) even when the
        # disconnect is cancelled half-way, so they can't be found afterwards.
        procs = self._owned_processes(include_foreign)

        tasks = [t for t in self._in_flight if not t.done()]
        for t in tasks:
            t.cancel()  # each turn's finally disconnects its own client
        # Tear down pooled clients alongside. Each owns a Node subprocess that
        # would otherwise linger after our event loop closes.
        pooled = [e.client for e in self._client_pool.values()]
        self._client_pool.clear()

        async def _disconnect(client) -> None:
            try:
                await client.disconnect()
            except Exception:
                pass

        pending = tasks + [asyncio.ensure_future(_disconnect(c)) for c in pooled]
        if pending:
            await asyncio.wait(pending, timeout=_SHUTDOWN_GRACE_S)

        # Re-snapshot: a turn may have spawned its subprocess while we waited.
        seen: set[int] = set()
        unique = []
        for p in procs + self._owned_processes(include_foreign):
            if id(p) not in seen:
                seen.add(id(p))
                unique.append(p)
        await _terminate_processes(unique, _SHUTDOWN_TERM_S)

        still = [t for t in pending if not t.done()]
        if still:
            # Their subprocesses are dead now, so the disconnects unblock.
            await asyncio.wait(still, timeout=_SHUTDOWN_SETTLE_S)

    # ----- public API (thread-safe) -----

    def submit(
        self,
        image_b64: Optional[str],
        transcript: str,
        model_override: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        local_session_id: Optional[int] = None,
        local_turn_id: Optional[int] = None,
        system_prompt_append_override: Optional[str] = None,
        extra_images: Optional[list[str]] = None,
        lane: str = "interactive",
    ) -> int:
        """Submit a turn. Returns the job id immediately.

        - `model_override`: per-job model id; falls back to `self._model`.
        - `resume_session_id`: if set, opens the SDK with `resume=<id>` so prior
          conversation history loads. Holler enforces a per-resume lock so only
          one turn at a time touches that JSONL store.
        - `local_session_id` / `local_turn_id`: correlation IDs assigned by the
          dispatcher BEFORE calling submit; forwarded in the "submitted" event
          so JobStore can match incoming events back to a Store row.
        - `lane`: "interactive" (default) or "background". Each lane has its
          own concurrency semaphore so autonomous workers never queue-starve
          operator turns.
        """
        if self._loop is None:
            raise RuntimeError("Session not started")
        if self._stopping.is_set():
            raise RuntimeError("Session is stopping")

        effective_model = model_override or self._model

        # Smart batch: if this PTT resumes a session and there's already a
        # not-yet-fired Job pending for that session, merge into it.
        if resume_session_id:
            with self._merge_lock:
                pending = self._pending_by_sid.get(resume_session_id)
                if pending is not None and not pending.fired:
                    pending.extra_content.append({
                        "image_b64": image_b64,
                        "transcript": transcript,
                        "local_turn_id": local_turn_id,
                    })
                    merge_count = 1 + len(pending.extra_content)  # primary + extras
                    # Tell consumers a new PTT joined an existing Job. JobStore
                    # treats this as "another turn belongs to job N" instead of
                    # creating a new Job.
                    self._on_event(SessionEvent(
                        pending.id, "merged_in",
                        {
                            "transcript": transcript,
                            "model": effective_model,
                            "resume_session_id": resume_session_id,
                            "local_session_id": local_session_id,
                            "local_turn_id": local_turn_id,
                            "merged_into_job_id": pending.id,
                            "merge_count": merge_count,
                        },
                    ))
                    return pending.id

        with self._id_lock:
            job_id = self._next_job_id
            self._next_job_id += 1

        job = _Job(
            id=job_id,
            image_b64=image_b64,
            transcript=transcript,
            submitted_at=time.time(),
            model_override=model_override,
            resume_session_id=resume_session_id,
            local_session_id=local_session_id,
            local_turn_id=local_turn_id,
            system_prompt_append_override=system_prompt_append_override,
            lane=lane if lane in ("interactive", "background") else "interactive",
        )

        # Seed additional images into the FIRST turn (image-only extras render as
        # more image blocks in the same user message — see _gen). Used by the
        # Superintendent worker to attach every chained screenshot at once.
        if extra_images:
            for img in extra_images:
                if img:
                    job.extra_content.append(
                        {"image_b64": img, "transcript": "", "local_turn_id": None}
                    )

        # Synchronous "submitted" event so the panel can show the row instantly.
        self._on_event(SessionEvent(
            job_id, "submitted",
            {
                "transcript": transcript,
                "model": effective_model,
                "resume_session_id": resume_session_id,
                "local_session_id": local_session_id,
                "local_turn_id": local_turn_id,
            },
        ))

        # Register as the pending Job for this session BEFORE scheduling, so
        # subsequent submits can find it for merging.
        if resume_session_id:
            with self._merge_lock:
                self._pending_by_sid[resume_session_id] = job

        future = asyncio.run_coroutine_threadsafe(self._spawn(job), self._loop)
        return job_id

    def will_reuse_client(
        self,
        resume_session_id: Optional[str],
        *,
        model_override: Optional[str] = None,
        system_prompt_append_override: Optional[str] = None,
        margin_s: float = 60.0,
    ) -> bool:
        """Best-effort prediction (thread-safe read): will a turn submitted now
        for `resume_session_id` reuse the live pooled client — i.e. the same
        subprocess that already saw the previous turn's screenshot?

        Conservative: False unless a pool entry exists with matching model and
        append, and it is not close to being reaped (`margin_s` before the idle
        timeout). Callers use this to decide whether an unchanged screenshot
        may be skipped; when in doubt the image must be sent, because a fresh
        subprocess only gets a text replay of prior turns.
        """
        if not resume_session_id:
            return False
        entry = self._client_pool.get(resume_session_id)
        if entry is None:
            return False
        # A turn in flight (or queued) on this session could still fail and
        # evict the client before ours runs — don't bet on reuse then.
        lock = self._resume_locks.get(resume_session_id)
        if (lock is not None and lock.locked()) or self._resume_lock_refs.get(resume_session_id):
            return False
        model = model_override or self._model
        append = system_prompt_append_override
        if append is None:
            append = self._system_prompt_append
        if entry.model != model or entry.append != (append or ""):
            return False
        if self._worktree_manager is None and entry.cwd != self._repo_cwd:
            return False
        return (time.monotonic() - entry.last_used) < (_POOL_IDLE_TIMEOUT_S - margin_s)

    def warm_async(self) -> None:
        """Vestigial: ephemeral clients have no useful warm step. Kept so callers
        in `runners/full.py` and `runners/phase4.py` don't need to change."""
        return

    # ----- internal -----

    def cancel_local_session(self, local_session_id: int) -> bool:
        """Best-effort cancel of every in-flight job for a local session id.

        Used by the Superintendent reaper to actually stop a wedged worker —
        before this, the 45m timeout only flipped the ledger row while the
        turn kept streaming for hours.
        """
        if self._loop is None:
            return False

        async def _do():
            hit = False
            for jid, sid in list(self._local_sid_by_job.items()):
                if sid != local_session_id:
                    continue
                t = self._tasks_by_job.get(jid)
                if t is not None and not t.done():
                    t.cancel()
                    hit = True
            return hit

        try:
            fut = asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return bool(fut.result(timeout=2.0))
        except Exception:
            return False

    def cancel_job(self, job_id: int) -> bool:
        """Best-effort cancel of an in-flight or queued job."""
        if self._loop is None:
            return False
        async def _do():
            t = self._tasks_by_job.get(job_id)
            if t is not None and not t.done():
                t.cancel()
                return True
            # An agent-driven follow-up turn has no task of its own; it runs
            # inside the pooled client, so interrupt the client instead.
            for client, router in list(self._routers_by_local_sid.values()):
                if job_id and router.owner_job_id() == job_id:
                    try:
                        await client.interrupt()
                        return True
                    except Exception:
                        log.exception("interrupt for follow-up job %d failed", job_id)
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return bool(fut.result(timeout=2.0))
        except Exception:
            return False

    async def _reap_idle_clients(self) -> None:
        """Periodically dispose pooled clients idle longer than the timeout.

        Skips any pool key whose resume lock is currently held — that means a
        turn is in flight using the client. The reaper will catch it on the
        next tick after the lock releases.
        """
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(_POOL_REAP_INTERVAL_S)
                await self._reap_idle_once()
        except asyncio.CancelledError:
            return

    async def _reap_idle_once(self) -> None:
        """One reaper pass.

        Selection AND removal from the pool happen with no await in between,
        so they are atomic w.r.t. the event loop: a turn that grabs the
        resume lock after this point finds no pool entry and connects a fresh
        client, instead of picking up a client we are about to disconnect.
        (Previously each victim was popped only after awaiting the previous
        victim's disconnect, leaving a window for a turn to start using it.)
        """
        now = time.monotonic()
        victims: list[tuple[str, _PoolEntry]] = []
        for key, entry in list(self._client_pool.items()):
            idle = entry.idle_for(now)
            if idle <= _POOL_IDLE_TIMEOUT_S:
                continue
            if entry.router is not None and entry.router.has_live_agents() and idle < _AGENT_STALL_S:
                continue  # subagents still working — their results come back here
            lock = self._resume_locks.get(key)
            if lock is not None and lock.locked():
                continue  # active turn — defer
            self._client_pool.pop(key, None)
            victims.append((key, entry))
        for key, entry in victims:
            try:
                await entry.client.disconnect()
            except Exception:
                pass
            log.info("reaped idle pooled client for session %s", key)

    def _call_replay_loader(self, job: _Job) -> str:
        """Invoke the replay loader, excluding the turn being sent now (and
        anything after it, e.g. merged extras) from the replay — otherwise
        the in-flight request appears twice, once as a "(no reply produced)"
        prior turn. Loaders that take only the session id are still
        supported (legacy signature)."""
        loader = self._replay_loader
        if loader is None:
            return ""
        if self._replay_loader_takes_turn is None:
            try:
                inspect.signature(loader).bind(job.local_session_id, job.local_turn_id)
                self._replay_loader_takes_turn = True
            except (TypeError, ValueError):
                self._replay_loader_takes_turn = False
        if self._replay_loader_takes_turn:
            return loader(job.local_session_id, job.local_turn_id)
        return loader(job.local_session_id)

    def _release_pending_slot(self, job: _Job) -> None:
        """Stop `job` from accepting smart-batch merges and drop it from
        `_pending_by_sid` if it is still the merge target for its session.

        Must run whenever a job stops being a viable merge target: when it
        starts firing (`_execute`) AND when it ends without ever firing
        (cancelled/failed while queued). Otherwise every later PTT to that
        session is "merged" into a dead job forever. Thread-safe."""
        with self._merge_lock:
            job.fired = True
            sid = job.resume_session_id
            if sid and self._pending_by_sid.get(sid) is job:
                del self._pending_by_sid[sid]

    def _new_router(self, client: Any, job: _Job, model: str) -> TurnRouter:
        """Start the single message reader for a freshly connected client."""

        def _emit(job_id: int, kind: str, data: dict) -> None:
            self._on_event(SessionEvent(job_id, kind, data))

        def _open_followup(labels: list[str]) -> int:
            with self._id_lock:
                jid = self._next_job_id
                self._next_job_id += 1
            self._on_event(SessionEvent(jid, "followup", {
                "labels": labels,
                "model": model,
                "local_session_id": router.local_session_id,
                "resume_session_id": router.session_id,
            }))
            self._on_event(SessionEvent(jid, "started", {"model": model, "merge_count": 1}))
            return jid

        router = TurnRouter(emit=_emit, open_followup=_open_followup,
                            local_session_id=job.local_session_id)
        router.start(client)
        return router

    def stop_agent(self, local_session_id: int, tool_use_id: str) -> bool:
        """Ask the CLI to stop one subagent of a session (thread-safe). The
        CLI answers with a task_notification(status=stopped), which arrives
        as an ``agent_done`` event."""
        if self._loop is None:
            return False

        async def _do() -> bool:
            pair = self._routers_by_local_sid.get(local_session_id)
            if pair is None:
                return False
            client, router = pair
            task_id = router.task_id_for(tool_use_id)
            if not task_id or router.closed:
                return False
            await client.stop_task(task_id)
            return True

        try:
            fut = asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return bool(fut.result(timeout=5.0))
        except Exception:
            log.exception("stop_agent(%s, %s) failed", local_session_id, tool_use_id)
            return False

    @staticmethod
    def _turn_lock_key(job: _Job) -> Optional[str]:
        """Key that serializes turns targeting the same conversation: the
        Claude session id when known, else the local session row (a resumed
        session whose sdk id hasn't attached yet must still not run two turns
        at once in the same worktree)."""
        if job.resume_session_id:
            return job.resume_session_id
        if job.local_session_id is not None:
            return f"local:{job.local_session_id}"
        return None

    async def _spawn(self, job: _Job) -> None:
        """Schedule the ephemeral worker as a tracked task."""
        task = asyncio.create_task(self._run_job_ephemeral(job))
        self._in_flight.add(task)
        self._tasks_by_job[job.id] = task
        if job.local_session_id is not None:
            self._local_sid_by_job[job.id] = job.local_session_id

        def _done_cb(t: asyncio.Task) -> None:
            # A task cancelled before its coroutine ever ran never reaches
            # _run_job_ephemeral's finally — release the merge slot here too.
            self._release_pending_slot(job)
            self._in_flight.discard(t)
            self._tasks_by_job.pop(job.id, None)
            self._local_sid_by_job.pop(job.id, None)
            # Prune resume lock if no more refs.
            sid = self._turn_lock_key(job)
            if sid:
                refs = self._resume_lock_refs.get(sid, 1) - 1
                if refs <= 0:
                    self._resume_lock_refs.pop(sid, None)
                    self._resume_locks.pop(sid, None)
                else:
                    self._resume_lock_refs[sid] = refs
            # Reaper: if the task ended but JobStore never received a terminal
            # event (Qt thread bridge race, missed emit, etc.), synthesize one
            # so the row doesn't sit at status="running" forever.
            if self._is_terminal is not None:
                try:
                    if not self._is_terminal(job.id):
                        log.warning(
                            "job %d: task ended but JobStore not terminal — "
                            "emitting reaper error event",
                            job.id,
                        )
                        self._on_event(SessionEvent(
                            job.id, "error",
                            {"error": "task ended without terminal event"},
                        ))
                except Exception:
                    log.exception("job %d: reaper check failed", job.id)

        lock_key = self._turn_lock_key(job)
        if lock_key:
            self._resume_lock_refs[lock_key] = self._resume_lock_refs.get(lock_key, 0) + 1
        task.add_done_callback(_done_cb)

    async def _run_job_ephemeral(self, job: _Job) -> None:
        semaphore = self._semaphores.get(job.lane) or self._semaphores["interactive"]
        # Per-resume lock: serialize turns that target the same Claude session.
        resume_lock: Optional[asyncio.Lock] = None
        lock_key = self._turn_lock_key(job)
        if lock_key:
            resume_lock = self._resume_locks.setdefault(lock_key, asyncio.Lock())

        try:
            async with semaphore:
                if resume_lock is not None:
                    async with resume_lock:
                        await self._execute(job)
                else:
                    await self._execute(job)
        except asyncio.CancelledError:
            self._on_event(SessionEvent(job.id, "error", {"error": "cancelled"}))
            raise
        except Exception as e:
            log.exception("job %d failed", job.id)
            self._on_event(SessionEvent(job.id, "error", {"error": repr(e)}))
        finally:
            # Cancelled/failed while still queued (never reached _execute):
            # drop it as the session's merge target.
            self._release_pending_slot(job)

    async def _execute(self, job: _Job) -> None:
        # Lock the job so no further merges happen once we start firing.
        # Release the slot so the NEXT submit() opens a fresh merge target
        # instead of growing this one mid-flight, then snapshot the extras
        # (no merge can append once fired=True is set under the lock).
        self._release_pending_slot(job)
        with self._merge_lock:
            merged_extras = list(job.extra_content)

        # Terminal-event guarantee: the receive_response loop has two exit
        # paths — emit "result" on ResultMessage, or drain without yielding
        # one (transport hiccup, SDK version drift on the terminal-token
        # shape, server timeout). Without a terminal event JobStore leaves
        # the row at status="running" forever. Track that we saw one and
        # synthesize an "error" from finally if not.
        saw_terminal = False
        stale_cli = False

        model = job.model_override or self._model
        append_text = job.system_prompt_append_override
        if append_text is None:
            append_text = self._system_prompt_append

        # Resolve cwd: worktree mode → per-session worktree path; otherwise
        # the shared repo. WorktreeManager.acquire is idempotent — repeated
        # turns for the same session reuse the same worktree dir.
        job_cwd = self._repo_cwd
        if self._worktree_manager is not None and job.local_session_id is not None:
            try:
                wt_path = self._worktree_manager.acquire(job.local_session_id)
                job_cwd = str(wt_path)
            except Exception:
                # HALT: silent fallback to shared cwd was the failure mode that
                # let parallel Holler sessions land rogue commits on master on
                # 2026-05-12. If worktree acquire fails, surface the error and
                # refuse to run the turn rather than collide in the main checkout.
                log.exception(
                    "WorktreeManager.acquire failed for session %s; aborting turn",
                    job.local_session_id,
                )
                raise
        elif job.local_session_id is not None and self._session_repo_resolver is not None:
            # Same HALT rule: a lookup failure must abort the turn, never run a
            # project session in the default repo by accident.
            project_repo = self._session_repo_resolver(job.local_session_id)
            if project_repo:
                job_cwd = str(project_repo)

        # Note: SDK-side `resume=<id>` was removed in favor of Holler-owned
        # replay (see _replay_loader). Relying on the SDK's hidden JSONL at
        # `~/.claude/projects/<encoded-cwd>/<id>.jsonl` was the root cause of
        # the "session has no memory after Holler restart" bug — that JSONL
        # is often missing, stale, or empty after a restart. We now rebuild
        # context ourselves from Holler's SQLite store on every fresh
        # subprocess spawn; resume_session_id is kept only as a pool key and
        # a stable identifier in events.
        opts_kwargs: dict[str, Any] = {
            "permission_mode": "bypassPermissions",
            "model": model,
            "cwd": job_cwd,
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": append_text,
            },
            "max_buffer_size": _MAX_BUFFER_SIZE,
            # The SDK's bundled CLI lags the operator's `claude update`; a
            # stale one rejects newer models outright (see claude_cli).
            "cli_path": claude_cli.cli_path(),
            # Echo each user message at the start of the turn that answers it:
            # how the TurnRouter tells our turn from one the CLI starts by
            # itself when a background agent finishes.
            "extra_args": {"replay-user-messages": None},
        }
        hooks = _hooks_for_cwd(job_cwd)
        if hooks:
            opts_kwargs["hooks"] = hooks
        # Per-session env: lets slash commands (`/beam-start` etc.) know which
        # Holler session is invoking them, so they can write/check the
        # corresponding marker file. Missing var = "not running under Holler",
        # which keeps standalone Claude Code usage unchanged.
        env = _prompt_cache_env()
        if job.local_session_id is not None:
            env["HOLLER_SESSION_ID"] = str(job.local_session_id)
        opts_kwargs["env"] = env
        if job.local_session_id is not None:
            # Touch the aliveness marker. wipe_all() at Holler startup
            # already cleared any stale markers from a previous run, so
            # this is the canonical signal that "session X is live."
            try:
                from holler import session_locks
                session_locks.mark_alive(job.local_session_id)
            except Exception:
                log.exception("failed to mark session %s alive", job.local_session_id)

        # Pool lookup: if a connected client exists for this sdk_session_id
        # AND its options match this turn's, reuse it. Otherwise dispose any
        # stale entry and spin up a fresh client.
        pool_key = job.resume_session_id
        client = None
        router: Optional[TurnRouter] = None
        reused = False
        if pool_key:
            entry = self._client_pool.get(pool_key)
            if entry is not None:
                if (
                    entry.model == model
                    and entry.append == (append_text or "")
                    and entry.cwd == job_cwd
                    and entry.router is not None
                    and not entry.router.closed
                ):
                    client = entry.client
                    router = entry.router
                    reused = True
                else:
                    # Options changed (e.g. model swap, profile edit) or the
                    # client's stream died — drop the stale entry; we'll
                    # connect a fresh one below.
                    self._client_pool.pop(pool_key, None)
                    try:
                        await entry.client.disconnect()
                    except Exception:
                        pass

        if client is None:
            options = ClaudeAgentOptions(**opts_kwargs)
            client = ClaudeSDKClient(options=options)
        self._active_clients.add(client)

        # `captured_sid` is set when we observe a SystemMessage(init) carrying
        # a session_id. For fresh sessions this is how we learn the sid so the
        # client can be pooled after the turn completes.
        captured_sid: Optional[str] = None

        try:
            if not reused:
                await client.connect()
                router = self._new_router(client, job, model)
            assert router is not None
            self._routers_by_local_sid[job.local_session_id or 0] = (client, router)
            self._on_event(SessionEvent(
                job.id, "started",
                {"model": model, "merge_count": 1 + len(merged_extras)},
            ))

            # Holler-owned replay: when the SDK subprocess is fresh (not a
            # pool reuse) and the Holler DB has prior turns for this session,
            # prepend a synthetic "prior conversation" text block to the
            # user message. This is how the model recovers context across
            # Holler restarts, since the SDK's JSONL is no longer trusted.
            # `replay_text` is set when we should inject; "" or None means
            # skip (new session, no prior turns, no loader wired).
            replay_text = ""
            if (
                not reused
                and self._replay_loader is not None
                and job.local_session_id is not None
            ):
                try:
                    replay_text = self._call_replay_loader(job) or ""
                except Exception:
                    log.exception(
                        "replay_loader failed for local_session_id=%s; "
                        "falling through with no prior context",
                        job.local_session_id,
                    )
                    replay_text = ""

            async def _gen():
                content: list[dict[str, Any]] = []
                # Prior-conversation replay block (only when subprocess is
                # fresh and the session has history on disk). Lands FIRST in
                # the content list so the model reads context before the new
                # request. Prompt caching makes subsequent same-process turns
                # effectively free, but a Holler restart will pay this once.
                if replay_text:
                    content.append({"type": "text", "text": replay_text})
                # Primary content.
                if job.image_b64:
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64", "media_type": "image/png", "data": job.image_b64,
                        },
                    })
                content.append({"type": "text", "text": job.transcript or "(no transcript)"})
                # Merged extras: each is treated as another image+text pair
                # within the same user message. Claude sees them all together.
                for extra in merged_extras:
                    if extra.get("image_b64"):
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "base64", "media_type": "image/png",
                                "data": extra["image_b64"],
                            },
                        })
                    if extra.get("transcript"):
                        content.append({"type": "text", "text": extra["transcript"]})
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "parent_tool_use_id": None,
                }

            # Register before sending: the router hands this job the turn
            # that echoes its message (see holler/turn_router.py).
            try:
                turn_done = router.expect_turn(job.id)
                await client.query(_gen())
                msg = await turn_done
            except asyncio.CancelledError:
                router.abandon(job.id)
                raise
            except PumpClosed:
                msg = None
            if router.session_id:
                captured_sid = router.session_id
            if msg is not None:
                final_usage = _extract_usage(msg.usage) if getattr(msg, "usage", None) else {}
                self._on_event(SessionEvent(
                    job.id, "result",
                    {
                        "is_error": bool(msg.is_error),
                        "duration_ms": msg.duration_ms,
                        "num_turns": msg.num_turns,
                        "result": msg.result or "",
                        "stop_reason": msg.stop_reason,
                        "total_cost_usd": msg.total_cost_usd,
                        "usage": final_usage,
                    },
                ))
                saw_terminal = True
                too_old = claude_cli.parse_too_old(msg.result or "") if msg.is_error else None
                if too_old:
                    # The CLI this client runs predates the model. Re-probe
                    # (the operator may have just run `claude update`), don't
                    # pool the client so the next turn spawns fresh, and let
                    # the picker hide the model until a new-enough CLI exists.
                    claude_cli.reset()
                    stale_cli = True
                    try:
                        from holler import model_discovery
                        model_discovery.record_cli_requirement(model, too_old[1])
                    except Exception:
                        log.exception("failed to record CLI requirement for %s", model)
        finally:
            # Decide whether to keep the client alive in the pool. Only do so
            # on a clean turn — any exception, cancel, or drain-without-
            # ResultMessage disposes the client since it may be in a bad
            # state.
            try:
                import sys
                exc_in_flight = sys.exc_info()[0] is not None
                keep_alive = saw_terminal and not exc_in_flight and not stale_cli

                if keep_alive:
                    final_key = pool_key or captured_sid
                    if final_key:
                        new_entry = _PoolEntry(
                            client=client,
                            model=model,
                            append=(append_text or ""),
                            cwd=job_cwd,
                            last_used=time.monotonic(),
                            router=router,
                            # Replay (if any) just happened on this client; mark
                            # the pool entry so the next reuse path (which it
                            # will hit via `reused = True`) doesn't double-inject.
                            replayed_in_process=bool(replay_text),
                        )
                        old = self._client_pool.get(final_key)
                        if old is not None and old.client is not client:
                            # Lost a race (or a recreate happened mid-turn) —
                            # close the old loser before installing.
                            try:
                                await old.client.disconnect()
                            except Exception:
                                pass
                        self._client_pool[final_key] = new_entry
                    else:
                        # Successful turn with no usable sid — orphan client; dispose.
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                else:
                    # Failure / cancel / drained without terminal — evict any
                    # entry pointing at this client and disconnect.
                    if pool_key:
                        existing = self._client_pool.get(pool_key)
                        if existing is not None and existing.client is client:
                            self._client_pool.pop(pool_key, None)
                    if not saw_terminal and not exc_in_flight:
                        log.warning(
                            "job %d: receive_response drained without ResultMessage — "
                            "synthesizing terminal error event",
                            job.id,
                        )
                        self._on_event(SessionEvent(
                            job.id, "error",
                            {"error": "stream ended without ResultMessage"},
                        ))
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            finally:
                # Only now is the subprocess either pooled or gone; until
                # here stop() must still be able to find it.
                self._active_clients.discard(client)
