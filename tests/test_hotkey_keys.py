"""PTT key matching / parsing / Windows unhook (E1, E7, E14)."""

from __future__ import annotations

import sys
import types

from pynput.keyboard import Key, KeyCode

from holler import hotkey
from holler.hotkey import _acceptable_keys, _key_matches, _parse_pynput_key, validate_ptt_hotkey


def test_right_alt_does_not_accept_left_alt():
    acc = _acceptable_keys(_parse_pynput_key("right alt"))
    assert _key_matches(Key.alt_r, acc)
    assert not _key_matches(Key.alt_l, acc), "Left Option must not trigger 'right alt'"


def test_right_side_modifiers_never_accept_left_side():
    for right, left in ((Key.alt_r, Key.alt_l), (Key.ctrl_r, Key.ctrl_l),
                        (Key.shift_r, Key.shift_l), (Key.cmd_r, Key.cmd_l)):
        assert not _key_matches(left, _acceptable_keys(right)), right
        assert not _key_matches(right, _acceptable_keys(left)), left


def test_generic_alias_added_only_when_distinct_from_both_sides(monkeypatch):
    """On platforms where Key.alt is its own keycode, the alias still helps."""
    from pynput import keyboard as kb

    class FakeKey:
        alt, alt_l, alt_r = "ALT", "ALT_L", "ALT_R"
        ctrl, ctrl_l, ctrl_r = "C", "C_L", "C_R"
        shift, shift_l, shift_r = "S", "S_L", "S_R"
        cmd, cmd_l, cmd_r = "M", "M_L", "M_R"

    monkeypatch.setattr(kb, "Key", FakeKey)
    assert _acceptable_keys("ALT_R") == {"ALT_R", "ALT"}

    FakeKey.alt = "ALT_L"  # macOS: generic == left side
    assert _acceptable_keys("ALT_R") == {"ALT_R"}


def test_combo_ptt_never_becomes_bare_last_key():
    assert _parse_pynput_key("ctrl+space") == Key.alt_r
    assert _parse_pynput_key("+") == KeyCode.from_char("+")


def test_validate_ptt_hotkey():
    assert validate_ptt_hotkey("right alt") is None
    assert validate_ptt_hotkey("f12") is None
    assert validate_ptt_hotkey("+") is None
    assert "single key" in validate_ptt_hotkey("ctrl+space")
    assert validate_ptt_hotkey("  ") is not None


def _fake_keyboard_module():
    """Mimics the real `keyboard` lib's _hooks bookkeeping (hook_key/unhook)."""
    mod = types.ModuleType("keyboard")
    mod.KEY_DOWN, mod.KEY_UP = "down", "up"
    mod._hooks = {}
    mod.store = []  # registered callbacks

    def hook_key(key, callback, suppress=False):
        mod.store.append(callback)

        def remove_():
            del mod._hooks[callback]
            del mod._hooks[key]
            del mod._hooks[remove_]
            mod.store.remove(callback)

        mod._hooks[callback] = mod._hooks[key] = mod._hooks[remove_] = remove_
        return remove_

    mod.hook_key = hook_key
    mod.on_press_key = lambda k, cb, suppress=False: hook_key(k, lambda e: e.event_type == "up" or cb(e))
    mod.on_release_key = lambda k, cb, suppress=False: hook_key(k, lambda e: e.event_type == "down" or cb(e))
    mod.unhook = lambda remove: mod._hooks[remove]()
    return mod


def test_windows_backend_press_release_and_clean_unhook(monkeypatch):
    kbmod = _fake_keyboard_module()
    monkeypatch.setitem(sys.modules, "keyboard", kbmod)
    monkeypatch.setattr(hotkey, "_USE_KEYBOARD_LIB", True)
    events = []
    lst = hotkey.HotkeyListener("right alt", lambda: events.append("p"), lambda: events.append("r"))
    lst.start()
    ev = lambda t: types.SimpleNamespace(event_type=t)
    for cb in list(kbmod.store):
        cb(ev("down")); cb(ev("down")); cb(ev("up"))
    assert events == ["p", "r"]
    lst.stop()
    assert kbmod.store == [], "a PTT hook leaked after stop()"
    assert kbmod._hooks == {}
