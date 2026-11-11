"""Phase 4 — transcript (holler/detail.py, markdown_render.py, tool_format.py).

Real offscreen Qt widgets against a temp Store; app data is redirected to
tmp_path so nothing touches ~/Library/Application Support/holler.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from holler import theme  # noqa: E402
from holler.detail import DetailView  # noqa: E402
from holler.jobs import ContentBlock, Job, JobStore  # noqa: E402
from holler.sdk_session import SessionEvent  # noqa: E402


def _pump(app, n: int = 6) -> None:
    for _ in range(n):
        app.processEvents()


@pytest.fixture
def store(app, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state
    from holler.store import Store

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)
    s = Store(tmp_path / "holler.db")
    yield s
    s.close()


@pytest.fixture
def opened(monkeypatch):
    calls: list[QtCore.QUrl] = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl",
                        lambda u: calls.append(QtCore.QUrl(u)) or True)
    return calls


def _run(jobs, store, sid, job_id, events, *, result="DONE", is_error=False, error=None,
         transcript=None, shot=None):
    tid = store.add_turn(session_id=sid, transcript=transcript or f"turn {job_id}",
                         screenshot_path=shot, job_id=job_id)
    jobs.apply_event(SessionEvent(job_id, "submitted", {
        "transcript": transcript or f"turn {job_id}", "local_session_id": sid, "local_turn_id": tid}))
    jobs.apply_event(SessionEvent(job_id, "started", {}))
    for kind, data in events:
        jobs.apply_event(SessionEvent(job_id, kind, data))
    if error is not None:
        jobs.apply_event(SessionEvent(job_id, "error", {"error": error}))
    elif result is not None:
        jobs.apply_event(SessionEvent(job_id, "result", {
            "result": result, "is_error": is_error, "duration_ms": 1200}))
    return tid


def _view(app, size=(700, 500)) -> DetailView:
    v = DetailView()
    v.resize(*size)
    v.show()
    _pump(app)
    return v


def _show(v, store, jobs, sid, live=None, **kw):
    turns = store.get_turns(sid)
    v.show_session(store.get_session(sid), turns,
                   store.get_tool_calls_for_turns([t.id for t in turns]), live, **kw)


def _text(v) -> str:
    return v._reply_browser.toPlainText()


# ── errored turns show the error, not "(no reply)" ──────────────────────────

def test_live_errored_job_shows_error_text(app):
    v = _view(app)
    job = Job(id=5, transcript="deploy", submitted_at=0.0, status="error",
              error="CLIConnectionError: Cannot write to terminated process")
    v.show_job(job)
    _pump(app)
    t = _text(v)
    assert "Turn failed" in t and "Cannot write to terminated process" in t
    assert "No reply" not in t and "(no reply)" not in t


def test_committed_errored_turn_keeps_its_error_after_restart(app, store):
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    jobs = JobStore(store)
    _run(jobs, store, sid, 1, [], error="boom: the SDK process exited")
    turn = store.get_turns(sid)[0]
    assert turn.is_error
    # The reason is persisted with the turn's blocks…
    assert json.loads(turn.blocks_json) == [{"t": "error", "text": "boom: the SDK process exited"}]
    # …so a fresh view (no Job in memory) still shows why it failed.
    v = _view(app)
    _show(v, store, JobStore(store), sid)
    _pump(app)
    t = _text(v)
    assert "Turn failed" in t and "boom: the SDK process exited" in t
    assert "No reply" not in t


def test_legacy_errored_turn_without_blocks_shows_banner(app, store):
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    tid = store.add_turn(session_id=sid, transcript="t", screenshot_path=None, job_id=None)
    store.update_turn(tid, final_text="CLIConnectionError: exit 143", is_error=1, ended_at=2.0)
    v = _view(app)
    _show(v, store, JobStore(store), sid)
    _pump(app)
    t = _text(v)
    assert "Turn failed" in t and t.count("CLIConnectionError: exit 143") == 1


def test_errored_turn_with_streamed_text_keeps_text_once(app, store):
    from holler.detail import _render_committed_turn_html

    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    jobs = JobStore(store)
    _run(jobs, store, sid, 1, [("text_delta", {"text": "PARTIAL-ANSWER"})], error="kaput")
    turn = store.get_turns(sid)[0]
    html = _render_committed_turn_html(turn, store.get_tool_calls(turn.id))
    assert html.count("PARTIAL-ANSWER") == 1 and "kaput" in html


# ── Esc stops a running turn from anywhere in the host window ───────────────

class _Host(QtWidgets.QWidget):
    """Mimics the Sessions window: composer has focus, transcript NoFocus."""

    def __init__(self):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        self.detail = DetailView()
        self.detail._reply_browser.setFocusPolicy(QtCore.Qt.NoFocus)
        self.composer = QtWidgets.QPlainTextEdit()
        lay.addWidget(self.detail)
        lay.addWidget(self.composer)


@pytest.fixture
def host(app):
    h = _Host()
    h.resize(600, 500)
    h.show()
    h.activateWindow()
    _pump(app)
    h.composer.setFocus()
    _pump(app)
    yield h
    DetailView.set_capture_active_predicate(None)
    h.close()
    h.deleteLater()


def test_esc_in_composer_stops_running_turn(app, host):
    job = Job(id=11, transcript="go", submitted_at=0.0, status="running", started_at=1.0)
    host.detail.show_job(job)
    got = []
    host.detail.cancel_requested.connect(got.append)
    QtTest.QTest.keyClick(host.composer, QtCore.Qt.Key_Escape)
    assert got == [11]


def test_esc_ignored_when_idle_or_capturing(app, host):
    got = []
    host.detail.cancel_requested.connect(got.append)
    done = Job(id=12, transcript="go", submitted_at=0.0, status="done")
    host.detail.show_job(done)
    QtTest.QTest.keyClick(host.composer, QtCore.Qt.Key_Escape)
    assert got == []
    running = Job(id=13, transcript="go", submitted_at=0.0, status="running", started_at=1.0)
    host.detail.show_job(running)
    DetailView.set_capture_active_predicate(lambda: True)
    QtTest.QTest.keyClick(host.composer, QtCore.Qt.Key_Escape)
    assert got == [], "Esc during a push-to-talk capture belongs to the capture"
    DetailView.set_capture_active_predicate(None)
    QtTest.QTest.keyClick(host.composer, QtCore.Qt.Key_Escape)
    assert got == [13]


def test_one_live_bar_with_stop_button(app):
    v = _view(app)
    assert v._live_bar is v._live_bottom_bar
    assert v._cancel_btn.text() == "Stop" and "Esc" in v._cancel_btn.toolTip()
    assert v._cancel_btn.accessibleName() == "Stop turn"
    assert not v._cancel_btn.icon().isNull()
    v.show_job(Job(id=1, transcript="x", submitted_at=0.0, status="running", started_at=1.0))
    _pump(app)
    assert v._live_bottom_bar.isVisible() and v._cancel_btn.isVisible()
    got = []
    v.cancel_requested.connect(got.append)
    v._cancel_btn.click()
    assert got == [1]


# ── streaming updates only the live segment ─────────────────────────────────

def _session_with_live(store, jobs):
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    for k in range(1, 4):
        _run(jobs, store, sid, k, [("text_delta", {"text": f"COMMITTED-{k} words here"})],
             result=f"COMMITTED-{k} words here")
    _run(jobs, store, sid, 9, [("text_delta", {"text": "LIVE-0 "})], result=None)
    return sid


def test_streaming_tick_keeps_selection_and_committed_document(app, store, monkeypatch):
    jobs = JobStore(store)
    sid = _session_with_live(store, jobs)
    v = _view(app)
    _show(v, store, jobs, sid, live=jobs.get(9))
    _pump(app)
    b = v._reply_browser
    cur = b.document().find("COMMITTED-2")
    assert not cur.isNull()
    b.setTextCursor(cur)
    calls = []
    real = b.setHtml
    monkeypatch.setattr(b, "setHtml", lambda h: (calls.append(1), real(h)))
    for i in range(5):
        jobs.apply_event(SessionEvent(9, "text_delta", {"text": f"LIVE-{i + 1} "}))
        v.update_streaming(jobs.get(9))
        v._render_timer.stop()
        v._render_now()
        _pump(app, 2)
    assert calls == [], "a streaming tick must not rebuild the whole document"
    assert b.textCursor().selectedText() == "COMMITTED-2"
    t = _text(v)
    assert "LIVE-5" in t and t.count("COMMITTED-1 words") == 1 and t.count("LIVE-0") == 1


def test_refresh_with_unchanged_turns_keeps_document(app, store, monkeypatch):
    """Sessions-window metadata churn re-calls show_session(auto_scroll=False)."""
    jobs = JobStore(store)
    sid = _session_with_live(store, jobs)
    v = _view(app)
    _show(v, store, jobs, sid, live=jobs.get(9))
    _pump(app)
    b = v._reply_browser
    b.setTextCursor(b.document().find("COMMITTED-1"))
    calls = []
    real = b.setHtml
    monkeypatch.setattr(b, "setHtml", lambda h: (calls.append(1), real(h)))
    _show(v, store, jobs, sid, live=jobs.get(9), auto_scroll=False)
    _pump(app)
    assert calls == [] and b.textCursor().selectedText() == "COMMITTED-1"
    # A new committed turn does rebuild.
    _run(jobs, store, sid, 10, [("text_delta", {"text": "NEWER"})], result="NEWER")
    _show(v, store, jobs, sid, live=jobs.get(10), auto_scroll=False)
    assert calls == [1] and "NEWER" in _text(v)


def test_terminal_render_after_streaming_has_no_duplicates(app, store):
    jobs = JobStore(store)
    sid = _session_with_live(store, jobs)
    v = _view(app)
    _show(v, store, jobs, sid, live=jobs.get(9))
    jobs.apply_event(SessionEvent(9, "text_delta", {"text": "\n\n```python\nx = 1\n```\n"}))
    v.update_streaming(jobs.get(9))
    v._render_now()
    jobs.apply_event(SessionEvent(9, "result", {"result": "", "duration_ms": 10}))
    v.update_streaming(jobs.get(9))
    _pump(app)
    t = _text(v)
    assert t.count("LIVE-0") == 1 and t.count("x = 1") == 1 and t.count("You · Turn 4".upper()) <= 1


# ── tool cards: expandable output, diffs ─────────────────────────────────────

def _bash_session(store):
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    jobs = JobStore(store)
    out = "\n".join(f"OUTLINE-{i}" for i in range(30))
    _run(jobs, store, sid, 1, [
        ("tool_use", {"id": "u1", "name": "Bash", "input": {"command": "seq 30"}}),
        ("tool_result", {"tool_use_id": "u1", "content": out, "is_error": False}),
        ("tool_use", {"id": "u2", "name": "Edit", "input": {
            "file_path": "/r/a.py", "old_string": "OLDLINE\nkeep", "new_string": "NEWLINE\nkeep"}}),
        ("tool_result", {"tool_use_id": "u2", "content": "ok", "is_error": False}),
    ])
    return sid


def test_more_lines_link_expands_full_output_in_place(app, store):
    sid = _bash_session(store)
    v = _view(app)
    _show(v, store, JobStore(store), sid)
    _pump(app)
    assert "OUTLINE-2" in _text(v) and "OUTLINE-20" not in _text(v)
    tc = store.get_tool_calls(store.get_turns(sid)[0].id)[0]
    assert f"holler://expand/tc{tc.id}" in v._reply_browser.toHtml()
    v._on_anchor_clicked(QtCore.QUrl(f"holler://expand/tc{tc.id}"))
    _pump(app)
    assert "OUTLINE-29" in _text(v) and "Show less" in _text(v)
    v._on_anchor_clicked(QtCore.QUrl(f"holler://expand/tc{tc.id}"))
    _pump(app)
    assert "OUTLINE-20" not in _text(v)


def test_edit_renders_red_green_diff_from_input(app, store):
    from holler.detail import _render_tool_block_html

    html = _render_tool_block_html(
        "Edit", {"file_path": "/r/a.py", "old_string": "OLDLINE", "new_string": "NEWLINE"},
        "ok", False, False)
    assert theme.DIFF_ADD_BG in html and theme.DIFF_DEL_BG in html
    assert html.index("OLDLINE") < html.index("NEWLINE") and "✓" in html
    sid = _bash_session(store)
    v = _view(app)
    _show(v, store, JobStore(store), sid)
    t = _text(v)
    assert "− OLDLINE" in t and "+ NEWLINE" in t


def test_thinking_is_collapsed_until_expanded(app):
    v = _view(app)
    long = "STEP " * 60 + "SECRET-TAIL"
    job = Job(id=3, transcript="x", submitted_at=0.0, status="running", started_at=1.0)
    job.blocks = [ContentBlock(kind="thinking", text=long)]
    v.show_job(job)
    _pump(app)
    assert "SECRET-TAIL" not in _text(v) and "Thinking" in _text(v)
    v._on_anchor_clicked(QtCore.QUrl("holler://expand/j3-k0"))
    _pump(app)
    assert "SECRET-TAIL" in _text(v)


# ── copy affordances ─────────────────────────────────────────────────────────

def test_copy_code_and_reply_links(app, store):
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    jobs = JobStore(store)
    reply = "Here:\n\n```python\nprint('COPYME')\n```\n"
    _run(jobs, store, sid, 1, [("text_delta", {"text": reply})], result=reply)
    v = _view(app)
    notices = []
    v.copy_notice.connect(notices.append)
    _show(v, store, JobStore(store), sid)
    html = v._reply_browser.toHtml()
    import re
    code = re.search(r"holler://copy/code/([\w-]+)", html).group(1)
    msg = re.search(r"holler://copy/msg/([\w-]+)", html).group(1)
    assert "python" in _text(v)  # language label on the block header
    v._on_anchor_clicked(QtCore.QUrl(f"holler://copy/code/{code}"))
    assert QtWidgets.QApplication.clipboard().text() == "print('COPYME')\n"
    v._on_anchor_clicked(QtCore.QUrl(f"holler://copy/msg/{msg}"))
    assert QtWidgets.QApplication.clipboard().text() == reply.strip()
    assert notices == ["Copied code", "Copied reply"]


# ── screenshots inline ───────────────────────────────────────────────────────

def _png(path, w=320, h=200):
    img = QtGui.QImage(w, h, QtGui.QImage.Format_RGB32)
    img.fill(QtGui.QColor(theme.ACCENT))
    img.save(str(path))
    return str(path)


def test_turn_screenshot_renders_inline_and_opens(app, store, opened, tmp_path):
    shot = _png(tmp_path / "s.png")
    sid = store.create_session(model="m", transcript="t", screenshot_path=shot)
    jobs = JobStore(store)
    tid = _run(jobs, store, sid, 1, [], result="ok", shot=shot)
    v = _view(app)
    _show(v, store, JobStore(store), sid)
    _pump(app)
    # Header thumbnail hidden (the turn carries it inline); no empty box.
    assert not v._thumb.isVisible()
    img = v._reply_browser.loadResource(QtGui.QTextDocument.ImageResource,
                                        QtCore.QUrl(f"holler-shot:{tid}"))
    assert isinstance(img, QtGui.QImage) and not img.isNull()
    assert f"holler-shot:{tid}" in v._reply_browser.toHtml()
    v._on_anchor_clicked(QtCore.QUrl(f"holler://open/shot/{tid}"))
    assert [u.toLocalFile() for u in opened] == [shot]


def test_header_thumbnail_hidden_without_screenshot(app):
    v = _view(app)
    v.show_job(Job(id=1, transcript="x", submitted_at=0.0, status="done"))
    _pump(app)
    assert not v._thumb.isVisible()
    assert v._thumb.accessibleName() and v._thumb.toolTip() == "Open screenshot"


# ── user turn ────────────────────────────────────────────────────────────────

def test_screen_unchanged_suffix_is_display_only(app, store):
    from holler import detail
    from holler.runners import full

    assert detail.SCREEN_UNCHANGED_SUFFIX == full.SCREEN_UNCHANGED_SUFFIX
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    said = "fix the button" + full.SCREEN_UNCHANGED_SUFFIX
    _run(JobStore(store), store, sid, 1, [], result="ok", transcript=said)
    assert store.get_turns(sid)[0].transcript == said  # stored data untouched
    v = _view(app)
    _show(v, store, JobStore(store), sid)
    t = _text(v)
    assert "fix the button" in t and "screen unchanged" not in t and "Same screen".upper() in t.upper()


def test_user_turn_is_a_neutral_card_and_reply_has_no_bullet(app):
    from holler.detail import _render_assistant_text_html, _render_user_turn_html
    from holler.domain import Turn

    t = Turn(id=1, session_id=1, seq=1, transcript="hello there", screenshot_path=None,
             final_text="", duration_ms=0, started_at=0, ended_at=0, is_error=False, job_id=None)
    html = _render_user_turn_html(1, t)
    assert f"bgcolor='{theme.SURFACE}'" in html and "italic" not in html and "You · Turn 1" in html
    assert "●" not in _render_assistant_text_html("reply")


# ── tool_format ──────────────────────────────────────────────────────────────

def test_edit_diff_rows():
    from holler.tool_format import diff_stats, edit_diff

    rows = edit_diff("MultiEdit", {"edits": [{"old_string": "a", "new_string": "b"},
                                             {"old_string": "c\nd", "new_string": "c"}]})
    assert ("-", "a") in rows and ("+", "b") in rows and ("@", "…") in rows and ("-", "d") in rows
    assert diff_stats(rows) == (1, 2)
    assert edit_diff("Write", {"content": "x\ny"}) == [("+", "x"), ("+", "y")]
    assert edit_diff("Read", {"file_path": "x"}) is None
    assert edit_diff("Edit", {"old_string": "same", "new_string": "same"}) is None


def test_live_verbs_use_plain_words():
    from holler.tool_format import summarize

    assert summarize("Bash", {"command": "cd /r && pytest -x"}) == "Running pytest -x"
    assert summarize("Grep", {"pattern": "foo"}) == "Searching for “foo”"
    assert summarize("Task", {"subagent_type": "Plan", "prompt": "p"}) == "Delegating to Plan: p"


def test_esc_stops_turn_in_real_sessions_window(app, store, tmp_path):
    """The routed bug: in the Sessions window focus lives in the composer
    (transcript is NoFocus), so DetailView.keyPressEvent never saw Esc."""
    from holler.sessions_window import SessionsWindow

    jobs = JobStore(store)
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    _run(jobs, store, sid, 21, [("text_delta", {"text": "working"})], result=None)
    w = SessionsWindow(store, jobs, tmp_path / "captures", tmp_path)
    try:
        w.resize(1100, 700)
        w.show()
        _pump(app)
        w.select_session(sid)
        _pump(app, 10)
        assert w._detail._job is not None and w._detail._job.status == "running"
        got = []
        w.cancel_job_requested.connect(got.append)
        w._text_input.setFocus()
        _pump(app)
        QtTest.QTest.keyClick(w._text_input, QtCore.Qt.Key_Escape)
        assert got == [21]
    finally:
        w.close()
        w.deleteLater()
        _pump(app)
