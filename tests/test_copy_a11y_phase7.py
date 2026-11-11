"""Phase 7 (copy & accessibility) regressions: keyboard Undo for the toast,
the speech-model state in the header, plain-language notices, and the
accessible names/text added to rows and buttons."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

# Import the real modules at collection time (test_sidebar_organization stubs
# PySide6 in sys.modules if they aren't loaded yet).
import holler.jobs  # noqa: E402,F401
import holler.panel  # noqa: E402,F401
import holler.sessions_window  # noqa: E402,F401
import holler.store  # noqa: E402,F401


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)

    from holler.jobs import JobStore
    from holler.sessions_window import SessionsWindow
    from holler.store import Store

    store = Store(tmp_path / "holler.db")
    jobs = JobStore(store)
    w = SessionsWindow(store, jobs, tmp_path / "captures", tmp_path)
    w.show()
    w.activateWindow()
    app.processEvents()
    yield w
    _hide_toasts()
    holler.panel.set_toast_click_blocker(None)
    w.close()
    w.deleteLater()
    app.processEvents()
    store.close()


def _hide_toasts():
    for card in list(holler.panel._STACK.visible):
        try:
            card.hide()
        except RuntimeError:
            pass
    holler.panel._STACK.queue.clear()


def _mk(w, n: int) -> list[int]:
    ids = [w._store.create_session(model="m", transcript=f"session {i}", screenshot_path=None,
                                   status="idle") for i in range(n)]
    w._refresh_list()
    return ids


def _undo_key(app, target: QtWidgets.QWidget) -> None:
    """⌘Z on macOS (Qt's ControlModifier), Ctrl+Z elsewhere."""
    # The offscreen platform activates a newly shown toast (a real macOS /
    # Windows desktop honours WA_ShowWithoutActivating): give the window back.
    target.window().activateWindow()
    app.processEvents()
    target.setFocus(QtCore.Qt.OtherFocusReason)
    app.processEvents()
    QtTest.QTest.keyClick(target, QtCore.Qt.Key_Z, QtCore.Qt.ControlModifier)
    app.processEvents()


# ── Toast Undo from the keyboard ─────────────────────────────────────────────

def test_undo_key_restores_the_archived_session(app, win):
    sid = _mk(win, 2)[0]
    win._on_archive_requested(sid)
    assert win._store.get_session(sid).archived
    _undo_key(app, win._list)
    assert not win._store.get_session(sid).archived


def test_undo_key_reopens_a_session_marked_done(app, win):
    sid = _mk(win, 1)[0]
    win._on_review_toggled(sid)
    assert win._store.get_session(sid).reviewed_at is not None
    _undo_key(app, win._list)
    assert win._store.get_session(sid).reviewed_at is None


def test_undo_key_stays_with_a_text_field_that_has_history(app, win):
    sid = _mk(win, 1)[0]
    win._text_input.setPlainText("")
    win._text_input.insertPlainText("draft")  # an undoable edit
    win._on_archive_requested(sid)
    _undo_key(app, win._text_input)
    assert win._store.get_session(sid).archived, "the composer's own undo must win"
    assert win._text_input.toPlainText() == ""


def test_undo_key_reaches_the_toast_from_an_empty_composer(app, win):
    """The composer has focus nearly all the time; with nothing to undo there
    the key goes to the toast."""
    sid = _mk(win, 1)[0]
    win._text_input.clear()
    win._on_archive_requested(sid)
    _undo_key(app, win._text_input)
    assert not win._store.get_session(sid).archived


def test_undo_key_is_ignored_during_a_capture_hold(app, win):
    """⌘Z during a hold removes the last stroke; it must not also undo."""
    sid = _mk(win, 1)[0]
    win._on_archive_requested(sid)
    holler.panel.set_toast_click_blocker(lambda: True)
    _undo_key(app, win._list)
    assert win._store.get_session(sid).archived
    holler.panel.set_toast_click_blocker(None)
    _undo_key(app, win._list)
    assert not win._store.get_session(sid).archived


def test_undo_key_never_runs_an_action_button(app, win):
    ran = []
    win.show_inline_notice("Screen Recording is off", action=("Open Settings", lambda: ran.append(1)))
    _undo_key(app, win._list)
    assert ran == []


def test_undo_key_runs_once_then_nothing(app, win):
    sid = _mk(win, 1)[0]
    win._on_archive_requested(sid)
    _undo_key(app, win._list)
    win._store.archive(sid, True)  # archived again by other means
    _undo_key(app, win._list)  # the toast's Undo is spent
    assert win._store.get_session(sid).archived


def test_undo_button_tooltip_names_the_shortcut(app, win):
    sid = _mk(win, 1)[0]
    win._on_archive_requested(sid)
    card = holler.panel._STACK.visible[-1]
    assert card._undo_btn.isVisibleTo(card)
    assert card._undo_btn.toolTip().startswith("Undo (")


# ── speech-model state in the header ─────────────────────────────────────────

def test_header_status_reflects_speech_model_state(win):
    win.set_speech_state("loading")
    assert "Loading speech model" in win._warmth.text()
    win.set_speech_state("failed")
    assert "Speech unavailable" in win._warmth.text()
    assert "Ready" not in win._warmth.text()
    assert win._warmth.accessibleName().startswith("Status: Speech unavailable")
    win.set_speech_state("ready")
    assert "Ready" in win._warmth.text()


# ── accessible names / text ──────────────────────────────────────────────────

def test_profile_picker_rows_have_accessible_text(app):
    from holler.sessions_window import ProfilePickerDialog

    d = ProfilePickerDialog()
    try:
        assert d.windowTitle() == "New session"
        for i in range(d._list.count()):
            assert d._list.item(i).data(QtCore.Qt.AccessibleTextRole)
        assert d._list.item(0).data(QtCore.Qt.AccessibleTextRole).startswith("No profile")
    finally:
        d.deleteLater()


def test_icon_buttons_and_search_clear_are_named(win):
    for b in (win._chain_discard_btn, win._sup_model_btn, win._model_badge):
        assert b.accessibleName()
    win.set_model_label("Opus 5")
    assert win._model_badge.accessibleName() == "Model: Opus 5"
    win.set_superintendent_model_label("Sonnet 5")
    assert win._sup_model_btn.accessibleName() == "Worker model: Sonnet 5"
    assert "🛠" not in win._sup_model_btn.text()
    clear = win._search.findChildren(QtWidgets.QToolButton)
    assert clear and all(b.accessibleName() == "Clear search" and b.toolTip() for b in clear)


def test_chain_bar_copy_is_plain(win):
    sid = _mk(win, 1)[0]
    win.select_session(sid)
    win.set_session_chain_status(sid, True, 2)
    assert win._chain_label.text() == "Chaining — 2 captures held · every hold is added until you start the session"
    assert win._chain_file_btn.accessibleName() == "Start session from chain (2)"
    win.set_session_chain_status(sid, False, 0)
    assert "--" not in win._chain_label.text() and "dump" not in win._chain_label.text()


# ── plain-language notices ───────────────────────────────────────────────────

def test_completion_toast_names_the_session_not_a_job_id(app):
    from holler.jobs import Job, JobStore
    from holler.panel import Panel

    p = Panel(JobStore(None), model_label="M")
    try:
        p.set_session_title_resolver(lambda sid: "Fix the login bug" if sid == 7 else "")
        done = Job(id=12, transcript="t", submitted_at=0.0, status="done",
                   final_text="All tests pass.\nmore", local_session_id=7)
        assert p._completion_toast_text(done) == ("✓ “Fix the login bug”: All tests pass.", "success")
        failed = Job(id=12, transcript="t", submitted_at=0.0, status="error",
                     error="boom", local_session_id=7)
        text, kind = p._completion_toast_text(failed)
        assert text == "✗ “Fix the login bug” failed: boom" and kind == "error"
        stopped = Job(id=12, transcript="t", submitted_at=0.0, status="error",
                      error="cancelled", local_session_id=7)
        assert p._completion_toast_text(stopped) == ("Stopped “Fix the login bug”", "info")
        assert "#12" not in text
    finally:
        p.deleteLater()


def test_resume_failed_notice_is_reassuring_and_named(tmp_path):
    full = pytest.importorskip("holler.runners.full")
    from holler.store import Store

    store = Store(tmp_path / "h.db")
    try:
        sid = store.create_session(model="m", transcript="x", screenshot_path=None,
                                   status="idle", title="Refactor auth")
        msg = full._resume_failed_message(store, sid)
        assert "“Refactor auth”" in msg and "Nothing was lost" in msg
        assert "#" not in msg and "continuing fresh" not in msg
        assert "this session" in full._resume_failed_message(store, 999)
    finally:
        store.close()


def test_cancel_of_a_finished_turn_says_so_plainly():
    import inspect

    full = pytest.importorskip("holler.runners.full")
    src = inspect.getsource(full)
    assert "already finished?" not in src and "job #" not in src


def test_selected_session_routing_raises_no_toast():
    from holler.session_routing import decide

    d = decide("hello", active_local_session_id=3, candidates=[(3, "T", "sdk")])
    assert d.kind == "resume" and not d.note
