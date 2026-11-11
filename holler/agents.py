"""Subagents (Claude's Agent/Task tool) as Holler sees them.

``AgentRun`` is the in-memory record the Agents pane renders; JobStore builds
it from the ``agent_*`` SessionEvents the TurnRouter emits. Agent-driven
follow-up turns (the CLI's own turn after a background agent finishes) are
stored as ordinary turns whose transcript starts with ``AGENT_TURN_PREFIX`` —
that marker is how the transcript tells them apart from what the operator
said, with no schema change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

AGENT_TURN_PREFIX = "⟲ Agent update"  # ⟲

_ACTIVITY_CAP = 80

# Lifecycle. "running" covers launched-but-not-yet-started too: the CLI
# starts a subagent within milliseconds of the tool call.
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"
TERMINAL = (COMPLETED, FAILED, STOPPED)


def agent_turn_transcript(labels: list[str]) -> str:
    names = [str(x).strip() for x in labels if str(x).strip()]
    if not names:
        return AGENT_TURN_PREFIX
    return f"{AGENT_TURN_PREFIX} — " + ", ".join(names) + " finished"


def is_agent_turn(transcript: str | None) -> bool:
    return bool(transcript) and str(transcript).startswith(AGENT_TURN_PREFIX)


def agent_turn_label(transcript: str) -> str:
    """What the transcript header shows under "AGENTS" for such a turn."""
    rest = str(transcript)[len(AGENT_TURN_PREFIX):].lstrip(" —")
    return rest or "A background agent finished"


@dataclass
class AgentActivity:
    ts: float
    kind: str  # "tool" | "text" | "error" | "task"
    text: str
    name: str = ""


@dataclass
class AgentRun:
    tool_use_id: str
    local_session_id: int | None
    description: str = ""
    subagent_type: str = ""
    background: bool = False
    prompt: str = ""
    job_id: int = 0
    task_id: str = ""
    status: str = RUNNING
    started_ts: float = field(default_factory=time.time)
    ended_ts: float = 0.0
    # Latest one-line "what it's doing" (task_progress description or the
    # subagent's last tool call).
    doing: str = ""
    last_tool: str = ""
    total_tokens: int = 0
    tool_uses: int = 0
    summary: str = ""
    output_file: str = ""
    activity: list[AgentActivity] = field(default_factory=list)
    stop_requested: bool = False

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    def elapsed_s(self, now: float | None = None) -> float:
        end = self.ended_ts or (now if now is not None else time.time())
        return max(0.0, end - self.started_ts)

    def title(self) -> str:
        return self.description or self.subagent_type or "Agent"

    def add_activity(self, kind: str, text: str, name: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.activity.append(AgentActivity(time.time(), kind, text, name))
        if len(self.activity) > _ACTIVITY_CAP:
            del self.activity[: len(self.activity) - _ACTIVITY_CAP]
        if kind in ("tool", "task"):
            self.doing = text
            if name:
                self.last_tool = name

    def apply(self, kind: str, data: dict) -> None:
        """Fold one ``agent_*`` event into this record."""
        if kind == "agent_launched":
            self.description = data.get("description") or self.description
            self.subagent_type = data.get("subagent_type") or self.subagent_type
            self.background = bool(data.get("background", self.background))
            self.prompt = data.get("prompt") or self.prompt
            self.job_id = int(data.get("job_id") or self.job_id or 0)
        elif kind == "agent_started":
            self.task_id = data.get("task_id") or self.task_id
            if not self.description:
                self.description = data.get("description") or ""
        elif kind == "agent_progress":
            self.task_id = data.get("task_id") or self.task_id
            doing = str(data.get("description") or "").strip()
            if doing:
                self.doing = doing
            if data.get("last_tool_name"):
                self.last_tool = str(data["last_tool_name"])
            self._take_usage(data.get("usage"))
        elif kind == "agent_activity":
            self.add_activity(str(data.get("kind") or "text"), str(data.get("text") or ""),
                              str(data.get("name") or ""))
        elif kind == "agent_done":
            status = str(data.get("status") or COMPLETED)
            self.status = status if status in TERMINAL else COMPLETED
            self.ended_ts = time.time()
            self.summary = str(data.get("summary") or self.summary)
            self.output_file = str(data.get("output_file") or self.output_file)
            self._take_usage(data.get("usage"))

    def _take_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        try:
            self.total_tokens = int(usage.get("total_tokens") or self.total_tokens)
            self.tool_uses = int(usage.get("tool_uses") or self.tool_uses)
        except (TypeError, ValueError):
            pass


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"
