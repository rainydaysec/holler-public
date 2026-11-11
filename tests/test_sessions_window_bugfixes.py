"""Regression tests for Sessions-window bug fixes: routing-pill style, keyboard
navigation focus, Mac delete key, platform shortcut hints, Settings / Quit
shortcuts, safe confirm defaults, quick-switcher row height, New-session flash
restore, and quit-with-running-sessions confirmation.

Reuses the offscreen `win` fixture from test_sessions_window_selection (temp
Store + JobStore, app-data dir redirected to tmp_path).
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from holler import sessions_window as sw  # noqa: E402
from tests.test_sessions_window_selection import (  # noqa: E402,F401
    _add_job,
    _listed_ids,
    _mk,
    qapp,
    win,
)


def _show(w):
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowExposed(w)
    QtWidgets.QApplication.processEvents()


def _current_sid(w):
    it = w._list.currentItem()
    s = it.data(QtCore.Qt.UserRole) if it is not None else None
    return s.id if s is not None else None


# ── routing pill: outline-only in both states (BRAND.md) ────────────────────


def test_routing_pill_is_outline_only_in_both_states(win):
    ids = _mk(win, 1)
    win._start_fresh_routing("")
    new_qss = win._routing_pill.styleSheet()
    assert "background: transparent" in new_qss
    assert sw._GREEN not in new_qss
    win.select_session(ids[0])
    cont_qss = win._routing_pill.styleSheet()
    assert "background: transparent" in cont_qss
    assert sw._GREEN not in cont_qss
    assert new_qss != cont_qss  # states still distinguishable


# ── keyboard navigation keeps focus on the list ─────────────────────────────


def test_arrow_keys_walk_rows_without_focus_jumping_to_composer(win):
    _mk(win, 5)
    win._sort_key = "title"  # no bucket headers between rows
    win._refresh_list()
    _show(win)
    order = _listed_ids(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    win._list.setCurrentRow(0)
    QtWidgets.QApplication.processEvents()
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    assert _current_sid(win) == order[0]
    for _ in range(3):
        QTest.keyClick(win._list, QtCore.Qt.Key_Down)
        QtWidgets.QApplication.processEvents()
    assert _current_sid(win) == order[3]
    assert win._active_session_id == order[3]
    assert win._list.hasFocus()


def test_mouse_click_on_row_still_focuses_composer(win):
    _mk(win, 3)
    win._sort_key = "title"
    win._refresh_list()
    _show(win)
    order = _listed_ids(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    rect = win._list.visualItemRect(win._list.item(1))
    QTest.mouseClick(win._list.viewport(), QtCore.Qt.LeftButton,
                     QtCore.Qt.NoModifier, QtCore.QPoint(rect.left() + 4, rect.center().y()))
    QtWidgets.QApplication.processEvents()
    assert win._active_session_id == order[1]
    assert win._text_input.hasFocus()


def test_programmatic_select_focuses_composer(win):
    ids = _mk(win, 2)
    _show(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    win.select_session(ids[0])
    QtWidgets.QApplication.processEvents()
    assert win._text_input.hasFocus()


# ── Mac delete key (Backspace) archives when the list has focus ─────────────


def test_backspace_on_list_archives_focused_row(win):
    ids = _mk(win, 2)
    _show(win)
    order = _listed_ids(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    win._select_row_by_id(order[0])
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    QtWidgets.QApplication.processEvents()
    QTest.keyClick(win._list, QtCore.Qt.Key_Backspace)
    QtWidgets.QApplication.processEvents()
    assert win._store.get_session(order[0]).archived
    assert set(ids) == set(order)


def test_backspace_in_search_box_does_not_archive(win):
    ids = _mk(win, 1)
    _show(win)
    win.select_session(ids[0])
    win._search.setFocus(QtCore.Qt.OtherFocusReason)
    win._search.setText("ab")
    QtWidgets.QApplication.processEvents()
    QTest.keyClick(win._search, QtCore.Qt.Key_Backspace)
    QtWidgets.QApplication.processEvents()
    assert win._search.text() == "a"
    assert not win._store.get_session(ids[0]).archived


# ── platform-correct shortcut hints ─────────────────────────────────────────


def test_key_hint_is_platform_native():
    hint = sw._key_hint("Ctrl+N")
    if sys.platform == "darwin":
        assert hint == "⌘N"
        assert sw._key_hint("Ctrl+,") == "⌘,"
    else:
        assert hint == "Ctrl+N"


def test_header_menu_and_search_use_native_hints(win, monkeypatch):
    seen: list[str] = []
    real = QtWidgets.QMenu

    class _Rec(real):
        def exec(self, *a, **k):  # noqa: A003
            seen.extend(a_.text() for a_ in self.actions() if not a_.isSeparator())
            return None

    monkeypatch.setattr(QtWidgets, "QMenu", _Rec)
    win._show_header_menu()
    assert f"New session\t{sw._key_hint('Ctrl+N')}" in seen
    assert f"Settings…\t{sw._key_hint('Ctrl+,')}" in seen
    assert f"Quit Holler\t{sw._key_hint('Ctrl+Q')}" in seen
    assert sw._key_hint("Ctrl+F") in win._search.placeholderText()
    if sys.platform == "darwin":
        assert not any("Ctrl+" in t for t in seen)
        assert "Ctrl+" not in win._search.placeholderText()


# ── Settings shortcut / Quit shortcut ───────────────────────────────────────


def _shortcut(w, portable: str):
    for sc in w.findChildren(QtGui.QShortcut):
        if sc.key().toString(QtGui.QKeySequence.PortableText) == portable and sc.parent() is w:
            return sc
    return None


def test_ctrl_comma_opens_settings(win):
    fired = []
    win.settings_requested.connect(lambda: fired.append(True))
    _show(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    QTest.keyClick(win._list, QtCore.Qt.Key_Comma, QtCore.Qt.ControlModifier)
    QtWidgets.QApplication.processEvents()
    assert fired


def test_quit_shortcut_bound_only_off_mac(win):
    sc = _shortcut(win, "Ctrl+Q")
    if sys.platform == "darwin":
        assert sc is None  # Qt's application menu already provides ⌘Q
    else:
        assert sc is not None


# ── destructive confirms default to No ──────────────────────────────────────


def _capture_question(monkeypatch, answer=QtWidgets.QMessageBox.No):
    calls = []

    def _q(*a, **kw):
        calls.append((a, kw))
        return answer

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(_q))
    return calls


def _default_button(call):
    a, kw = call
    return kw.get("defaultButton", a[4] if len(a) > 4 else None)


def test_delete_confirm_defaults_to_no(win, monkeypatch):
    ids = _mk(win, 1)
    calls = _capture_question(monkeypatch)
    win._delete_session(win._store.get_session(ids[0]))
    assert calls and _default_button(calls[0]) == QtWidgets.QMessageBox.No
    assert win._store.get_session(ids[0]) is not None


def test_bulk_archive_uses_undo_toast_not_a_confirm(win, monkeypatch):
    """Archive is recoverable: no modal confirm, an Undo in the notice that
    brings the whole batch back (Delete keeps its confirm, above)."""
    ids = _mk(win, 3)
    calls = _capture_question(monkeypatch)
    undo = []
    monkeypatch.setattr(win, "show_inline_notice",
                        lambda text, **kw: undo.append((text, kw.get("undo_callback"))))
    items = [win._list.item(i) for i in range(win._list.count())
             if win._list.item(i).data(QtCore.Qt.UserRole) is not None
             and not win._list.item(i).data(QtCore.Qt.UserRole + 1)
             and win._list.item(i).data(QtCore.Qt.UserRole).id in ids[:2]]
    win._bulk_archive(items)
    assert not calls
    assert all(win._store.get_session(i).archived for i in ids[:2])
    assert not win._store.get_session(ids[2]).archived
    text, cb = undo[-1]
    assert text == "Archived 2 sessions" and cb is not None
    cb()
    assert not any(win._store.get_session(i).archived for i in ids)


def test_bulk_mark_done_offers_undo_restoring_each_state(win, monkeypatch):
    ids = _mk(win, 3)
    win._store.mark_reviewed(ids[0], True)  # one already Done
    win._set_filter("active")
    win._sort_key = "title"
    win._search.setText("session")  # search lists Done rows too
    win._search_timer.stop()
    win._refresh_list()
    seen = []
    monkeypatch.setattr(win, "show_inline_notice",
                        lambda text, **kw: seen.append((text, kw.get("undo_callback"))))
    win._list.selectAll()
    win._toggle_reviewed_focused_row()
    assert all(win._store.get_session(i).reviewed_at is not None for i in ids)
    text, cb = seen[-1]
    assert text == "Marked 3 sessions Done" and cb is not None
    cb()
    assert win._store.get_session(ids[0]).reviewed_at is not None
    assert win._store.get_session(ids[1]).reviewed_at is None
    assert win._store.get_session(ids[2]).reviewed_at is None


# ── quick switcher rows are tall enough for title + subtitle ────────────────


def test_quick_switcher_rows_not_clipped(win):
    _mk(win, 3)
    dlg = sw.QuickSwitcherDialog(win._store, [], parent=win)
    dlg.show()
    QTest.qWaitForWindowExposed(dlg)
    QtWidgets.QApplication.processEvents()
    r = dlg._results
    assert r.count() == 3
    for i in range(r.count()):
        w = r.itemWidget(r.item(i))
        for lbl in w.findChildren(QtWidgets.QLabel):
            assert lbl.height() >= lbl.sizeHint().height(), (lbl.text(), lbl.geometry())
    dlg.reject()


# ── New-session button flash restores the resting style ─────────────────────


def test_new_button_flash_restores_original_style_on_rapid_repeat(win):
    resting = win._new_btn.styleSheet()
    win._start_fresh_routing("")
    win._start_fresh_routing("")  # within the 450ms flash
    QTest.qWait(700)
    assert win._new_btn.styleSheet() == resting


# ── closing with live sessions asks first ───────────────────────────────────


class _SystemClose(QtGui.QCloseEvent):
    """A close the window system sent (title-bar ×) — spontaneous."""

    def spontaneous(self) -> bool:  # noqa: D401
        return True


@pytest.fixture
def quit_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(QtWidgets.QApplication, "quit", staticmethod(lambda: calls.append(1)))
    return calls


def _patch_box(monkeypatch, choose: str):
    shown = []
    real = QtWidgets.QMessageBox

    class _Box(real):
        def exec(self):  # noqa: A003
            shown.append(self)
            self._chosen = next(
                b for b in self.buttons()
                if (choose == "quit" and b.text() == "Quit")
                or (choose == "default" and b is self.defaultButton())
            )
            return 0

        def clickedButton(self):
            return getattr(self, "_chosen", None)

    monkeypatch.setattr(QtWidgets, "QMessageBox", _Box)
    return shown


def test_close_with_nothing_running_quits_without_prompt(win, monkeypatch, quit_calls):
    _mk(win, 1)
    shown = _patch_box(monkeypatch, "quit")
    ev = _SystemClose()
    win.closeEvent(ev)
    assert not shown
    assert ev.isAccepted()
    assert quit_calls == [1]


def test_close_with_running_session_cancel_by_default(win, monkeypatch, quit_calls):
    ids = _mk(win, 2)
    _add_job(win, 1, ids[0], "running")
    _add_job(win, 2, ids[0], "queued")
    _add_job(win, 3, ids[1], "queued")
    shown = _patch_box(monkeypatch, "default")
    ev = _SystemClose()
    win.closeEvent(ev)
    assert len(shown) == 1
    assert "2 sessions are still working" in shown[0].text()
    assert shown[0].defaultButton().text() != "Quit"
    assert not ev.isAccepted()
    assert quit_calls == []


def test_close_with_running_session_quit_confirmed(win, monkeypatch, quit_calls):
    ids = _mk(win, 1)
    _add_job(win, 1, ids[0], "running")
    shown = _patch_box(monkeypatch, "quit")
    ev = _SystemClose()
    win.closeEvent(ev)
    assert len(shown) == 1
    assert "1 session is still working" in shown[0].text()
    assert ev.isAccepted()
    assert quit_calls == [1]


def test_app_quit_close_never_prompts(win, monkeypatch, quit_calls):
    """Non-spontaneous close = app quit already in progress (e.g. self-relaunch
    after spawning its replacement) — must not be cancellable."""
    ids = _mk(win, 1)
    _add_job(win, 1, ids[0], "running")
    shown = _patch_box(monkeypatch, "default")
    ev = QtGui.QCloseEvent()
    win.closeEvent(ev)
    assert not shown
    assert ev.isAccepted()


def test_header_menu_quit_confirms_when_running(win, monkeypatch, quit_calls):
    ids = _mk(win, 1)
    _add_job(win, 1, ids[0], "running")
    shown = _patch_box(monkeypatch, "default")
    win._request_quit()
    assert len(shown) == 1 and quit_calls == []


def test_quit_confirmed_once_is_not_asked_again(win, monkeypatch):
    """⌘Q confirms via the app-level guard, then Qt closes the window: the
    window's own close path must not prompt a second time."""
    from PySide6 import QtWidgets
    from holler.jobs import Job

    win._job_store.add(Job(id=77, transcript="t", submitted_at=0.0, status="running"))
    shown = []

    def fake_exec(box):
        shown.append(box.text())
        for b in box.buttons():
            if b.text() == "Quit":
                b.click()
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", fake_exec)
    assert win._confirm_quit_if_busy() is True
    assert win._confirm_quit_if_busy() is True
    assert len(shown) == 1


# ── Review fixes: Backspace needs a visible selection; typing goes to composer ─


def test_backspace_without_selection_archives_nothing(win):
    """Clicking blank list space clears the selection but leaves a current
    row; a Backspace then must not archive that invisible target."""
    ids = _mk(win, 2)
    _show(win)
    order = _listed_ids(win)
    win._select_row_by_id(order[0])
    win._list.clearSelection()
    QtWidgets.QApplication.processEvents()
    # Clicking blank list space leaves keyboard focus on the list.
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    QtWidgets.QApplication.processEvents()
    assert win._list.hasFocus()
    assert win._list.currentItem() is not None
    QTest.keyClick(win._list, QtCore.Qt.Key_Backspace)
    QtWidgets.QApplication.processEvents()
    assert not any(win._store.get_session(i).archived for i in ids)


def test_forward_delete_keeps_current_row_fallback(win):
    """Key_Delete keeps its original behavior (falls back to the current row)."""
    _mk(win, 2)
    _show(win)
    order = _listed_ids(win)
    win._select_row_by_id(order[0])
    win._list.clearSelection()
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    QtWidgets.QApplication.processEvents()
    # The forward-Delete binding is unchanged; exercise its handler directly
    # (offscreen QTest doesn't route Key_Delete through the shortcut map).
    win._archive_focused_row()
    QtWidgets.QApplication.processEvents()
    assert win._store.get_session(order[0]).archived


def test_typing_on_list_goes_to_composer_and_space_does_not_mark_done(win):
    _mk(win, 3)
    win._sort_key = "title"
    win._refresh_list()
    _show(win)
    order = _listed_ids(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    win._list.setCurrentRow(0)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    QTest.keyClick(win._list, QtCore.Qt.Key_Down)
    QtWidgets.QApplication.processEvents()
    assert win._list.hasFocus()
    QTest.keyClick(win._list, QtCore.Qt.Key_H, QtCore.Qt.NoModifier)
    QtWidgets.QApplication.processEvents()
    # Real keystrokes follow focus, which the first character moved.
    QTest.keyClicks(QtWidgets.QApplication.focusWidget(), "i there")
    QtWidgets.QApplication.processEvents()
    assert win._text_input.toPlainText() == "hi there"
    assert win._text_input.hasFocus()
    assert not any(win._store.get_session(i).reviewed_at for i in order)


def test_modified_keys_on_list_are_not_forwarded(win):
    _mk(win, 2)
    _show(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    QTest.keyClick(win._list, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
    QtWidgets.QApplication.processEvents()
    assert win._text_input.toPlainText() == ""


def test_non_spontaneous_close_does_not_quit_again(win, monkeypatch):
    """Qt closes windows *during* a quit (non-spontaneous close). Quitting
    again from there makes Cocoa exit() immediately, skipping aboutToQuit and
    session.stop()'s subprocess reaping — so only a user close may quit."""
    from PySide6 import QtGui
    import holler.sessions_window as swm

    calls = []
    monkeypatch.setattr(swm, "quit_app", lambda: calls.append("quit"))
    ev = QtGui.QCloseEvent()  # constructed in code → not spontaneous
    assert not ev.spontaneous()
    win.closeEvent(ev)
    assert calls == []
    assert ev.isAccepted()
