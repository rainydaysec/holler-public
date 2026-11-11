"""Phase 3 (Sessions window overhaul) regression tests: calm rail rows, brand
colour discipline, header / routing, inline rename, New-session split button,
⌘K palette, empty dashboard, window state, accessibility, the Phase 5 notice
and capture-status hooks, and the "(screen unchanged…)" display fix.

Reuses the offscreen `win` fixture from test_sessions_window_selection (temp
Store + JobStore, app-data dir redirected to tmp_path).
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from holler import sessions_window as sw  # noqa: E402
from holler import theme  # noqa: E402
from tests.test_sessions_window_selection import (  # noqa: E402,F401
    _add_job,
    _listed_ids,
    _mk,
    qapp,
    win,
)


def _show(w, size=(1220, 760)):
    w.resize(*size)
    w.show()
    w.activateWindow()
    QTest.qWaitForWindowExposed(w)
    QtWidgets.QApplication.processEvents()


def _pump(n=4):
    for _ in range(n):
        QtWidgets.QApplication.processEvents()


def _row(**kw):
    from holler.domain import Session

    base = dict(id=1, created_at=1.0, last_activity_at=1.0, model="claude-opus-5",
                transcript="t", title="A short title", status="idle",
                sdk_session_id=None, screenshot_path=None, pinned=False, archived=False, notes="")
    item_kw = {k: kw.pop(k) for k in ("is_running", "live_verb", "live_elapsed_s") if k in kw}
    base.update(kw)
    return sw.SessionListItem(Session(**base), {"fresh_in": 1200, "output": 300}, now=2.0, **item_kw)


# ── 1. calm rail ─────────────────────────────────────────────────────────────


def test_row_state_is_glyph_and_text_not_a_card_fill(qapp):
    unread = _row()
    done = _row(reviewed_at=1.5)
    error = _row(status="error")
    running = _row(is_running=True, live_verb="Bash: npm test", live_elapsed_s=38)
    rows = {"unread": unread, "done": done, "error": error, "running": running}
    for st, r in rows.items():
        assert r.state() == st
        assert r._mark.state() == st
        # The row never fills itself; the list paints hover / selection.
        assert r.styleSheet() == sw.SessionListItem._ROW_QSS
        assert r._mark.accessibleName()
    # Unread / running / error titles are semibold; Done is regular + muted.
    assert f"font-weight: {theme.WEIGHT_SEMIBOLD}" in unread._title_lbl.styleSheet()
    assert f"font-weight: {theme.WEIGHT_REGULAR}" in done._title_lbl.styleSheet()
    assert theme.FG_MUTED in done._title_lbl.styleSheet()
    # State in words too: the live verb while running, "Failed" after an error.
    assert running._meta.fullText().startswith("Bash: npm test · 38s")
    assert error._meta.fullText().startswith("Failed · ")
    # One meta line, one separator.
    for r in rows.values():
        assert "  " not in r._meta.fullText() and "•" not in r._meta.fullText()


def test_running_pulse_repaints_the_dot_without_touching_stylesheets(qapp):
    r = _row(is_running=True, live_verb="Reading")
    before = (r.styleSheet(), r._title_lbl.styleSheet(), r._meta.styleSheet(),
              r._mark.styleSheet())
    repaints = []
    orig = r._mark.update
    r._mark.update = lambda *a: repaints.append(1) or orig(*a)
    row_repaints = []
    orig_row = r.update
    r.update = lambda *a: row_repaints.append(1) or orig_row(*a)
    for phase in range(8):
        r.pulse_dot(phase)
    assert repaints  # the halo animates…
    assert len(row_repaints) == 8  # …and so does the row-wide running wash…
    assert (r.styleSheet(), r._title_lbl.styleSheet(), r._meta.styleSheet(),
            r._mark.styleSheet()) == before  # …without QSS churn
    # Stopping freezes it: a non-running row ignores pulses.
    r.set_is_running(False)
    repaints.clear()
    row_repaints.clear()
    r.pulse_dot(1)
    assert not repaints and not row_repaints


def test_review_button_tooltip_and_name_follow_done_state(qapp):
    """Routed bug: the row ✓ tooltip stayed "Mark … done" on a Done row."""
    open_row = _row()
    done_row = _row(reviewed_at=1.5)
    assert open_row._review_btn.toolTip() == "Mark Done (Space)"
    assert open_row._review_btn.accessibleName() == "Mark Done"
    assert done_row._review_btn.toolTip() == "Reopen (Space)"
    assert done_row._review_btn.accessibleName() == "Reopen"
    for r in (open_row, done_row):
        for b in (r._review_btn, r._cancel_btn, r._close_btn):
            assert b.text() == "" and not b.icon().isNull()
            assert b.toolTip() and b.accessibleName()


def test_rows_are_sized_to_their_title(win):
    short = win._store.create_session(model="m", transcript="t", screenshot_path=None,
                                      status="idle", title="Short")
    long_ = win._store.create_session(
        model="m", transcript="t", screenshot_path=None, status="idle",
        title="Investigate why the canvas summary panel stops updating after the websocket "
              "reconnects on flaky networks and the retry backoff kicks in")
    win._refresh_list()
    _show(win, (1100, 760))
    win._splitter.setSizes([300, 800])
    _pump(8)
    win._fit_row_heights()
    rs, rl = win._row_widgets[short], win._row_widgets[long_]
    assert rs._title_lbl.lines() == 1
    assert rl._title_lbl.lines() == 2
    hs = win._list.visualItemRect(win._item_for_session(short)).height()
    hl = win._list.visualItemRect(win._item_for_session(long_)).height()
    assert hl - hs == rl._title_lbl.fontMetrics().lineSpacing()
    # Hovering (actions appear) never changes a row's height.
    before = rl.fit_to_width(win._list.viewport().width())
    rl._hovered = True
    rl._sync_close_btn()
    assert rl.fit_to_width(win._list.viewport().width()) == before


# ── 2. brand ────────────────────────────────────────────────────────────────


def _hue_band_ok(color: str) -> bool:
    h = QtGui.QColor(color).hslHue()
    return 55 <= h < 345


def test_profile_hues_avoid_red_and_amber_bands(qapp):
    names = [f"profile-{i}" for i in range(400)] + ["engineering", "investigation", "ui-ux", "planning"]
    assert all(_hue_band_ok(sw._color_for_profile(n)) for n in names)


def test_no_amber_on_restart_ir_toggle_or_custom_tag(win):
    for b in (win._restart_btn, win._text_smaller_btn, win._text_bigger_btn, win._menu_btn):
        assert theme.BRAND_AMBER not in b.styleSheet()
        assert theme.BRAND_AMBER_SOFT not in b.styleSheet()
    win._sup_mode = "investigation"
    win._style_mode_toggle()
    assert theme.BRAND_AMBER_DEEP not in win._mode_ir_btn.styleSheet()
    assert win._mode_ir_btn.isChecked()
    d = sw.ProfilePickerDialog()
    tag_row = d._make_row("Mine", "desc", source="user")
    tag = next(lbl for lbl in tag_row.findChildren(QtWidgets.QLabel) if lbl.text() == "Custom")
    assert theme.PIN not in tag.styleSheet() and theme.BRAND_AMBER not in tag.styleSheet()
    d.deleteLater()


# ── 3. header / routing ─────────────────────────────────────────────────────


def test_routing_shown_once_status_strip_only_for_multiselect(win):
    ids = _mk(win, 3)
    _show(win)
    assert win._status_strip.isHidden()
    win.select_session(ids[0])
    _pump()
    assert win._status_strip.isHidden()
    assert win._routing_pill.text().startswith("→ ")
    ph = win._text_input.placeholderText()
    assert win._hotkey_label in ph and "continue this session" in ph
    win._list.selectAll()
    _pump()
    assert not win._status_strip.isHidden()
    assert "3 selected" in win._ptt_status.text()
    win.select_session(ids[1])
    _pump()
    assert win._status_strip.isHidden()


def test_usage_and_quota_collapse_into_one_chip_when_narrow(win):
    ids = _mk(win, 1)
    j = _add_job(win, 5, ids[0], "running")
    j.input_tokens, j.output_tokens = 6_000, 1_000
    now = time.time()
    win._job_store._rate_limits = {
        "five_hour": {"status": "allowed_warning", "utilization": 0.64,
                      "resets_at": now + 3600, "seen_at": now},
    }
    _show(win, (1220, 760))
    win.select_session(ids[0])
    win._refresh_usage()
    win._refresh_quota()
    assert not win._usage.isHidden() and not win._quota.isHidden()
    assert win._usage_compact.isHidden()
    win.resize(theme.HEADER_COMPACT_W - 200, 760)
    _pump()
    assert win._usage.isHidden() and win._quota.isHidden()
    assert not win._usage_compact.isHidden()
    text = win._usage_compact.text()
    assert "7.0k tokens" in text and "5h 64%" in text and text.count("⚠") == 1
    tip = win._usage_compact.toolTip()
    assert "Token usage" in tip and "Usage limits" in tip
    win.resize(1220, 760)
    _pump()
    assert win._usage_compact.isHidden() and not win._quota.isHidden()


def test_header_icon_buttons_have_icons_names_and_tooltips(win):
    for b, name in ((win._text_smaller_btn, "Smaller text"), (win._text_bigger_btn, "Larger text"),
                    (win._restart_btn, "Restart Holler"), (win._menu_btn, "Menu")):
        assert b.text() == ""
        assert not b.icon().isNull()
        assert b.accessibleName() == name
        assert b.toolTip()
    win.set_model_label("Opus 5")
    assert win._model_badge.toolTip()


# ── 4. inline rename / split button / bulk ──────────────────────────────────


def _no_input_dialog(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("QInputDialog must not be used for rename")

    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", staticmethod(_boom))


def test_inline_rename_enter_commits(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 2)
    _show(win)
    win.select_session(ids[0])
    _pump()
    win._rename_item(win._item_for_session(ids[0]))
    ed = win._rename_editor
    assert isinstance(ed, QtWidgets.QLineEdit) and ed.isVisible()
    assert ed.parent() is win._list.viewport()
    ed.setText("Renamed inline")
    QTest.keyClick(ed, QtCore.Qt.Key_Return)
    _pump()
    assert win._rename_editor is None
    assert win._store.get_session(ids[0]).title == "Renamed inline"
    assert win._row_widgets[ids[0]]._title_lbl.fullText() == "Renamed inline"


def test_inline_rename_escape_cancels_and_space_types(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 1)
    _show(win)
    before = win._store.get_session(ids[0]).title
    win._select_row_by_id(ids[0])
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    win._rename_focused_row()  # F2's handler
    ed = win._rename_editor
    ed.clear()
    QTest.keyClicks(ed, "a b")  # Space types; it must not mark the row Done
    assert ed.text() == "a b"
    assert win._store.get_session(ids[0]).reviewed_at is None
    QTest.keyClick(ed, QtCore.Qt.Key_Escape)
    _pump()
    assert win._rename_editor is None
    assert win._store.get_session(ids[0]).title == before


def test_inline_rename_click_away_commits_and_empty_is_ignored(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 1)
    _show(win)
    win._rename_item(win._item_for_session(ids[0]))
    ed = win._rename_editor
    ed.setText("   ")
    win._text_input.setFocus(QtCore.Qt.MouseFocusReason)
    _pump()
    assert win._rename_editor is None
    assert win._store.get_session(ids[0]).title.strip()  # unchanged, not blank
    win._rename_item(win._item_for_session(ids[0]))
    win._rename_editor.setText("Kept on blur")
    win._text_input.setFocus(QtCore.Qt.MouseFocusReason)
    _pump()
    assert win._store.get_session(ids[0]).title == "Kept on blur"


def test_background_refresh_does_not_kill_an_inline_rename(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 2)
    _show(win)
    win._rename_item(win._item_for_session(ids[0]))
    ed = win._rename_editor
    ed.setText("Half typ")
    win._on_session_changed(ids[1])  # sibling activity
    _pump()
    assert win._rename_editor is ed and ed.text() == "Half typ"
    assert win._store.get_session(ids[0]).title != "Half typ"
    QTest.keyClick(ed, QtCore.Qt.Key_Return)
    _pump()
    assert win._store.get_session(ids[0]).title == "Half typ"


def test_double_click_and_menu_still_rename(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 1)
    _show(win)
    win._list.itemDoubleClicked.emit(win._item_for_session(ids[0]))
    assert win._rename_editor is not None
    win._finish_rename(commit=False)

    class _AutoMenu(QtWidgets.QMenu):
        def exec(self, *_a, **_kw):
            return next(a for a in self.actions() if a.text().startswith("Rename"))

    monkeypatch.setattr(QtWidgets, "QMenu", _AutoMenu)
    pos = win._list.visualItemRect(win._item_for_session(ids[0])).center()
    win._show_context_menu(pos)
    assert win._rename_editor is not None


def test_new_session_split_button(win, monkeypatch):
    opened = []

    class _Picker:
        def __init__(self, *_a, **_k):
            opened.append(1)
            self.selected_name = "engineering"

        def exec(self):
            return QtWidgets.QDialog.Accepted

    monkeypatch.setattr(sw, "ProfilePickerDialog", _Picker)
    profiles = []
    win.profile_selected.connect(profiles.append)
    win._new_btn.click()
    assert profiles == [""] and not opened  # main half: no dialog, no profile
    QTest.keyClick(win, QtCore.Qt.Key_N, QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier)
    win._on_new_session_clicked()  # ⌘⇧N handler
    assert opened and profiles[-1] == "engineering"

    # The arrow's menu lists profiles and keeps the picker reachable.
    seen = []

    class _Menu(QtWidgets.QMenu):
        def exec(self, *_a, **_k):
            seen.extend(a.text() for a in self.actions() if not a.isSeparator())
            return next(a for a in self.actions() if a.text().startswith("Choose profile"))

    monkeypatch.setattr(QtWidgets, "QMenu", _Menu)
    n = len(opened)
    win._new_menu_btn.click()
    assert "No profile" in seen and any(t.startswith("Choose profile…") for t in seen)
    assert len(opened) == n + 1
    assert win._new_menu_btn.accessibleName() and win._new_menu_btn.toolTip()


def test_ctrl_n_is_the_default_new_session(win):
    sc = [s for s in win.findChildren(QtGui.QShortcut)
          if s.key().toString(QtGui.QKeySequence.PortableText) == "Ctrl+N" and s.parent() is win]
    assert len(sc) == 1
    profiles = []
    win.profile_selected.connect(profiles.append)
    sc[0].activated.emit()
    assert profiles == [""]


# ── 5. ⌘K palette ────────────────────────────────────────────────────────────


def _palette(win, query=""):
    d = sw.QuickSwitcherDialog(win._store, win._recents, parent=win, actions=win._palette_actions())
    d.show()
    QTest.qWaitForWindowExposed(d)
    _pump()
    if query:
        d._input.setText(query)
        _pump()
    return d


def _palette_rows(d):
    out = []
    for i in range(d._results.count()):
        it = d._results.item(i)
        if it.data(d._HEADER):
            out.append(("header", it.data(QtCore.Qt.AccessibleTextRole)))
        elif it.data(d._ACTION) is not None:
            out.append(("action", it.data(d._ACTION).label))
        else:
            out.append(("session", it.data(QtCore.Qt.UserRole)))
    return out


def test_palette_is_frameless_and_keyboard_driven(win):
    ids = _mk(win, 3)
    _show(win)
    win._recents = [ids[0]]
    d = _palette(win)
    assert d.windowFlags() & QtCore.Qt.FramelessWindowHint
    rows = _palette_rows(d)
    # Recents first, then the rest, then the actions.
    sel = [r for r in rows if r[0] != "header"]
    assert sel[0] == ("session", ids[0])
    assert rows.index(("header", "Actions")) > rows.index(("session", ids[0]))
    assert d._results.currentItem().data(QtCore.Qt.UserRole) == ids[0]
    QTest.keyClick(d._input, QtCore.Qt.Key_Down)
    assert d._input.hasFocus()
    cur = d._results.currentItem()
    assert cur.data(QtCore.Qt.UserRole) == sel[1][1]
    QTest.keyClick(d._input, QtCore.Qt.Key_Up)
    QTest.keyClick(d._input, QtCore.Qt.Key_Up)  # clamps at the top
    assert d._results.currentItem().data(QtCore.Qt.UserRole) == ids[0]
    QTest.keyClick(d._input, QtCore.Qt.Key_Return)
    assert d.result() == QtWidgets.QDialog.Accepted and d.selected_id == ids[0]


def test_palette_highlights_matches_and_shows_no_matches(win):
    win._store.create_session(model="m", transcript="t", screenshot_path=None, status="idle",
                              title="Refactor auth middleware", profile="engineering")
    win._refresh_list()
    d = _palette(win, "auth")
    widgets = [d._results.itemWidget(d._results.item(i)) for i in range(d._results.count())]
    lbl = next(lb for w in widgets for lb in w.findChildren(sw._HighlightLabel)
               if lb.fullText() == "Refactor auth middleware")
    assert lbl.textFormat() == QtCore.Qt.RichText
    assert f"color:{theme.ACCENT};'>auth</span>" in lbl.text()
    subs = [lb.text() for w in widgets for lb in w.findChildren(QtWidgets.QLabel)
            if not isinstance(lb, sw._HighlightLabel)]
    spec = sw.get_profile("engineering")
    if spec is not None:
        assert any(spec.display in t for t in subs)  # display name, not the key
    d._input.setText("zzqqxx-nothing")
    _pump()
    assert d._results.isHidden() and not d._empty.isHidden()
    assert "No matches" in d._empty.text()
    d.reject()


def test_palette_actions_run_in_the_window(win, monkeypatch):
    ids = _mk(win, 2)
    _show(win)
    win.select_session(ids[0])
    _pump()
    labels = [a.label for a in win._palette_actions()]
    for want in ("New session", "Settings…", "Show Done sessions", "Pin this session",
                 "Archive this session", "Mark this session Done"):
        assert want in labels, want
    fired = []
    win.settings_requested.connect(lambda: fired.append("settings"))

    def run(query):
        d = _palette(win, query)
        QTest.keyClick(d._input, QtCore.Qt.Key_Return)
        assert d.selected_action is not None
        d.selected_action.callback()

    run("settings")
    assert fired == ["settings"]
    run("show done")
    assert win._current_filter == "done"
    win.select_session(ids[0])
    run("pin this")
    assert win._store.get_session(ids[0]).pinned


# ── 6. empty dashboard ──────────────────────────────────────────────────────


def test_dashboard_recents_are_links_and_name_the_real_key(win, tmp_path):
    ids = _mk(win, 2)
    win._recents = [ids[1]]
    win._clear_active_session()
    html = win._dashboard_html
    assert f"{sw._DASH_SESSION_SCHEME}:{ids[1]}" in html
    assert win._hotkey_label in html
    assert "🎙" not in html and "data:image/png;base64," in html
    win._on_dashboard_anchor(QtCore.QUrl(f"{sw._DASH_SESSION_SCHEME}:{ids[1]}"))
    assert win._active_session_id == ids[1]


def test_dashboard_first_run_state(win):
    win._clear_active_session()
    assert "Welcome to Holler" in win._dashboard_html
    fired = []
    win.settings_requested.connect(lambda: fired.append(1))
    win._on_dashboard_anchor(QtCore.QUrl(f"{sw._DASH_ACTION_SCHEME}:settings"))
    assert fired
    _mk(win, 1)
    win._render_empty_dashboard()
    assert "Welcome to Holler" not in win._dashboard_html


def test_dashboard_refreshes_while_visible_and_skips_identical_html(win, monkeypatch):
    ids = _mk(win, 1)
    _show(win)
    win._clear_active_session()
    calls = []
    real = win._detail.show_dashboard
    monkeypatch.setattr(win._detail, "show_dashboard", lambda html: calls.append(html) or real(html))
    win._refresh_dashboard_if_visible()
    assert calls == []  # unchanged → not re-set (no scroll reset / flicker)
    win._store.set_title(ids[0], "A fresh title")
    win._recents = [ids[0]]
    win._refresh_dashboard_if_visible()
    assert len(calls) == 1 and "A fresh title" in calls[0]
    # Not while a session is showing.
    win.select_session(ids[0])
    calls.clear()
    win._refresh_dashboard_if_visible()
    assert calls == []
    # Back to the dashboard: re-set even though its HTML is unchanged.
    win._clear_active_session()
    assert calls


# ── 7. window state ─────────────────────────────────────────────────────────


@pytest.fixture
def saved_state(monkeypatch):
    from holler import state as _state

    saved: dict = {}
    monkeypatch.setattr(_state, "merged_save_debounced", lambda upd, **k: saved.update(upd))
    monkeypatch.setattr(_state, "load", lambda *a, **k: dict(saved))
    return saved


def test_maximised_state_and_normal_rect_persist_and_restore(win, saved_state):
    _show(win, (740, 620))  # fits the offscreen screen (800×800)
    normal = win.geometry()
    win.showMaximized()
    _pump(6)
    if not win.isMaximized():
        pytest.skip("platform can't maximise offscreen")
    assert saved_state["sessions_window_state"] == "maximized"
    assert saved_state["sessions_window_normal"]["w"] == normal.width()
    win.showNormal()
    _pump(6)
    assert saved_state["sessions_window_state"] == "normal"

    saved_state["sessions_window_state"] = "maximized"
    from holler.jobs import JobStore

    w2 = sw.SessionsWindow(win._store, JobStore(win._store), win._captures_dir, win._repo_cwd)
    try:
        w2.show_and_focus()
        _pump(6)
        assert w2.isMaximized()
        w2.showNormal()
        _pump(6)
        assert abs(w2.width() - normal.width()) <= 2  # un-maximise lands on the saved rect
    finally:
        w2.hide()
        w2.deleteLater()


def test_reset_layout(win, saved_state):
    _show(win, (900, 650))
    win._splitter.setSizes([500, 400])
    win._on_splitter_moved()
    assert win._splitter_user_set
    win.reset_layout()
    _pump(6)
    assert not win._splitter_user_set
    assert saved_state["sessions_splitter_user_set"] is False
    sp = win._splitter
    assert abs(sp.sizes()[0] - max(160, int(sp.width() * 0.30))) <= 2
    seen = []

    class _Rec(QtWidgets.QMenu):
        def exec(self, *a, **k):  # noqa: A003
            seen.extend(a_.text() for a_ in self.actions())
            return None

    import unittest.mock as um
    with um.patch.object(QtWidgets, "QMenu", _Rec):
        win._show_header_menu()
    assert "Reset layout" in seen


def test_popout_geometry_persists(win, saved_state):
    ids = _mk(win, 1)
    _show(win)
    sess = win._store.get_session(ids[0])
    win._popout_session(sess)
    pop = win._detached_windows[-1]
    pop.setGeometry(150, 140, 700, 520)
    pop.close()
    _pump()
    assert saved_state["sessions_popout_geometry"]["w"] == 700
    win._popout_session(sess)
    pop2 = win._detached_windows[-1]
    assert (pop2.width(), pop2.height()) == (700, 520)
    pop2.close()


def test_sticky_bucket_is_an_overlay_not_a_layout_row(win):
    ids = _mk(win, 30)
    from tests.visual.scenarios import _set_times

    now = time.time()
    for i, sid in enumerate(ids):
        _set_times(win._store, sid, created=now - i * 3 * 3600, last=now - i * 3 * 3600)
    win._refresh_list()
    _show(win, (1220, 700))
    geo = win._list.geometry()
    sb = win._list.verticalScrollBar()
    sb.setValue(sb.maximum() // 2)
    _pump()
    assert not win._sticky_bucket.isHidden()
    assert win._sticky_bucket.parent() is win._list
    assert win._list.geometry() == geo  # showing it didn't move the list
    assert win._sticky_bucket.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents)


# ── 8. accessibility / focus ────────────────────────────────────────────────


def test_key_controls_have_accessible_names(win):
    for w in (win._search, win._list, win._text_input, win._send_btn, win._new_btn,
              win._new_menu_btn, win._sort_btn, win._model_badge, *win._chips.values()):
        assert w.accessibleName(), w
    ids = _mk(win, 1)
    it = win._item_for_session(ids[0])
    assert it.data(QtCore.Qt.AccessibleTextRole)


def test_clicked_buttons_do_not_take_focus(win):
    """Coordinator item: a click-focused chip kept its accent ring, so after
    ⌘1 two chips looked selected (Windows gives buttons StrongFocus)."""
    _mk(win, 2)
    _show(win)
    win._list.setFocus(QtCore.Qt.OtherFocusReason)
    _pump()
    QTest.mouseClick(win._chips["done"], QtCore.Qt.LeftButton)
    _pump()
    assert win._current_filter == "done"
    assert not win._chips["done"].hasFocus()
    for b in (*win._chips.values(), win._sort_btn, win._new_btn, win._new_menu_btn,
              win._restart_btn, win._menu_btn, win._model_badge, win._send_btn,
              win._text_smaller_btn, win._text_bigger_btn):
        assert b.focusPolicy() == QtCore.Qt.TabFocus, b
    # Tab still reaches them in order from the search box.
    assert win._search.nextInFocusChain() is not None


# ── Phase 5 hooks: rich notices + capture status ────────────────────────────


def test_show_inline_notice_passes_action_click_and_kind(qapp, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state
    from holler.jobs import JobStore
    from holler.store import Store

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)
    store = Store(tmp_path / "h.db")
    w = sw.SessionsWindow(store, JobStore(store), tmp_path / "c", tmp_path)
    calls = []
    monkeypatch.setattr(w._toast, "show_message", lambda *a, **k: calls.append((a, k)))
    try:
        w.show_inline_notice("plain")
        assert calls[-1][1] == {"undo_callback": None}  # unchanged for old callers
        cb = lambda: None  # noqa: E731
        w.show_inline_notice("x", undo_callback=cb)
        assert calls[-1][1] == {"undo_callback": cb}
        act = ("Open Settings", cb)
        w.show_inline_notice("y", action=act, on_click=cb, kind="error")
        assert calls[-1][1] == {"undo_callback": None, "action": act, "on_click": cb, "kind": "error"}
        # Still invokable as a one-string Qt slot (runner uses invokeMethod).
        QtCore.QMetaObject.invokeMethod(w, "show_inline_notice", QtCore.Qt.DirectConnection,
                                        QtCore.Q_ARG(str, "queued"))
        assert calls[-1][0][0] == "queued"
    finally:
        w.deleteLater()
        store.close()


def test_capture_status_line(win):
    ids = _mk(win, 2)
    _show(win)
    win.select_session(ids[0])
    win.set_capture_status("Transcribing…")
    assert not win._capture_status.isHidden() and win._capture_status.text() == "Transcribing…"
    assert win._capture_status_timer.isActive()
    assert win._capture_status_timer.interval() == 30_000
    _add_job(win, 41, ids[1], "queued")  # another session's job: stays
    assert not win._capture_status.isHidden()
    _add_job(win, 42, ids[0], "queued")  # the active session's new job: clears
    assert win._capture_status.isHidden()
    win.set_capture_status("Transcribing…")
    win.clear_capture_status()
    assert win._capture_status.isHidden() and not win._capture_status_timer.isActive()
    win.set_capture_status("Transcribing…")
    win._capture_status_timer.timeout.emit()  # 30 s safety net
    assert win._capture_status.isHidden()


# ── routed bug: model-only marker in the user's own message ─────────────────


def test_screen_unchanged_marker_is_not_shown_in_the_users_message(win):
    # The transcript view gets the raw turn: it strips the marker itself and
    # says "Same screen" instead (stripping it first lost that note).
    ids = _mk(win, 1)
    win._store.add_turn(session_id=ids[0], transcript="look again (screen unchanged since last turn)",
                        screenshot_path=None, job_id=None)
    win.select_session(ids[0])
    html = win._detail._reply_browser.toHtml()
    assert "look again" in html and "Same screen" in html
    assert "screen unchanged" not in html
    win._popout_session(win._store.get_session(ids[0]))
    pop = win._detached_windows[-1]
    pop_html = pop._view._reply_browser.toHtml()
    assert "Same screen" in pop_html and "screen unchanged" not in pop_html
    pop.close()
    assert sw._display_transcript("plain") == "plain"
    assert sw._display_transcript("x (screen unchanged since last turn)") == "x"


def test_detail_copy_notice_is_shown_by_the_window(win):
    win._detail.copy_notice.emit("Copied 12 characters")
    assert win._test_notices[-1] == "Copied 12 characters"
    ids = _mk(win, 1)
    win._popout_session(win._store.get_session(ids[0]))
    pop = win._detached_windows[-1]
    pop._view.copy_notice.emit("Copied code")
    assert win._test_notices[-1] == "Copied code"
    pop.close()


def test_queued_follow_up_never_displaces_the_running_turn(win):
    """Routed bug: with A running and B queued in one session, whichever job
    updated last became "live" — turns rendered out of order and Stop / Esc
    targeted the queued job."""
    ids = _mk(win, 1)
    sid = ids[0]
    st = win._store
    t1 = st.add_turn(session_id=sid, transcript="first", screenshot_path=None, job_id=None)
    st.update_turn(t1, final_text="one", ended_at=time.time())
    t2 = st.add_turn(session_id=sid, transcript="second", screenshot_path=None, job_id=None)
    t3 = st.add_turn(session_id=sid, transcript="third", screenshot_path=None, job_id=None)
    a = _add_job(win, 101, sid, "running")
    a.local_turn_id = t2
    b = _add_job(win, 102, sid, "queued")
    b.local_turn_id = t3
    win.select_session(sid)
    _pump()

    def check():
        assert win._detail._job is not None and win._detail._job.id == 101
        shown = [t.id for t in win._detail._prior_turns]
        assert shown == [t1, t2]  # in order; the queued turn is held back

    check()
    for jid in (102, 101, 102, 102, 101, 102):  # interleaved updates
        win._on_job_updated(jid)
        _pump()
        check()
    cancelled = []
    win.cancel_job_requested.connect(cancelled.append)
    win._on_row_cancel_requested(sid)
    win._detail.cancel_requested.emit(win._detail._job.id)
    assert cancelled == [101, 101]
    # A finishes → B becomes the live turn, after A's.
    a.status = "done"
    st.update_turn(t2, final_text="two", ended_at=time.time())
    win._on_job_updated(101)
    _pump()
    assert win._detail._job.id == 102
    assert [t.id for t in win._detail._prior_turns] == [t1, t2, t3]


def test_review_fixes_palette_header_click_and_per_batch_undo(win, monkeypatch):
    ids = _mk(win, 4)
    _show(win)
    d = _palette(win)
    hdr = next(d._results.item(i) for i in range(d._results.count())
               if d._results.item(i).data(d._HEADER))
    d._results.itemClicked.emit(hdr)
    assert d.result() != QtWidgets.QDialog.Accepted and d.selected_id is None
    d.reject()
    cbs = []
    monkeypatch.setattr(win, "show_inline_notice", lambda text, **kw: cbs.append(kw.get("undo_callback")))

    def items(sids):
        return [win._item_for_session(s) for s in sids]

    win._bulk_archive(items(ids[:2]))
    win._set_filter("active")
    win._bulk_archive(items(ids[2:]))
    cbs[0]()  # Undo on the FIRST notice brings back the first batch only
    assert not any(win._store.get_session(i).archived for i in ids[:2])
    assert all(win._store.get_session(i).archived for i in ids[2:])


def test_second_inline_rename_survives_the_first_ones_refresh(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 2)
    _show(win)
    win._rename_item(win._item_for_session(ids[0]))
    win._rename_editor.setText("First renamed")
    win._rename_item(win._item_for_session(ids[1]))  # e.g. from the row menu
    second = win._rename_editor
    _pump(6)
    assert win._rename_editor is second and second.isVisible()
    assert win._store.get_session(ids[0]).title == "First renamed"
    QTest.keyClick(second, QtCore.Qt.Key_Escape)
    _pump()
    assert win._row_widgets[ids[0]]._title_lbl.fullText() == "First renamed"


def test_rename_editor_follows_its_row_when_rows_reflow(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    sids = [win._store.create_session(
        model="m", transcript="t", screenshot_path=None, status="idle",
        title=f"{i} Investigate why the canvas summary panel stops updating after reconnects")
        for i in range(6)]
    win._refresh_list()
    _show(win, (760, 700))
    _pump(6)
    win._fit_row_heights()
    target = sids[0]  # listed last (oldest)
    win._rename_item(win._item_for_session(target))
    win.resize(1600, 700)
    win._splitter.setSizes([900, 700])
    _pump(10)
    win._fit_row_heights()
    _pump(4)
    ed = win._rename_editor
    title = win._row_widgets[target]._title_lbl
    ty = title.mapTo(win._list.viewport(), QtCore.QPoint(0, 0)).y()
    assert abs(ed.geometry().center().y() - (ty + title.fontMetrics().lineSpacing() // 2)) <= 4
    win._finish_rename(commit=False)


# ── Phase 3 review round 2 ──────────────────────────────────────────────────


def _palette_act(win, label):
    return next(a for a in win._palette_actions() if a.label == label)


def test_palette_rename_targets_the_active_session_not_the_current_row(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 4)
    _show(win)
    # Multi-select: the list's current row is another session.
    win.select_session(ids[0])
    _pump()
    win._list.setCurrentItem(win._item_for_session(ids[2]), QtCore.QItemSelectionModel.Select)
    _pump()
    assert win._active_session_id == ids[0]
    assert win._list.currentItem().data(QtCore.Qt.UserRole).id == ids[2]
    _palette_act(win, "Rename this session…").callback()
    assert win._rename_sid == ids[0]
    win._finish_rename(commit=False)
    # Mark Done hid the active row: rename brings it back, not the top row.
    win._list.clearSelection()
    win.select_session(ids[1])
    _pump()
    _palette_act(win, "Mark this session Done").callback()
    _pump()
    assert win._item_for_session(ids[1]) is None
    win._list.setFocus(QtCore.Qt.TabFocusReason)
    _pump()
    _palette_act(win, "Rename this session…").callback()
    _pump()
    assert win._rename_sid == ids[1] and win._rename_editor.isVisible()
    win._finish_rename(commit=False)


def test_click_away_rename_does_not_overwrite_a_fresh_auto_title(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 1)
    win._store.set_title(ids[0], "Old title", auto=True)
    win._refresh_list()
    _show(win)
    win._rename_item(win._item_for_session(ids[0]))
    win._store.set_title(ids[0], "Fresh auto title", auto=True)  # AutoTitler lands
    win._text_input.setFocus(QtCore.Qt.MouseFocusReason)  # click away, nothing typed
    _pump()
    assert win._rename_editor is None
    sess = win._store.get_session(ids[0])
    assert sess.title == "Fresh auto title" and sess.auto_titled
    # An actual edit is still saved (as a manual title).
    win._rename_item(win._item_for_session(ids[0]))
    win._rename_editor.setText("Mine")
    QTest.keyClick(win._rename_editor, QtCore.Qt.Key_Return)
    _pump()
    sess = win._store.get_session(ids[0])
    assert sess.title == "Mine" and not sess.auto_titled


def test_stacked_single_row_undo_notices_undo_their_own_session(win, monkeypatch):
    ids = _mk(win, 4)
    _show(win)
    cbs = []
    monkeypatch.setattr(win, "show_inline_notice", lambda text, **kw: cbs.append(kw.get("undo_callback")))
    win._on_review_toggled(ids[0])
    win._on_review_toggled(ids[1])
    cbs[0]()  # Undo on the OLDER notice
    assert win._store.get_session(ids[0]).reviewed_at is None
    assert win._store.get_session(ids[1]).reviewed_at is not None
    win._on_archive_requested(ids[2])
    win._on_archive_requested(ids[3])
    cbs[2]()
    assert not win._store.get_session(ids[2]).archived
    assert win._store.get_session(ids[3]).archived


def _bucketed_list(win, n=60):
    ids = _mk(win, n)
    _show(win, (1220, 700))
    _pump()
    return ids


def test_sticky_bucket_never_covers_the_current_row_or_the_rename_editor(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _bucketed_list(win)
    win._list.scrollToBottom()
    _pump()
    win.select_session(ids[30])
    _pump()
    win._list.setFocus()
    _pump()
    st = win._sticky_bucket
    for _ in range(25):
        QTest.keyClick(win._list, QtCore.Qt.Key_Up)
        _pump(2)
        r = win._list.visualItemRect(win._list.currentItem())
        if not st.isHidden():
            assert r.top() >= st.geometry().bottom() + 1 - win._list.viewport().geometry().y()
    assert win._list.verticalScrollBar().value() > 0 and not st.isHidden()  # exercised the overlay
    # Inline rename: the overlay hides while the editor is open, then returns.
    win._rename_focused_row()
    _pump()
    assert win._rename_editor is not None and st.isHidden()
    win._list.verticalScrollBar().setValue(win._list.verticalScrollBar().value() + 1)
    _pump()
    assert st.isHidden()
    win._finish_rename(commit=False)
    _pump()
    assert not st.isHidden()


def test_restored_fullscreen_state_is_persisted_as_normal(win, saved_state):
    saved_state.update({"sessions_window_state": "fullscreen",
                        "sessions_window_normal": {"x": 40, "y": 40, "w": 600, "h": 500}})
    win.show_and_focus()
    _pump()
    assert not win.isFullScreen()
    assert saved_state["sessions_window_state"] == "normal"
    assert win._persisted_window_state == "normal"


def test_reset_layout_from_mac_fullscreen_waits_for_the_normal_state(win, saved_state, monkeypatch):
    _show(win, (600, 500))
    win.move(10, 10)
    _pump()
    default = QtCore.QRect(*sw.compute_default_sw_geometry())
    state = {"v": QtCore.Qt.WindowFullScreen}
    shown_normal = []
    monkeypatch.setattr(sw.sys, "platform", "darwin")
    monkeypatch.setattr(win, "windowState", lambda: state["v"])
    monkeypatch.setattr(win, "showNormal", lambda: shown_normal.append(1))
    before = win.geometry()
    win.reset_layout()
    _pump()
    assert shown_normal and win.geometry() == before  # not yet: the exit animation would undo it
    state["v"] = QtCore.Qt.WindowNoState
    QtWidgets.QApplication.sendEvent(win, QtGui.QWindowStateChangeEvent(QtCore.Qt.WindowFullScreen))
    _pump()
    assert win.geometry() == default


def test_pending_profile_is_cleared_once_a_session_is_active(win):
    ids = _mk(win, 2)
    _show(win)
    win._start_fresh_routing("deploy")
    assert "deploy" in win._routing_pill.text()
    win.select_session(ids[0])  # e.g. the PTT created it and the runner selected it
    _pump()
    win._on_archive_requested(ids[0])  # back to "new session"
    _pump()
    assert "deploy" not in win._routing_pill.text()
    assert "deploy" not in win._text_input.placeholderText()


def test_context_menu_actions_survive_a_rebuild_while_the_menu_is_open(win, monkeypatch):
    _no_input_dialog(monkeypatch)
    ids = _mk(win, 3)
    _show(win)

    class _RebuildMenu(QtWidgets.QMenu):
        def exec(self, *_a, **_kw):
            win._refresh_list()  # session_changed from a running sibling
            _pump()
            return next(a for a in self.actions() if a.text().startswith("Rename"))

    monkeypatch.setattr(QtWidgets, "QMenu", _RebuildMenu)
    pos = win._list.visualItemRect(win._item_for_session(ids[1])).center()
    win._show_context_menu(pos)
    assert win._rename_sid == ids[1] and win._rename_editor is not None
    win._finish_rename(commit=False)


def test_dashboard_is_not_rebuilt_while_hidden_or_minimised(win, monkeypatch):
    ids = _mk(win, 1)
    _show(win)
    win._clear_active_session()
    calls = []
    real = win._detail.show_dashboard
    monkeypatch.setattr(win._detail, "show_dashboard", lambda html: calls.append(html) or real(html))
    win._recents = [ids[0]]
    win.hide()
    win._store.set_title(ids[0], "Renamed while hidden")
    win._refresh_dashboard_if_visible()
    assert calls == []
    win.show()
    _pump()
    assert calls and "Renamed while hidden" in calls[-1]  # caught up on show
    calls.clear()
    monkeypatch.setattr(win, "isMinimized", lambda: True)
    win._store.set_title(ids[0], "Renamed while minimised")
    win._refresh_dashboard_if_visible()
    assert calls == []
