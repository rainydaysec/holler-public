"""Capture-UX backend groundwork: Screen Recording permission, HiDPI stroke
thickness, Esc/undo during a PTT hold, and the polled mic level."""

from __future__ import annotations

import sys
import time
import types

import numpy as np
import pytest
from PIL import Image
from pynput.keyboard import Key, KeyCode

from holler import audio, composite, hotkey, mac_permissions, screen
from holler.composite import compose, stroke_width_px
from holler.screen import WindowSnapshot, downscale


# ----- 1. Screen Recording permission ----------------------------------------

def _fake_quartz(monkeypatch, preflight=None, request=None):
    q = types.ModuleType("Quartz")
    if preflight is not None:
        q.CGPreflightScreenCaptureAccess = preflight
    if request is not None:
        q.CGRequestScreenCaptureAccess = request
    monkeypatch.setitem(sys.modules, "Quartz", q)
    monkeypatch.setattr(mac_permissions.sys, "platform", "darwin")


def test_screen_recording_status_maps_preflight(monkeypatch):
    _fake_quartz(monkeypatch, preflight=lambda: True)
    assert mac_permissions.screen_recording_status() == "granted"
    _fake_quartz(monkeypatch, preflight=lambda: False)
    assert mac_permissions.screen_recording_status() == "denied"


def test_screen_recording_status_unknown_when_unavailable(monkeypatch):
    _fake_quartz(monkeypatch)  # no preflight symbol
    assert mac_permissions.screen_recording_status() == "unknown"

    def boom():
        raise RuntimeError("x")

    _fake_quartz(monkeypatch, preflight=boom)
    assert mac_permissions.screen_recording_status() == "unknown"


def test_screen_recording_helpers_are_noops_off_mac(monkeypatch):
    calls = []
    monkeypatch.setattr(mac_permissions.sys, "platform", "win32")
    monkeypatch.setattr(mac_permissions.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert mac_permissions.screen_recording_status() == "unknown"
    assert mac_permissions.request_screen_recording() is False
    mac_permissions.open_screen_recording_settings()
    assert calls == []


def test_request_and_open_settings(monkeypatch):
    asked = []
    _fake_quartz(monkeypatch, request=lambda: asked.append(1) or False)
    assert mac_permissions.request_screen_recording() is False and asked == [1]
    calls = []
    monkeypatch.setattr(mac_permissions.subprocess, "Popen", lambda args: calls.append(args))
    mac_permissions.open_screen_recording_settings()
    assert calls and calls[0][1].endswith("Privacy_ScreenCapture")


class _FakeBackend:
    def __init__(self, result, limited):
        self._r = (result, limited)

    def capture_window_ex(self, wid):
        return self._r

    def capture_window(self, wid):  # pragma: no cover - ex path preferred
        return self._r[0]


def test_capture_hwnd_flags_permission_limited(monkeypatch):
    img = Image.new("RGB", (200, 100))
    monkeypatch.setattr(screen, "_backend", _FakeBackend((img, (0, 0, 100, 50), "T"), True))
    monkeypatch.setattr(screen.sys, "platform", "darwin")
    snap = screen.capture_hwnd(1)
    assert snap is not None and snap.permission_limited is True
    assert snap.logical_size == (100, 50) and snap.pixels_per_point == 2.0


def test_capture_hwnd_none_unchanged_unless_asked_to_raise(monkeypatch):
    monkeypatch.setattr(screen, "_backend", _FakeBackend(None, True))
    assert screen.capture_hwnd(7) is None  # legacy callers: plain None
    with pytest.raises(screen.ScreenRecordingPermissionError) as ei:
        screen.capture_hwnd(7, raise_on_permission=True)
    assert ei.value.hwnd == 7
    # A failure NOT caused by permission stays None even when asked to raise.
    monkeypatch.setattr(screen, "_backend", _FakeBackend(None, False))
    assert screen.capture_hwnd(7, raise_on_permission=True) is None


def test_backend_without_ex_still_works(monkeypatch):
    img = Image.new("RGB", (10, 10))
    be = types.SimpleNamespace(capture_window=lambda wid: (img, (0, 0, 10, 10), "W"))
    monkeypatch.setattr(screen, "_backend", be)
    monkeypatch.setattr(screen.sys, "platform", "darwin")
    snap = screen.capture_hwnd(3)
    assert snap.permission_limited is False and snap.image is img


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
@pytest.mark.parametrize("status,names,expected", [
    ("granted", False, False),
    ("denied", True, True),
    ("unknown", False, True),   # window list works, every foreign name withheld
    ("unknown", True, False),
])
def test_mac_capture_window_ex_permission_verdict(monkeypatch, status, names, expected):
    from holler import _screen_mac

    bounds = {"X": 0, "Y": 0, "Width": 100, "Height": 50}
    monkeypatch.setattr(_screen_mac, "_window_info", lambda wid: {"kCGWindowBounds": bounds})
    monkeypatch.setattr(_screen_mac.Quartz, "CGWindowListCreateImage", lambda *a: object())
    img = Image.new("RGB", (200, 100))
    monkeypatch.setattr(_screen_mac, "_cgimage_to_pil", lambda cg: img)
    monkeypatch.setattr(mac_permissions, "screen_recording_status", lambda: status)
    win = {"kCGWindowOwnerPID": -1, "kCGWindowLayer": 0, "kCGWindowNumber": 5}
    if names:
        win["kCGWindowName"] = "Doc"
    monkeypatch.setattr(_screen_mac, "_on_screen_windows", lambda: [win])
    result, limited = _screen_mac.capture_window_ex(9)
    assert result is not None and result[0] is img
    assert limited is expected
    # The legacy 3-tuple surface is unchanged.
    assert _screen_mac.capture_window(9)[1] == (0, 0, 100, 50)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
def test_mac_capture_window_ex_none_image_reports_permission(monkeypatch):
    from holler import _screen_mac

    bounds = {"X": 0, "Y": 0, "Width": 100, "Height": 50}
    monkeypatch.setattr(_screen_mac, "_window_info", lambda wid: {"kCGWindowBounds": bounds})
    monkeypatch.setattr(_screen_mac.Quartz, "CGWindowListCreateImage", lambda *a: None)
    monkeypatch.setattr(mac_permissions, "screen_recording_status", lambda: "denied")
    assert _screen_mac.capture_window_ex(9) == (None, True)
    monkeypatch.setattr(_screen_mac, "_window_info", lambda wid: None)
    assert _screen_mac.capture_window_ex(9) == (None, False)


# ----- 2. stroke thickness ---------------------------------------------------

def _thickness(img: Image.Image, x: int) -> float:
    """Stroke thickness (px) at column x of a red-on-black image: summed
    red coverage, so anti-aliased fringes count fractionally."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    return float(a[:, x, 0].sum() / 255.0)


def _hline(y, x0, x1, scale=1):
    return [[(x0 * scale, y * scale), (x1 * scale, y * scale)]]


def test_legacy_width_is_snapshot_pixels():
    img = Image.new("RGB", (400, 200))
    out = compose(img, _hline(50, 20, 180, 2), "#FF0000", 4)
    assert _thickness(out, 200) == pytest.approx(4, abs=0.6)


@pytest.mark.parametrize("scale", [1, 2])
def test_stroke_scales_with_pixels_per_point(scale):
    """4pt in the overlay must be 4px at 1x and 8px at 2x (it was 4px at both)."""
    img = Image.new("RGB", (200 * scale, 100 * scale))
    out = compose(img, _hline(50, 20, 180, scale), "#FF0000", 4, logical_size=(200, 100))
    assert _thickness(out, 100 * scale) == pytest.approx(4 * scale, abs=0.6)


def test_minimum_thickness_survives_downscale():
    # 4x-dense 4000px capture, 1pt stroke (4px), downscaled 8x to 500px: ~0.5px.
    img = Image.new("RGB", (4000, 1000))
    out = compose(img, _hline(125, 50, 950, 4), "#FF0000", 1,
                  logical_size=(1000, 250), max_long_edge=500)
    small = downscale(out, 500)
    assert small.size == (500, 125)
    assert _thickness(small, 250) >= composite.MIN_OUTPUT_STROKE_PX - 0.3


def test_stroke_width_px_math():
    assert stroke_width_px(4, (400, 200)) == 4
    assert stroke_width_px(4, (400, 200), (200, 100)) == 8
    # 3000px → 1500 halves; need >= 2px out → >= 4px in.
    assert stroke_width_px(1, (3000, 1000), max_long_edge=1500) == 4
    assert stroke_width_px(8, (3000, 1000), max_long_edge=1500) == 8


def test_strokes_are_antialiased_and_round_capped():
    img = Image.new("RGB", (100, 100))
    out = compose(img, [[(20, 20), (80, 60)]], "#FF0000", 6)
    reds = set(np.asarray(out)[:, :, 0].ravel().tolist())
    assert any(0 < v < 255 for v in reds), "diagonal edge should be anti-aliased"
    # Round cap: the stroke extends ~r beyond the endpoint along the line.
    assert np.asarray(out)[20, 18, 0] > 128


def test_no_strokes_returns_snapshot_and_short_strokes_ignored():
    img = Image.new("RGB", (10, 10))
    assert compose(img, []) is img
    assert compose(img, [[(1, 1)]]) is img


def test_window_snapshot_defaults_keep_positional_construction():
    s = WindowSnapshot(Image.new("RGB", (4, 2)), (0, 0, 2, 1), "t")
    assert s.permission_limited is False and s.pixels_per_point == 2.0


# ----- 3. Esc / undo during a hold ------------------------------------------

def _listener(key="right alt"):
    ev = []
    lst = hotkey.HotkeyListener(
        key, lambda: ev.append("p"), lambda: ev.append("r"),
        on_cancel=lambda: ev.append("cancel"), on_undo=lambda: ev.append("undo"),
    )
    lst._acceptable = hotkey._acceptable_keys(hotkey._parse_pynput_key(key))
    return lst, ev


def _undo_mod():
    return Key.cmd if sys.platform == "darwin" else Key.ctrl


def test_esc_cancels_only_while_held_and_once_per_hold():
    lst, ev = _listener()
    lst._pynput_press(Key.esc)                  # not held → ignored
    lst._pynput_press(Key.alt_r)
    lst._pynput_press(Key.esc)
    lst._pynput_press(Key.esc)                  # key repeat
    lst._pynput_release(Key.alt_r)
    lst._pynput_press(Key.alt_r)                # new hold re-arms
    lst._pynput_press(Key.esc)
    assert ev == ["p", "cancel", "r", "p", "cancel"]


def test_undo_needs_modifier_and_hold():
    lst, ev = _listener()
    z = KeyCode.from_char("z")
    lst._pynput_press(Key.alt_r)
    lst._pynput_press(z)                        # bare z → nothing
    lst._pynput_press(_undo_mod())
    lst._pynput_press(z)
    lst._pynput_press(KeyCode.from_char("\x1a"))  # ctrl-char form
    lst._pynput_release(_undo_mod())
    lst._pynput_press(z)
    lst._pynput_release(Key.alt_r)
    lst._pynput_press(_undo_mod())
    lst._pynput_press(z)                        # not held → ignored
    assert ev == ["p", "undo", "undo", "r"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS keycode")
def test_undo_matches_mac_z_keycode():
    lst, ev = _listener()
    lst._pynput_press(Key.alt_r)
    lst._pynput_press(Key.cmd)
    lst._pynput_press(KeyCode(vk=6, char="Ω"))
    assert ev == ["p", "undo"]


def test_ptt_modifier_is_not_the_undo_modifier():
    key = "right cmd" if sys.platform == "darwin" else "right ctrl"
    lst, ev = _listener(key)
    ptt = Key.cmd_r if sys.platform == "darwin" else Key.ctrl_r
    lst._pynput_press(ptt)
    lst._pynput_press(KeyCode.from_char("z"))
    assert ev == ["p"]


def test_callbacks_optional_and_exceptions_swallowed():
    lst = hotkey.HotkeyListener("right alt", lambda: None, lambda: None)
    lst._acceptable = hotkey._acceptable_keys(Key.alt_r)
    lst._pynput_press(Key.alt_r)
    lst._pynput_press(Key.esc)  # no on_cancel → no error
    boom = hotkey.HotkeyListener("right alt", lambda: None, lambda: None,
                                 on_cancel=lambda: 1 / 0)
    boom._acceptable = hotkey._acceptable_keys(Key.alt_r)
    boom._pynput_press(Key.alt_r)
    boom._pynput_press(Key.esc)


def test_windows_backend_esc_and_ctrl_z(monkeypatch):
    """The blocking Esc / Ctrl+Z hook exists only while the PTT key is held:
    installed on press, removed on release (or once a key it swallowed is
    released), always removed by stop()."""
    from tests.test_hotkey_keys import _fake_keyboard_module

    kbmod = _fake_keyboard_module()
    globals_ = []
    pressed = set()

    def hook(cb, suppress=False):
        assert suppress is True  # blocking: its return value decides
        globals_.append(cb)
        return cb

    def unhook(h):
        if h in globals_:
            globals_.remove(h)
        else:
            kbmod._hooks[h]()

    kbmod.hook = hook
    kbmod.unhook = unhook
    kbmod.is_pressed = lambda n: n in pressed
    monkeypatch.setitem(sys.modules, "keyboard", kbmod)
    monkeypatch.setattr(hotkey, "_USE_KEYBOARD_LIB", True)
    ev = []
    lst = hotkey.HotkeyListener("right alt", lambda: ev.append("p"), lambda: ev.append("r"),
                                on_cancel=lambda: ev.append("cancel"),
                                on_undo=lambda: ev.append("undo"))
    lst.start()
    e = lambda t, n=None: types.SimpleNamespace(event_type=t, name=n)
    (ptt_cb,) = kbmod.store
    assert globals_ == []                       # nothing blocking while idle
    pressed.add("right alt")
    ptt_cb(e("down"))
    (any_cb,) = globals_                        # installed for the hold
    assert any_cb(e("down", "z")) is True       # no ctrl
    pressed.add("left ctrl")
    assert any_cb(e("down", "z")) is False
    assert any_cb(e("up", "z")) is False        # its key-up too
    assert any_cb(e("up", "esc")) is True       # unrelated key-up passes
    assert any_cb(e("down", "esc")) is False
    assert any_cb(e("up", "esc")) is False
    ptt_cb(e("up"))
    pressed.discard("right alt")
    assert ev == ["p", "undo", "cancel", "r"]
    assert globals_ == []                       # removed on release

    # Released while a swallowed key is still down: the hook stays for that
    # key's key-up, then removes itself (off keyboard's hook thread).
    pressed.add("right alt")
    ptt_cb(e("down"))
    (any_cb,) = globals_
    assert any_cb(e("down", "esc")) is False
    ptt_cb(e("up"))
    pressed.discard("right alt")
    assert globals_ == [any_cb]
    assert any_cb(e("up", "esc")) is False
    deadline = time.monotonic() + 2.0
    while globals_ and time.monotonic() < deadline:
        time.sleep(0.01)
    assert globals_ == []

    pressed.add("right alt")
    ptt_cb(e("down"))                           # stop() mid-hold removes it all
    assert len(globals_) == 1
    lst.stop()
    assert globals_ == [] and kbmod.store == []
    ptt_cb(e("up"))
    ptt_cb(e("down"))                           # a late event can't reinstall it
    assert globals_ == []


def test_windows_backend_without_extra_callbacks_adds_no_global_hook(monkeypatch):
    from tests.test_hotkey_keys import _fake_keyboard_module

    kbmod = _fake_keyboard_module()  # has no .hook → would raise if used
    monkeypatch.setitem(sys.modules, "keyboard", kbmod)
    monkeypatch.setattr(hotkey, "_USE_KEYBOARD_LIB", True)
    lst = hotkey.HotkeyListener("right alt", lambda: None, lambda: None)
    lst.start()
    lst.stop()


# ----- 4. polled mic level ---------------------------------------------------

def test_current_level_tracks_blocks_without_callback(monkeypatch):
    captured = {}

    class _Stream:
        def __init__(self, **kw):
            captured["cb"] = kw["callback"]

        def start(self): pass
        def stop(self): pass
        def close(self): pass

    monkeypatch.setattr("sounddevice.InputStream", lambda **kw: _Stream(**kw))
    monkeypatch.setattr(audio, "_WATCHDOG_SECONDS", 0.01)
    r = audio.Recorder()
    assert r.current_level() == 0.0
    r.start()
    captured["cb"](np.full((160, 1), 0.1, dtype=np.float32), 160, None, None)
    assert r.current_level() == pytest.approx(0.6, abs=1e-4)
    seen = []
    r.on_level = seen.append
    captured["cb"](np.full((160, 1), 0.5, dtype=np.float32), 160, None, None)
    assert seen == [1.0] and r.current_level() == 1.0
    r.stop()
    assert r.current_level() == 0.0
