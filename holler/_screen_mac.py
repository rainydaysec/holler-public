"""macOS backend for active-window capture (Quartz / Cocoa via pyobjc).

Mirrors the surface of `_screen_win.py`:
    set_dpi_awareness() -> None
    get_foreground_window_id() -> Optional[int]
    capture_window(wid) -> Optional[(Image, (l, t, r, b), title)]   # rect in points
    is_window_visible(wid) -> bool

plus the macOS-only `capture_window_ex(wid) -> (result | None, permission_limited)`
which also reports whether the Screen Recording permission is missing.

"Window id" here is a CGWindowID (an int), so the rest of holler — which was
written for Win32 HWNDs — keeps treating it as an opaque int.

`get_foreground_window_id()` deliberately returns None when holler itself is
the frontmost app, so the ForegroundTracker keeps "what the operator was last
looking at" instead of screenshotting holler's own UI. On Windows that
exclusion is done with the panel/overlay HWNDs; here a PID check is simpler
and survives windows (detail views, dialogs) created after startup.

Requires the **Screen Recording** permission (System Settings → Privacy &
Security → Screen Recording). Without it CGWindowListCreateImage returns a
desktop-wallpaper-only image or None, and CGWindowListCopyWindowInfo still
lists windows but withholds other apps' kCGWindowName. `capture_window_ex`
flags that case so the UI can explain it instead of sending wallpaper.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional, Tuple

import Quartz
from AppKit import NSBitmapImageRep, NSWorkspace
from PIL import Image

log = logging.getLogger(__name__)

# NSBitmapImageFileType.PNG — numeric constant is stable across pyobjc versions.
_NS_BITMAP_FILETYPE_PNG = 4


def set_dpi_awareness() -> None:
    """No-op on macOS — Cocoa is per-display HiDPI aware by default."""
    return


def _on_screen_windows() -> list:
    """All on-screen windows, front-to-back, excluding desktop chrome."""
    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    return list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])


def is_self_frontmost() -> bool:
    """True when holler itself is the frontmost app.

    Lets callers distinguish "the user is focused on holler" from "there's no
    capturable window at all" — get_foreground_window_id() returns None for both.
    """
    front_app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front_app is None:
        return False
    return front_app.processIdentifier() == os.getpid()


def get_foreground_window_id() -> Optional[int]:
    """CGWindowID of the frontmost normal window of the frontmost app.

    Returns None when holler itself is frontmost (so callers fall back to the
    last real window) or when no suitable window is found.
    """
    front_app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front_app is None:
        return None
    pid = front_app.processIdentifier()
    if pid == os.getpid():
        return None  # don't screenshot ourselves
    for w in _on_screen_windows():
        if w.get("kCGWindowOwnerPID") != pid:
            continue
        # Layer 0 == normal app window; menus, the Dock, status items are != 0.
        if int(w.get("kCGWindowLayer", 0) or 0) != 0:
            continue
        num = w.get("kCGWindowNumber")
        if num is None:
            continue
        # CGWindowList is ordered front-to-back, so the first match is frontmost.
        return int(num)
    return None


def _window_info(window_id: int) -> Optional[dict]:
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionIncludingWindow, int(window_id)
    )
    infos = list(infos or [])
    return infos[0] if infos else None


def _cgimage_to_pil(cgimg) -> Optional[Image.Image]:
    """Convert a CGImageRef to a PIL.Image via an NSBitmapImageRep PNG round-trip.

    Slower than poking the raw pixel buffer, but format-agnostic and robust —
    we capture once per push-to-talk, so the ~10ms PNG encode/decode is noise.
    """
    rep = NSBitmapImageRep.alloc().initWithCGImage_(cgimg)
    if rep is None:
        return None
    png = rep.representationUsingType_properties_(_NS_BITMAP_FILETYPE_PNG, None)
    if png is None:
        return None
    return Image.open(io.BytesIO(bytes(png))).convert("RGB")


def _window_names_hidden() -> bool:
    """True when the window list works but every other app's normal window has
    its kCGWindowName withheld — what macOS does without Screen Recording.

    Some windows legitimately have no title, so this only fires when there is
    at least one foreign layer-0 window and NONE of them carries a name key.
    """
    me = os.getpid()
    foreign = [
        w for w in _on_screen_windows()
        if w.get("kCGWindowOwnerPID") != me and int(w.get("kCGWindowLayer", 0) or 0) == 0
    ]
    if not foreign:
        return False
    return not any("kCGWindowName" in w for w in foreign)


def _permission_limited() -> bool:
    """Best judgement of "Screen Recording is not granted to this process".

    CGPreflightScreenCaptureAccess (never prompts) is authoritative when it is
    available; otherwise fall back to the hidden-window-names heuristic. Never
    raises — a failed check reads as "not limited" so capture is unaffected.
    """
    try:
        from holler.mac_permissions import screen_recording_status

        status = screen_recording_status()
        if status == "granted":
            return False
        if status == "denied":
            return True
        return _window_names_hidden()
    except Exception:
        log.debug("screen-recording permission check failed", exc_info=True)
        return False


CaptureTuple = Tuple[Image.Image, Tuple[int, int, int, int], str]


def capture_window_ex(window_id: int) -> Tuple[Optional[CaptureTuple], bool]:
    """`capture_window` plus a Screen Recording verdict.

    Returns (result, permission_limited). `result` is exactly what
    capture_window returns (None on failure). `permission_limited` is True when
    the Screen Recording permission is missing, in which case a non-None image
    is wallpaper/blank rather than the window's content, and a None result is
    most likely caused by the missing permission. The verdict is only computed
    when there is a window to capture (an unknown id is not a permission issue).
    """
    info = _window_info(window_id)
    if info is None:
        return None, False
    result = _capture_from_info(window_id, info)
    return result, _permission_limited()


def capture_window(window_id: int) -> Optional[CaptureTuple]:
    """Capture a specific window by CGWindowID.

    Returns (image, rect, title) where rect is in *points* — CGWindowBounds,
    which share Qt's macOS logical global coordinate space (origin top-left of
    the main display). The image is in device pixels (≈2× on Retina).

    The rect used to be scaled to "device pixels" by the window's own capture
    scale, and overlay.py then divided by the DPR of whichever screen the
    *pixel* centre landed on. With mixed-DPI displays those were different
    screens, so the overlay appeared on the wrong monitor at the wrong size.
    Points need no per-screen mapping at all.
    """
    info = _window_info(window_id)
    if info is None:
        return None
    return _capture_from_info(window_id, info)


def _capture_from_info(window_id: int, info: dict) -> Optional[CaptureTuple]:
    bounds = info.get("kCGWindowBounds") or {}
    bx = float(bounds.get("X", 0.0))
    by = float(bounds.get("Y", 0.0))
    bw = float(bounds.get("Width", 0.0))
    bh = float(bounds.get("Height", 0.0))
    if bw <= 0 or bh <= 0:
        log.warning("window %s has zero area; skipping", window_id)
        return None

    # CGWindowListCreateImage grabs the window's pixels directly — no screen
    # coordinate math, and BestResolution gives Retina-native pixels.
    cgimg = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        int(window_id),
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution,
    )
    if cgimg is None:
        log.warning("CGWindowListCreateImage returned None for window %s "
                    "(Screen Recording permission?)", window_id)
        return None
    img = _cgimage_to_pil(cgimg)
    if img is None:
        return None

    left = int(round(bx))
    top = int(round(by))
    right = left + int(round(bw))
    bottom = top + int(round(bh))
    title = info.get("kCGWindowName") or info.get("kCGWindowOwnerName") or ""
    return img, (left, top, right, bottom), str(title)


def is_window_visible(window_id: int) -> bool:
    """True if the window id is currently in the on-screen window list."""
    target = int(window_id)
    for w in _on_screen_windows():
        num = w.get("kCGWindowNumber")
        if num is not None and int(num) == target:
            return True
    return False
