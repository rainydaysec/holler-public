"""Phase 4 review fixes — transcript (detail.py, markdown_render.py,
tool_format.py, jobs.py). Offscreen Qt against a temp Store (app data
redirected to tmp_path via the shared ``store`` fixture)."""
from __future__ import annotations

import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtGui  # noqa: E402

from holler import theme  # noqa: E402
from holler.jobs import Job, JobStore  # noqa: E402
from holler.sdk_session import SessionEvent  # noqa: E402

from tests.test_transcript_phase4 import (  # noqa: E402,F401
    _pump, _run, _show, _text, _view, store,
)


# ── 1. internal image schemes are chrome-only ───────────────────────────────

def test_icon_resource_rejects_unknown_names_and_huge_sizes(app):
    v = _view(app)
    b = v._reply_browser

    def load(src):
        return b.loadResource(int(QtGui.QTextDocument.ImageResource), QtCore.QUrl(src))

    ok = load(f"holler-icon:copy/ffffff/{theme.TRANSCRIPT_ICON}")
    assert isinstance(ok, QtGui.QImage) and not ok.isNull()
    assert not load("holler-icon:mic/8b949e/48").isNull()  # the empty-state glyph
    for bad in ("holler-icon:copy/ffffff/30000", "holler-icon:copy/ffffff/65",
                "holler-icon:copy/ffffff/0", "holler-icon:nope/ffffff/12",
                "holler-icon:copy/zzzzzz/12"):
        img = load(bad)
        assert isinstance(img, QtGui.QImage) and img.isNull(), bad


def test_markdown_cannot_embed_internal_images():
    from holler.detail import _render_assistant_text_html
    from holler.markdown_render import render_body

    md = ("a ![big](holler-icon:copy/ffffff/30000) b ![s](HOLLER-SHOT:1) "
          "c ![o](holler://open/shot/1) d ![e](holler&#45;icon:copy/fff/12) "
          "e ![w](https://example.com/x.png)\n\n|t|\n|-|\n|![q](holler-shot:2)|")
    for html in (render_body(md), _render_assistant_text_html(md)):
        low = html.lower()
        assert "holler-icon:" not in low and "holler-shot:" not in low and "holler://" not in low
        assert "big" in html and "s" in html  # the alt text stays readable
        assert '<img src="https://example.com/x.png"' in html  # ordinary images untouched
    # Holler's own chrome (Copy link icon) still uses the scheme.
    from holler.detail import _RenderCtx
    assert "holler-icon:copy/" in _render_assistant_text_html("```\nx\n```", ctx=_RenderCtx("t1"))


# ── 2. diff body lines that look like headers ───────────────────────────────

def test_edit_diff_keeps_dash_dash_and_plus_plus_lines():
    from holler.tool_format import diff_stats, edit_diff

    rows = edit_diff("Edit", {"file_path": "/r/q.sql",
                              "old_string": "-- old comment\nSELECT 1;",
                              "new_string": "++x\nSELECT 1;"})
    assert ("-", "-- old comment") in rows and ("+", "++x") in rows
    assert diff_stats(rows) == (1, 1)


# ── 3. expand / collapse in the live turn holds the viewport ────────────────

@pytest.mark.parametrize("tailing", [False, True])
def test_live_turn_expand_keeps_clicked_line_in_place(app, store, tailing):
    jobs = JobStore(store)
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    for k in range(1, 4):
        _run(jobs, store, sid, k, [("text_delta", {"text": "old reply " * 200})],
             result="old reply " * 200)
    out = "\n".join(f"OUT-{i}" for i in range(200))
    _run(jobs, store, sid, 9, [
        ("tool_use", {"id": "u1", "name": "Bash", "input": {"command": "seq"}}),
        ("tool_result", {"tool_use_id": "u1", "content": out, "is_error": False}),
        ("text_delta", {"text": "Short final."})], result=None)
    v = _view(app)
    _show(v, store, jobs, sid, live=jobs.get(9))
    _pump(app)
    time.sleep(0.7)  # past the session-entry settle window
    _pump(app)
    b = v._reply_browser
    sb = b.verticalScrollBar()
    sb.setValue(sb.maximum())
    _pump(app)
    v._tailing = tailing

    def y(word):
        return b.cursorRect(b.document().find(word)).top()

    before = y("OUT-2")
    assert 0 <= before < b.viewport().height()
    for _ in range(2):  # expand, then collapse
        v._on_anchor_clicked(QtCore.QUrl("holler://expand/j9-u1"))
        _pump(app, 20)
        assert abs(y("OUT-2") - before) <= 3
    v._on_anchor_clicked(QtCore.QUrl("holler://expand/j9-u1"))
    _pump(app, 20)
    assert "OUT-150" in _text(v)
    assert abs(y("OUT-2") - before) <= 3


# ── 4. a stopped turn is "Stopped", not a failure ───────────────────────────

def test_stopped_turn_is_neutral_live_and_committed(app, store):
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    jobs = JobStore(store)
    _run(jobs, store, sid, 1, [("text_delta", {"text": "Half an ans"})], error="cancelled")
    # Status bookkeeping is unchanged: the turn is still recorded as errored
    # (sessions list status, superintendent and stable builds read that).
    turn = store.get_turns(sid)[0]
    assert turn.is_error and jobs.get(1).status == "error"
    assert json.loads(turn.blocks_json)[-1] == {"t": "error", "text": "cancelled"}

    live = _view(app)
    live.show_job(jobs.get(1))
    _pump(app)
    committed = _view(app)
    _show(committed, store, JobStore(store), sid)
    _pump(app)
    for v in (live, committed):
        t = _text(v)
        assert "Stopped" in t and "Half an ans" in t
        assert "Turn failed" not in t and "cancelled" not in t and "No reply" not in t
        assert theme.ERROR_BANNER_BG.lower() not in v._reply_browser.toHtml().lower()


def test_real_errors_still_show_the_banner(app):
    v = _view(app)
    v.show_job(Job(id=2, transcript="x", submitted_at=0.0, status="error", error="cancelled by API"))
    _pump(app)
    assert "Turn failed" in _text(v) and "cancelled by API" in _text(v)


# ── 5. expanding one card only re-renders its own turn ──────────────────────

def test_expand_rerenders_only_its_turn(app, store, monkeypatch):
    from holler import detail

    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    jobs = JobStore(store)
    out = "\n".join(f"L{i}" for i in range(30))
    for k in range(1, 11):
        _run(jobs, store, sid, k, [
            ("tool_use", {"id": f"u{k}", "name": "Bash", "input": {"command": "seq"}}),
            ("tool_result", {"tool_use_id": f"u{k}", "content": out, "is_error": False}),
            ("text_delta", {"text": f"Reply {k}"})], result=f"Reply {k}")
    v = _view(app)
    _show(v, store, jobs, sid)
    _pump(app)
    calls = []
    real = detail._render_committed_turn_html
    monkeypatch.setattr(detail, "_render_committed_turn_html",
                        lambda *a, **kw: (calls.append(a[0].id), real(*a, **kw))[1])
    turns = store.get_turns(sid)
    tc = store.get_tool_calls(turns[4].id)[0]
    v._on_anchor_clicked(QtCore.QUrl(f"holler://expand/tc{tc.id}"))
    _pump(app)
    assert calls == [turns[4].id]
    assert "L29" in _text(v)
    calls.clear()
    tc2 = store.get_tool_calls(turns[7].id)[0]
    v._on_anchor_clicked(QtCore.QUrl(f"holler://expand/tc{tc2.id}"))
    _pump(app)
    assert calls == [turns[7].id]


# ── 6. a failed merged turn keeps its reply ─────────────────────────────────

def test_failed_merged_turn_shows_reply_not_as_error(app, store):
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    jobs = JobStore(store)
    t1 = store.add_turn(session_id=sid, transcript="turn 1", screenshot_path=None, job_id=1)
    t2 = store.add_turn(session_id=sid, transcript="turn 2 merged", screenshot_path=None, job_id=None)
    jobs.apply_event(SessionEvent(1, "submitted", {
        "transcript": "turn 1", "local_session_id": sid, "local_turn_id": t1}))
    jobs.apply_event(SessionEvent(1, "merged_in", {"local_turn_id": t2, "transcript": "turn 2 merged"}))
    jobs.apply_event(SessionEvent(1, "started", {}))
    jobs.apply_event(SessionEvent(1, "text_delta", {"text": "I will check the logs now."}))
    jobs.apply_event(SessionEvent(1, "error", {"error": "boom"}))
    merged = {t.id: t for t in store.get_turns(sid)}[t2]
    assert merged.is_error and merged.final_text == "I will check the logs now."

    from holler.detail import _render_committed_turn_html
    html = _render_committed_turn_html(merged, [])
    assert "Turn failed" in html and "boom" in html
    assert html.count("I will check the logs now.") == 1
    # The reply renders as a reply (before the banner), not as the error text.
    assert html.index("I will check the logs now.") < html.index("Turn failed")


def test_failed_merged_turn_with_unstreamed_error_result(store):
    """An SDK error result the stream never showed is the failure reason."""
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    jobs = JobStore(store)
    t1 = store.add_turn(session_id=sid, transcript="turn 1", screenshot_path=None, job_id=1)
    t2 = store.add_turn(session_id=sid, transcript="turn 2", screenshot_path=None, job_id=None)
    jobs.apply_event(SessionEvent(1, "submitted", {
        "transcript": "turn 1", "local_session_id": sid, "local_turn_id": t1}))
    jobs.apply_event(SessionEvent(1, "merged_in", {"local_turn_id": t2, "transcript": "turn 2"}))
    jobs.apply_event(SessionEvent(1, "result", {"result": "Prompt is too long", "is_error": True}))
    merged = {t.id: t for t in store.get_turns(sid)}[t2]
    from holler.detail import _render_committed_turn_html
    html = _render_committed_turn_html(merged, [])
    assert "Turn failed" in html and html.count("Prompt is too long") == 1


# ── 7. the screen-unchanged marker never shows in single-job chrome ─────────

def test_show_job_header_and_popup_title_strip_screen_suffix(app):
    from holler.detail import SCREEN_UNCHANGED_SUFFIX, DetailWindow

    job = Job(id=3, transcript="fix the button" + SCREEN_UNCHANGED_SUFFIX,
              submitted_at=0.0, status="done")
    v = _view(app)
    v.show_job(job)
    assert v._transcript.text() == "fix the button"
    w = DetailWindow(job, None, JobStore(None))
    try:
        assert w.windowTitle() == "fix the button — Holler"
    finally:
        w.close()
        w.deleteLater()
