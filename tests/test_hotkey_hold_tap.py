"""The PTT listener stays a listen-only macOS tap; Esc / ⌘Z are swallowed by a
second, ACTIVE tap that exists only while the PTT key is held. An always-on
active tap made every keystroke in every app wait on holler's Python
callback, so typing lagged whenever holler's main thread held the GIL."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from holler import hotkey as hk

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS event tap")


def _wait(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.002)
    return True


class _FakeTap(threading.Thread):
    """Stands in for the hold-tap pynput listener: a thread that runs until
    stop(), counting how many are alive at once."""

    lock = threading.Lock()
    live = 0
    peak = 0
    made: list = []
    start_delay = 0.0

    def __init__(self, **kwargs):
        super().__init__(daemon=True)
        self.kwargs = kwargs
        self._stop_evt = threading.Event()
        self._running = False
        _FakeTap.made.append(self)

    @classmethod
    def reset(cls):
        cls.live = cls.peak = 0
        cls.made = []
        cls.start_delay = 0.0

    @property
    def running(self):
        return self._running

    def start(self):
        time.sleep(_FakeTap.start_delay)
        super().start()

    def run(self):
        with _FakeTap.lock:
            _FakeTap.live += 1
            _FakeTap.peak = max(_FakeTap.peak, _FakeTap.live)
        self._running = True
        self._stop_evt.wait()
        with _FakeTap.lock:
            _FakeTap.live -= 1

    def stop(self):
        self._running = False
        self._stop_evt.set()


class _FakeMain:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def fake_listener(monkeypatch):
    """A started HotkeyListener (pynput path) whose main listener and hold
    tap are fakes — no real event tap is created."""
    _FakeTap.reset()
    mains = []
    monkeypatch.setattr(hk, "_main_thread_safe",
                        lambda cls: (lambda **k: mains.append(_FakeMain(**k)) or mains[-1]))
    monkeypatch.setattr(hk, "_hold_tap_class", lambda: _FakeTap)
    base = set(threading.enumerate())
    calls = []
    hl = hk.HotkeyListener("right alt", lambda: calls.append("p"), lambda: calls.append("r"),
                           on_cancel=lambda: calls.append("cancel"),
                           on_undo=lambda: calls.append("undo"))
    hl._start_pynput()
    if sys.platform != "darwin":  # the hold tap is macOS-only; drive it anyway
        hl._hold_cls = _FakeTap
    hl.mains, hl.calls = mains, calls
    yield hl
    hl._stop_pynput()
    for t in _FakeTap.made:
        t.stop()
    assert _wait(lambda: set(threading.enumerate()) <= base)


def _idle(hl):
    return _wait(lambda: hl._hold_worker is None and _FakeTap.live == 0)


def test_no_intercept_without_a_hold(fake_listener):
    hl = fake_listener
    (main,) = hl.mains
    assert "darwin_intercept" not in main.kwargs and main.kwargs["suppress"] is False
    time.sleep(0.05)
    assert _FakeTap.made == [] and hl._hold_worker is None and hl._hold_tap is None


def test_hold_starts_an_intercepting_tap_and_release_stops_it(fake_listener):
    hl = fake_listener
    hl._handle_press(None)
    assert _wait(lambda: _FakeTap.live == 1)
    (tap,) = _FakeTap.made
    assert tap.kwargs == {"suppress": False, "darwin_intercept": hl._darwin_intercept}
    assert hl._hold_tap is tap
    hl._handle_release(None)
    assert _idle(hl) and not tap.is_alive() and hl._hold_tap is None
    assert hl.calls == ["p", "r"]


def test_rapid_press_release_cycles_leak_nothing(fake_listener):
    hl = fake_listener
    for i in range(50):
        hl._handle_press(None)
        if i % 3 == 0:
            time.sleep(0.001)
        hl._handle_release(None)
        if i % 7 == 0:
            _wait(lambda: _FakeTap.live == 0)
    assert _idle(hl)
    assert _FakeTap.peak <= 1 and not any(t.is_alive() for t in _FakeTap.made)
    assert hl._hold_tap is None
    assert hl.calls == ["p", "r"] * 50


def test_press_and_release_never_wait_for_the_tap(fake_listener):
    hl = fake_listener
    _FakeTap.start_delay = 0.4  # a slow tap start
    t0 = time.monotonic()
    hl._handle_press(None)
    hl._handle_release(None)
    hl._handle_press(None)
    assert time.monotonic() - t0 < 0.1
    assert _wait(lambda: _FakeTap.live == 1)
    hl._handle_release(None)
    assert _idle(hl) and _FakeTap.peak == 1


def test_stop_mid_hold_removes_the_tap_and_refuses_new_ones(fake_listener):
    hl = fake_listener
    hl._handle_press(None)
    assert _wait(lambda: _FakeTap.live == 1)
    t0 = time.monotonic()
    hl._stop_pynput()
    assert time.monotonic() - t0 < 1.0
    assert _FakeTap.live == 0 and hl._hold_worker is None
    hl._handle_release(None)
    hl._handle_press(None)  # a late event after stop()
    time.sleep(0.05)
    assert len(_FakeTap.made) == 1 and hl._hold_worker is None


def test_overlong_hold_removes_the_tap(fake_listener, monkeypatch):
    monkeypatch.setattr(hk, "_HOLD_TAP_MAX_SECONDS", 0.1)
    hl = fake_listener
    hl._handle_press(None)
    assert _wait(lambda: _FakeTap.live == 1)
    assert _idle(hl) and hl._held  # the release never came
    hl._handle_release(None)


def test_tap_lingers_for_a_swallowed_keys_key_up(fake_listener):
    hl = fake_listener
    hl._handle_press(None)
    assert _wait(lambda: _FakeTap.live == 1)
    hl._swallowed.add(hk._VK_ESC)  # Esc swallowed, still down
    hl._handle_release(None)
    time.sleep(0.15)
    assert _FakeTap.live == 1
    hl._swallowed.discard(hk._VK_ESC)  # its key-up was swallowed
    assert _idle(hl)


@darwin_only
def test_missed_release_seen_in_event_flags_removes_the_tap(fake_listener):
    import Quartz as Q

    from tests.test_capture_moment_review import _ev

    hl = fake_listener
    assert hl._ptt_flag == Q.kCGEventFlagMaskAlternate
    hl._handle_press(None)
    assert _wait(lambda: _FakeTap.live == 1)
    a = _ev(0, True, Q.kCGEventFlagMaskAlternate)
    assert hl._darwin_intercept(Q.kCGEventKeyDown, a) is a
    time.sleep(0.05)
    assert _FakeTap.live == 1  # PTT still down per the flags
    b = _ev(0, True, 0)  # PTT up, release missed
    assert hl._darwin_intercept(Q.kCGEventKeyDown, b) is b
    assert _idle(hl) and hl._held


@darwin_only
def test_swallowed_keys_fire_their_callbacks_from_the_hold_tap(fake_listener):
    """A key the hold tap swallows never reaches the PTT listener, so the
    intercept fires on_cancel / on_undo itself."""
    import Quartz as Q
    from pynput import keyboard as kb

    from tests.test_capture_moment_review import _ev

    hl = fake_listener
    alt, cmd = Q.kCGEventFlagMaskAlternate, Q.kCGEventFlagMaskCommand
    hl._handle_press(None)
    assert hl._darwin_intercept(Q.kCGEventKeyDown, _ev(53, True, alt)) is None
    assert hl._darwin_intercept(Q.kCGEventKeyDown, _ev(53, True, alt)) is None  # once
    hl._pynput_press(kb.Key.cmd)
    assert hl._darwin_intercept(Q.kCGEventKeyDown, _ev(6, True, alt | cmd)) is None
    assert hl._darwin_intercept(Q.kCGEventKeyUp, _ev(6, False, alt | cmd)) is None
    assert hl.calls == ["p", "cancel", "undo"]
    hl._handle_release(None)


# ── real listeners on this Mac (no events are injected) ────────────────────

def _own_taps():
    """{eventTapID: options} for this process's event taps (0 = active,
    1 = listen-only)."""
    import Quartz as Q

    err, taps, n = Q.CGGetEventTapList(256, None, None)
    assert err == 0
    return {t.eventTapID: t.options for t in (taps or [])[:n]
            if t.tappingProcess == os.getpid()}


@darwin_only
def test_real_listener_is_listen_only_and_hold_tap_is_transient():
    import Quartz as Q
    from AppKit import NSSystemDefined

    base_threads = set(threading.enumerate())
    base_taps = _own_taps()
    hl = hk.HotkeyListener("right alt", lambda: None, lambda: None,
                           on_cancel=lambda: None, on_undo=lambda: None)
    hl.start()
    pl = hl._listener
    try:
        pl.wait()
        if not pl.IS_TRUSTED:
            pytest.skip("this process isn't trusted for Accessibility")
        assert pl.running and pl.is_alive() and pl._intercept is None
        assert not hasattr(pl, "_holler_tap")  # not the active-tap guard
        assert _wait(lambda: len(_own_taps()) == len(base_taps) + 1)
        new = {k: v for k, v in _own_taps().items() if k not in base_taps}
        assert list(new.values()) == [Q.kCGEventTapOptionListenOnly]

        for _ in range(3):  # a few holds through the internal API
            hl._handle_press(None)
            assert _wait(lambda: getattr(hl._hold_tap, "_holler_tap", None) is not None)
            ht = hl._hold_tap
            assert ht.running and ht._intercept == hl._darwin_intercept
            assert Q.CGEventTapIsEnabled(ht._holler_tap)
            assert not type(ht)._EVENTS & Q.CGEventMaskBit(NSSystemDefined)
            options = sorted(v for k, v in _own_taps().items() if k not in base_taps)
            assert options == [Q.kCGEventTapOptionDefault, Q.kCGEventTapOptionListenOnly]
            hl._handle_release(None)
            assert _wait(lambda: hl._hold_worker is None)
            assert not ht.is_alive() and ht._holler_tap is None
            assert _wait(lambda: len(_own_taps()) == len(base_taps) + 1)

        hl._handle_press(None)  # stop() mid-hold
        assert _wait(lambda: hl._hold_tap is not None)
        ht = hl._hold_tap
    finally:
        t0 = time.monotonic()
        hl.stop()
        assert time.monotonic() - t0 < 2 * hk._HOLD_TAP_STOP_SECONDS + 1
    pl.join(3.0)
    assert not pl.is_alive() and not ht.is_alive()
    # No active tap survives. (Stock pynput never invalidates its own tap on
    # stop, so the PTT listener's listen-only one may stay registered.)
    assert _wait(lambda: sorted(v for k, v in _own_taps().items() if k not in base_taps)
                 in ([], [Q.kCGEventTapOptionListenOnly]))
    assert _wait(lambda: set(threading.enumerate()) == base_threads)
