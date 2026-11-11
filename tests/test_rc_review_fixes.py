"""Fixes from the release-candidate review."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from tests.test_live_segment_leak import _pump, _setup, _tick, store  # noqa: E402,F401


def test_key_recording_flag_outlives_recording_by_a_grace_period(monkeypatch):
    """The listener's queued press can arrive after Qt delivered the same key
    to the Record button (which ended recording) — it must still be ignored."""
    from holler import hotkey

    hotkey.set_key_recording(False)
    now = [100.0]
    monkeypatch.setattr(hotkey.time, "monotonic", lambda: now[0])
    hotkey.set_key_recording(True)
    assert hotkey.key_recording_active()
    hotkey.set_key_recording(False, grace_s=1.0)
    assert hotkey.key_recording_active()
    now[0] += 1.01
    assert not hotkey.key_recording_active()
    hotkey.set_key_recording(False)
    assert not hotkey.key_recording_active()


def test_record_button_stop_keeps_push_to_talk_paused_briefly(app, monkeypatch):
    from holler import hotkey, settings_dialog

    calls = []
    monkeypatch.setattr(settings_dialog, "set_key_recording",
                        lambda active, grace_s=0.0: calls.append((active, grace_s)))
    target = QtWidgets.QLineEdit()
    btn = settings_dialog.HotkeyRecordButton(target)
    btn.start()
    btn.stop()
    assert calls[-1] == (False, hotkey.KEY_RECORDING_GRACE_S)


def test_elided_chain_label_speaks_the_full_text(app):
    from holler.sessions_window import _ElideRightLabel

    lbl = _ElideRightLabel("Chaining — 3 screenshots held for the inbox")
    lbl.resize(60, 20)
    lbl.show()
    app.processEvents()
    assert QtWidgets.QLabel.text(lbl).endswith("…")
    assert lbl.accessibleName() == "Chaining — 3 screenshots held for the inbox"


def test_resume_notice_is_informational_not_an_error(tmp_path):
    import holler.runners.full as full
    from holler.panel import toast_kind
    from holler.store import Store

    s = Store(tmp_path / "h.db")
    try:
        sid = s.create_session(model="m", transcript="x", screenshot_path=None, title="Refactor auth")
        assert toast_kind(full._resume_failed_message(s, sid)) == "info"
    finally:
        s.close()


def test_held_undo_key_does_not_walk_back_through_toasts(app):
    from PySide6.QtTest import QTest

    from holler.sessions_window import _GlobalUndoFilter

    class _Toast:
        def __init__(self):
            self.calls = 0

        def trigger_undo(self):
            self.calls += 1
            return True

    win = QtWidgets.QWidget()
    btn = QtWidgets.QPushButton("x", win)  # focus target with no text undo
    win._toast = _Toast()
    win.show()
    win.activateWindow()
    QTest.qWaitForWindowExposed(win)
    btn.setFocus()
    app.processEvents()
    f = _GlobalUndoFilter(win)
    mods = QtCore.Qt.MetaModifier if False else QtCore.Qt.ControlModifier
    press = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Z, mods, "z", False)
    repeat = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Z, mods, "z", True)
    assert f.eventFilter(btn, press) is True
    assert win._toast.calls == 1
    assert f.eventFilter(btn, repeat) is False
    assert win._toast.calls == 1
    win.close()


def test_budget_rerender_keeps_scroll_position_without_waiting(app, store, monkeypatch):
    """Scrolled up mid-stream: the forced full render (in-place budget spent)
    must not show the top of the transcript for a frame."""
    from holler.detail import DetailView

    v, jobs = _setup(app, store)
    v.resize(700, 260)
    _pump(app)
    sb = v._reply_browser.verticalScrollBar()
    assert sb.maximum() > 200
    sb.triggerAction(QtWidgets.QAbstractSlider.SliderToMaximum)
    sb.triggerAction(QtWidgets.QAbstractSlider.SliderPageStepSub)
    _pump(app)
    before = sb.value()
    assert before > 0
    monkeypatch.setattr(DetailView, "_INPLACE_ELEMENT_BUDGET", 0)
    jobs.apply_event(__import__("holler.sdk_session", fromlist=["SessionEvent"]).SessionEvent(
        1, "text_delta", {"text": " more"}))
    v.update_streaming(jobs.get(1))
    v._render_timer.stop()
    v._render_now()
    # No event-loop pass yet.
    assert abs(sb.value() - before) <= 2, (sb.value(), before)


def test_closed_sessions_window_stops_filtering_app_events(app, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state
    from holler.jobs import JobStore
    from holler.sessions_window import SessionsWindow
    from holler.store import Store

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)
    st = Store(tmp_path / "h.db")
    w = SessionsWindow(st, JobStore(st), tmp_path / "c", tmp_path)
    seen = []
    orig = w._undo_filter.eventFilter
    monkeypatch.setattr(w._undo_filter, "eventFilter", lambda o, e: (seen.append(1), orig(o, e))[1])
    w.show()
    w.close()  # non-spontaneous close: no quit, filters removed
    seen.clear()
    QtWidgets.QApplication.sendEvent(QtWidgets.QWidget(), QtCore.QEvent(QtCore.QEvent.User))
    assert seen == []
    st.close()


def test_view_deleted_right_after_render_leaves_no_pending_callbacks(app, store):
    """Deferred scroll/anchor restores used to capture the view's scrollbar in
    a context-free 0 ms timer; deleting the view (e.g. closing a pop-out) right
    after a render ran them on a dead object, leaving an error pending that
    poisoned every later event filter. They're now bound to the view."""
    import shiboken6

    v, jobs = _setup(app, store)
    sb = v._reply_browser.verticalScrollBar()
    sb.triggerAction(QtWidgets.QAbstractSlider.SliderToMinimum)  # not tailing → deferred restore
    v._doc_key = None
    v._render_now()
    shiboken6.delete(v)
    _pump(app)  # the autouse excepthook guard fails the test on any error
