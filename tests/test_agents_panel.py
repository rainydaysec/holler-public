"""Agents pane in the sessions window: auto-open, cards, Stop, header count,
and agent-update turns in the transcript."""

from __future__ import annotations

import os

from PySide6 import QtWidgets

from holler import agents
from holler.sdk_session import SessionEvent
from tests.test_sessions_window_selection import qapp, win  # noqa: F401 (fixtures)


def _pump(n: int = 5) -> None:
    for _ in range(n):
        QtWidgets.QApplication.processEvents()


def _session_with_turn(w, text="research two things in parallel"):
    sid = w._store.create_session(model="m", transcript=text, screenshot_path=None, status="idle")
    tid = w._store.add_turn(session_id=sid, transcript=text, screenshot_path=None, job_id=1)
    w._store.update_turn(tid, final_text="I've started two parallel investigations.",
                         ended_at=1.0, duration_ms=1000)
    w._refresh_list()
    return sid


def _agent(w, sid, tuid, desc, *, background=True, **extra):
    base = {"tool_use_id": tuid, "local_session_id": sid}
    w._job_store.apply_event(SessionEvent(1, "agent_launched", {
        **base, "description": desc, "subagent_type": "general-purpose",
        "background": background, "prompt": f"Investigate: {desc}"}))
    w._job_store.apply_event(SessionEvent(1, "agent_started", {**base, "task_id": f"task-{tuid}"}))
    for kind, data in extra.get("events", []):
        w._job_store.apply_event(SessionEvent(1, kind, {**base, **data}))


def test_pane_opens_when_selected_session_launches_agents(win):
    sid = _session_with_turn(win)
    win._select_session(sid)
    _pump()
    assert not win._agents_panel.isVisible() or win._agents_panel.isHidden()

    _agent(win, sid, "tA", "Audit Canvas CrowdStrike usage")
    _pump()
    assert win._agents_btn.isChecked()
    assert not win._agents_panel.isHidden()
    assert win._agents_panel.card_for("tA") is not None
    assert "1 running" in win._agents_btn.text()
    win._refresh_warmth()
    assert "1 agent" in win._warmth.accessibleName()


def test_closing_pane_keeps_it_closed_for_that_session(win):
    sid = _session_with_turn(win)
    win._select_session(sid)
    _agent(win, sid, "tA", "First")
    _pump()
    win._agents_panel.close_requested.emit()
    _pump()
    assert win._agents_panel.isHidden()
    _agent(win, sid, "tB", "Second")
    _pump()
    assert win._agents_panel.isHidden(), "operator dismissed it — don't pop it back"
    # The header toggle still opens it on demand.
    win._agents_btn.click()
    _pump()
    assert not win._agents_panel.isHidden()


def test_cards_reflect_lifecycle_and_stop_emits(win):
    sid = _session_with_turn(win)
    win._select_session(sid)
    stops: list[tuple[int, str]] = []
    win.stop_agent_requested.connect(lambda s, t: stops.append((s, t)))
    _agent(win, sid, "tA", "Map the Falcon Intel API", events=[
        ("agent_progress", {"description": "Fetching falcon.crowdstrike.com docs",
                            "last_tool_name": "WebFetch",
                            "usage": {"total_tokens": 18400, "tool_uses": 6}}),
    ])
    _pump()
    card = win._agents_panel.card_for("tA")
    assert "Running" in card._meta.text() and "18.4k tok" in card._meta.text()
    assert "Fetching falcon" in card._doing.text()
    assert not card._stop_btn.isHidden()

    card._stop_btn.click()
    _pump()
    assert stops == [(sid, "tA")]
    assert "Stopping" in card._meta.text()
    assert card._stop_btn.isHidden()

    win._job_store.apply_event(SessionEvent(1, "agent_done", {
        "tool_use_id": "tA", "local_session_id": sid, "status": "stopped", "summary": ""}))
    _pump()
    assert "Stopped" in win._agents_panel.card_for("tA")._meta.text()
    assert "running" not in win._agents_btn.text()


def test_expanding_card_shows_activity_and_prompt(win):
    sid = _session_with_turn(win)
    win._select_session(sid)
    _agent(win, sid, "tA", "Audit", events=[
        ("agent_activity", {"kind": "tool", "name": "Grep", "text": "Searching for “falcon”"}),
        ("agent_activity", {"kind": "error", "text": "rg: permission denied"}),
    ])
    _pump()
    card = win._agents_panel.card_for("tA")
    assert card._details.isHidden()
    card._chevron.click()
    _pump()
    card = win._agents_panel.card_for("tA")
    assert not card._details.isHidden()
    html = card._details.text()
    assert "falcon" in html and "permission denied" in html and "Investigate: Audit" in html


def test_switching_sessions_swaps_cards(win):
    a = _session_with_turn(win, "one")
    b = _session_with_turn(win, "two")
    win._select_session(a)
    _agent(win, a, "tA", "A's agent")
    _agent(win, b, "tB", "B's agent")
    _pump()
    assert win._agents_panel.card_for("tA") and not win._agents_panel.card_for("tB")
    win._select_session(b)
    _pump()
    assert win._agents_panel.card_for("tB") and not win._agents_panel.card_for("tA")


def test_agent_update_turn_renders_as_agents_label(win):
    sid = _session_with_turn(win)
    t = agents.agent_turn_transcript(["Audit Canvas"])
    tid = win._store.add_turn(session_id=sid, transcript=t, screenshot_path=None, job_id=9)
    win._store.update_turn(tid, final_text="The audit found three call sites.", ended_at=2.0)
    win._select_session(sid)
    _pump()
    html = win._detail._browser.toHtml() if hasattr(win._detail, "_browser") else ""
    if not html:
        browsers = win._detail.findChildren(QtWidgets.QTextBrowser)
        html = "".join(b.toHtml() for b in browsers)
    assert "Audit Canvas finished" in html
    assert "three call sites" in html


def test_screenshot(win, tmp_path):
    """Not an assertion test: renders a realistic pane for eyeballing when
    HOLLER_AGENTS_SHOT is set to an output path."""
    out = os.environ.get("HOLLER_AGENTS_SHOT")
    sid = _session_with_turn(win, "Maximize CrowdStrike threat intel in Canvas — research in parallel")
    win.resize(1500, 860)
    win.show()
    win._select_session(sid)
    _agent(win, sid, "t1", "Audit Canvas CrowdStrike usage", events=[
        ("agent_progress", {"description": "Reading canvas/services/crowdstrike.py",
                            "last_tool_name": "Read", "usage": {"total_tokens": 41200, "tool_uses": 14}}),
    ])
    _agent(win, sid, "t2", "Map Falcon Intel API (CAO tier)", events=[
        ("agent_progress", {"description": "Fetching falcon.crowdstrike.com/documentation",
                            "usage": {"total_tokens": 23900, "tool_uses": 9}}),
        ("agent_done", {"status": "completed", "usage": {"total_tokens": 30100, "tool_uses": 11},
                        "summary": "CAO unlocks Intel actors, indicators, reports and MITRE "
                                   "mappings; Canvas uses only /intel/combined/actors today."}),
    ])
    _agent(win, sid, "t3", "Check Wiz ↔ Falcon overlap", events=[
        ("agent_done", {"status": "failed", "summary": "Wiz API returned 401."}),
    ])
    _pump(20)
    if out:
        win.grab().save(out)
    assert len(win._agents_panel._cards) == 3


def test_pane_opens_when_switching_to_session_with_running_agents(win):
    a = _session_with_turn(win, "one")
    b = _session_with_turn(win, "two")
    win._select_session(a)
    _agent(win, b, "tB", "B's agent")  # launched while another session is on screen
    _pump()
    assert win._agents_panel.isHidden()
    win._select_session(b)
    _pump()
    assert not win._agents_panel.isHidden()
    assert win._agents_panel.card_for("tB") is not None


def test_restart_prompt_counts_sessions_with_running_agents(win):
    from holler.runners.full import _busy_session_count
    sid = _session_with_turn(win)
    assert _busy_session_count(win._job_store) == 0
    _agent(win, sid, "tA", "Long build")
    assert _busy_session_count(win._job_store) == 1


def test_sidebar_row_shows_working_while_only_agents_run(win):
    """2026-10-02: the turn ended (row showed done) while its background
    agents kept working — the row must read as working until they finish."""
    sid = _session_with_turn(win)
    win._refresh_list()
    win._refresh_visible_totals()
    row = win._row_widgets[sid]
    assert not row._is_running

    _agent(win, sid, "tA", "Resume WP-C", events=[
        ("agent_progress", {"description": "Running mkdir -p /tmp/x"})])
    win._refresh_visible_totals()
    assert row._is_running and row.state() == "running"
    verb, elapsed, _ = win._live_state_for(sid, __import__("time").time())
    assert verb.startswith("1 agent running") and "mkdir" in verb

    win._current_filter = "running"
    win._refresh_list()
    assert sid in win._row_widgets

    win._job_store.apply_event(SessionEvent(1, "agent_done", {
        "tool_use_id": "tA", "local_session_id": sid, "status": "completed", "summary": "ok"}))
    win._current_filter = "active"
    win._refresh_list()
    win._refresh_visible_totals()
    assert not win._row_widgets[sid]._is_running
