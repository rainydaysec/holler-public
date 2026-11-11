"""Single reader for one ClaudeSDKClient: routes every streamed message to
the turn it belongs to and tracks the client's subagents.

Why this exists: when Claude launches background agents
(``Agent(run_in_background=true)``) the CLI keeps them running after the turn
ends, and when each one finishes it STARTS A NEW TURN ON ITS OWN (system
``init`` → assistant reply → ``ResultMessage``) without any user input.
Reading only until the first ``ResultMessage`` per user turn — what
``receive_response()`` does — leaves those agent-driven turns buffered in the
pooled client, and the operator's next message then reads the stale buffer and
is answered with the leftover agent turn (observed 2026-10-02).

So each client gets one pump task that reads ``receive_messages()`` for the
client's whole life. Turn ownership:

- Holler starts the CLI with ``--replay-user-messages``, so a turn that
  answers a user message echoes that message (a parent-less ``UserMessage``
  with no tool_result blocks) before any reply content. A turn carrying the
  echo belongs to the oldest job waiting on this client.
- A turn without the echo was started by the CLI itself after a
  ``task_notification``: it becomes a new "agent update" job, opened through
  ``open_followup``.
- Fallback for a CLI that never echoes: a turn with no echo and no agent
  completion owed goes to the waiting job, which is the pre-router behavior.

Subagent traffic (messages carrying ``parent_tool_use_id``) and the CLI's
``task_started`` / ``task_progress`` / ``task_notification`` system messages
become ``agent_*`` events keyed by the top-level Agent ``tool_use_id``.

The router never imports Qt or the Session; it talks to the outside world
through two callbacks so it can be unit-tested with synthetic messages.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)

from holler import tool_format

log = logging.getLogger(__name__)

# Tool names that spawn a subagent. "Task" is the pre-2.1 name.
AGENT_TOOL_NAMES = ("Agent", "Task")

# How much of one subagent step to keep for the activity trail.
_ACTIVITY_TEXT_CAP = 280

Emit = Callable[[int, str, dict], None]
# open_followup(labels) -> job id for a new agent-driven turn.
OpenFollowup = Callable[[list[str]], int]


class PumpClosed(Exception):
    """The client's message stream ended (disconnect, crash, transport error)."""


@dataclass
class _Waiter:
    job_id: int
    future: asyncio.Future
    session_init_sent: bool = False


@dataclass
class _AgentInfo:
    tool_use_id: str
    description: str = ""
    subagent_type: str = ""
    background: bool = False
    task_id: str = ""
    running: bool = False


@dataclass
class TurnRouter:
    emit: Emit
    open_followup: OpenFollowup
    local_session_id: int | None = None
    session_id: str = ""
    # Jobs whose query has been sent and whose turn hasn't started yet.
    _waiting: deque = field(default_factory=deque)
    # Who the turn in progress belongs to: a _Waiter (user turn), an int (an
    # agent-driven follow-up job id), or None between turns.
    _owner: Any = None
    # Top-level Agent/Task tool calls, by tool_use_id.
    _agents: dict[str, _AgentInfo] = field(default_factory=dict)
    # Tool calls made INSIDE a subagent → the top-level agent's tool_use_id.
    _nested: dict[str, str] = field(default_factory=dict)
    # task_id → tool_use_id for every task this client has started.
    _tasks: dict[str, str] = field(default_factory=dict)
    _running_tasks: set[str] = field(default_factory=set)
    # Background agents that finished since the last agent-driven turn began;
    # the CLI owes us a follow-up turn for them.
    _owed: list[str] = field(default_factory=list)
    last_message_at: float = field(default_factory=time.monotonic)
    closed: bool = False
    _pump_task: asyncio.Task | None = None

    # ----- lifecycle -----

    def start(self, client: Any) -> None:
        self._pump_task = asyncio.create_task(self._pump(client))

    async def stop(self) -> None:
        t = self._pump_task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._close(PumpClosed("router stopped"))

    async def _pump(self, client: Any) -> None:
        err: BaseException = PumpClosed("stream ended")
        try:
            async for msg in client.receive_messages():
                try:
                    self.handle(msg)
                except Exception:
                    log.exception("turn router failed on %s", type(msg).__name__)
        except asyncio.CancelledError:
            err = PumpClosed("router stopped")
            raise
        except Exception as e:  # transport died
            err = e
        finally:
            self._close(err)

    def _close(self, err: BaseException) -> None:
        if self.closed:
            return
        self.closed = True
        owner = self._owner
        self._owner = None
        # A clean end reaches waiting turns as PumpClosed (→ "stream ended
        # without ResultMessage"); a transport error is re-raised as itself.
        exc = PumpClosed(str(err)) if isinstance(err, PumpClosed) else err
        if isinstance(owner, int):
            self.emit(owner, "error", {"error": f"agent update interrupted: {err}"})
        elif isinstance(owner, _Waiter) and not owner.future.done():
            owner.future.set_exception(exc)
        while self._waiting:
            w = self._waiting.popleft()
            if not w.future.done():
                w.future.set_exception(exc)
        # Agents die with their subprocess.
        for task_id in list(self._running_tasks):
            self._finish_task(task_id, "stopped", "Holler closed this session's Claude process.", None, "")

    # ----- turn API used by Session._execute -----

    def expect_turn(self, job_id: int) -> asyncio.Future:
        """Register a job whose user message is about to be sent. The future
        resolves with that turn's ResultMessage."""
        if self.closed:
            raise PumpClosed("client closed")
        fut = asyncio.get_running_loop().create_future()
        self._waiting.append(_Waiter(job_id, fut))
        return fut

    def abandon(self, job_id: int) -> None:
        """A waiting/owning job was cancelled; stop routing to it."""
        self._waiting = deque(w for w in self._waiting if w.job_id != job_id)
        if isinstance(self._owner, _Waiter) and self._owner.job_id == job_id:
            self._owner = None

    def has_live_agents(self) -> bool:
        return bool(self._running_tasks) or bool(self._owed) or isinstance(self._owner, int)

    def task_id_for(self, tool_use_id: str) -> str:
        info = self._agents.get(tool_use_id)
        return info.task_id if info else ""

    # ----- routing -----

    def handle(self, msg: Any) -> None:
        self.last_message_at = time.monotonic()
        if isinstance(msg, SystemMessage):
            self._on_system(msg)
            return
        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            self.emit(self._any_job_id(), "rate_limit", {
                "status": info.status,
                "rate_limit_type": info.rate_limit_type,
                "utilization": info.utilization,
                "resets_at": info.resets_at,
                "overage_status": info.overage_status,
                "overage_resets_at": info.overage_resets_at,
                "overage_disabled_reason": info.overage_disabled_reason,
            })
            return
        parent = getattr(msg, "parent_tool_use_id", None)
        if parent and isinstance(msg, (AssistantMessage, UserMessage)):
            self._on_subagent(msg, parent)
            return
        if isinstance(msg, UserMessage) and _is_user_echo(msg):
            self._claim_for_waiter()
            return
        if self._owner is None:
            self._decide_owner()
        owner_id = self.owner_job_id()
        if isinstance(msg, AssistantMessage):
            self._on_assistant(msg, owner_id)
        elif isinstance(msg, UserMessage):
            self._on_tool_results(msg, owner_id)
        elif isinstance(msg, ResultMessage):
            self._on_result(msg, owner_id)

    def _any_job_id(self) -> int:
        oid = self.owner_job_id()
        if oid:
            return oid
        if self._waiting:
            return self._waiting[0].job_id
        return 0

    def owner_job_id(self) -> int:
        o = self._owner
        if isinstance(o, _Waiter):
            return o.job_id
        if isinstance(o, int):
            return o
        return 0

    def _claim_for_waiter(self) -> None:
        if isinstance(self._owner, _Waiter):
            return  # second echo inside a turn (merged extras) — same owner
        if isinstance(self._owner, int):
            # The CLI folded our message into the turn it was already running.
            # Hand the turn to the user job from here on; the follow-up job
            # ends with what it streamed so far.
            self.emit(self._owner, "result", {"is_error": False, "result": "", "duration_ms": 0})
            self._owner = None
        if self._waiting:
            self._owner = self._waiting.popleft()
            self._send_session_init(self._owner)

    def _decide_owner(self) -> None:
        if self._waiting and not self._owed:
            # No echo seen and no agent completion pending: this is the
            # user's turn on a CLI that doesn't echo.
            self._owner = self._waiting.popleft()
            self._send_session_init(self._owner)
            return
        labels = list(dict.fromkeys(self._owed))
        self._owed.clear()
        try:
            self._owner = int(self.open_followup(labels))
        except Exception:
            log.exception("failed to open agent follow-up turn")
            self._owner = 0

    def _send_session_init(self, w: _Waiter) -> None:
        if self.session_id and not w.session_init_sent:
            w.session_init_sent = True
            self.emit(w.job_id, "session_init", {"session_id": self.session_id})

    def _on_system(self, msg: SystemMessage) -> None:
        subtype = getattr(msg, "subtype", "")
        data = getattr(msg, "data", None) or {}
        if subtype == "init":
            sid = data.get("session_id", "") if isinstance(data, dict) else ""
            if sid:
                self.session_id = sid
            for w in list(self._waiting)[:1]:
                self._send_session_init(w)
            return
        if subtype == "task_started":
            self._on_task_started(
                str(getattr(msg, "task_id", "") or data.get("task_id", "")),
                str(getattr(msg, "tool_use_id", "") or data.get("tool_use_id", "") or ""),
                str(getattr(msg, "description", "") or data.get("description", "")),
                str(getattr(msg, "task_type", "") or data.get("task_type", "") or ""),
            )
        elif subtype == "task_progress":
            task_id = str(getattr(msg, "task_id", "") or data.get("task_id", ""))
            tuid = self._tasks.get(task_id) or str(getattr(msg, "tool_use_id", "") or "")
            if tuid in self._agents:
                usage = getattr(msg, "usage", None) or data.get("usage") or {}
                self._emit_agent("agent_progress", tuid, {
                    "task_id": task_id,
                    "description": str(getattr(msg, "description", "") or data.get("description", "")),
                    "last_tool_name": getattr(msg, "last_tool_name", None) or data.get("last_tool_name") or "",
                    "usage": dict(usage) if isinstance(usage, dict) else {},
                })
        elif subtype == "task_notification":
            usage = getattr(msg, "usage", None) or data.get("usage")
            self._finish_task(
                str(getattr(msg, "task_id", "") or data.get("task_id", "")),
                str(getattr(msg, "status", "") or data.get("status", "") or "completed"),
                str(getattr(msg, "summary", "") or data.get("summary", "") or ""),
                dict(usage) if isinstance(usage, dict) else None,
                str(getattr(msg, "output_file", "") or data.get("output_file", "") or ""),
                tool_use_id=str(getattr(msg, "tool_use_id", "") or data.get("tool_use_id", "") or ""),
            )

    def _on_task_started(self, task_id: str, tool_use_id: str, description: str, task_type: str) -> None:
        if not task_id:
            return
        self._tasks[task_id] = tool_use_id
        self._running_tasks.add(task_id)
        info = self._agents.get(tool_use_id)
        if info is not None:
            info.task_id = task_id
            info.running = True
            self._emit_agent("agent_started", tool_use_id, {
                "task_id": task_id,
                "description": info.description or description,
                "subagent_type": info.subagent_type,
                "background": info.background,
                "task_type": task_type,
            })
            return
        root = self._nested.get(tool_use_id)
        if root:
            # A background shell/agent launched from inside a subagent.
            self._emit_agent("agent_activity", root, {
                "kind": "task", "text": f"started background task: {description}",
            })
        # Otherwise: a top-level non-agent background task (e.g. Bash
        # run_in_background) — counted as live so the client isn't reaped.

    def _finish_task(self, task_id: str, status: str, summary: str,
                     usage: dict | None, output_file: str, *, tool_use_id: str = "") -> None:
        self._running_tasks.discard(task_id)
        tuid = self._tasks.get(task_id) or tool_use_id
        info = self._agents.get(tuid)
        if info is None:
            return
        if not info.running:
            return  # already reported (foreground result, or a repeat)
        info.running = False
        if info.background and not self.closed:
            # The CLI answers every background completion with a turn of its
            # own (possibly batched with others).
            self._owed.append(info.description or "agent")
        self._emit_agent("agent_done", tuid, {
            "task_id": task_id,
            "status": status,
            "summary": summary,
            "usage": usage or {},
            "output_file": output_file,
        })

    def _on_assistant(self, msg: AssistantMessage, owner_id: int) -> None:
        for block in msg.content:
            if isinstance(block, TextBlock):
                self.emit(owner_id, "text_delta", {"text": block.text})
            elif isinstance(block, ToolUseBlock):
                if block.name in AGENT_TOOL_NAMES:
                    inp = block.input if isinstance(block.input, dict) else {}
                    info = _AgentInfo(
                        tool_use_id=block.id,
                        description=str(inp.get("description") or "")[:200],
                        subagent_type=str(inp.get("subagent_type") or ""),
                        background=bool(inp.get("run_in_background")),
                    )
                    self._agents[block.id] = info
                    self._emit_agent("agent_launched", block.id, {
                        "description": info.description,
                        "subagent_type": info.subagent_type,
                        "background": info.background,
                        "prompt": str(inp.get("prompt") or "")[:4000],
                        "job_id": owner_id,
                    })
                self.emit(owner_id, "tool_use", {"name": block.name, "input": block.input, "id": block.id})
            elif isinstance(block, ThinkingBlock):
                self.emit(owner_id, "thinking", {"text": block.thinking})
        usage = getattr(msg, "usage", None)
        if usage is not None:
            from holler.sdk_session import _extract_usage
            msg_id = getattr(msg, "message_id", None) or getattr(msg, "uuid", None) or ""
            self.emit(owner_id, "usage", {"message_id": str(msg_id), **_extract_usage(usage)})

    def _on_tool_results(self, msg: UserMessage, owner_id: int) -> None:
        for tool_use_id, content, is_error in _tool_results(msg):
            self.emit(owner_id, "tool_result", {
                "tool_use_id": tool_use_id, "content": content, "is_error": is_error,
            })
            info = self._agents.get(tool_use_id)
            if info is not None and not info.background and _is_async_launch(content):
                # The CLI backgrounded an agent the model didn't flag
                # run_in_background (it does this on its own): the result is
                # only a launch receipt; completion comes as a notification.
                info.background = True
                self._emit_agent("agent_launched", tool_use_id, {"background": True})
                continue
            if info is not None and not info.background:
                # A foreground agent's result IS its completion; the CLI may
                # or may not also send a task_notification.
                if info.running and info.task_id:
                    self._running_tasks.discard(info.task_id)
                info.running = False
                self._emit_agent("agent_done", tool_use_id, {
                    "task_id": info.task_id,
                    "status": "failed" if is_error else "completed",
                    "summary": _content_text(content)[:8000],
                    "usage": {},
                    "output_file": "",
                })

    def _on_result(self, msg: ResultMessage, owner_id: int) -> None:
        owner = self._owner
        self._owner = None
        if isinstance(owner, _Waiter):
            if not owner.future.done():
                owner.future.set_result(msg)
            return
        if owner_id:
            from holler.sdk_session import _extract_usage
            self.emit(owner_id, "result", {
                "is_error": bool(msg.is_error),
                "duration_ms": msg.duration_ms,
                "num_turns": msg.num_turns,
                "result": msg.result or "",
                "stop_reason": msg.stop_reason,
                "total_cost_usd": msg.total_cost_usd,
                "usage": _extract_usage(msg.usage) if getattr(msg, "usage", None) else {},
            })

    def _on_subagent(self, msg: Any, parent: str) -> None:
        root = self._nested.get(parent, parent)
        if root not in self._agents:
            return
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    self._nested[block.id] = root
                    self._emit_agent("agent_activity", root, {
                        "kind": "tool",
                        "name": block.name,
                        "text": tool_format.summarize(block.name, block.input, max_len=_ACTIVITY_TEXT_CAP),
                    })
                elif isinstance(block, TextBlock) and block.text.strip():
                    self._emit_agent("agent_activity", root, {
                        "kind": "text", "text": block.text.strip()[:_ACTIVITY_TEXT_CAP],
                    })
        else:
            for _tuid, content, is_error in _tool_results(msg):
                if is_error:
                    self._emit_agent("agent_activity", root, {
                        "kind": "error", "text": _content_text(content)[:_ACTIVITY_TEXT_CAP],
                    })

    def _emit_agent(self, kind: str, tool_use_id: str, data: dict) -> None:
        payload = {
            "tool_use_id": tool_use_id,
            "local_session_id": self.local_session_id,
            **data,
        }
        self.emit(self.owner_job_id() or 0, kind, payload)


def _is_user_echo(msg: UserMessage) -> bool:
    """The CLI's replay of a user message we sent (vs. a tool_result carrier)."""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for block in content:
            if getattr(block, "tool_use_id", None):
                return False
            if isinstance(block, dict) and block.get("tool_use_id"):
                return False
        return bool(content)
    return False


def _is_async_launch(content: Any) -> bool:
    return _content_text(content).lstrip().startswith("Async agent launched")


def _tool_results(msg: Any):
    content = getattr(msg, "content", None) or []
    if not isinstance(content, list):
        return
    for block in content:
        tool_use_id = getattr(block, "tool_use_id", None)
        if not tool_use_id and isinstance(block, dict):
            tool_use_id = block.get("tool_use_id")
        if not tool_use_id:
            continue
        block_content = getattr(block, "content", None)
        if block_content is None and isinstance(block, dict):
            block_content = block.get("content")
        is_error = bool(getattr(block, "is_error", False)) or (
            isinstance(block, dict) and bool(block.get("is_error"))
        )
        yield tool_use_id, block_content, is_error


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif hasattr(b, "text"):
                parts.append(str(getattr(b, "text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)
