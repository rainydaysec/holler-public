"""Phase 5 — the capture moment: recording HUD, Esc / ⌘Z during the hold,
smoothed strokes, voice-only captures without Screen Recording, toasts."""
from __future__ import annotations

import inspect

import numpy as np
import pytest
from PIL import Image
from PySide6 import QtCore, QtGui

from holler import theme
from holler.jobs import Job, JobStore
from holler.runners import full
from holler.transcriber import Transcription


# ── overlay ──────────────────────────────────────────────────────────────────

def _overlay(app, size=(800, 500), **hud):
    from holler.overlay import OverlayWindow

    ov = OverlayWindow(stroke_color="#FF2E2E", stroke_width=4)
    w, h = size
    ov.show_for(Image.new("RGB", (w * 2, h * 2)), (0, 0, w, h), **hud)
    app.processEvents()
    return ov


def _mouse(ov, kind, x, y, button=QtCore.Qt.LeftButton):
    types = {"press": QtCore.QEvent.MouseButtonPress, "move": QtCore.QEvent.MouseMove,
             "release": QtCore.QEvent.MouseButtonRelease}
    pos = QtCore.QPointF(x, y)
    buttons = QtCore.Qt.NoButton if kind == "release" else button
    ev = QtGui.QMouseEvent(types[kind], pos, ov.mapToGlobal(pos), button if kind != "move" else QtCore.Qt.NoButton,
                           buttons, QtCore.Qt.NoModifier)
    {"press": ov.mousePressEvent, "move": ov.mouseMoveEvent, "release": ov.mouseReleaseEvent}[kind](ev)


def _draw(ov, pts, release=True):
    _mouse(ov, "press", *pts[0])
    for p in pts[1:]:
        _mouse(ov, "move", *p)
    if release:
        _mouse(ov, "release", *pts[-1])


def test_undo_removes_in_progress_stroke_then_last_finished(app):
    ov = _overlay(app)
    try:
        _draw(ov, [(10, 300), (60, 320), (120, 300)])
        _draw(ov, [(200, 300), (260, 340), (300, 300)])
        _draw(ov, [(400, 300), (420, 340)], release=False)  # still dragging
        assert len(ov.get_strokes()) == 3
        assert ov.undo_last_stroke() is True  # the in-progress one first
        assert len(ov.get_strokes()) == 2
        _mouse(ov, "move", 450, 350)  # same drag can't resurrect it
        assert len(ov.get_strokes()) == 2
        assert ov.undo_last_stroke() is True
        assert ov.undo_last_stroke() is True
        assert ov.get_strokes() == []
        assert ov.undo_last_stroke() is False
    finally:
        ov.hide()
        ov.deleteLater()


def test_strokes_are_float_source_coords_smoothed_and_decimated(app):
    from holler.overlay import smooth_stroke

    ov = _overlay(app)  # 2x snapshot over an 800x500 window
    try:
        _draw(ov, [(10.25, 300.5), (10.5, 300.5), (60, 320), (120, 300), (180, 330)])
        (stroke,) = ov.get_strokes()
        # sub-pixel jitter (< 1 px) was dropped; endpoints are exact, in 2x pixels
        assert stroke[0] == (20.5, 601.0)
        assert stroke[-1] == (360.0, 660.0)
        assert all(isinstance(c, float) for p in stroke for c in p)
        assert len(stroke) > 4  # curve was sampled, not the raw 4 points
    finally:
        ov.hide()
        ov.deleteLater()
    # 2-point strokes stay straight lines; smoothing passes through midpoints
    assert smooth_stroke([(0, 0), (10, 0)]) == [(0.0, 0.0), (10.0, 0.0)]
    pts = smooth_stroke([(0, 0), (10, 0), (10, 10)])
    assert (10.0, 5.0) in [(round(x, 6), round(y, 6)) for x, y in pts]


@pytest.mark.parametrize("size", [(1600, 900), (420, 300), (300, 220), (150, 120), (60, 40)])
def test_hud_clamped_inside_window(app, size):
    ov = _overlay(app, size, destination="→ Continuing “A very long session title that must elide”",
                  source="Window", warning="Screen Recording is off", hints=True)
    try:
        r = ov._hud_rect()
        assert r.left() >= 0 and r.top() >= 0
        assert r.right() < size[0] and r.bottom() < size[1]
        assert r.width() <= theme.HUD_WIDTH
        ov.grab()  # paints without raising at every size
    finally:
        ov.hide()
        ov.deleteLater()


def test_hud_dodges_the_pointer(app):
    ov = _overlay(app)
    try:
        top = ov._hud_rect()
        _mouse(ov, "move", top.center().x(), top.center().y())
        assert ov._hud_rect().top() > top.bottom()
    finally:
        ov.hide()
        ov.deleteLater()


def test_hud_font_is_readable_and_hint_platform_aware():
    from holler import overlay

    assert theme.FONT_MD >= 11
    assert overlay.format_elapsed(0) == "0:00"
    assert overlay.format_elapsed(65.9) == "1:05"
    assert overlay.format_elapsed(-3) == "0:00"
    assert "Esc to cancel" in overlay.capture_hint()
    assert overlay.undo_key_label() in overlay.capture_hint()


def test_mic_level_rises_fast_falls_smoothly(app):
    ov = _overlay(app)
    try:
        ov.set_mic_level(0.8)
        assert ov._mic_level == pytest.approx(0.8)
        ov.set_mic_level(0.0)
        assert 0.0 < ov._mic_level < 0.8
        ov.set_mic_level(5)
        assert ov._mic_level == 1.0
    finally:
        ov.hide()
        ov.deleteLater()


def test_show_for_resets_hud_and_strokes(app):
    ov = _overlay(app, destination="→ New session", hints=True)
    try:
        _draw(ov, [(10, 10), (50, 50)])
        ov.show_for(Image.new("RGB", (100, 100)), (0, 0, 100, 100))
        assert ov.get_strokes() == []
        assert ov._destination == "" and ov._hints is False
    finally:
        ov.hide()
        ov.deleteLater()


# ── runner: cancel / undo / destination / voice-only ───────────────────────

class _Rec:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1
        return np.zeros(0, dtype=np.float32)


class _Ov:
    def __init__(self):
        self.hidden = 0

    def hide(self):
        self.hidden += 1


def test_cancel_discards_capture_and_release_dispatches_nothing():
    state = {"snapshot": object()}
    rec, ov = _Rec(), _Ov()
    assert full._cancel_capture(state, ov, rec) is True
    assert "snapshot" not in state and rec.stopped == 1 and ov.hidden == 1
    assert full._release_was_cancelled(state) is True
    # the flag is consumed: the next hold's release is normal
    assert full._release_was_cancelled(state) is False


def test_cancel_without_capture_is_a_noop():
    state = {"snapshot": None}
    rec, ov = _Rec(), _Ov()
    assert full._cancel_capture(state, ov, rec) is False
    assert rec.stopped == 0 and ov.hidden == 0
    assert full._release_was_cancelled(state) is False


def test_cancel_and_undo_wired_to_both_hotkey_listeners():
    assert hasattr(full._InvokeOnUiThread, "requested_cancel")
    assert hasattr(full._InvokeOnUiThread, "requested_undo")
    src = inspect.getsource(full.main)
    assert src.count("HotkeyListener(") == 2
    assert src.count("on_cancel=lambda: invoker.requested_cancel.emit()") == 2
    assert src.count("on_undo=lambda: invoker.requested_undo.emit()") == 2


class _Sess:
    def __init__(self, title):
        self.title = title


class _Store:
    def get_session(self, sid):
        return _Sess("Fix the login bug") if sid == 3 else None


def test_capture_destination():
    """HUD line for the press-time target (`_ptt_target_sid`)."""
    d = full._capture_destination
    assert d({}) == "→ New session"
    assert d({"pending_profile": "reviewer"}) == "→ New session · reviewer"
    st = {"active_session_id": 3, "persistent_store": _Store(), "active_session_title": "stale"}
    assert d(st, 3) == "→ Continuing “Fix the login bug”"
    assert d({"active_session_id": 9, "active_session_title": "Old"}, 9) == "→ Continuing “Old”"
    assert d({"active_session_id": 9, "superintendent_inbox_sid": 9}, 9) == "→ Evidence inbox"
    st = {"active_session_id": 3, "superintendent_inbox_sid": 9}
    assert d(st, 9) == "→ New Superintendent session · Deploy"
    st["superintendent_mode"] = "investigation"
    assert d(st, 9) == "→ New Superintendent session · Investigate"
    st["superintendent_chain_armed"] = True
    assert d(st, 9).startswith("→ Added to chain (1)")
    from holler import session_chain
    st = {"session_chains": {session_chain.NEW_SESSION: session_chain.new_chain()}}
    assert d(st, None).startswith("→ Added to chain (1)")


def test_screen_recording_messages():
    assert "can't see other windows" in full._screen_recording_message(True)
    assert "without a screenshot" in full._screen_recording_message(False)
    for b in (True, False):
        assert "Screen Recording" in full._screen_recording_message(b)


class _Tr:
    def transcribe_detailed(self, audio):
        # The real type, not a stand-in: dispatch reads derived properties
        # (vad_found_speech, looks_fabricated) a hand-rolled fake silently loses.
        return Transcription("look at this", 2.0, 1.5, no_speech_prob=0.01)


class _Session:
    def __init__(self):
        self.calls = []

    def submit(self, image_b64, transcript, **kw):
        self.calls.append((image_b64, transcript, kw))
        return 1

    def will_reuse_client(self, *a, **k):
        return False


class _Cfg:
    debug_save_captures = False
    stroke_color = "#f00"
    stroke_width = 4
    max_screenshot_long_edge = 1280
    mic_device = ""
    claude_model = "m"
    claude_models: list[str] = []  # voice-prefix resolution reads the picker pool


class _Snap:
    def __init__(self, limited):
        self.image = Image.new("RGB", (200, 100))
        self.rect = (0, 0, 100, 50)
        self.permission_limited = limited

    @property
    def logical_size(self):
        return (100, 50)


def _run_dispatch(monkeypatch, limited, strokes):
    composed = []

    def _compose(img, s, color, width, **kw):
        composed.append(kw)
        return img
    monkeypatch.setattr(full, "compose", _compose)
    monkeypatch.setattr(full, "build_append", lambda **k: "")
    sess = _Session()
    state = {"cfg": _Cfg(), "persistent_store": None}
    full._dispatch_job(_Snap(limited), strokes, np.zeros(16000, dtype=np.float32), _Tr(),
                       sess, _Cfg(), state, panel=None)
    return composed, sess.calls


def test_limited_capture_is_sent_voice_only(monkeypatch):
    composed, calls = _run_dispatch(monkeypatch, True, [[(1.0, 1.0), (5.0, 5.0)]])
    assert composed == []  # no wallpaper composited
    (image_b64, transcript, _kw), = calls
    assert image_b64 is None and transcript == "look at this"


def test_compose_gets_logical_size_and_long_edge(monkeypatch):
    composed, calls = _run_dispatch(monkeypatch, False, [[(1.0, 1.0), (5.0, 5.0)]])
    assert composed == [{"logical_size": (100, 50), "max_long_edge": 1280}]
    assert calls[0][0]  # image sent


def test_signal_when_done_fires_even_on_error(app):
    sig = full._CaptureUiSignals()
    got = []
    sig.transcribe_finished.connect(got.append)

    def boom():
        raise ValueError("x")
    with pytest.raises(ValueError):
        full._signal_when_done(boom, sig.transcribe_finished, 7)()
    full._signal_when_done(lambda: None, sig.transcribe_finished, 8)()
    app.processEvents()
    assert got == [7, 8]


# ── toasts ───────────────────────────────────────────────────────────────────

@pytest.fixture
def toasts(app):
    from holler import panel

    made = []

    def make():
        t = panel.Toast()
        made.append(t)
        return t
    panel._STACK.visible.clear()
    panel._STACK.queue.clear()
    yield make
    for t in made:
        for c in [t, *t._spares]:
            c.hide()
            c.deleteLater()
    panel._STACK.queue.clear()
    app.processEvents()


def test_toasts_stack_up_to_three_then_queue(app, toasts):
    from holler import panel

    t = toasts()
    cards = [t.show_message(f"message {i}", (1200, 800)) for i in range(4)]
    app.processEvents()
    assert all(c is not None for c in cards[:3]) and cards[3] is None
    assert len({id(c) for c in cards[:3]}) == 3
    rects = [c.geometry() for c in cards[:3]]
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert not a.intersects(b), "stacked toasts overlap"
    assert cards[2].geometry().bottom() > cards[0].geometry().bottom()  # newest lowest
    # the queue drains the oldest early, and a freed slot shows the queued one
    assert cards[0]._timer.remainingTime() <= theme.TOAST_MS_DRAIN * 1.1  # coarse-timer slack
    cards[0].hide()
    app.processEvents()
    assert any(c._text == "message 3" for c in panel._STACK.visible)
    assert not panel._STACK.queue


def test_toast_dedupes_identical_text(app, toasts):
    t = toasts()
    a = t.show_message("same", (900, 500))
    b = t.show_message("same", (900, 500))
    assert a is b


def test_toasts_from_two_owners_do_not_overlap(app, toasts):
    a = toasts().show_message("from the window", (900, 500))
    b = toasts().show_message("from the panel", (900, 500))
    app.processEvents()
    assert not a.geometry().intersects(b.geometry())


def test_toast_kinds_durations_and_action(app, toasts):
    from holler import panel

    assert panel.toast_kind("✗ job #1: boom") == "error"
    assert panel.toast_kind("Couldn't open a microphone.") == "error"
    assert panel.toast_kind("✓ job #1: ok") == "success"
    assert panel.toast_kind("Archived “x”") == "info"
    hits = []
    c = toasts().show_message("Holler can't see other windows.", (900, 500),
                              action=("Open Settings", lambda: hits.append(1)))
    assert c._kind == "error" and c._timer.interval() == theme.TOAST_MS_ERROR
    assert c._undo_btn.text() == "Open Settings" and not c._undo_btn.isHidden()
    c._undo_btn.click()
    assert hits == [1] and not c.isVisible()
    d = toasts().show_message("Saved", (900, 500))
    assert d._timer.interval() == theme.TOAST_MS_INFO and d._undo_btn.isHidden()
    e = toasts().show_message("Archived", (900, 500), undo_callback=lambda: None)
    assert e._timer.interval() == theme.TOAST_MS_ACTION and e._undo_btn.text() == "Undo"


def test_toast_click_opens_and_dismisses(app, toasts):
    hits = []
    c = toasts().show_message("✓ done", (900, 500), on_click=lambda: hits.append(1))
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
                           QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    c.mousePressEvent(ev)
    assert hits == [1] and not c.isVisible()


def test_sticky_toast_is_not_queued(app, toasts):
    t = toasts()
    for i in range(3):
        t.show_message(f"m{i}", (900, 500))
    assert t.show_message("Transcribing…", (900, 500), sticky=True) is None
    from holler import panel
    assert not panel._STACK.queue


# ── legacy panel ─────────────────────────────────────────────────────────────

def test_panel_errored_job_does_not_say_running(app):
    from holler.panel import JobRow

    row = JobRow(Job(id=1, transcript="t", submitted_at=0.0, status="error", error="boom"))
    assert row._reply.text() == "Failed: boom"
    row.refresh(Job(id=1, transcript="t", submitted_at=0.0, status="error"))
    assert row._reply.text() == "Failed"
    row.refresh(Job(id=1, transcript="t", submitted_at=0.0, status="running"))
    assert row._reply.text() == "Working…"


def test_panel_icon_buttons_are_labelled(app):
    from holler.panel import JobRow, Panel

    p = Panel(JobStore(None), model_label="M")
    row = JobRow(Job(id=1, transcript="t", submitted_at=0.0, status="running"))
    try:
        for b in (p._menu_btn, p._routing_clear_btn, row._cancel_btn):
            assert b.text() == "" and not b.icon().isNull()
            assert b.toolTip() and b.accessibleName()
    finally:
        p.deleteLater()


def test_panel_completion_toast_click_requests_open(app, monkeypatch):
    from holler.panel import Panel

    store = JobStore(None)
    p = Panel(store, model_label="M")
    job = Job(id=4, transcript="t", submitted_at=0.0, status="running")
    store.add(job)
    seen = {}
    monkeypatch.setattr(p._toast, "show_message", lambda text, corner, **k: seen.update(k, text=text))
    opened = []
    p.toast_open_requested.connect(opened.append)
    job.status = "error"
    job.final_text = "nope"
    p._on_job_updated(4)
    assert seen["kind"] == "error"
    seen["on_click"]()
    assert opened == [4]
    p.deleteLater()


def test_panel_stopped_job_is_not_a_failure(app):
    """A user Stop is reported as error "cancelled"; the panel shows the same
    neutral "Stopped" as the transcript, not "Failed: cancelled"."""
    from holler.panel import JobRow

    row = JobRow(Job(id=2, transcript="t", submitted_at=0.0, status="error", error="cancelled"))
    assert row._reply.text() == "Stopped"
    row.refresh(Job(id=2, transcript="t", submitted_at=0.0, status="error", error="cancelled by API"))
    assert row._reply.text() == "Failed: cancelled by API"
