"""Push-to-talk hotkey listener + global hotkey registration.

Two backends, picked by platform:
  Windows         -> the `keyboard` library (low-level WH_KEYBOARD_LL hook)
  macOS / Linux   -> `pynput` (Quartz event tap on macOS, X11 on Linux)

`HotkeyListener` gives callers clean press/release callbacks for the
push-to-talk key. Both backends fire the press event repeatedly while a key is
held (key repeat); we debounce so callbacks see exactly one press per physical
hold.

`add_global_hotkey()` registers a fire-once-on-press combo (the Sessions-window
toggle), returning a handle the caller can pass to `remove_global_hotkey()`.

macOS notes:
  * pynput's event tap needs the **Accessibility** permission
    (System Settings -> Privacy & Security -> Accessibility). Without it the
    listener starts but never receives key events.
  * "right alt" maps to the Right Option key.

Windows note: keyboard's WH_KEYBOARD_LL hook does NOT see input destined for
UAC-elevated foreground windows. Run holler elevated if you need to scribble on
elevated apps.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# `keyboard` is Windows-only here (see requirements.txt env markers); every
# other platform uses pynput.
_USE_KEYBOARD_LIB = sys.platform == "win32"

_ax_bound = False


def _ensure_ax_trusted_bound() -> None:
    """Work around a pynput/pyobjc crash on macOS.

    pynput's Quartz listener reads ``HIServices.AXIsProcessTrusted()`` from its
    background event-tap thread. On current pyobjc + Python 3.14 that symbol is
    lazily bound, and resolving it for the first time off the main thread can
    raise ``KeyError: 'AXIsProcessTrusted'`` — which kills the listener thread,
    so the push-to-talk key silently stops working.

    Fix: on the main thread, force a plain callable into the module's __dict__
    (sourced from ApplicationServices, which re-exports the same C function).
    That shadows pyobjc's lazy ``__getattr__`` so pynput's later access from the
    listener thread is a simple dict hit — no lazy resolution, no KeyError.
    Idempotent; no-op off macOS.
    """
    global _ax_bound
    if _ax_bound or sys.platform != "darwin":
        return
    _ax_bound = True
    try:
        import HIServices
    except Exception:
        log.debug("HIServices import failed; skipping AX binding", exc_info=True)
        return
    try:
        from ApplicationServices import AXIsProcessTrusted
        HIServices.AXIsProcessTrusted = AXIsProcessTrusted
        log.info("pre-bound HIServices.AXIsProcessTrusted for pynput listener")
    except Exception:
        # Fallback: warm the native lazy binding on the main thread.
        try:
            HIServices.AXIsProcessTrusted()
        except Exception:
            log.warning(
                "could not bind AXIsProcessTrusted; PTT hotkey may fail on macOS",
                exc_info=True,
            )


_keycode_ctx_bound = False


def _ensure_keycode_context_cached() -> None:
    """Work around a pynput/pyobjc segfault on macOS.

    pynput's keyboard Listener calls ``keycode_context()`` at the top of its
    background ``_run`` — TISCopyCurrentKeyboardInputSource plus wrapping the
    result as an ObjC object. Off the main thread that intermittently
    segfaults (EXC_BAD_ACCESS in ``objc_opt_self``) during startup, so a
    restart sometimes died right after "hotkey registered" with nothing in
    the log. Fix: read the layout once on the main thread and hand pynput a
    context manager that yields the cached (plain-Python) value. Idempotent;
    no-op off macOS or off the main thread.
    """
    global _keycode_ctx_bound
    if (_keycode_ctx_bound or sys.platform != "darwin"
            or threading.current_thread() is not threading.main_thread()):
        return
    _keycode_ctx_bound = True
    try:
        import contextlib

        import pynput.keyboard._darwin as kb_darwin

        with kb_darwin.keycode_context() as ctx:
            cached = ctx

        @contextlib.contextmanager
        def _cached_keycode_context():
            yield cached

        kb_darwin.keycode_context = _cached_keycode_context
        log.info("cached keyboard layout on main thread for pynput listener")
    except Exception:
        log.warning("could not cache keyboard layout; pynput will read it itself",
                    exc_info=True)


def _main_thread_safe(listener_cls):
    """`listener_cls` with system-defined events left out of its event tap.

    pynput's macOS listener turns every NSSystemDefined event (Caps Lock
    transitions, media / volume / brightness keys) into an NSEvent on its tap
    thread. On macOS 26 that reaches the Text Services Manager, which asserts
    it is on the main queue and kills the process (SIGTRAP) — holler died on
    a Caps Lock press with nothing in its log. Holler's hotkeys never need
    those events: ordinary keys come as key-down/up and modifiers (Caps Lock
    included) as flags-changed, both handled without NSEvent.
    """
    if sys.platform != "darwin":
        return listener_cls
    _ensure_keycode_context_cached()
    from Quartz import (
        CGEventMaskBit,
        kCGEventFlagsChanged,
        kCGEventKeyDown,
        kCGEventKeyUp,
    )

    return type(listener_cls.__name__, (listener_cls,), {
        "_EVENTS": (
            CGEventMaskBit(kCGEventKeyDown)
            | CGEventMaskBit(kCGEventKeyUp)
            | CGEventMaskBit(kCGEventFlagsChanged)
        ),
    })


def _active_tap_guard(listener_cls):
    """`listener_cls` hardened for an ACTIVE (intercepting) macOS event tap.

    A `darwin_intercept` makes pynput create a filtering tap: every keystroke
    in the session waits on holler's callback, and its return value decides
    whether the event survives. So:
      * an exception anywhere in pynput's handler returns the event unchanged
        (never swallowed);
      * a tap macOS disabled (slow callback / secure input) is re-enabled
        instead of silently dropping the push-to-talk key;
      * the tap is disabled and invalidated when the listener thread ends, so
        a dead listener can't stall system-wide input.
    Apply on top of `_main_thread_safe` (its event mask is inherited).
    """
    if sys.platform != "darwin":
        return listener_cls
    from Quartz import (
        CFMachPortInvalidate,
        CGEventTapEnable,
        kCGEventTapDisabledByTimeout,
        kCGEventTapDisabledByUserInput,
    )
    disabled_types = (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput)

    def _create_event_tap(self):
        tap = super(cls, self)._create_event_tap()
        self._holler_tap = tap
        return tap

    def _handler(self, proxy, event_type, event, refcon):
        if event_type in disabled_types:
            try:
                tap = getattr(self, "_holler_tap", None)
                if tap is not None and self.running:
                    CGEventTapEnable(tap, True)
            except Exception:
                pass
            return event
        try:
            return super(cls, self)._handler(proxy, event_type, event, refcon)
        except Exception:
            return event

    def _run(self):
        try:
            super(cls, self)._run()
        finally:
            tap = getattr(self, "_holler_tap", None)
            self._holler_tap = None
            if tap is not None:
                try:
                    CGEventTapEnable(tap, False)
                    CFMachPortInvalidate(tap)
                except Exception:
                    pass

    cls = type(listener_cls.__name__, (listener_cls,), {
        "_create_event_tap": _create_event_tap,
        "_handler": _handler,
        "_run": _run,
    })
    return cls


def _hold_tap_class():
    """The pynput listener class for the hold-only ACTIVE tap (macOS).

    An active tap makes macOS wait on holler's Python callback for every
    keystroke in the session, so typing everywhere lags whenever holler's
    main thread holds the GIL. The always-on push-to-talk listener therefore
    stays listen-only; this class is instantiated only while the PTT key is
    held, with `darwin_intercept` doing the swallowing. It decodes nothing
    (no on_press / on_release, no keyboard-layout context — the TIS calls
    pynput makes for that are skipped) and is hardened by
    `_active_tap_guard`. Built on the main thread so every Quartz symbol is
    resolved there, never on the tap thread.
    """
    from pynput import keyboard as kb
    from pynput._util.darwin import ListenerMixin

    base = _main_thread_safe(kb.Listener)

    def _run(self):
        ListenerMixin._run(self)  # skips Listener._run's keycode_context()

    def _handle_message(self, *_args):
        pass

    hold = type(base.__name__, (base,), {"_run": _run, "_handle_message": _handle_message})
    return _active_tap_guard(hold)


# Hold-only tap limits: a hold longer than this is treated as a missed
# release and the tap is removed; stopping a tap waits at most
# _HOLD_TAP_STOP_SECONDS for its thread; after the PTT release the tap stays
# up to _HOLD_TAP_KEYUP_GRACE seconds for key-ups of keys it swallowed.
_HOLD_TAP_MAX_SECONDS = 600.0
_HOLD_TAP_STOP_SECONDS = 2.0
_HOLD_TAP_KEYUP_GRACE = 1.0


# True while a "Record" button (Settings / first run) is capturing a new
# push-to-talk key. The global listener keeps running meanwhile, so pressing
# the CURRENT push-to-talk key to try it — or as the new key — would start a
# real capture; the runner ignores push-to-talk presses (and their releases)
# while this is set. GUI thread only.
_key_recording = False
# After recording stops, stay "recording" a moment longer: the global
# listener's press arrives on the GUI thread as a queued signal, possibly AFTER
# Qt delivered the same key to the Record button (which ended recording).
_key_recording_until = 0.0
KEY_RECORDING_GRACE_S = 1.0


def set_key_recording(active: bool, grace_s: float = 0.0) -> None:
    global _key_recording, _key_recording_until
    _key_recording = bool(active)
    _key_recording_until = (time.monotonic() + grace_s) if (not active and grace_s > 0) else 0.0


def key_recording_active() -> bool:
    return _key_recording or time.monotonic() < _key_recording_until


# --------------------------------------------------------------------------
# pynput key-spec parsing
# --------------------------------------------------------------------------

def validate_ptt_hotkey(spec: str) -> "str | None":
    """Return a user-facing error if `spec` can't be a push-to-talk key, else None.

    PTT is a single held key. A combo like "ctrl+space" used to be silently
    reduced to its last component on macOS, so a bare spacebar started a
    recording. Reject combos at the input fields instead. A lone "+" is the
    literal plus key and is allowed.
    """
    s = (spec or "").strip()
    if not s:
        return "Enter a push-to-talk key, such as “right alt” or “f12”."
    if "+" in s and s != "+":
        return (
            f"“{s}” is a key combination. The push-to-talk key must be a "
            "single key, such as “right alt” or “f12”."
        )
    return None


def _parse_pynput_key(spec: str):
    """Map a `keyboard`-style key name (e.g. "right alt", "f12") to a single
    pynput key object used as the push-to-talk trigger.

    PTT combos aren't supported. A combo (e.g. a hand-edited config) falls back
    to Right Alt rather than triggering on its last component — "ctrl+space"
    must never turn the bare spacebar into a record key.
    """
    from pynput import keyboard as kb

    spec = (spec or "").strip().lower()
    if "+" in spec and spec != "+":
        log.warning("PTT hotkey %r is a combo (unsupported); defaulting to Right Alt", spec)
        return kb.Key.alt_r

    named = {
        "right alt": kb.Key.alt_r, "ralt": kb.Key.alt_r, "alt gr": kb.Key.alt_r,
        "left alt": kb.Key.alt_l, "lalt": kb.Key.alt_l,
        "alt": kb.Key.alt, "option": kb.Key.alt,
        "right ctrl": kb.Key.ctrl_r, "rctrl": kb.Key.ctrl_r,
        "left ctrl": kb.Key.ctrl_l, "lctrl": kb.Key.ctrl_l,
        "ctrl": kb.Key.ctrl, "control": kb.Key.ctrl,
        "right shift": kb.Key.shift_r, "rshift": kb.Key.shift_r,
        "left shift": kb.Key.shift_l, "lshift": kb.Key.shift_l,
        "shift": kb.Key.shift,
        "right cmd": kb.Key.cmd_r, "right command": kb.Key.cmd_r, "right super": kb.Key.cmd_r,
        "left cmd": kb.Key.cmd_l, "left command": kb.Key.cmd_l, "left super": kb.Key.cmd_l,
        "cmd": kb.Key.cmd, "command": kb.Key.cmd, "super": kb.Key.cmd, "win": kb.Key.cmd,
        "space": kb.Key.space, "tab": kb.Key.tab, "enter": kb.Key.enter,
        "esc": kb.Key.esc, "escape": kb.Key.esc,
        "caps lock": kb.Key.caps_lock, "capslock": kb.Key.caps_lock,
    }
    if spec in named:
        return named[spec]
    # function keys f1..f20
    if spec.startswith("f") and spec[1:].isdigit():
        fk = getattr(kb.Key, spec, None)
        if fk is not None:
            return fk
    # single literal character
    if len(spec) == 1:
        return kb.KeyCode.from_char(spec)

    log.warning("unrecognized PTT hotkey %r; defaulting to Right Alt", spec)
    return kb.Key.alt_r


def _acceptable_keys(target) -> set:
    """Keys that should count as a press of `target`.

    pynput on some platforms reports the side-agnostic modifier (`Key.alt`)
    rather than `Key.alt_r`/`Key.alt_l`. To keep "right alt" working regardless,
    a side-specific target also accepts the generic modifier — but only when the
    generic key is distinct from BOTH sides. On macOS pynput defines
    `Key.alt == Key.alt_l` (same virtual keycode), so blindly adding the generic
    alias made "right alt" fire on the LEFT Option key (and "right cmd" on every
    Cmd+C).
    """
    from pynput import keyboard as kb

    out = {target}
    pairs = {
        kb.Key.alt_r: (kb.Key.alt, kb.Key.alt_l), kb.Key.alt_l: (kb.Key.alt, kb.Key.alt_r),
        kb.Key.ctrl_r: (kb.Key.ctrl, kb.Key.ctrl_l), kb.Key.ctrl_l: (kb.Key.ctrl, kb.Key.ctrl_r),
        kb.Key.shift_r: (kb.Key.shift, kb.Key.shift_l), kb.Key.shift_l: (kb.Key.shift, kb.Key.shift_r),
        kb.Key.cmd_r: (kb.Key.cmd, kb.Key.cmd_l), kb.Key.cmd_l: (kb.Key.cmd, kb.Key.cmd_r),
    }
    if target in pairs:
        generic, other_side = pairs[target]
        if generic != target and generic != other_side:
            out.add(generic)
    return out


def _key_matches(pressed, acceptable: set) -> bool:
    from pynput import keyboard as kb

    if pressed in acceptable:
        return True
    # KeyCode equality is brittle (vk vs char); compare the char explicitly.
    if isinstance(pressed, kb.KeyCode) and pressed.char:
        for a in acceptable:
            if isinstance(a, kb.KeyCode) and a.char and a.char.lower() == pressed.char.lower():
                return True
    return False


# --------------------------------------------------------------------------
# Push-to-talk listener
# --------------------------------------------------------------------------

def _is_undo_letter(key) -> bool:
    """True for the physical Z key as pynput reports it with ⌘/Ctrl held.

    With Ctrl held pynput can report the control character "\x1a" instead of
    "z" (Windows/X11), and on macOS ⌘ may change the char, so the macOS
    virtual keycode for Z (kVK_ANSI_Z = 6) is also accepted.
    """
    from pynput import keyboard as kb

    if not isinstance(key, kb.KeyCode):
        return False
    ch = key.char
    if ch is not None and ch.lower() in ("z", "\x1a"):
        return True
    return sys.platform == "darwin" and getattr(key, "vk", None) == 6


def _undo_modifier_keys() -> set:
    """The modifier that turns Z into undo: ⌘ on macOS, Ctrl elsewhere."""
    from pynput import keyboard as kb

    if sys.platform == "darwin":
        return {kb.Key.cmd, kb.Key.cmd_l, kb.Key.cmd_r}
    return {kb.Key.ctrl, kb.Key.ctrl_l, kb.Key.ctrl_r}


# macOS virtual keycodes (kVK_Escape, kVK_ANSI_Z).
_VK_ESC = 53
_VK_Z = 6

_qz_api = None


def _quartz_intercept_api():
    """(kCGEventKeyDown, kCGEventKeyUp, kCGKeyboardEventKeycode,
    kCGEventFlagMaskCommand, CGEventGetIntegerValueField, CGEventGetFlags),
    resolved once on the main thread — pyobjc binds lazily, and a first
    lookup on the tap thread is what broke AXIsProcessTrusted (see
    _ensure_ax_trusted_bound). The tap callback then only reads a global."""
    global _qz_api
    if _qz_api is None:
        from Quartz import (
            CGEventGetFlags,
            CGEventGetIntegerValueField,
            kCGEventFlagMaskCommand,
            kCGEventKeyDown,
            kCGEventKeyUp,
            kCGKeyboardEventKeycode,
        )
        _qz_api = (kCGEventKeyDown, kCGEventKeyUp, kCGKeyboardEventKeycode, kCGEventFlagMaskCommand,
                   CGEventGetIntegerValueField, CGEventGetFlags)
    return _qz_api


class HotkeyListener:
    """Calls `on_press` once per hold, `on_release` when the key is released.

    Optional capture-time callbacks, active ONLY while the PTT key is held:
      * `on_cancel()` — Esc pressed during the hold. Fires at most once per
        hold (key repeat ignored). The PTT release still arrives later and
        still fires `on_release`; the caller decides what a cancelled hold
        does with it.
      * `on_undo()` — ⌘Z (macOS) / Ctrl+Z (elsewhere) pressed during the
        hold. Fires once per Z key-down (auto-repeat repeats it, like any
        undo shortcut). A modifier that IS the PTT key doesn't count, so
        with PTT = "right cmd" a bare Z is not undo.

    While the PTT key is held, the Esc / ⌘Z / Ctrl+Z keystrokes these
    callbacks act on are consumed so they don't ALSO reach the focused app
    (Esc would interrupt a terminal agent; Ctrl+Z would undo real work) —
    holler's overlay never takes focus, so the app would otherwise see them.
    Only those keys, only while held: every other key, all modifiers and the
    PTT key itself always pass through. A swallowed key-down's matching
    key-up is swallowed too.

    The always-on listener is passive — a listen-only tap on macOS, a
    non-blocking hook on Windows. Swallowing needs an intercepting hook that
    every keystroke waits on, so it exists ONLY during a hold: macOS runs a
    second, short-lived active tap (started off the listener thread after
    on_press, stopped on release / stop() / a missed release / after
    _HOLD_TAP_MAX_SECONDS); Windows installs its blocking hook on press and
    removes it on release. An Esc pressed in the first few ms of a hold,
    before that tap is up, still cancels but may reach the app. ALL callbacks run on the
    listener's background thread — the caller must marshal to the Qt GUI
    thread (e.g. emit a Qt signal), exactly as for on_press/on_release.
    A callback that raises is logged and swallowed.
    """

    def __init__(
        self,
        key: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
        on_undo: Optional[Callable[[], None]] = None,
    ):
        self._key = key
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._on_undo = on_undo
        self._held = False
        self._cancelled = False
        self._lock = threading.Lock()
        # Undo-modifier keys currently down (pynput backend), excluding the
        # PTT key itself.
        self._mods_down: set = set()
        # keyboard-lib backend: global hook for Esc / Ctrl+Z (None if unused).
        self._extra_handle = None
        # Keys whose key-down was consumed during a hold; their key-up is
        # consumed too (macOS keycodes / keyboard-lib scan codes).
        self._swallowed: set = set()
        # pynput backend: what the PTT key itself is, so it's never consumed,
        # and its modifier flag (0 if it isn't a modifier) — a hold is only
        # trusted while the event's flags still show the key down.
        self._ptt_is_esc = False
        self._ptt_is_z = False
        self._ptt_flag = 0
        # keyboard-lib backend handles
        self._press_handle = None
        self._release_handle = None
        # pynput backend handle
        self._listener = None
        self._acceptable: set = set()
        # keyboard-lib backend: the blocking Esc / Ctrl+Z hook callback,
        # installed (as _extra_handle) only while the PTT key is held.
        self._kb_module = None
        self._kb_extra_cb = None
        self._kb_lock = threading.Lock()
        # macOS hold-only active tap. _hold_cls is None unless enabled (and
        # after stop()); a single worker thread moves the live tap
        # (_hold_tap) toward _hold_want, and exits when idle.
        self._hold_cls = None
        self._hold_cv = threading.Condition()
        self._hold_want = False
        self._hold_deadline = 0.0
        self._hold_linger_until = 0.0
        self._hold_worker: Optional[threading.Thread] = None
        self._hold_tap = None

    def start(self) -> None:
        if _USE_KEYBOARD_LIB:
            self._start_keyboard()
        else:
            self._start_pynput()
        log.info("hotkey listener started (key=%s)", self._key)

    def stop(self) -> None:
        if _USE_KEYBOARD_LIB:
            self._stop_keyboard()
        else:
            self._stop_pynput()
        log.info("hotkey listener stopped")

    # ----- keyboard lib (Windows) -----

    def _start_keyboard(self) -> None:
        import keyboard  # local import — heavy + global state

        # ONE hook for both directions. Registering on_press_key +
        # on_release_key for the same key stores both under the same key name in
        # keyboard's internal _hooks dict, so the second unhook raised KeyError
        # before removing its callback — leaking the release hook. A single
        # hook_key has a single, cleanly removable handle.
        def _on_event(event) -> None:
            if getattr(event, "event_type", None) == getattr(keyboard, "KEY_UP", "up"):
                self._handle_release(event)
            else:
                self._handle_press(event)

        self._press_handle = keyboard.hook_key(self._key, _on_event, suppress=False)
        self._release_handle = None

        if self._on_cancel is not None or self._on_undo is not None:
            # A second, global hook for Esc / Ctrl+Z. It's a *blocking* hook
            # (suppress=True): keyboard calls it synchronously and drops the
            # event when it returns False — used only for those keys while
            # the PTT key is held; everything else returns True (passes).
            # Every keystroke waits on a blocking hook, so it's installed
            # only during a hold (_kb_hold_begin / _kb_hold_end).
            with self._kb_lock:
                self._kb_module = keyboard
                self._kb_extra_cb = self._keyboard_extra_hook(keyboard)

    def _kb_hold_begin(self) -> None:
        """PTT pressed (keyboard backend): install the blocking hook."""
        with self._kb_lock:
            if self._kb_extra_cb is None or self._extra_handle is not None:
                return
            try:
                self._extra_handle = self._kb_module.hook(self._kb_extra_cb, suppress=True)
            except Exception:
                log.warning("could not install the Esc/undo hook", exc_info=True)

    def _kb_hold_end(self, force: bool = False) -> None:
        """PTT released (keyboard backend): remove the blocking hook — unless
        a key it swallowed is still down, so that key's key-up is swallowed
        too (the hook removes itself after it), or a new hold began.
        `force` (stop()) always removes it."""
        with self._kb_lock:
            if self._extra_handle is None or (not force and (self._held or self._swallowed)):
                return
            handle, self._extra_handle = self._extra_handle, None
            try:
                self._kb_module.unhook(handle)
            except (KeyError, ValueError):
                log.warning("keyboard unhook failed for Esc/undo hook", exc_info=True)

    def _keyboard_extra_hook(self, keyboard):
        """The keyboard-lib blocking hook: fires on_cancel / on_undo during a
        hold and returns False (consume) for exactly those keystrokes, True
        (pass through) for everything else — including on any exception."""
        # A Ctrl that IS the PTT key doesn't count as the undo modifier.
        ptt = (self._key or "").strip().lower()
        ctrl_names = [n for n in ("left ctrl", "right ctrl")
                      if n != ptt and ptt not in ("ctrl", "control")]
        ptt_names = {"esc", "escape"} if ptt in ("esc", "escape") else {ptt}
        key_up = getattr(keyboard, "KEY_UP", "up")

        def _ptt_down() -> bool:
            # keyboard's pressed-key table is updated synchronously, so it
            # also catches a stale _held (a missed release).
            try:
                return bool(keyboard.is_pressed(ptt))
            except Exception:
                return True

        def _on_any(event) -> bool:
            try:
                name = (getattr(event, "name", None) or "").lower()
                code = getattr(event, "scan_code", None)
                ident = code if code is not None else name
                if getattr(event, "event_type", None) == key_up:
                    if ident in self._swallowed:
                        self._swallowed.discard(ident)
                        if not self._swallowed and not self._held:
                            # the hold ended before this key-up: remove the
                            # hook now (not from inside keyboard's hook call)
                            threading.Thread(target=self._kb_hold_end, daemon=True).start()
                        return False
                    return True
                if name in ptt_names or not self._held or not _ptt_down():
                    return True  # the PTT key itself / not capturing
                if name in ("esc", "escape") and self._on_cancel is not None:
                    self._swallowed.add(ident)
                    self._handle_cancel()
                    return False
                if name == "z" and self._on_undo is not None:
                    try:
                        ctrl = any(keyboard.is_pressed(n) for n in ctrl_names)
                    except Exception:
                        ctrl = False
                    if ctrl:
                        self._swallowed.add(ident)
                        self._handle_undo()
                        return False
                return True
            except Exception:
                log.debug("Esc/undo hook failed; passing the key through", exc_info=True)
                return True

        return _on_any

    def _stop_keyboard(self) -> None:
        import keyboard

        for handle in (self._press_handle, self._release_handle):
            if handle is None:
                continue
            try:
                keyboard.unhook(handle)
            except (KeyError, ValueError):
                log.warning("keyboard unhook failed for PTT key %r", self._key, exc_info=True)
        self._press_handle = None
        self._release_handle = None
        self._kb_hold_end(force=True)
        with self._kb_lock:
            self._kb_extra_cb = None  # a late press can't reinstall it
        self._swallowed.clear()

    # ----- pynput (macOS / Linux) -----

    def _start_pynput(self) -> None:
        _ensure_ax_trusted_bound()
        from pynput import keyboard as kb

        target = _parse_pynput_key(self._key)
        self._acceptable = _acceptable_keys(target)

        # suppress=False so the key still reaches the focused app — holler is a
        # passive listener, not a key grabber. Always a listen-only tap: the
        # active tap that swallows Esc / ⌘Z runs only during a hold.
        if sys.platform == "darwin" and (
            getattr(self, "_on_cancel", None) is not None
            or getattr(self, "_on_undo", None) is not None
        ):
            self._ptt_is_esc = _key_matches(kb.Key.esc, self._acceptable)
            self._ptt_is_z = _is_undo_letter(target)
            self._ptt_flag = int(getattr(kb.Listener, "_MODIFIER_FLAGS", {}).get(target, 0) or 0)
            _quartz_intercept_api()  # import on this (main) thread, not the tap's
            hold_cls = _hold_tap_class()
            with self._hold_cv:
                self._hold_cls = hold_cls
        self._listener = _main_thread_safe(kb.Listener)(
            on_press=self._pynput_press, on_release=self._pynput_release, suppress=False,
        )
        self._listener.start()

    def _stop_pynput(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._close_hold_tap()
        self._mods_down.clear()
        self._swallowed.clear()

    # ----- macOS hold-only active tap -----

    def _set_hold_tap(self, want: bool) -> None:
        """Ask for the hold tap to run (PTT pressed) or not (released).
        Never blocks on the tap: a worker thread starts / stops it."""
        with self._hold_cv:
            if want:
                if self._hold_cls is None:
                    return  # not enabled, or the listener is stopped
                self._hold_deadline = time.monotonic() + _HOLD_TAP_MAX_SECONDS
            elif self._hold_want:
                self._hold_linger_until = time.monotonic() + _HOLD_TAP_KEYUP_GRACE
            self._hold_want = want
            if want and self._hold_worker is None:
                worker = threading.Thread(
                    target=self._hold_worker_run, name="holler-hold-tap", daemon=True)
                self._hold_worker = worker
                worker.start()
            self._hold_cv.notify_all()

    def _hold_worker_run(self) -> None:
        """The only place a hold tap is created or stopped, so there is never
        more than one. Exits (clearing _hold_worker) once idle."""
        cv = self._hold_cv
        while True:
            with cv:
                while True:
                    now = time.monotonic()
                    tap = self._hold_tap
                    if self._hold_want and tap is None:
                        action = "start"
                        break
                    if not self._hold_want and tap is not None:
                        if (self._swallowed and self._hold_cls is not None
                                and now < self._hold_linger_until):
                            # a swallowed key is still down: catch its key-up
                            cv.wait(min(0.05, self._hold_linger_until - now))
                            continue
                        action = "stop"
                        break
                    if not self._hold_want:
                        self._hold_worker = None
                        return
                    if now >= self._hold_deadline:
                        log.warning("PTT held for over %.0fs; removing the Esc/undo tap",
                                    _HOLD_TAP_MAX_SECONDS)
                        self._hold_want = False
                        continue
                    cv.wait(self._hold_deadline - now)
                cls = self._hold_cls
            if action == "start":
                tap = None
                try:
                    tap = cls(suppress=False, darwin_intercept=self._darwin_intercept)
                    tap.start()
                except Exception:
                    log.warning("could not start the Esc/undo tap", exc_info=True)
                with cv:
                    self._hold_tap = tap
                    if tap is None:
                        self._hold_want = False  # don't retry in a loop
            else:
                self._stop_tap(tap)
                with cv:
                    self._hold_tap = None

    @staticmethod
    def _stop_tap(tap) -> None:
        """Stop a pynput listener and wait (bounded) for its thread."""
        deadline = time.monotonic() + _HOLD_TAP_STOP_SECONDS
        # stop() is a no-op until the listener thread has marked itself
        # running — and the thread would then run forever.
        while tap.is_alive() and not tap.running and time.monotonic() < deadline:
            time.sleep(0.002)
        try:
            tap.stop()
            tap.join(max(0.0, deadline - time.monotonic()))
        except Exception:
            log.debug("Esc/undo tap stop raised", exc_info=True)
        if tap.is_alive():
            log.warning("Esc/undo tap thread did not stop within %.1fs", _HOLD_TAP_STOP_SECONDS)

    def _close_hold_tap(self) -> None:
        """stop(): remove any hold tap and refuse new ones; bounded wait."""
        with self._hold_cv:
            self._hold_cls = None
            self._hold_want = False
            worker = self._hold_worker
            self._hold_cv.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(2 * _HOLD_TAP_STOP_SECONDS + 1.0)

    def _darwin_intercept(self, event_type, event):
        """The hold tap's `darwin_intercept`: runs on its tap thread for every
        key event, before the listen-only PTT listener sees it. Returns None to
        consume the event (firing on_cancel / on_undo for a consumed key-down,
        as the PTT listener never sees it), else the event unchanged — always
        unchanged on error."""
        try:
            down, up, keycode_field, cmd_mask, get_field, get_flags = _quartz_intercept_api()
            flags = int(get_flags(event))
            if self._ptt_flag and not (flags & self._ptt_flag):
                # The PTT modifier is up: its release was seen (or missed).
                self._set_hold_tap(False)
            if event_type == down:
                is_down = True
            elif event_type == up:
                is_down = False
            else:
                return event  # flags-changed (modifiers) always pass
            keycode = int(get_field(event, keycode_field))
            if self._swallow_decision(is_down, keycode, flags, cmd_mask):
                if is_down:
                    if keycode == _VK_ESC:
                        self._handle_cancel()
                    else:
                        self._handle_undo()
                return None
        except Exception:
            pass
        return event

    def _swallow_decision(self, is_down: bool, keycode: int, flags: int, cmd_mask: int) -> bool:
        """True = consume this key event (macOS keycodes). Esc on key-down
        while held (unless Esc is the PTT key); Z on key-down while held with
        a non-PTT ⌘ down (unless Z is the PTT key); and the key-up of any
        key-down consumed here. Everything else passes."""
        if not is_down:
            if keycode in self._swallowed:
                self._swallowed.discard(keycode)
                return True
            return False
        if not self._held:
            return False
        if self._ptt_flag and not (flags & self._ptt_flag):
            return False  # stale hold (a missed release): the PTT isn't down
        if keycode == _VK_ESC:
            take = self._on_cancel is not None and not self._ptt_is_esc
        elif keycode == _VK_Z:
            take = (self._on_undo is not None and not self._ptt_is_z
                    and bool(self._mods_down) and bool(flags & cmd_mask))
        else:
            take = False
        if take:
            self._swallowed.add(keycode)
        return take

    def _pynput_press(self, key) -> None:
        if _key_matches(key, self._acceptable):
            self._handle_press(key)
            return
        if self._on_cancel is None and self._on_undo is None:
            return
        if key in _undo_modifier_keys():
            self._mods_down.add(key)
            return
        from pynput import keyboard as kb

        if key == kb.Key.esc:
            self._handle_cancel()
        elif self._mods_down and _is_undo_letter(key):
            self._handle_undo()

    def _pynput_release(self, key) -> None:
        if _key_matches(key, self._acceptable):
            self._handle_release(key)
            return
        self._mods_down.discard(key)

    # ----- shared debounce -----

    def _handle_press(self, _event) -> None:
        with self._lock:
            if self._held:
                return
            self._held = True
            self._cancelled = False
        try:
            self._on_press()
        except Exception:
            log.exception("on_press handler raised")
        self._hold_begin()

    def _handle_release(self, _event) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
        self._hold_end()
        try:
            self._on_release()
        except Exception:
            log.exception("on_release handler raised")

    def _hold_begin(self) -> None:
        """After on_press: arm Esc / ⌘Z swallowing for this hold (no-op unless
        enabled by start()). Never raises into the listener."""
        try:
            self._kb_hold_begin()
            self._set_hold_tap(True)
        except Exception:
            log.exception("could not arm Esc/undo for this hold")

    def _hold_end(self) -> None:
        try:
            self._kb_hold_end()
            self._set_hold_tap(False)
        except Exception:
            log.exception("could not disarm Esc/undo after this hold")

    def _handle_cancel(self) -> None:
        cb = self._on_cancel
        if cb is None:
            return
        with self._lock:
            if not self._held or self._cancelled:
                return
            self._cancelled = True
        try:
            cb()
        except Exception:
            log.exception("on_cancel handler raised")

    def _handle_undo(self) -> None:
        cb = self._on_undo
        if cb is None:
            return
        with self._lock:
            if not self._held:
                return
        try:
            cb()
        except Exception:
            log.exception("on_undo handler raised")


# --------------------------------------------------------------------------
# Global (fire-on-press) hotkey — the Sessions-window toggle
# --------------------------------------------------------------------------

def _to_pynput_combo(combo: str) -> str:
    """Convert a `keyboard`-style combo ("ctrl+shift+h") to pynput's
    GlobalHotKeys syntax ("<ctrl>+<shift>+h")."""
    mods = {
        "ctrl": "<ctrl>", "control": "<ctrl>",
        "shift": "<shift>",
        "alt": "<alt>", "option": "<alt>",
        "cmd": "<cmd>", "command": "<cmd>", "super": "<cmd>", "win": "<cmd>",
    }
    out = []
    for raw in combo.split("+"):
        p = raw.strip().lower()
        if not p:
            continue
        if p in mods:
            out.append(mods[p])
        elif len(p) == 1:
            out.append(p)
        else:
            out.append(f"<{p}>")
    return "+".join(out)


_HINT_MODS = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "super": "cmd", "win": "cmd",
}
_HINT_MAC_GLYPHS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
_HINT_ORDER = ("ctrl", "alt", "shift", "cmd")  # Apple's menu order: ⌃⌥⇧⌘


def global_hotkey_hint(combo: str, platform: Optional[str] = None) -> str:
    """Display form of a global-hotkey combo as `add_global_hotkey` registers
    it — LITERAL modifiers: "ctrl" is the Control key on every platform (pynput
    / keyboard don't remap it to ⌘ the way Qt shortcuts do). macOS gets menu
    glyphs ("ctrl+shift+h" → "⌃⇧H"), elsewhere "Ctrl+Shift+H"."""
    plat = platform or sys.platform
    mods: set[str] = set()
    keys: list[str] = []
    for raw in (combo or "").split("+"):
        p = raw.strip().lower()
        if not p:
            continue
        if p in _HINT_MODS:
            mods.add(_HINT_MODS[p])
        else:
            keys.append(p.upper() if len(p) == 1 else p[:1].upper() + p[1:])
    ordered = [m for m in _HINT_ORDER if m in mods]
    if plat == "darwin":
        return "".join(_HINT_MAC_GLYPHS[m] for m in ordered) + "".join(keys)
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift",
             "cmd": "Win" if plat == "win32" else "Super"}
    return "+".join([names[m] for m in ordered] + keys)


def add_global_hotkey(combo: str, callback: Callable[[], None]) -> Optional[object]:
    """Register a global fire-on-press hotkey. Returns a handle for
    `remove_global_hotkey()` (None on the Windows/keyboard backend, which
    manages its hooks internally)."""
    if _USE_KEYBOARD_LIB:
        import keyboard
        keyboard.add_hotkey(combo, callback)
        return None

    from pynput import keyboard as kb
    spec = _to_pynput_combo(combo)
    listener = _main_thread_safe(kb.GlobalHotKeys)({spec: callback})
    listener.start()
    log.info("global hotkey registered: %s (%s)", combo, spec)
    return listener


def remove_global_hotkey(handle: Optional[object]) -> None:
    """Tear down a hotkey registered via `add_global_hotkey()`."""
    if handle is None:
        return
    try:
        handle.stop()  # pynput GlobalHotKeys
    except Exception:
        pass
