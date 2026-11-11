"""Parallel / background subagents: TurnRouter routing, JobStore agent
registry, and agent-driven follow-up turns.

Message sequences mirror what CLI 2.1.28x actually streams (captured with a
live probe on 2026-10-02): after a turn that launched background agents ends,
the CLI emits task_notification for each finished agent and then starts a turn
of its own (init → assistant → ResultMessage) — with no user message.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
)

from holler import agents
from holler.turn_router import PumpClosed, TurnRouter

# ── message builders ────────────────────────────────────────────────────────


def _init(sid="sdk-1"):
    return SystemMessage(subtype="init", data={"session_id": sid})


def _echo(text):
    return UserMessage(content=text)


def _say(text, parent=None):
    return AssistantMessage(content=[TextBlock(text=text)], model="m", parent_tool_use_id=parent)


def _launch(tuid, desc, background=True, parent=None):
    return AssistantMessage(
        content=[ToolUseBlock(id=tuid, name="Agent", input={
            "description": desc, "prompt": f"do {desc}", "subagent_type": "general-purpose",
            "run_in_background": background,
        })],
        model="m", parent_tool_use_id=parent,
    )


def _tool(tuid, name, inp, parent=None):
    return AssistantMessage(content=[ToolUseBlock(id=tuid, name=name, input=inp)],
                            model="m", parent_tool_use_id=parent)


def _result_for(tuid, text, parent=None, is_error=False):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tuid, content=text, is_error=is_error)],
                       parent_tool_use_id=parent)


def _started(task_id, tuid, desc):
    return TaskStartedMessage(subtype="task_started", data={}, task_id=task_id, description=desc,
                              uuid="u", session_id="sdk-1", tool_use_id=tuid, task_type="local_agent")


def _progress(task_id, tuid, desc, tool="Bash", tokens=1200, uses=1):
    return TaskProgressMessage(subtype="task_progress", data={}, task_id=task_id, description=desc,
                               usage={"total_tokens": tokens, "tool_uses": uses, "duration_ms": 900},
                               uuid="u", session_id="sdk-1", tool_use_id=tuid, last_tool_name=tool)


def _notify(task_id, tuid, summary, status="completed"):
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id=task_id,
                                   status=status, output_file="/tmp/out", summary=summary,
                                   uuid="u", session_id="sdk-1", tool_use_id=tuid,
                                   usage={"total_tokens": 13000, "tool_uses": 3, "duration_ms": 11000})


def _done(text="ok"):
    return ResultMessage(subtype="success", duration_ms=10, duration_api_ms=9, is_error=False,
                         num_turns=1, session_id="sdk-1", result=text, total_cost_usd=0.01)


class _Recorder:
    def __init__(self):
        self.events: list[tuple[int, str, dict]] = []
        self.followups: list[list[str]] = []
        self._next = 100

    def emit(self, job_id, kind, data):
        self.events.append((job_id, kind, data))

    def open_followup(self, labels):
        self.followups.append(labels)
        self._next += 1
        return self._next

    def kinds(self, job_id):
        return [k for j, k, _ in self.events if j == job_id]

    def text(self, job_id):
        return "".join(d["text"] for j, k, d in self.events if j == job_id and k == "text_delta")


def _router(rec):
    return TurnRouter(emit=rec.emit, open_followup=rec.open_followup, local_session_id=7)


def _run(coro):
    return asyncio.run(coro)


# ── TurnRouter ──────────────────────────────────────────────────────────────


def test_user_turn_resolves_on_its_result_and_keeps_reply():
    rec = _Recorder()

    async def go():
        r = _router(rec)
        fut = r.expect_turn(1)
        for m in (_init(), _echo("hi"), _say("hello"), _done("hello")):
            r.handle(m)
        return await fut

    res = _run(go())
    assert res.result == "hello"
    assert rec.text(1) == "hello"
    assert ("session_init" in rec.kinds(1))
    assert rec.followups == []


def test_background_completion_after_turn_becomes_followup_job():
    """The 2026-10-02 bug: agent results arriving after the turn ended were
    left in the buffer and answered the operator's NEXT message."""
    rec = _Recorder()

    async def go():
        r = _router(rec)
        fut = r.expect_turn(1)
        for m in (_init(), _echo("research two things"),
                  _launch("tA", "Audit Canvas"), _started("a1", "tA", "Audit Canvas"),
                  _result_for("tA", "Async agent launched successfully."),
                  _launch("tB", "Map CS API"), _started("b1", "tB", "Map CS API"),
                  _result_for("tB", "Async agent launched successfully."),
                  _say("Started two investigations."), _done()):
            r.handle(m)
        await fut
        assert r.has_live_agents()
        # Turn is over. Agent B finishes; the CLI starts its own turn.
        for m in (_notify("b1", "tB", "API map done"), _init(),
                  _say("The CrowdStrike API map is done."), _done("x")):
            r.handle(m)
        # Operator now asks something; agent A finishes during that wait.
        fut2 = r.expect_turn(2)
        for m in (_notify("a1", "tA", "Audit done"), _init(), _say("Both done: ..."), _done(),
                  _init(), _echo("i don't see those?"), _say("Here they are."), _done("Here they are.")):
            r.handle(m)
        res2 = await fut2
        assert not r.has_live_agents()
        return res2

    res2 = _run(go())
    assert rec.followups == [["Map CS API"], ["Audit Canvas"]]
    assert rec.text(101) == "The CrowdStrike API map is done."
    assert rec.kinds(101)[-1] == "result"
    assert rec.text(102) == "Both done: ..."
    # The operator's question got its own answer, not the stale agent turn.
    assert res2.result == "Here they are."
    assert rec.text(2) == "Here they are."
    assert "Started two investigations." in rec.text(1)


def test_turn_without_echo_falls_back_to_waiting_job():
    """A CLI that doesn't echo: with no agent completion owed, the next turn
    is the user's (pre-router behavior)."""
    rec = _Recorder()

    async def go():
        r = _router(rec)
        fut = r.expect_turn(1)
        for m in (_init(), _say("hello"), _done("hello")):
            r.handle(m)
        return await fut

    assert _run(go()).result == "hello"
    assert rec.followups == []


def test_agent_events_carry_progress_activity_and_summary():
    rec = _Recorder()

    async def go():
        r = _router(rec)
        r.expect_turn(1)
        for m in (_init(), _echo("go"), _launch("tA", "Audit Canvas"),
                  _started("a1", "tA", "Audit Canvas"),
                  _tool("sub1", "Grep", {"pattern": "crowdstrike"}, parent="tA"),
                  _progress("a1", "tA", "Searching for crowdstrike", tool="Grep"),
                  _result_for("sub1", "boom", parent="tA", is_error=True),
                  _say("Found 3 call sites", parent="tA"),
                  _started("bash1", "sub1", "nested shell"),
                  _notify("a1", "tA", "Canvas uses CS in 3 places")):
            r.handle(m)

    _run(go())
    agent_events = [(k, d) for _, k, d in rec.events if k.startswith("agent_")]
    kinds = [k for k, _ in agent_events]
    assert kinds[0] == "agent_launched"
    assert "agent_started" in kinds and "agent_progress" in kinds and kinds[-1] == "agent_done"
    assert all(d["tool_use_id"] == "tA" and d["local_session_id"] == 7 for _, d in agent_events)
    acts = [d for k, d in agent_events if k == "agent_activity"]
    assert [a["kind"] for a in acts] == ["tool", "error", "text", "task"]
    assert "crowdstrike" in acts[0]["text"]
    # Subagent interior traffic never reaches the parent job's transcript.
    assert "Found 3 call sites" not in rec.text(1)


def test_foreground_agent_completes_on_its_tool_result():
    rec = _Recorder()

    async def go():
        r = _router(rec)
        fut = r.expect_turn(1)
        for m in (_init(), _echo("go"), _launch("tA", "Quick look", background=False),
                  _started("a1", "tA", "Quick look"),
                  _result_for("tA", "Found it."), _say("Done."), _done()):
            r.handle(m)
        await fut
        return r

    r = _run(go())
    done = [d for _, k, d in rec.events if k == "agent_done"]
    assert len(done) == 1 and done[0]["summary"] == "Found it."
    assert not r.has_live_agents()


def test_closing_fails_waiters_and_stops_running_agents():
    rec = _Recorder()

    async def go():
        r = _router(rec)
        fut = r.expect_turn(1)
        for m in (_init(), _echo("go"), _launch("tA", "Long job"), _started("a1", "tA", "Long job")):
            r.handle(m)
        r._close(PumpClosed("stream ended"))
        with pytest.raises(PumpClosed):
            await fut

    _run(go())
    done = [d for _, k, d in rec.events if k == "agent_done"]
    assert done and done[0]["status"] == "stopped"


def test_pump_reads_receive_messages_until_stream_ends():
    rec = _Recorder()

    class _Client:
        async def receive_messages(self):
            for m in (_init(), _echo("hi"), _say("yo"), _done("yo")):
                await asyncio.sleep(0)
                yield m

    async def go():
        r = _router(rec)
        fut = r.expect_turn(1)
        r.start(_Client())
        res = await fut
        await asyncio.sleep(0.01)
        return res, r

    res, r = _run(go())
    assert res.result == "yo"
    assert r.closed


# ── AgentRun / JobStore ─────────────────────────────────────────────────────


def test_agent_run_folds_events():
    run = agents.AgentRun(tool_use_id="tA", local_session_id=7)
    run.apply("agent_launched", {"description": "Audit", "subagent_type": "Explore", "background": True})
    run.apply("agent_started", {"task_id": "a1"})
    run.apply("agent_progress", {"description": "Reading files", "last_tool_name": "Read",
                                 "usage": {"total_tokens": 2500, "tool_uses": 4}})
    assert run.running and run.doing == "Reading files" and run.total_tokens == 2500
    run.apply("agent_done", {"status": "completed", "summary": "All good",
                             "usage": {"total_tokens": 9000, "tool_uses": 6}})
    assert run.status == agents.COMPLETED and run.summary == "All good" and run.tool_uses == 6
    assert run.ended_ts > 0


def test_agent_turn_transcript_roundtrip():
    t = agents.agent_turn_transcript(["Audit Canvas", "Map API"])
    assert agents.is_agent_turn(t)
    assert agents.agent_turn_label(t) == "Audit Canvas, Map API finished"
    assert not agents.is_agent_turn("hello")
    assert agents.agent_turn_label(agents.agent_turn_transcript([])) == "A background agent finished"


def test_jobstore_registry_and_followup_turn(qapp, tmp_path):
    from holler.jobs import JobStore
    from holler.sdk_session import SessionEvent
    from holler.store import Store

    store = Store(tmp_path / "holler.db")
    try:
        sid = store.create_session(model="m", transcript="t", screenshot_path=None)
        jobs = JobStore(store)
        changed: list[int] = []
        jobs.agents_changed.connect(changed.append)

        jobs.apply_event(SessionEvent(0, "agent_launched", {
            "tool_use_id": "tA", "local_session_id": sid, "description": "Audit", "background": True}))
        jobs.apply_event(SessionEvent(0, "agent_started", {
            "tool_use_id": "tA", "local_session_id": sid, "task_id": "a1"}))
        assert [a.title() for a in jobs.agents_for_session(sid)] == ["Audit"]
        assert len(jobs.running_agents()) == 1
        jobs.apply_event(SessionEvent(0, "agent_done", {
            "tool_use_id": "tA", "local_session_id": sid, "status": "completed", "summary": "ok"}))
        assert jobs.running_agents() == []
        assert changed and changed[-1] == sid

        jobs.apply_event(SessionEvent(55, "followup", {
            "labels": ["Audit"], "model": "m", "local_session_id": sid, "resume_session_id": "sdk"}))
        jobs.apply_event(SessionEvent(55, "started", {"model": "m"}))
        jobs.apply_event(SessionEvent(55, "text_delta", {"text": "Audit says ok"}))
        jobs.apply_event(SessionEvent(55, "result", {"is_error": False, "result": "Audit says ok",
                                                     "duration_ms": 5}))
        turns = store.get_turns(sid)
        assert len(turns) == 1
        assert agents.is_agent_turn(turns[0].transcript)
        assert turns[0].final_text == "Audit says ok"
    finally:
        store.close()


@pytest.fixture(scope="module")
def qapp():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_cli_backgrounded_agent_without_flag_stays_running():
    """2026-10-02: agents launched without run_in_background were backgrounded
    by the CLI anyway; their 'Async agent launched' receipt was mistaken for
    completion, so the pane showed them Done instantly and never opened."""
    rec = _Recorder()

    async def go():
        r = _router(rec)
        fut = r.expect_turn(1)
        for m in (_init(), _echo("go"), _launch("tA", "Phase 0", background=False),
                  _started("a1", "tA", "Phase 0"),
                  _result_for("tA", "Async agent launched successfully. agentId: x"),
                  _say("Started."), _done()):
            r.handle(m)
        await fut
        assert r.has_live_agents()
        for m in (_notify("a1", "tA", "Phase 0 built"), _init(), _say("Phase 0 is done."), _done()):
            r.handle(m)
        return r

    r = _run(go())
    done = [d for _, k, d in rec.events if k == "agent_done"]
    assert len(done) == 1 and done[0]["summary"] == "Phase 0 built"
    assert rec.followups == [["Phase 0"]]
    assert not r.has_live_agents()
