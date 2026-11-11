"""Observable job store for the panel UI.

JobStore holds Job records. The panel listens to its Qt signals to render rows,
expand text, and toast on completion. SessionEvents from sdk_session are
funnelled through JobStore.apply_event() which mutates Jobs and re-emits
notifications.

If a `Store` is wired in, JobStore also writes through to the SQLite layer:
  - on session_init: attach_sdk_session_id(local_session_id, sdk_session_id)
  - on result/error:  update_turn(local_turn_id, final_text=..., is_error=..., ...)
                      + add_tool_call rows for each entry in tool_log
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6 import QtCore

from holler import agents as _agents
from holler import state as _state
from holler.agents import AgentRun
from holler.sdk_session import SessionEvent

_AGENT_EVENTS = ("agent_launched", "agent_started", "agent_progress", "agent_activity", "agent_done")


@dataclass
class ToolEntry:
    id: str  # tool_use_id from the SDK
    name: str
    input: dict
    started_ts: float
    ended_ts: float = 0.0
    duration_ms: int = 0
    result_preview: str = ""
    is_error: bool = False


@dataclass
class ContentBlock:
    """Ordered block of either streaming text or a tool call, as Claude emits it.
    Used by the live conversation renderer to interleave tools with text the
    same way Claude Code's terminal UI does."""
    kind: str  # "text" | "tool" | "thinking"
    text: str = ""                # kind="text" or "thinking"
    tool_name: str = ""           # kind="tool"
    tool_input: dict = field(default_factory=dict)
    tool_id: str = ""
    tool_result: str = ""
    tool_is_error: bool = False
    started_ts: float = 0.0
    ended_ts: float = 0.0


@dataclass
class Job:
    id: int
    transcript: str
    submitted_at: float
    thumbnail_path: Optional[str] = None
    status: str = "queued"  # queued | running | done | error
    streaming_text: str = ""
    final_text: str = ""
    error: str = ""
    duration_ms: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    model: str = ""
    sdk_session_id: str = ""
    resume_session_id: str = ""
    # Detailed tool tracking (item D + E).
    current_tool: Optional[ToolEntry] = None  # most recent in-flight tool
    tool_log: list[ToolEntry] = field(default_factory=list)
    # Ordered blocks for inline rendering (Claude Code-style live view).
    blocks: list[ContentBlock] = field(default_factory=list)
    # Token usage (updated live via "usage" events) + final cost from ResultMessage.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_cost_usd: float = 0.0
    # Per-AssistantMessage usage keyed by message_id; we sum across to get
    # turn totals. Stored as field so re-emissions of the same message_id
    # don't double-count.
    _usage_by_msg: dict = field(default_factory=dict)
    # Phase 2 — DB correlation
    local_session_id: Optional[int] = None
    local_turn_id: Optional[int] = None
    # V3 smart-batch: extra DB turn rows whose content was merged into this Job's
    # SDK call. Each is a dict with 'local_turn_id' and 'transcript'.
    merged_turns: list[dict] = field(default_factory=list)


_TOOL_LOG_CAP = 200
_RESULT_PREVIEW_CAP = 8 * 1024  # 8 KB stored per result (detail view shows it)


class JobStore(QtCore.QObject):
    job_added = QtCore.Signal(int)  # job_id
    # Coalesced job_updated — emitted at most ~10Hz per job during streaming.
    # See `_mark_update` + `_dirty_flush` for the debouncer.
    job_updated = QtCore.Signal(int)
    # Internal signal used to hop a "dirty" notification from the SDK
    # asyncio thread to the GUI thread (where the coalescer QTimer lives).
    _dirty = QtCore.Signal(int)
    session_changed = QtCore.Signal(int)  # local_session_id (Phase 2)
    session_rotated = QtCore.Signal()  # deprecated; never emitted under ephemeral model
    rate_limit_changed = QtCore.Signal()  # latest RateLimitEvent processed
    # Emitted when an SDK resume failed because Claude Code lost the JSONL.
    # Args: local_session_id, human-readable reason. Runner wires this to a
    # toast so the operator knows the session lost its history but continues.
    resume_failed = QtCore.Signal(int, str)
    # A subagent of this local session changed (launched, progressed,
    # finished). Emitted from the SDK thread; Qt queues it to GUI receivers.
    agents_changed = QtCore.Signal(int)

    def __init__(self, store: Optional[Any] = None) -> None:
        super().__init__()
        self._jobs: dict[int, Job] = {}
        self._order: list[int] = []  # newest first for panel display
        self._store = store  # holler.store.Store or None
        # Streaming sends one SessionEvent per TextBlock — at 50-100/sec for a
        # fast reply. The downstream listener fan-out (session list totals
        # query, panel row repaint, warmth/usage refreshes, detail re-render)
        # is heavy enough that emitting per token saturates the GUI thread.
        # Coalesce: collect dirty job_ids in a set, emit at most once per
        # ~80ms. Terminal events (result/error) and session_init/merged_in
        # pass immediate=True and emit synchronously so the UI lights up
        # instantly on completion.
        # Last cumulative `total_cost_usd` seen per CLI subprocess, so
        # `_turn_cost_usd` can turn that running total into a per-turn figure.
        # Bounded in `_turn_cost_usd`; one float per subprocess.
        self._cost_cursor: dict[str, float] = {}
        self._dirty_ids: set[int] = set()
        self._coalesce_timer = QtCore.QTimer(self)
        self._coalesce_timer.setSingleShot(True)
        self._coalesce_timer.setInterval(80)
        self._coalesce_timer.timeout.connect(self._flush_dirty)
        # Cross-thread bridge: SDK thread calls _dirty.emit(job_id), Qt's
        # auto-queued connection posts the slot to the GUI thread.
        self._dirty.connect(self._enqueue_dirty)

        # Latest Anthropic rate-limit snapshot per window type. The CLI only
        # emits RateLimitEvent on state transitions (allowed → warning, etc.),
        # so we persist the last-seen value to disk and reload on launch — the
        # operator sees their actual quota status immediately, not "nothing
        # yet" until the next transition.
        try:
            persisted = _state.load().get("rate_limits", {})
            if not isinstance(persisted, dict):
                persisted = {}
        except Exception:
            persisted = {}
        self._rate_limits: dict[str, dict] = dict(persisted)
        # Subagents by top-level Agent tool_use_id, in launch order.
        self._agents: dict[str, AgentRun] = {}

    def agents_for_session(self, local_session_id: Optional[int]) -> list[AgentRun]:
        return [a for a in list(self._agents.values()) if a.local_session_id == local_session_id]

    def agent(self, tool_use_id: str) -> Optional[AgentRun]:
        return self._agents.get(tool_use_id)

    def running_agents(self) -> list[AgentRun]:
        return [a for a in list(self._agents.values()) if a.running]

    def mark_agent_stop_requested(self, tool_use_id: str) -> None:
        a = self._agents.get(tool_use_id)
        if a is not None and a.running:
            a.stop_requested = True
            self.agents_changed.emit(a.local_session_id if a.local_session_id is not None else -1)

    def _apply_agent_event(self, ev: SessionEvent) -> None:
        tuid = str(ev.data.get("tool_use_id") or "")
        if not tuid:
            return
        run = self._agents.get(tuid)
        if run is None:
            run = AgentRun(tool_use_id=tuid, local_session_id=ev.data.get("local_session_id"))
            self._agents[tuid] = run
        run.apply(ev.kind, ev.data)
        sid = run.local_session_id
        self.agents_changed.emit(sid if sid is not None else -1)

    def get(self, job_id: int) -> Optional[Job]:
        return self._jobs.get(job_id)

    def rate_limits(self) -> dict[str, dict]:
        """Return a copy of the last-known rate-limit snapshots, keyed by
        ``rate_limit_type`` (``five_hour``, ``seven_day``, ``seven_day_opus``,
        ``seven_day_sonnet``, ``overage``). Empty dict if nothing has been
        observed yet (and nothing was persisted from a prior session)."""
        return dict(self._rate_limits)

    def all(self) -> list[Job]:
        return [self._jobs[jid] for jid in self._order if jid in self._jobs]

    def is_terminal(self, job_id: int) -> bool:
        """True iff the job exists and is in a terminal state ("done"/"error").
        Used by sdk_session's _done_cb reaper to detect jobs that finished
        the asyncio task without ever emitting a terminal SessionEvent."""
        job = self._jobs.get(job_id)
        return job is not None and job.status in ("done", "error")

    def add(self, job: Job) -> None:
        self._jobs[job.id] = job
        self._order.insert(0, job.id)
        self.job_added.emit(job.id)

    def apply_event(self, ev: SessionEvent, *, thumbnail_path: Optional[str] = None) -> None:
        if ev.kind in _AGENT_EVENTS:
            self._apply_agent_event(ev)
            return

        if ev.kind == "followup":
            # The CLI started a turn on its own because background agents
            # finished. Give it a turn row so it renders and persists like
            # any other reply, labelled as an agent update.
            transcript = _agents.agent_turn_transcript(list(ev.data.get("labels") or []))
            local_sid = ev.data.get("local_session_id")
            turn_id = None
            if self._store and isinstance(local_sid, int):
                try:
                    turn_id = self._store.add_turn(
                        session_id=local_sid, transcript=transcript,
                        screenshot_path=None, job_id=ev.job_id,
                    )
                except Exception:
                    import logging
                    logging.exception("followup job %d: add_turn failed", ev.job_id)
            job = Job(
                id=ev.job_id,
                transcript=transcript,
                submitted_at=time.time(),
                model=ev.data.get("model", ""),
                resume_session_id=ev.data.get("resume_session_id") or "",
                local_session_id=local_sid,
                local_turn_id=turn_id,
            )
            self.add(job)
            if isinstance(local_sid, int):
                self.session_changed.emit(local_sid)
            return

        if ev.kind == "submitted":
            job = Job(
                id=ev.job_id,
                transcript=ev.data.get("transcript", ""),
                submitted_at=time.time(),
                thumbnail_path=thumbnail_path,
                model=ev.data.get("model", ""),
                resume_session_id=ev.data.get("resume_session_id") or "",
                local_session_id=ev.data.get("local_session_id"),
                local_turn_id=ev.data.get("local_turn_id"),
            )
            self.add(job)
            return

        if ev.kind == "merged_in":
            # A new PTT was merged into an existing pending Job — no new Job
            # row, just record the extra turn so final_text fans out to it later.
            job = self._jobs.get(ev.job_id)
            if job is not None:
                job.merged_turns.append({
                    "local_turn_id": ev.data.get("local_turn_id"),
                    "transcript": ev.data.get("transcript", ""),
                })
                self._mark_update(job.id, immediate=True)
            return

        if ev.kind == "session_rotated":
            self.session_rotated.emit()
            return

        if ev.kind == "session_init":
            job = self._jobs.get(ev.job_id)
            if job is not None:
                sdk_id = ev.data.get("session_id", "")
                job.sdk_session_id = sdk_id or job.sdk_session_id
                # Tell the DB to attach this sdk_session_id to the local row.
                if self._store and job.local_session_id and sdk_id:
                    try:
                        self._store.attach_sdk_session_id(job.local_session_id, sdk_id)
                        self.session_changed.emit(job.local_session_id)
                    except Exception:
                        pass
                self.job_updated.emit(job.id)
            return

        if ev.kind == "resume_failed":
            # SDK couldn't find the JSONL for the cached sdk_session_id —
            # the Holler session row is kept (its transcript history matters)
            # but we NULL the dead sdk_session_id so future PTTs into the same
            # row create a fresh SDK session instead of hammering this error.
            local_id = ev.data.get("local_session_id")
            reason = str(ev.data.get("reason") or "session history lost")
            if self._store and isinstance(local_id, int):
                try:
                    self._store.clear_sdk_session_id(local_id)
                except Exception:
                    import logging as _logging
                    _logging.exception("clear_sdk_session_id failed for %s", local_id)
            # Mirror the cleared state into any in-memory Job for this row.
            job = self._jobs.get(ev.job_id)
            if job is not None:
                job.sdk_session_id = ""
                job.resume_session_id = ""
            if isinstance(local_id, int):
                self.resume_failed.emit(local_id, reason)
                self.session_changed.emit(local_id)
            return

        if ev.kind == "rate_limit":
            # Per-window snapshot. Multiple windows are tracked in parallel
            # (five_hour, seven_day, seven_day_opus, seven_day_sonnet, overage)
            # and keep their last-known values independently. The SDK emits
            # this only on transitions, so persisting to disk is what gives
            # the operator a real-time-feeling readout across restarts.
            rtype = ev.data.get("rate_limit_type") or "_unknown"
            snapshot = {
                "status": ev.data.get("status"),
                "utilization": ev.data.get("utilization"),
                "resets_at": ev.data.get("resets_at"),
                "overage_status": ev.data.get("overage_status"),
                "overage_resets_at": ev.data.get("overage_resets_at"),
                "overage_disabled_reason": ev.data.get("overage_disabled_reason"),
                "seen_at": time.time(),
            }
            self._rate_limits[rtype] = snapshot
            try:
                _state.merged_save_debounced({"rate_limits": dict(self._rate_limits)})
            except Exception:
                # Persistence is best-effort — the in-memory cache is the
                # source of truth for the current process.
                pass
            self.rate_limit_changed.emit()
            return

        job = self._jobs.get(ev.job_id)
        if job is None:
            return

        # Once a job is terminal, a second terminal event (e.g. the
        # sdk_session reaper's synthesized "error" racing the real "result",
        # or "cancelled" after completion) must not flip done→error or
        # re-persist the job's tool calls (which would duplicate them).
        if ev.kind in ("result", "error") and job.status in ("done", "error"):
            return

        if ev.kind == "started":
            job.status = "running"
            job.started_at = time.time()
            # The 'started' event also carries the model the SDK actually used.
            m = ev.data.get("model")
            if m:
                job.model = m
        elif ev.kind == "text_delta":
            if job.status == "queued":
                job.status = "running"
                job.started_at = time.time()
            text = ev.data.get("text", "")
            if text:
                job.streaming_text += text
                job.final_text = job.streaming_text
                # Append to the trailing text block, or open a new one if the
                # last block was a tool/thinking.
                if job.blocks and job.blocks[-1].kind == "text":
                    job.blocks[-1].text += text
                else:
                    job.blocks.append(ContentBlock(kind="text", text=text))
        elif ev.kind == "tool_use":
            if job.status == "queued":
                job.status = "running"
                job.started_at = time.time()
            entry = ToolEntry(
                id=str(ev.data.get("id", "")),
                name=ev.data.get("name", "?"),
                input=ev.data.get("input") or {},
                started_ts=time.time(),
            )
            job.tool_log.append(entry)
            if len(job.tool_log) > _TOOL_LOG_CAP:
                job.tool_log = job.tool_log[-_TOOL_LOG_CAP:]
            job.current_tool = entry
            # Append as an inline block so the renderer interleaves it with text.
            job.blocks.append(ContentBlock(
                kind="tool",
                tool_name=entry.name,
                tool_input=entry.input,
                tool_id=entry.id,
                started_ts=entry.started_ts,
            ))
        elif ev.kind == "tool_result":
            tool_use_id = str(ev.data.get("tool_use_id", ""))
            content = ev.data.get("content")
            is_error = bool(ev.data.get("is_error", False))
            preview = _result_preview(content, is_error)
            for entry in reversed(job.tool_log):
                if entry.id == tool_use_id:
                    entry.ended_ts = time.time()
                    entry.duration_ms = int((entry.ended_ts - entry.started_ts) * 1000)
                    entry.result_preview = preview
                    entry.is_error = is_error
                    if job.current_tool is entry:
                        job.current_tool = None
                    break
            for b in reversed(job.blocks):
                if b.kind == "tool" and b.tool_id == tool_use_id:
                    b.ended_ts = time.time()
                    b.tool_result = preview
                    b.tool_is_error = is_error
                    break
        elif ev.kind == "thinking":
            text = ev.data.get("text", "")
            if text:
                job.blocks.append(ContentBlock(kind="thinking", text=text))
        elif ev.kind == "usage":
            # Each AssistantMessage in an agentic loop has its own usage. Sum
            # across distinct message_ids; re-emissions update the latest value.
            msg_id = str(ev.data.get("message_id", "") or f"_anon_{len(job._usage_by_msg)}")
            job._usage_by_msg[msg_id] = {
                "input_tokens": int(ev.data.get("input_tokens", 0) or 0),
                "output_tokens": int(ev.data.get("output_tokens", 0) or 0),
                "cache_creation_input_tokens": int(ev.data.get("cache_creation_input_tokens", 0) or 0),
                "cache_read_input_tokens": int(ev.data.get("cache_read_input_tokens", 0) or 0),
            }
            job.input_tokens = sum(u["input_tokens"] for u in job._usage_by_msg.values())
            job.output_tokens = sum(u["output_tokens"] for u in job._usage_by_msg.values())
            job.cache_creation_input_tokens = sum(
                u["cache_creation_input_tokens"] for u in job._usage_by_msg.values()
            )
            job.cache_read_input_tokens = sum(
                u["cache_read_input_tokens"] for u in job._usage_by_msg.values()
            )
        elif ev.kind == "result":
            job.status = "error" if ev.data.get("is_error") else "done"
            job.duration_ms = int(ev.data.get("duration_ms", 0))
            job.ended_at = time.time()
            r = ev.data.get("result") or ""
            if r:
                job.final_text = r
            job.total_cost_usd = self._turn_cost_usd(job, ev.data.get("total_cost_usd"))
            # ResultMessage.usage: empirically (2026-05-12, claude-agent-sdk
            # current) the `output_tokens` field reports the full-turn output
            # that per-AssistantMessage events under-count, so we add it under
            # key "_result" to recover the missing output. Verified
            # non-duplicative of the last AssistantMessage (verdict NEW across
            # 6 sample turns). KNOWN CONCERN: input/cache_creation/cache_read
            # fields are identical between sum-of-AssistantMessages and
            # ResultMessage in the same samples, so adding them likely
            # 2×-inflates those three columns. Needs SDK-side investigation
            # before changing — wrong shape of fix would under-count output.
            final_usage = ev.data.get("usage") or {}
            if final_usage:
                job._usage_by_msg["_result"] = {
                    "input_tokens": int(final_usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(final_usage.get("output_tokens", 0) or 0),
                    "cache_creation_input_tokens": int(final_usage.get("cache_creation_input_tokens", 0) or 0),
                    "cache_read_input_tokens": int(final_usage.get("cache_read_input_tokens", 0) or 0),
                }
                job.input_tokens = sum(u["input_tokens"] for u in job._usage_by_msg.values())
                job.output_tokens = sum(u["output_tokens"] for u in job._usage_by_msg.values())
                job.cache_creation_input_tokens = sum(
                    u["cache_creation_input_tokens"] for u in job._usage_by_msg.values()
                )
                job.cache_read_input_tokens = sum(
                    u["cache_read_input_tokens"] for u in job._usage_by_msg.values()
                )
            job.current_tool = None
            self._persist_job_completion(job)
        elif ev.kind == "error":
            job.status = "error"
            job.error = ev.data.get("error", "")
            job.ended_at = time.time()
            job.current_tool = None
            self._persist_job_completion(job)

        # Terminal events get immediate emission so completion / failure
        # paints without waiting for the coalesce window.
        self._mark_update(job.id, immediate=(ev.kind in ("result", "error")))

    def _mark_update(self, job_id: int, *, immediate: bool = False) -> None:
        """Notify listeners that ``job_id`` mutated.

        Default path (immediate=False) coalesces high-frequency streaming
        updates: marks the id dirty, the GUI-thread flush timer fires at most
        ~12.5Hz to emit the consolidated `job_updated` signal. immediate=True
        emits synchronously — use for terminal/structural changes (result,
        error, merged_in) the operator needs to see without delay.
        """
        if immediate:
            self.job_updated.emit(job_id)
            return
        # Hop to the GUI thread; the _dirty → _enqueue_dirty queued
        # connection handles the cross-thread bounce.
        self._dirty.emit(job_id)

    @QtCore.Slot(int)
    def _enqueue_dirty(self, job_id: int) -> None:
        """GUI-thread slot — accumulate dirty ids, start the flush timer."""
        self._dirty_ids.add(job_id)
        if not self._coalesce_timer.isActive():
            self._coalesce_timer.start()

    def _flush_dirty(self) -> None:
        ids = list(self._dirty_ids)
        self._dirty_ids.clear()
        for jid in ids:
            self.job_updated.emit(jid)

    # Keep the cursor from growing without bound across a long-running app.
    # Each entry is one float keyed by CLI subprocess; this many covers far
    # more live subprocesses than the pool ever holds.
    _COST_CURSOR_CAP = 512

    def _turn_cost_usd(self, job: Job, reported: Any) -> float:
        """This turn's cost, from the SDK's *cumulative* subprocess total.

        `ResultMessage.total_cost_usd` is the running total for the CLI
        subprocess, not the turn. Holler pools one subprocess across a
        session's turns, so the raw figure climbs monotonically (measured on a
        real session: turns 11-16 reported $11.17, $11.81, $12.50, $13.08,
        $13.55, $13.87 while each turn actually billed well under a dollar).
        Every `SUM(total_cost_usd)` over a session therefore over-reported
        spend by roughly the turn count. Store the increment instead.

        A value below the cursor means the subprocess restarted and its
        counter reset, so the reported figure is already this turn's cost.
        Concurrent turns on one subprocess split the increment arbitrarily
        between themselves, but their total stays right.
        """
        try:
            cumulative = float(reported or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if cumulative <= 0.0:
            return 0.0
        key = job.sdk_session_id or f"local:{job.local_session_id}"
        previous = self._cost_cursor.get(key, 0.0)
        if len(self._cost_cursor) >= self._COST_CURSOR_CAP and key not in self._cost_cursor:
            # dicts preserve insertion order: drop the oldest half.
            for stale in list(self._cost_cursor)[: self._COST_CURSOR_CAP // 2]:
                del self._cost_cursor[stale]
        self._cost_cursor[key] = cumulative
        return cumulative if cumulative < previous else cumulative - previous

    def _persist_job_completion(self, job: Job) -> None:
        """Flush final_text + tool_log to the DB on result/error. For merged
        jobs, write the same final_text + duration to every merged turn row
        so the conversation timeline shows the response on each."""
        if not self._store or not job.local_turn_id:
            return
        try:
            # Primary turn — carries the tool log AND final_text.
            # Token usage is attributed entirely to the primary turn so SUM
            # queries don't double-count across merged turns.
            self._store.update_turn(
                job.local_turn_id,
                final_text=job.final_text or "",
                duration_ms=job.duration_ms,
                ended_at=job.ended_at,
                is_error=1 if job.status == "error" else 0,
                input_tokens=int(job.input_tokens),
                output_tokens=int(job.output_tokens),
                cache_creation_input_tokens=int(job.cache_creation_input_tokens),
                cache_read_input_tokens=int(job.cache_read_input_tokens),
                total_cost_usd=float(job.total_cost_usd),
            )
            # Separate, guarded write: the reply above must persist even on a
            # DB whose schema lacks blocks_json (e.g. stamped past v14 by
            # another fork) — blocks only refine the committed rendering.
            # The model this turn actually ran: the badge can change between
            # turns, so the session row's creation model doesn't say.
            if job.model:
                try:
                    self._store.update_turn(job.local_turn_id, model=job.model)
                except Exception:
                    import logging
                    logging.exception("job %d: turn model write failed", job.id)
            blocks = _blocks_json(job)
            if blocks:
                try:
                    self._store.update_turn(job.local_turn_id, blocks_json=blocks)
                except Exception:
                    import logging
                    logging.exception("job %d: blocks_json write failed", job.id)
            for entry in job.tool_log:
                try:
                    input_json = json.dumps(entry.input, default=str)
                except Exception:
                    input_json = repr(entry.input)
                tcid = self._store.add_tool_call(
                    turn_id=job.local_turn_id,
                    name=entry.name,
                    input_json=input_json,
                    started_at=entry.started_ts,
                )
                self._store.update_tool_call(
                    tcid,
                    result_json=entry.result_preview,
                    is_error=1 if entry.is_error else 0,
                    ended_at=entry.ended_ts,
                )
            # Merged turns share the response but not the tool log (tool work
            # belonged to the primary turn). Mark them ended so the UI sees
            # them as part of the same logical turn.
            for mt in job.merged_turns:
                tid = mt.get("local_turn_id")
                if not tid:
                    continue
                self._store.update_turn(
                    tid,
                    final_text=job.final_text or "",
                    duration_ms=job.duration_ms,
                    ended_at=job.ended_at,
                    is_error=1 if job.status == "error" else 0,
                )
                # A failed merged turn records what its text is (the reply vs
                # the failure reason) — without blocks the detail view can
                # only guess, and a one-line reply would read as the error.
                merged_blocks = _merged_blocks_json(job) if job.status == "error" else ""
                if merged_blocks:
                    try:
                        self._store.update_turn(tid, blocks_json=merged_blocks)
                    except Exception:
                        import logging
                        logging.exception("job %d: merged blocks_json write failed", job.id)
            if job.local_session_id:
                self.session_changed.emit(job.local_session_id)
        except Exception:
            import logging
            logging.exception("failed to persist job completion to Store")


def _blocks_json(job: Job) -> str:
    """Compact ordered block sequence for the primary turn's ``blocks_json``.

    ``[{"t": "text", "text": ...}, {"t": "tool", "i": N}, ...]`` where N
    indexes the tool_call rows this turn persists (``job.tool_log`` order ==
    insertion/id order). Tool blocks and tool_log entries are appended in
    lockstep, but tool_log is front-trimmed at ``_TOOL_LOG_CAP`` — blocks
    whose entry was trimmed are dropped (their row was never written).
    Thinking blocks are omitted: committed turns don't show them. A failed
    job's ``error`` text is appended as ``{"t": "error", "text": ...}`` —
    the turns table has no column for it, and without it a committed errored
    turn could only say that it failed, not why.
    """
    n_tool_blocks = sum(1 for b in job.blocks if b.kind == "tool")
    offset = n_tool_blocks - len(job.tool_log)
    out: list[dict] = []
    k = 0
    for b in job.blocks:
        if b.kind == "text":
            if b.text.strip():
                out.append({"t": "text", "text": b.text})
        elif b.kind == "tool":
            i = k - offset
            k += 1
            if 0 <= i < len(job.tool_log):
                out.append({"t": "tool", "i": i})
    error_text = str(job.error or "").strip()
    if job.status == "error" and error_text:
        out.append({"t": "error", "text": error_text})
    if not out:
        return ""
    try:
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""


def _merged_blocks_json(job: Job) -> str:
    """``blocks_json`` for a merged turn of a failed job: the shared reply
    (when the stream showed it — an unstreamed result is the SDK's error
    string, not a reply) plus the job's ``error`` block. No tool blocks:
    merged turns carry no tool_call rows."""
    out: list[dict] = []
    final = job.final_text or ""
    streamed = [b.text for b in job.blocks if b.kind == "text"]
    norm = lambda t: " ".join(t.split())  # noqa: E731
    if final.strip() and (norm(final) in norm(" ".join(streamed))
                          or norm(final) in norm("".join(streamed))):
        out.append({"t": "text", "text": final})
    error_text = str(job.error or "").strip()
    if error_text:
        out.append({"t": "error", "text": error_text})
    if not out:
        return ""
    try:
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""


def _result_preview(content: Any, is_error: bool) -> str:
    """Stringify a tool_result payload to a bounded preview."""
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                chunks.append(str(getattr(block, "text", "")))
        text = "\n".join(c for c in chunks if c) or repr(content)[:_RESULT_PREVIEW_CAP]
    else:
        text = repr(content)
    if len(text) > _RESULT_PREVIEW_CAP:
        text = text[:_RESULT_PREVIEW_CAP] + "\n... [truncated]"
    if is_error and not text.startswith("ERROR"):
        text = "ERROR: " + text
    return text
