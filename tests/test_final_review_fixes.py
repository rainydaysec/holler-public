"""Regression tests for the end-to-end review's findings (items 2-10; item 1
lives in test_live_segment_leak.py)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


# ── 2: toasts carrying callbacks are never merged ───────────────────────────

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


def test_same_text_undo_toasts_keep_their_own_callbacks(app, toasts):
    t = toasts()
    restored = []
    a = t.show_message("Archived 2 sessions", (900, 500), undo_callback=lambda: restored.append("A"))
    b = t.show_message("Archived 2 sessions", (900, 500), undo_callback=lambda: restored.append("B"))
    assert a is not None and b is not None and a is not b
    b._on_undo_clicked()
    a._on_undo_clicked()
    assert sorted(restored) == ["A", "B"]


def test_same_text_action_and_click_toasts_not_merged(app, toasts):
    t = toasts()
    hits = []
    a = t.show_message("Done", (900, 500), on_click=lambda: hits.append(1))
    b = t.show_message("Done", (900, 500), on_click=lambda: hits.append(2))
    assert a is not b
    c = t.show_message("Saved", (900, 500))
    d = t.show_message("Saved", (900, 500), action=("Open", lambda: hits.append(3)))
    assert c is not d
    # A plain notice never merges into a card with callbacks either.
    toasts_plain = t.show_message("Done", (900, 500))
    assert toasts_plain is None or toasts_plain not in (a, b)


def test_plain_identical_toasts_still_merge(app, toasts):
    t = toasts()
    assert t.show_message("same", (900, 500)) is t.show_message("same", (900, 500))


# ── 3: smart-batched (merged) turns render with the live turn ───────────────

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


def _merged_setup(store):
    from holler.jobs import JobStore
    from holler.sdk_session import SessionEvent as E

    jobs = JobStore(store)
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    t0 = store.add_turn(session_id=sid, transcript="USER-ZERO", screenshot_path=None, job_id=None)
    store.update_turn(t0, final_text="REPLY-ZERO", ended_at=1.0, duration_ms=5)
    ta = store.add_turn(session_id=sid, transcript="USER-A first", screenshot_path=None, job_id=None)
    jobs.apply_event(E(1, "submitted", {"transcript": "USER-A first", "local_session_id": sid,
                                        "local_turn_id": ta}))
    tb = store.add_turn(session_id=sid, transcript="USER-MERGED second", screenshot_path=None, job_id=None)
    jobs.apply_event(E(1, "merged_in", {"local_turn_id": tb, "transcript": "USER-MERGED second"}))
    # A follow-up queued behind the job (its own job, not merged).
    tq = store.add_turn(session_id=sid, transcript="USER-QUEUED later", screenshot_path=None, job_id=None)
    jobs.apply_event(E(2, "submitted", {"transcript": "USER-QUEUED later", "local_session_id": sid,
                                        "local_turn_id": tq}))
    jobs.apply_event(E(1, "started", {}))
    jobs.apply_event(E(1, "text_delta", {"text": "REPLY-A answering both"}))
    return jobs, sid


def _order(text, keys):
    return [text.find(k) for k in keys]


def test_turns_up_to_live_keeps_merged_turns(app, store):
    from holler.sessions_window import _turns_up_to_live

    jobs, sid = _merged_setup(store)
    kept = [t.transcript for t in _turns_up_to_live(store.get_turns(sid), jobs.get(1))]
    assert kept == ["USER-ZERO", "USER-A first", "USER-MERGED second"]


def test_merged_turn_renders_in_order_while_running_and_after(app, store):
    from holler.detail import DetailView
    from holler.sdk_session import SessionEvent as E
    from holler.sessions_window import _turns_up_to_live

    jobs, sid = _merged_setup(store)
    v = DetailView()
    v.resize(700, 500)
    v.show()

    def show():
        turns = _turns_up_to_live(store.get_turns(sid), jobs.get(1))
        v.show_session(store.get_session(sid), turns,
                       store.get_tool_calls_for_turns([t.id for t in turns]), jobs.get(1))
        app.processEvents()
        return v._reply_browser.toPlainText()

    keys = ("USER-ZERO", "USER-A", "USER-MERGED", "REPLY-A")
    t = show()
    pos = _order(t, keys)
    assert -1 not in pos and pos == sorted(pos), t
    assert "USER-QUEUED" not in t
    jobs.apply_event(E(1, "result", {"result": "REPLY-A answering both", "duration_ms": 10}))
    t = show()
    pos = _order(t, keys)
    assert -1 not in pos and pos == sorted(pos), t
    assert t.count("REPLY-A answering both") == 1
    # Labels follow the transcript order.
    assert -1 < t.find("Turn 2") < t.find("Turn 3")


# ── 4: toast kind — explicit at call sites, quotes ignored when inferred ────

def test_toast_kind_ignores_quoted_titles():
    from holler.panel import toast_kind

    assert toast_kind("Archived “Fix failed login flow”") == "info"
    assert toast_kind("Reopened “Can't connect error”") == "info"
    assert toast_kind("Couldn't archive “Notes”") == "error"
    assert toast_kind("Generating a new title for “Login failed”…") == "info"
    assert toast_kind("✓ “Error handling”: done") == "success"


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state
    from holler.jobs import JobStore
    from holler.sessions_window import SessionsWindow
    from holler.store import Store

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)
    store = Store(tmp_path / "holler.db")
    w = SessionsWindow(store, JobStore(store), tmp_path / "captures", tmp_path)
    yield w
    from holler import panel

    for c in list(panel._STACK.visible):
        c.hide()
    panel._STACK.queue.clear()
    w.close()
    w.deleteLater()
    store.close()


@pytest.mark.parametrize("inference", ["real", "always-error"])
def test_archive_notice_with_alarming_title_is_info(app, win, monkeypatch, inference):
    from holler import panel

    if inference == "always-error":
        # The window states the kind itself — inference never runs.
        monkeypatch.setattr(panel, "toast_kind", lambda text: "error")
    sid = win._store.create_session(model="m", transcript="x", screenshot_path=None,
                                    status="idle", title="Fix failed login flow")
    win._refresh_list()
    win._on_archive_requested(sid)
    cards = [c for c in panel._STACK.visible if "Fix failed login flow" in c._text]
    assert cards and cards[-1]._kind == "info"
    win._store.archive(sid, False)
    win._refresh_list()
    win._bulk_archive([win._item_for_session(sid)])
    cards = [c for c in panel._STACK.visible if c._text == "Archived 1 session"]
    assert cards and cards[-1]._kind == "info"


def test_panel_inline_notice_takes_explicit_kind(app):
    from holler.jobs import JobStore
    from holler import panel as panel_mod
    from holler.panel import Panel

    p = Panel(JobStore(None), model_label="M")
    try:
        p.show_inline_notice("That run failed, harmlessly", kind="info")
        cards = [c for c in panel_mod._STACK.visible if c._text == "That run failed, harmlessly"]
        assert cards and cards[-1]._kind == "info"
    finally:
        for c in list(panel_mod._STACK.visible):
            c.hide()
        panel_mod._STACK.queue.clear()
        p.deleteLater()


# ── 5: Esc that cancelled a hold never also stops the running turn ──────────

class _Ov:
    def __init__(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def isVisible(self):
        return self.visible


class _Rec:
    def stop(self):
        return None


def test_capture_guard_holds_until_the_cancelled_hold_is_released():
    full = pytest.importorskip("holler.runners.full")
    state = {"snapshot": object()}
    ov = _Ov()
    assert full._capture_guard_active(state, ov) is True       # overlay up
    assert full._cancel_capture(state, ov, _Rec()) is True     # Esc: overlay hidden
    assert ov.isVisible() is False
    assert full._capture_guard_active(state, ov) is True       # key still held
    assert full._release_was_cancelled(state) is True          # release
    assert full._capture_guard_active(state, ov) is False


def test_esc_in_transcript_after_hold_cancel_does_not_stop_turn(app, store):
    from PySide6 import QtCore, QtGui

    full = pytest.importorskip("holler.runners.full")
    from holler.detail import DetailView
    from holler.jobs import JobStore
    from holler.sdk_session import SessionEvent as E

    jobs = JobStore(store)
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    tid = store.add_turn(session_id=sid, transcript="q", screenshot_path=None, job_id=None)
    jobs.apply_event(E(1, "submitted", {"transcript": "q", "local_session_id": sid, "local_turn_id": tid}))
    jobs.apply_event(E(1, "started", {}))
    v = DetailView()
    turns = store.get_turns(sid)
    v.show_session(store.get_session(sid), turns, {}, jobs.get(1))
    stopped = []
    v.cancel_requested.connect(stopped.append)
    state = {"snapshot": object()}
    ov = _Ov()
    DetailView.set_capture_active_predicate(lambda: full._capture_guard_active(state, ov))
    try:
        full._cancel_capture(state, ov, _Rec())
        esc = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Escape, QtCore.Qt.NoModifier)
        v.keyPressEvent(esc)
        assert stopped == []
        full._release_was_cancelled(state)
        v.keyPressEvent(esc)
        assert stopped == [1]
    finally:
        DetailView.set_capture_active_predicate(None)
        v.deleteLater()


# ── 6: recording a new push-to-talk key pauses push-to-talk ─────────────────

def test_record_button_sets_and_clears_key_recording_flag(app, monkeypatch):
    """Recording pauses push-to-talk, and the pause outlives the recording by
    a short grace period (the listener's queued press may arrive after Qt
    delivered the same key to the button)."""
    from PySide6 import QtWidgets

    from holler import hotkey
    from holler.settings_dialog import HotkeyRecordButton

    hotkey.set_key_recording(False)  # no grace left over from earlier tests
    now = [1000.0]
    monkeypatch.setattr(hotkey.time, "monotonic", lambda: now[0])
    host = QtWidgets.QWidget()
    edit = QtWidgets.QLineEdit(host)
    btn = HotkeyRecordButton(edit, host)
    host.show()
    try:
        assert hotkey.key_recording_active() is False
        btn.start()
        assert hotkey.key_recording_active() is True
        btn.stop()
        assert hotkey.key_recording_active() is True  # grace period
        now[0] += hotkey.KEY_RECORDING_GRACE_S + 0.01
        assert hotkey.key_recording_active() is False
        btn.start()
        host.hide()  # closing the dialog ends recording too
        now[0] += hotkey.KEY_RECORDING_GRACE_S + 0.01
        assert hotkey.key_recording_active() is False
    finally:
        hotkey.set_key_recording(False)
        host.deleteLater()


def test_ptt_press_and_its_release_ignored_while_recording():
    full = pytest.importorskip("holler.runners.full")
    from holler import hotkey

    state = {}
    try:
        hotkey.set_key_recording(True)
        assert full._ptt_press_ignored(state) is True
        hotkey.set_key_recording(False)       # the key got recorded on press
        assert full._ptt_release_ignored(state) is True   # …its release is still skipped
        assert full._ptt_release_ignored(state) is False
        assert full._ptt_press_ignored(state) is False    # normal again
    finally:
        hotkey.set_key_recording(False)


# ── 7 / 8: one relaunch at a time; Settings "Restart now" → Cancel says so ──

def _relauncher(confirm_result=True, spawned=True):
    full = pytest.importorskip("holler.runners.full")
    log = {"confirm": 0, "relaunch": [], "notices": [], "timer_active_in_confirm": []}

    def confirm():
        log["confirm"] += 1
        log["timer_active_in_confirm"].append(r.text_scale_pending())
        return confirm_result

    def relaunch(reason, fail_notice):
        log["relaunch"].append(reason)
        return spawned

    r = full._Relauncher(confirm=confirm, relaunch=relaunch,
                         notice=lambda text, kind: log["notices"].append((text, kind)),
                         text_scale_notice=lambda: "Text size 110% applies the next time Holler starts")
    return r, log


def test_restart_cancels_pending_text_size_restart(app):
    r, log = _relauncher()
    r.schedule_text_scale()
    r.restart()
    assert log["timer_active_in_confirm"] == [False], "the debounce must not fire during the prompt"
    assert log["relaunch"] == ["restart button"]
    r._text_scale_due()           # a late timeout
    r.restart()                   # a second click
    r.restart(from_settings=True)
    r.schedule_text_scale()
    assert not r.text_scale_pending()
    assert log["confirm"] == 1 and log["relaunch"] == ["restart button"]


def test_text_size_restart_then_restart_button_is_a_noop(app):
    r, log = _relauncher()
    r.schedule_text_scale()
    r._timer.stop()
    r._text_scale_due()
    r.restart()
    assert log["confirm"] == 1 and log["relaunch"] == ["text scale"]


def test_failed_spawn_allows_retry(app):
    r, log = _relauncher(spawned=False)
    r.restart()
    r.restart()
    assert log["relaunch"] == ["restart button", "restart button"]


def test_settings_restart_cancel_explains_when_changes_apply(app):
    r, log = _relauncher(confirm_result=False)
    r.restart(from_settings=True)
    assert log["notices"] == [("Saved — changes apply the next time Holler restarts.", "info")]
    assert log["relaunch"] == []
    log["notices"].clear()
    r.restart()                   # ⟳ Restart → Cancel: nothing pending, nothing to say
    assert log["notices"] == []
    r.schedule_text_scale()
    r.restart()                   # …unless a text-size change was waiting on it
    assert log["notices"] == [("Text size 110% applies the next time Holler starts", "info")]


# ── 9: the Sessions-window hotkey hint shows the real (literal) combo ───────

@pytest.mark.parametrize("combo, plat, hint", [
    ("ctrl+shift+h", "darwin", "⌃⇧H"),
    ("shift+ctrl+h", "darwin", "⌃⇧H"),
    ("cmd+alt+space", "darwin", "⌥⌘Space"),
    ("ctrl+shift+h", "win32", "Ctrl+Shift+H"),
    ("alt+f12", "linux", "Alt+F12"),
])
def test_global_hotkey_hint(combo, plat, hint):
    from holler.hotkey import global_hotkey_hint

    assert global_hotkey_hint(combo, plat) == hint


def _menu_texts(panel):
    from unittest import mock

    from PySide6 import QtWidgets

    seen = []
    real = QtWidgets.QMenu

    class _Recording(real):
        def exec(self, *a, **k):  # noqa: A003 - Qt name
            seen.extend(act.text() for act in self.actions())
            return None

    # Swap the module attribute (Shiboken ignores patching the method).
    with mock.patch.object(QtWidgets, "QMenu", _Recording):
        panel._show_menu()
    return seen


def test_panel_menu_hint_follows_configured_hotkey(app, monkeypatch):
    import sys

    from holler.hotkey import global_hotkey_hint
    from holler.jobs import JobStore
    from holler.panel import Panel

    p = Panel(JobStore(None), model_label="M", sessions_hotkey="ctrl+alt+j")
    try:
        texts = _menu_texts(p)
        item = [t for t in texts if t.startswith("Open Sessions window")]
        assert item == [f"Open Sessions window\t{global_hotkey_hint('ctrl+alt+j')}"]
        if sys.platform == "darwin":
            assert item[0].endswith("⌃⌥J") and "⌘" not in item[0]
    finally:
        p.deleteLater()
    p = Panel(JobStore(None), model_label="M")
    try:
        item = [t for t in _menu_texts(p) if t.startswith("Open Sessions window")]
        if sys.platform == "darwin":
            assert item == ["Open Sessions window\t⌃⇧H"]
    finally:
        p.deleteLater()


# ── 10: the chain bar label elides (…) instead of clipping mid-word ─────────

def test_chain_bar_label_elides_with_full_text_tooltip(app, win):
    from PySide6 import QtWidgets

    sid = win._store.create_session(model="m", transcript="x", screenshot_path=None, status="idle")
    win._refresh_list()
    win.resize(860, 700)
    win.show()
    win.select_session(sid)
    win.set_session_chain_status(sid, True, 3)
    for _ in range(5):
        app.processEvents()
    lbl = win._chain_label
    full = "Chaining — 3 captures held · every hold is added until you start the session"
    assert lbl.text() == full
    shown = QtWidgets.QLabel.text(lbl)
    fm = lbl.fontMetrics()
    assert fm.horizontalAdvance(shown) <= lbl.contentsRect().width()
    if shown != full:
        assert shown.endswith("…") and lbl.toolTip() == full
    # Wide enough → the whole text, no tooltip.
    win.resize(1600, 700)
    for _ in range(5):
        app.processEvents()
    assert QtWidgets.QLabel.text(lbl) == full and lbl.toolTip() == ""
    # Narrow → elided, tooltip carries it.
    lbl.resize(120, lbl.height())
    assert QtWidgets.QLabel.text(lbl).endswith("…") and lbl.toolTip() == full
