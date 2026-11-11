"""macOS privacy (TCC) permission helpers: Microphone, Accessibility, Screen Recording.

Microphone: on macOS the input stream can fail to open — or worse, open but deliver pure
silence — when the app that launched holler hasn't been granted the Microphone
privacy permission. PortAudio surfaces a denial as PaError -9986, which is
indistinguishable from other internal errors without asking the OS directly.

These helpers query and request that permission via AVFoundation. We load the
framework dynamically through pyobjc-core (already a dependency via
pyobjc-framework-Cocoa) so we don't need the heavier
pyobjc-framework-AVFoundation wheel.

Everything here is a no-op / "unknown" on non-macOS platforms so callers can
invoke it unconditionally.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

# AVAuthorizationStatus values (AVFoundation).
NOT_DETERMINED = 0
RESTRICTED = 1
DENIED = 2
AUTHORIZED = 3

_STATUS_NAMES = {
    NOT_DETERMINED: "notDetermined",
    RESTRICTED: "restricted",
    DENIED: "denied",
    AUTHORIZED: "authorized",
}

# AVMediaTypeAudio is the four-char code 'soun'.
_MEDIA_TYPE_AUDIO = "soun"

_av_capture_device = None
_av_load_attempted = False


def _load_avcapture():
    """Dynamically resolve AVCaptureDevice, or None if unavailable."""
    global _av_capture_device, _av_load_attempted
    if _av_load_attempted:
        return _av_capture_device
    _av_load_attempted = True
    if sys.platform != "darwin":
        return None
    try:
        import objc

        objc.loadBundle(
            "AVFoundation",
            globals(),
            bundle_path="/System/Library/Frameworks/AVFoundation.framework",
        )
        _av_capture_device = objc.lookUpClass("AVCaptureDevice")
    except Exception:
        log.exception("could not load AVFoundation for mic permission check")
        _av_capture_device = None
    return _av_capture_device


def is_macos() -> bool:
    return sys.platform == "darwin"


def mic_authorization_status() -> Optional[int]:
    """Current Microphone permission status, or None if it can't be determined
    (non-macOS, or AVFoundation unavailable)."""
    dev = _load_avcapture()
    if dev is None:
        return None
    try:
        return int(dev.authorizationStatusForMediaType_(_MEDIA_TYPE_AUDIO))
    except Exception:
        log.exception("authorizationStatusForMediaType_ failed")
        return None


def status_name(status: Optional[int]) -> str:
    if status is None:
        return "unknown"
    return _STATUS_NAMES.get(status, f"status={status}")


def is_denied() -> bool:
    return mic_authorization_status() in (DENIED, RESTRICTED)


def request_mic_access(callback: Optional[Callable[[bool], None]] = None) -> None:
    """Trigger the OS Microphone permission prompt (only shows when status is
    notDetermined). `callback(granted: bool)` fires on a background thread when
    the user responds. Safe no-op off macOS."""
    dev = _load_avcapture()
    if dev is None:
        if callback is not None:
            callback(False)
        return

    def _handler(granted) -> None:
        if callback is not None:
            try:
                callback(bool(granted))
            except Exception:
                log.exception("mic access callback raised")

    try:
        dev.requestAccessForMediaType_completionHandler_(_MEDIA_TYPE_AUDIO, _handler)
    except Exception:
        log.exception("requestAccessForMediaType_ failed")
        if callback is not None:
            threading.Thread(target=lambda: _handler(False), daemon=True).start()


def open_mic_privacy_settings() -> None:
    """Open System Settings on the Microphone privacy pane."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
            ]
        )
    except Exception:
        log.exception("could not open Microphone privacy settings")


# --- Accessibility (TCC) — required for the global PTT hotkey via pynput ---

_ax_funcs = None


def _load_ax():
    """Resolve the AXIsProcessTrusted[WithOptions] functions, or (None, ...)."""
    global _ax_funcs
    if _ax_funcs is not None:
        return _ax_funcs
    if sys.platform != "darwin":
        _ax_funcs = (None, None, None)
        return _ax_funcs
    try:
        from ApplicationServices import (
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
        _ax_funcs = (AXIsProcessTrusted, AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt)
    except Exception:
        log.exception("could not load Accessibility (AX) functions")
        _ax_funcs = (None, None, None)
    return _ax_funcs


def accessibility_trusted() -> bool:
    """True when this process is trusted for Accessibility (event monitoring).
    Returns True off macOS so callers can branch unconditionally."""
    if sys.platform != "darwin":
        return True
    fn, _, _ = _load_ax()
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        log.exception("AXIsProcessTrusted failed")
        return False


def request_accessibility() -> bool:
    """Prompt for Accessibility access. Unlike AXIsProcessTrusted(), the
    *WithOptions* + prompt variant registers this app in the Accessibility list
    and shows the system dialog (with an Open System Settings button), so the
    user can actually toggle it on. The grant still requires a relaunch to take
    effect. Returns the current trust state. No-op off macOS."""
    if sys.platform != "darwin":
        return True
    _, fn_opts, prompt_key = _load_ax()
    if fn_opts is None or prompt_key is None:
        return False
    try:
        return bool(fn_opts({prompt_key: True}))
    except Exception:
        log.exception("AXIsProcessTrustedWithOptions failed")
        return False


def open_accessibility_settings() -> None:
    """Open System Settings on the Accessibility privacy pane."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ]
        )
    except Exception:
        log.exception("could not open Accessibility settings")


# --- Screen Recording (TCC) — required to capture other apps' windows ---

def _load_screen_capture_funcs():
    """(CGPreflightScreenCaptureAccess, CGRequestScreenCaptureAccess), or Nones.

    Resolved on each call (pyobjc caches the module), so tests can monkeypatch
    `Quartz`. Both exist on macOS 10.15+.
    """
    if sys.platform != "darwin":
        return None, None
    try:
        import Quartz
    except Exception:
        log.exception("could not import Quartz for Screen Recording check")
        return None, None
    return (
        getattr(Quartz, "CGPreflightScreenCaptureAccess", None),
        getattr(Quartz, "CGRequestScreenCaptureAccess", None),
    )


def screen_recording_status() -> str:
    """Screen Recording permission: "granted" | "denied" | "unknown".

    Uses CGPreflightScreenCaptureAccess, which NEVER shows a prompt, so it is
    safe to call at any time (e.g. on every PTT or when painting a settings
    row). The API can't tell "never asked" from "refused" — both read
    "denied". "unknown" off macOS or if the call is unavailable/fails.

    Note: macOS caches the grant per process; a permission toggled on in
    System Settings generally only takes effect after holler is relaunched,
    and this keeps reporting "denied" until then. Cheap; any thread.
    """
    preflight, _ = _load_screen_capture_funcs()
    if preflight is None:
        return "unknown"
    try:
        return "granted" if bool(preflight()) else "denied"
    except Exception:
        log.exception("CGPreflightScreenCaptureAccess failed")
        return "unknown"


def request_screen_recording() -> bool:
    """Ask for Screen Recording access (CGRequestScreenCaptureAccess).

    The first call for a process that has never been asked shows the system
    dialog and adds holler to the Screen Recording list; later calls do not
    prompt again (send the user to `open_screen_recording_settings()`
    instead). Returns the current grant (False until relaunch, even if the
    user approves). Returns False off macOS / when unavailable. Call from the
    main thread.
    """
    _, request = _load_screen_capture_funcs()
    if request is None:
        return False
    try:
        return bool(request())
    except Exception:
        log.exception("CGRequestScreenCaptureAccess failed")
        return False


def open_screen_recording_settings() -> None:
    """Open System Settings on the Screen Recording privacy pane. No-op off macOS."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
            ]
        )
    except Exception:
        log.exception("could not open Screen Recording settings")
