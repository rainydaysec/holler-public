"""Holler died on a Caps Lock press (SIGTRAP, nothing logged): pynput's macOS
listener converts NSSystemDefined events to NSEvent on its tap thread, which
macOS 26's Text Services Manager only allows on the main queue. Holler's
listeners must not subscribe to those events at all."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS event tap")


def _bit(t):
    from Quartz import CGEventMaskBit
    return CGEventMaskBit(t)


@pytest.mark.parametrize("name", ["Listener", "GlobalHotKeys"])
def test_tap_mask_excludes_system_defined_but_keeps_keys(name):
    from AppKit import NSSystemDefined
    from pynput import keyboard as kb
    from Quartz import kCGEventFlagsChanged, kCGEventKeyDown, kCGEventKeyUp

    from holler.hotkey import _main_thread_safe

    base = getattr(kb, name)
    assert base._EVENTS & _bit(NSSystemDefined), "premise: stock pynput listens to them"
    cls = _main_thread_safe(base)
    assert issubclass(cls, base)
    assert not cls._EVENTS & _bit(NSSystemDefined)
    for t in (kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged):
        assert cls._EVENTS & _bit(t)


def test_listeners_are_built_with_the_safe_mask(monkeypatch):
    """Both holler entry points build their pynput listener through the
    filter (listeners are recorded, never started)."""
    from pynput import keyboard as kb

    import holler.hotkey as hk

    wrapped = []

    class _Fake:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    def spy(cls):
        wrapped.append(cls)
        return _Fake

    monkeypatch.setattr(hk, "_main_thread_safe", spy)
    hl = hk.HotkeyListener.__new__(hk.HotkeyListener)
    hl._key, hl._listener = "right alt", None
    hl._start_pynput()
    hk.add_global_hotkey("ctrl+shift+h", lambda: None)
    assert wrapped == [kb.Listener, kb.GlobalHotKeys]
