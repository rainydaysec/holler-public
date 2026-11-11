"""Phase 5 review fixes: Esc / ⌘Z consumed during a hold, HUD dodge without
flicker on short windows, one toast stack per screen, toasts inert during a
capture, and the "Transcribing…" toast's per-token bookkeeping."""
from __future__ import annotations

import sys
import types

import pytest
from PIL import Image
from PySide6 import QtCore, QtGui

from holler import hotkey as hk
from holler import theme

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS event tap")


# ── 1. Esc / ⌘Z consumed only while the PTT key is held ─────────────────────

def _mac_listener(key="right alt", cancel=True, undo=True):
    """A HotkeyListener configured as _start_pynput would, without a tap."""
    from pynput import keyboard as kb

    calls = []
    hl = hk.HotkeyListener(
        key, lambda: calls.append("press"), lambda: calls.append("release"),
        on_cancel=(lambda: calls.append("cancel")) if cancel else None,
        on_undo=(lambda: calls.append("undo")) if undo else None,
    )
    target = hk._parse_pynput_key(key)
    hl._acceptable = hk._acceptable_keys(target)
    hl._ptt_is_esc = hk._key_matches(kb.Key.esc, hl._acceptable)
    hl._ptt_is_z = hk._is_undo_letter(target)
    hl._ptt_flag = int(kb.Listener._MODIFIER_FLAGS.get(target, 0) or 0) if sys.platform == "darwin" else 0
    return hl, calls


def _ev(keycode, down=True, flags=0):
    """A real CGEvent — created, never posted (nothing reaches the session)."""
    from Quartz import CGEventCreateKeyboardEvent, CGEventSetFlags

    ev = CGEventCreateKeyboardEvent(None, keycode, down)
    CGEventSetFlags(ev, flags)
    return ev


def _qz():
    import Quartz
    return Quartz


@darwin_only
def test_intercept_consumes_esc_only_while_held():
    from pynput import keyboard as kb
    Q = _qz()
    hl, calls = _mac_listener()
    alt = Q.kCGEventFlagMaskAlternate
    down, up = Q.kCGEventKeyDown, Q.kCGEventKeyUp

    esc = _ev(53, True)
    assert hl._darwin_intercept(down, esc) is esc  # not held → passes
    assert hl._darwin_intercept(up, _ev(53, False)) is not None

    hl._pynput_press(kb.Key.alt_r)  # PTT down
    hl._pynput_press(kb.Key.esc)
    assert calls == ["press", "cancel"]
    assert hl._darwin_intercept(down, _ev(53, True, alt)) is None  # consumed
    assert hl._darwin_intercept(down, _ev(53, True, alt)) is None  # repeat too
    a = _ev(0, True, alt)  # plain "a" during the hold
    assert hl._darwin_intercept(down, a) is a
    hl._pynput_release(kb.Key.alt_r)  # PTT up before Esc up
    assert hl._darwin_intercept(up, _ev(53, False)) is None  # its key-up goes too
    nxt = _ev(53, False)
    assert hl._darwin_intercept(up, nxt) is nxt  # …only once
    esc2 = _ev(53, True)
    assert hl._darwin_intercept(down, esc2) is esc2  # released → normal again


@darwin_only
def test_intercept_consumes_cmd_z_but_not_plain_z():
    from pynput import keyboard as kb
    Q = _qz()
    hl, calls = _mac_listener()
    alt, cmd = Q.kCGEventFlagMaskAlternate, Q.kCGEventFlagMaskCommand
    down, up = Q.kCGEventKeyDown, Q.kCGEventKeyUp

    hl._pynput_press(kb.Key.alt_r)
    z = _ev(6, True, alt)
    assert hl._darwin_intercept(down, z) is z  # plain Z while held passes
    hl._pynput_press(kb.Key.cmd)
    assert hl._darwin_intercept(down, _ev(6, True, alt | cmd)) is None
    x = _ev(7, True, alt | cmd)  # ⌘X passes
    assert hl._darwin_intercept(down, x) is x
    assert hl._darwin_intercept(up, _ev(6, False, alt | cmd)) is None
    hl._pynput_release(kb.Key.alt_r)
    cz = _ev(6, True, cmd)  # ⌘Z with PTT up reaches the app
    assert hl._darwin_intercept(down, cz) is cz


@darwin_only
def test_intercept_passes_modifiers_ptt_and_stale_holds():
    from pynput import keyboard as kb
    Q = _qz()
    hl, _ = _mac_listener()
    hl._pynput_press(kb.Key.alt_r)
    fc = _ev(61, True, Q.kCGEventFlagMaskAlternate)
    assert hl._darwin_intercept(Q.kCGEventFlagsChanged, fc) is fc  # modifiers
    # _held is stale (a missed release): the event's flags say the PTT is up.
    esc = _ev(53, True, 0)
    assert hl._darwin_intercept(Q.kCGEventKeyDown, esc) is esc

    # PTT = Esc: Esc is the PTT key itself and is never consumed.
    hl, _ = _mac_listener("esc")
    hl._pynput_press(kb.Key.esc)
    e = _ev(53, True)
    assert hl._held and hl._darwin_intercept(Q.kCGEventKeyDown, e) is e

    # PTT = right cmd: a bare Z (⌘ flag from the PTT itself) is not undo.
    hl, _ = _mac_listener("right cmd")
    hl._pynput_press(kb.Key.cmd_r)
    z = _ev(6, True, Q.kCGEventFlagMaskCommand)
    assert hl._held and hl._darwin_intercept(Q.kCGEventKeyDown, z) is z


@darwin_only
def test_intercept_without_callbacks_and_on_errors_passes():
    from pynput import keyboard as kb
    Q = _qz()
    hl, _ = _mac_listener(cancel=False, undo=False)
    hl._pynput_press(kb.Key.alt_r)
    e = _ev(53, True, Q.kCGEventFlagMaskAlternate)
    assert hl._darwin_intercept(Q.kCGEventKeyDown, e) is e

    hl, _ = _mac_listener()
    hl._pynput_press(kb.Key.alt_r)

    def boom(*a):
        raise RuntimeError("x")
    hl._swallow_decision = boom
    e = _ev(53, True, Q.kCGEventFlagMaskAlternate)
    assert hl._darwin_intercept(Q.kCGEventKeyDown, e) is e
    junk = object()  # not a CGEvent at all
    assert hl._darwin_intercept(Q.kCGEventKeyDown, junk) is junk


def test_ptt_listener_is_listen_only_even_with_capture_callbacks(monkeypatch):
    """The always-on PTT listener never gets darwin_intercept or the active
    tap guard (an active tap makes every keystroke system-wide wait on
    holler's GIL); with on_cancel / on_undo on macOS only the hold-only tap
    class is prepared — nothing intercepting runs until a hold."""
    import holler.hotkey as mod

    built = []

    class _Fake:
        def __init__(self, *a, **k):
            built.append((type(self), k))

        def start(self):
            pass

    monkeypatch.setattr(mod, "_main_thread_safe", lambda cls: _Fake)
    guarded = []
    monkeypatch.setattr(mod, "_active_tap_guard", lambda cls: guarded.append(cls) or cls)
    hl, _ = _mac_listener()
    hl._start_pynput()
    plain = mod.HotkeyListener("right alt", lambda: None, lambda: None)
    plain._start_pynput()
    assert [c for c, _ in built] == [_Fake, _Fake]  # the listen-only class
    assert all("darwin_intercept" not in k and k["suppress"] is False for _, k in built)
    assert hl._hold_tap is None and hl._hold_worker is None
    assert plain._hold_cls is None
    if sys.platform == "darwin":
        assert hl._hold_cls is not None and len(guarded) == 1  # the hold class only
    else:
        assert hl._hold_cls is None and guarded == []


class _KbEvent:
    def __init__(self, name, up=False, scan_code=None):
        self.name = name
        self.event_type = "up" if up else "down"
        self.scan_code = scan_code if scan_code is not None else hash(name) % 1000


def _fake_keyboard(pressed):
    m = types.SimpleNamespace(KEY_UP="up", hooks=[])
    m.is_pressed = lambda n: n in pressed
    m.hook_key = lambda key, cb, suppress=False: ("key", key, cb, suppress)
    m.hook = lambda cb, suppress=False: m.hooks.append((cb, suppress)) or (cb, suppress)
    m.unhook = lambda h: m.hooks.remove(h) if h in m.hooks else None
    return m


def test_windows_hook_suppresses_esc_and_ctrl_z_only_while_held(monkeypatch):
    pressed: set = set()
    kbmod = _fake_keyboard(pressed)
    monkeypatch.setitem(sys.modules, "keyboard", kbmod)
    calls = []
    hl = hk.HotkeyListener("right alt", lambda: None, lambda: None,
                           on_cancel=lambda: calls.append("cancel"),
                           on_undo=lambda: calls.append("undo"))
    hl._start_keyboard()
    assert kbmod.hooks == []  # no blocking hook outside a hold
    pressed.add("right alt")
    hl._handle_press(None)
    (cb, suppress), = kbmod.hooks
    assert suppress is True  # a blocking hook: its return value decides

    assert cb(_KbEvent("esc")) is False and calls == ["cancel"]
    assert cb(_KbEvent("a")) is True
    assert cb(_KbEvent("z")) is True  # plain Z
    assert cb(_KbEvent("right alt")) is True  # the PTT key itself
    pressed.add("left ctrl")
    assert cb(_KbEvent("z")) is False and calls == ["cancel", "undo"]
    assert cb(_KbEvent("z", up=True)) is False  # matching key-ups
    assert cb(_KbEvent("esc", up=True)) is False
    assert cb(_KbEvent("esc", up=True)) is True
    # stale _held: keyboard's table says the PTT key is up
    pressed.discard("right alt")
    assert cb(_KbEvent("esc")) is True
    # errors pass the key through
    pressed.add("right alt")
    kbmod.is_pressed = lambda n: (_ for _ in ()).throw(RuntimeError("x"))
    assert cb(_KbEvent("z")) is True
    hl._held = False
    assert cb(_KbEvent("esc")) is True  # not held → passes
    assert cb(_KbEvent("esc", up=True)) is True
    hl._held = True
    hl._handle_release(None)
    assert kbmod.hooks == []  # removed on release
    hl._stop_keyboard()


def test_windows_no_extra_hook_without_callbacks(monkeypatch):
    kbmod = _fake_keyboard(set())
    monkeypatch.setitem(sys.modules, "keyboard", kbmod)
    hl = hk.HotkeyListener("right alt", lambda: None, lambda: None)
    hl._start_keyboard()
    assert kbmod.hooks == [] and hl._extra_handle is None


# ── 2. HUD dodge: no flip-flop on short windows, stays inside ───────────────

def _overlay(app, size, **hud):
    from holler.overlay import OverlayWindow

    ov = OverlayWindow(stroke_color="#FF2E2E", stroke_width=4)
    w, h = size
    ov.show_for(Image.new("RGB", (w * 2, h * 2)), (0, 0, w, h), **hud)
    app.processEvents()
    return ov


def _move(ov, x, y):
    pos = QtCore.QPointF(x, y)
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseMove, pos, ov.mapToGlobal(pos), QtCore.Qt.NoButton,
                           QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
    ov.mouseMoveEvent(ev)


@pytest.mark.parametrize("height", [150, 200, 260])
def test_hud_does_not_flip_flop_on_short_windows(app, height):
    ov = _overlay(app, (800, height), destination="→ New session",
                  warning="Screen Recording is off", source="Terminal", hints=True)
    try:
        flips, last = 0, ov._hud_bottom
        for i in range(10):  # wander over the vertical middle and both cards
            for y in (height // 2, ov._hud_rect().center().y(), ov._hud_rect(bottom=True).center().y()):
                _move(ov, 400 + i, y)
                if ov._hud_bottom != last:
                    flips, last = flips + 1, ov._hud_bottom
                r = ov._hud_rect()
                assert 0 <= r.top() and r.bottom() < height  # never past the window
        assert flips <= 10 * 2, "HUD flips back on its own"
        # Hovering the same spot never flips twice in a row.
        y = ov._hud_rect().center().y()
        states = []
        for _ in range(10):
            _move(ov, 400, y)
            states.append(ov._hud_bottom)
        assert len(set(states[1:])) == 1
        top, bottom = ov._hud_rect(bottom=False), ov._hud_rect(bottom=True)
        assert not top.intersects(bottom)
        ov.grab()
    finally:
        ov.hide()
        ov.deleteLater()


def test_hud_collapses_on_short_windows_and_full_on_tall(app):
    hud = dict(destination="→ New session", warning="w", hints=True)
    tall = _overlay(app, (800, 500), **hud)
    short = _overlay(app, (800, 150), **hud)
    try:
        assert tall._hud_level() == 2
        assert short._hud_level() == 0
        assert short._hud_rect().height() < tall._hud_rect().height()
    finally:
        for ov in (tall, short):
            ov.hide()
            ov.deleteLater()


def test_hud_stays_put_when_window_too_short_to_dodge(app):
    ov = _overlay(app, (300, 60), hints=True)
    try:
        before = ov._hud_rect()
        for _ in range(5):
            _move(ov, before.center().x(), before.center().y())
        assert ov._hud_rect() == before
        assert before.bottom() < 60
    finally:
        ov.hide()
        ov.deleteLater()


# ── 3 / 4. toasts: one stack per screen; inert during a capture ────────────

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
    panel.set_toast_click_blocker(None)
    for t in made:
        for c in [t, *t._spares]:
            c.hide()
            c.deleteLater()
    panel._STACK.queue.clear()
    app.processEvents()


def test_toasts_at_nearly_identical_corners_stack(app, toasts):
    screen = QtGui.QGuiApplication.primaryScreen().availableGeometry()
    corner = (screen.right(), screen.bottom())
    near = (corner[0] - 3, corner[1] - 5)  # e.g. a Sessions window's corner
    a = toasts().show_message("✓ job #1: done", corner)
    b = toasts().show_message("Cancelled — nothing was sent.", near)
    app.processEvents()
    assert a is not None and b is not None
    assert not a.geometry().intersects(b.geometry())
    assert b.geometry().top() > a.geometry().top()  # newest lowest, same stack
    assert a.geometry().right() == b.geometry().right()


def test_toast_clicks_ignored_while_blocked(app, toasts):
    from holler import panel

    capturing = [True]
    panel.set_toast_click_blocker(lambda: capturing[0])
    hits = []
    c = toasts().show_message("✓ done", (900, 500), on_click=lambda: hits.append("open"))
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
                           QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    c.mousePressEvent(ev)
    assert hits == [] and c.isVisible()
    d = toasts().show_message("Archived", (900, 500), action=("Undo it", lambda: hits.append("act")))
    d._undo_btn.click()
    assert hits == [] and d.isVisible()
    capturing[0] = False  # capture over: clicks work again
    d._undo_btn.click()
    c.mousePressEvent(ev)
    assert hits == ["act", "open"]


def test_toast_blocker_predicate_errors_do_not_block(app, toasts):
    from holler import panel

    panel.set_toast_click_blocker(lambda: 1 / 0)
    hits = []
    c = toasts().show_message("✓ x", (900, 500), on_click=lambda: hits.append(1))
    ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
                           QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    c.mousePressEvent(ev)
    assert hits == [1]


def test_full_runner_blocks_toast_clicks_during_capture():
    import inspect

    from holler.runners import full

    src = inspect.getsource(full.main)
    assert "set_toast_click_blocker(_capture_in_progress)" in src
    assert "overlay.isVisible()" in src


# ── 5. "Transcribing…" bookkeeping ──────────────────────────────────────────

class _Card:
    def __init__(self):
        from holler.runners.full import _TRANSCRIBING_TEXT

        self._text = _TRANSCRIBING_TEXT
        self.visible = True

    def isVisible(self):
        return self.visible

    def dismiss(self):
        self.visible = False


def _status():
    from holler.runners.full import _TranscribingStatus

    now = [0.0]
    timers = []
    shown = []

    def show():
        shown.append(_Card())
        return shown[-1]
    st = _TranscribingStatus(show, delay_ms=300, deadline_s=60.0, clock=lambda: now[0],
                             single_shot=lambda ms, fn: timers.append((ms, fn)))
    return st, now, timers, shown


def _fire(timers, ms):
    for t, fn in [x for x in timers if x[0] == ms]:
        fn()


def test_transcribing_failed_start_tracks_nothing():
    st, _now, timers, shown = _status()

    def bad(token):
        raise RuntimeError("can't start new thread")
    assert st.launch(bad) is None
    assert st.pending() == set() and timers == []
    _fire(timers, 300)
    assert shown == []


def test_transcribing_hung_dispatch_expires_at_its_deadline():
    st, now, timers, shown = _status()
    hung = st.launch(lambda t: None)
    _fire(timers, 300)
    assert len(shown) == 1 and shown[0].visible
    now[0] = 61.0  # past the hung dispatch's deadline
    _fire(timers, 60_050)
    assert not shown[0].visible and st.pending() == set()
    # a later turn shows its own toast and finishing it really dismisses it
    timers.clear()
    t2 = st.launch(lambda t: None)
    _fire(timers, 300)
    assert len(shown) == 2 and shown[1].visible
    st.finished(t2)
    assert not shown[1].visible
    st.finished(hung)  # a very late finish is harmless
    assert st.pending() == set()


def test_transcribing_overlapping_dispatches_share_one_toast():
    st, _now, timers, shown = _status()
    a = st.launch(lambda t: None)
    b = st.launch(lambda t: None)
    _fire(timers, 300)
    assert len(shown) == 1
    st.finished(a)
    assert shown[0].visible  # b still in flight
    st.finished(b)
    assert not shown[0].visible


def test_transcribing_quick_turn_never_shows():
    st, _now, timers, shown = _status()
    t = st.launch(lambda tok: None)
    st.finished(t)
    _fire(timers, 300)
    assert shown == []


def test_transcribing_token_reaches_the_worker():
    st, _now, _timers, _shown = _status()
    got = []
    tok = st.launch(got.append)
    assert got == [tok] and st.pending() == {tok}
