"""The live transcript segment is replaced in place on every streaming tick
(detail.DetailView._replace_live_segment). Those in-place edits must not grow
the document without bound:

* every ``<img>`` (Copy-link icons, thumbnails) used to leave an orphaned
  QTextFrame per tick — Qt's HTML importer gives each image its own frame
  object, which removing the text never frees;
* every table (tool cards, code blocks) leaves its formats behind — Qt frees
  those only on a full document reset, so a budget forces one now and then,
  keeping a selection in the committed turns.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtGui  # noqa: E402

from holler.detail import DetailView  # noqa: E402
from holler.jobs import JobStore  # noqa: E402
from holler.sdk_session import SessionEvent  # noqa: E402


def _pump(app, n: int = 4) -> None:
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


def _setup(app, store):
    """A session with one committed turn and a live turn streaming 10 code
    blocks (each with a Copy-icon header) and a few tool cards."""
    jobs = JobStore(store)
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    t0 = store.add_turn(session_id=sid, transcript="COMMITTED question", screenshot_path=None, job_id=None)
    store.update_turn(t0, final_text="COMMITTED-REPLY words here", ended_at=1.0, duration_ms=5)
    tid = store.add_turn(session_id=sid, transcript="live q", screenshot_path=None, job_id=1)
    jobs.apply_event(SessionEvent(1, "submitted", {"transcript": "live q", "local_session_id": sid,
                                                   "local_turn_id": tid}))
    jobs.apply_event(SessionEvent(1, "started", {}))
    for k in range(10):
        jobs.apply_event(SessionEvent(1, "text_delta", {"text": f"p{k}\n\n```py\nprint({k})\n```\n\n"}))
        if k % 3 == 0:
            jobs.apply_event(SessionEvent(1, "tool_use", {"id": f"u{k}", "name": "Bash",
                                                          "input": {"command": f"echo {k}"}}))
            jobs.apply_event(SessionEvent(1, "tool_result", {"tool_use_id": f"u{k}", "content": "ok"}))
    v = DetailView()
    v.resize(700, 500)
    v.show()
    turns = store.get_turns(sid)
    v.show_session(store.get_session(sid), turns,
                   store.get_tool_calls_for_turns([t.id for t in turns]), jobs.get(1))
    _pump(app)
    return v, jobs


def _tick(app, v, jobs, k):
    jobs.apply_event(SessionEvent(1, "text_delta", {"text": f" w{k}"}))
    v.update_streaming(jobs.get(1))
    v._render_timer.stop()
    v._render_now()


def _frames(v) -> int:
    return len(v._reply_browser.document().findChildren(QtGui.QTextFrame))


def test_in_place_ticks_do_not_leak_frames(app, store, monkeypatch):
    v, jobs = _setup(app, store)
    b = v._reply_browser
    calls = []
    real = b.setHtml
    monkeypatch.setattr(b, "setHtml", lambda h: (calls.append(1), real(h)))
    _tick(app, v, jobs, 0)
    base_frames = _frames(v)
    base_objects = len(b.document().children())
    for k in range(1, 60):
        _tick(app, v, jobs, k)
    assert calls == [], "these ticks must stay in place (no full render)"
    assert _frames(v) == base_frames
    assert len(b.document().children()) == base_objects
    # Same document a fresh full render builds.
    v._doc_key = None
    v._render_now()
    assert _frames(v) == base_frames


def test_long_stream_stays_bounded_and_keeps_selection(app, store, monkeypatch):
    v, jobs = _setup(app, store)
    b = v._reply_browser
    # Small budget so 500 ticks cross it several times.
    monkeypatch.setattr(DetailView, "_INPLACE_ELEMENT_BUDGET", 3000)
    cur = b.document().find("COMMITTED-REPLY")
    assert not cur.isNull()
    b.setTextCursor(cur)
    calls = []
    real = b.setHtml
    monkeypatch.setattr(b, "setHtml", lambda h: (calls.append(1), real(h)))
    _tick(app, v, jobs, 0)
    base_frames = _frames(v)
    base_formats = len(b.document().allFormats())
    peak_frames = peak_formats = 0
    for k in range(1, 501):
        _tick(app, v, jobs, k)
        if k % 25 == 0:
            peak_frames = max(peak_frames, _frames(v))
            peak_formats = max(peak_formats, len(b.document().allFormats()))
    assert calls, "the in-place budget must force an occasional full render"
    assert len(calls) < 50, "…but only occasionally"
    assert peak_frames == base_frames
    # Formats from replaced tables accumulate only up to the budget.
    assert peak_formats < base_formats + 3000
    assert b.textCursor().selectedText() == "COMMITTED-REPLY"
    assert v._reply_browser.toPlainText().count("COMMITTED-REPLY") == 1
    assert "w500" in v._reply_browser.toPlainText()
