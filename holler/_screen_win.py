"""Windows backend for active-window capture (Win32 / DWM via ctypes).

Extracted verbatim from the original single-file `screen.py` so the Windows
behavior is unchanged. `screen.py` is now a thin platform dispatcher that
imports this on `win32` and `_screen_mac` on `darwin`.

Every backend exposes the same surface:
    set_dpi_awareness() -> None
    get_foreground_window_id() -> Optional[int]
    capture_window(wid) -> Optional[(Image, (l, t, r, b), title)]   # rect in device px
    is_window_visible(wid) -> bool
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional, Tuple

from PIL import Image

log = logging.getLogger(__name__)


# DPI_AWARENESS_CONTEXT values (negative = handle).
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_dpi_awareness() -> None:
    """Mark the process per-monitor DPI v2 aware. Call once at process startup."""
    try:
        user32 = ctypes.windll.user32
        ok = user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if not ok:
            shcore = ctypes.windll.shcore
            shcore.SetProcessDpiAwareness(2)
        log.debug("set per-monitor DPI v2 awareness")
    except Exception:
        log.exception("failed to set DPI awareness; continuing")


def _get_window_rect(hwnd: int) -> Optional[Tuple[Tuple[int, int, int, int], str]]:
    """Return ((left, top, right, bottom), title) for hwnd, or None."""
    if not hwnd:
        return None
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    rect = wintypes.RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr != 0:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return (rect.left, rect.top, rect.right, rect.bottom), buf.value


def get_foreground_window_id() -> Optional[int]:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def capture_window(hwnd: int) -> Optional[Tuple[Image.Image, Tuple[int, int, int, int], str]]:
    """Capture a specific window by hwnd. Returns (image, device-px rect, title)."""
    info = _get_window_rect(hwnd)
    if info is None:
        return None
    (left, top, right, bottom), title = info
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        log.warning("hwnd %s has zero area; skipping", hwnd)
        return None

    import mss

    with mss.mss() as sct:
        bbox = {"left": left, "top": top, "width": width, "height": height}
        raw = sct.grab(bbox)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    return img, (left, top, right, bottom), title


def is_window_visible(hwnd: int) -> bool:
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return False
