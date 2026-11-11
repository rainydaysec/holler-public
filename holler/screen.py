"""Active-window screenshot + foreground tracking.

Returns a PIL.Image of a target window plus its rect in Qt *logical* global
coordinates (what QWidget.setGeometry takes), so the overlay can be placed with
no further math. The actual OS calls live in per-platform backends:

    Windows  ->  holler._screen_win   (Win32 / DWM via ctypes)
    macOS    ->  holler._screen_mac   (Quartz / Cocoa via pyobjc)

This module is the platform-agnostic surface: `WindowSnapshot`, `downscale`,
the `ForegroundTracker`, and the `capture_*` helpers callers already use. A
"window id" is an opaque int — a Win32 HWND on Windows, a CGWindowID on macOS.
The historical `hwnd` naming is kept so the rest of holler is untouched.

We expose `ForegroundTracker` so callers can capture "what the user was last
looking at" instead of "what's focused at PTT moment". When the user clicks
holler's own panel to interact with it, the panel becomes foreground — without
the tracker we'd screenshot ourselves.
"""

from __future__ import annotations

import io
import logging
import sys
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from PIL import Image
from PySide6 import QtCore, QtGui, QtWidgets

log = logging.getLogger(__name__)


# ----- platform backend selection -------------------------------------------

if sys.platform == "win32":
    from holler import _screen_win as _backend
elif sys.platform == "darwin":
    from holler import _screen_mac as _backend
else:  # pragma: no cover - unsupported platform, degrade gracefully
    _backend = None
    log.warning("no screen-capture backend for platform %r — capture disabled", sys.platform)


class ScreenRecordingPermissionError(RuntimeError):
    """Raised by `capture_hwnd(..., raise_on_permission=True)` when nothing could
    be captured and the macOS Screen Recording permission is the likely cause.
    `.hwnd` is the window id that was being captured."""

    def __init__(self, hwnd: int) -> None:
        super().__init__(f"Screen Recording permission missing (window {hwnd})")
        self.hwnd = hwnd


@dataclass
class WindowSnapshot:
    image: Image.Image
    # Qt logical-coordinate rect on the virtual desktop: (left, top, right, bottom).
    # The image itself is in device pixels (its size may be rect size × DPR).
    rect: Tuple[int, int, int, int]
    title: str
    # macOS: True when the Screen Recording permission is missing, so `image`
    # is wallpaper / blank rather than the window's content. Always False on
    # other platforms and for capture_self_window() (Qt grab needs no TCC).
    permission_limited: bool = False

    @property
    def logical_size(self) -> Tuple[int, int]:
        """(width, height) of `rect` in Qt logical points — the size the
        overlay was shown at. Pass to composite.compose(logical_size=...)."""
        left, top, right, bottom = self.rect
        return (max(1, int(right - left)), max(1, int(bottom - top)))

    @property
    def pixels_per_point(self) -> float:
        """Snapshot device pixels per logical point (≈2.0 on Retina)."""
        lw, lh = self.logical_size
        w, h = self.image.size
        return ((w / lw) + (h / lh)) / 2.0


def set_dpi_awareness() -> None:
    """Mark the process DPI-aware. Call once at process startup.

    No-op on platforms that don't need it (macOS Cocoa is HiDPI-aware already).
    """
    if _backend is not None:
        _backend.set_dpi_awareness()


def get_foreground_hwnd() -> Optional[int]:
    """Return the foreground window's id, or None.

    On macOS this returns None when holler itself is frontmost (so the tracker
    keeps the last real window). On Windows it returns the raw HWND and the
    tracker's exclude-set does the self-filtering.
    """
    if _backend is None:
        return None
    return _backend.get_foreground_window_id()


def is_self_frontmost() -> bool:
    """True when one of holler's own windows is the frontmost app.

    Used to give the user feedback that capture fell back to their last real
    window because they were focused on holler. Backends that don't implement
    this (Windows) report False — the tracker's exclude-set handles self there.
    """
    if _backend is None:
        return False
    fn = getattr(_backend, "is_self_frontmost", None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def capture_hwnd(hwnd: int, *, raise_on_permission: bool = False) -> Optional[WindowSnapshot]:
    """Capture a specific window by id.

    Returns None when the window can't be captured. On macOS the returned
    snapshot's `permission_limited` is True when Screen Recording is not
    granted (the image is then wallpaper / blank). With
    `raise_on_permission=True`, a capture that yields nothing because of the
    missing permission raises ScreenRecordingPermissionError instead of
    returning None; the default keeps the plain None. GUI thread (as before).
    """
    if _backend is None:
        return None
    capture_ex = getattr(_backend, "capture_window_ex", None)
    if capture_ex is not None:
        result, permission_limited = capture_ex(hwnd)
    else:
        result, permission_limited = _backend.capture_window(hwnd), False
    if result is None:
        if permission_limited and raise_on_permission:
            raise ScreenRecordingPermissionError(hwnd)
        return None
    image, rect, title = result
    if sys.platform == "win32":
        # The Win32 backend reports native (physical) virtual-desktop pixels.
        rect = native_rect_to_logical(rect, _qt_screens())
    # macOS: the backend already reports CG points, which ARE Qt's logical
    # global coordinates on macOS.
    if permission_limited:
        log.warning("captured window %s without Screen Recording permission "
                    "(image is wallpaper/blank)", hwnd)
    return WindowSnapshot(image=image, rect=rect, title=title,
                          permission_limited=bool(permission_limited))


ScreenInfo = Tuple[Tuple[int, int, int, int], float]  # ((x, y, w, h) logical, dpr)


def _qt_screens() -> list:
    """[(logical geometry (x, y, w, h), devicePixelRatio)], primary first."""
    app = QtGui.QGuiApplication.instance()
    if app is None:
        return []
    out = []
    primary = QtGui.QGuiApplication.primaryScreen()
    screens = list(QtGui.QGuiApplication.screens())
    if primary is not None and primary in screens:
        screens.remove(primary)
        screens.insert(0, primary)
    for s in screens:
        g = s.geometry()
        out.append(((g.x(), g.y(), g.width(), g.height()), float(s.devicePixelRatio() or 1.0)))
    return out


def native_rect_to_logical(
    rect: Tuple[int, int, int, int], screens: "list[ScreenInfo]"
) -> Tuple[int, int, int, int]:
    """Map a Windows native-pixel virtual-desktop rect to Qt logical coords.

    Qt 6 high-DPI scaling keeps each screen's top-left at its NATIVE position
    and scales only relative to that origin: logical = origin + (native -
    origin) / dpr, where origin == screen.geometry().topLeft(). So a screen's
    native extent is (x, y, w*dpr, h*dpr). Dividing global native coords by a
    DPR (the old code) is only right for a screen whose origin is (0, 0).

    The screen is the one whose native extent contains the rect's centre;
    fallback is the primary (first) screen. On a single monitor (origin 0,0)
    this reduces exactly to round(coord / dpr).
    """
    left, top, right, bottom = rect
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    chosen = None
    for (x, y, w, h), dpr in screens:
        if x <= cx < x + w * dpr and y <= cy < y + h * dpr:
            chosen = ((x, y), dpr)
            break
    if chosen is None:
        chosen = ((screens[0][0][0], screens[0][0][1]), screens[0][1]) if screens else ((0, 0), 1.0)
    (ox, oy), dpr = chosen
    dpr = dpr or 1.0
    l = ox + int(round((left - ox) / dpr))
    t = oy + int(round((top - oy) / dpr))
    w = int(round((right - left) / dpr))
    h = int(round((bottom - top) / dpr))
    return (l, t, l + w, t + h)


def capture_active_window() -> Optional[WindowSnapshot]:
    """Grab the foreground window. Returns None if no foreground window exists.

    Prefer the ForegroundTracker-based path in full app — this is the simple,
    no-tracker fallback (used by phase test runners).
    """
    hwnd = get_foreground_hwnd()
    if hwnd is None:
        return None
    return capture_hwnd(hwnd)


def _qimage_to_pil(qimg: QtGui.QImage) -> Optional[Image.Image]:
    """Convert a QImage to a PIL.Image via an in-memory PNG round-trip.

    Format-agnostic and robust — we capture once per push-to-talk, so the PNG
    encode/decode cost is noise, exactly as in the macOS backend.
    """
    if qimg.isNull():
        return None
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.OpenModeFlag.ReadWrite)
    if not qimg.save(buf, "PNG"):
        return None
    data = bytes(buf.data())
    buf.close()
    return Image.open(io.BytesIO(data)).convert("RGB")


def capture_self_window() -> Optional[WindowSnapshot]:
    """Capture holler's own active top-level window via Qt's `QWidget.grab()`.

    Unlike the OS window-capture path this renders the widget's own pixels, so
    it needs NO Screen Recording (TCC) permission and always sees holler even
    though the backend deliberately excludes holler's PID from OS capture. Used
    when the operator focuses holler to talk about its own UI.

    Must be called on the Qt GUI thread (it is — the PTT press slot runs there).
    Returns None if there's no visible holler window to grab.
    """
    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    widget = app.activeWindow()
    if widget is None:
        # No focused top-level (e.g. only the frameless panel is up) — grab the
        # frontmost visible holler top-level window instead.
        visible = [w for w in app.topLevelWidgets() if w.isVisible() and w.width() > 0]
        widget = visible[0] if visible else None
    if widget is None:
        return None

    pixmap = widget.grab()
    if pixmap.isNull():
        return None
    image = _qimage_to_pil(pixmap.toImage())
    if image is None:
        return None

    # Rect in Qt logical global coordinates, matching the OS-capture
    # convention (overlay.py places the overlay at exactly this rect).
    dpr = float(pixmap.devicePixelRatio() or 1.0)
    origin = widget.mapToGlobal(QtCore.QPoint(0, 0))
    left = int(origin.x())
    top = int(origin.y())
    rect = (left, top, left + int(round(image.width / dpr)), top + int(round(image.height / dpr)))
    title = widget.windowTitle() or "holler"
    return WindowSnapshot(image=image, rect=rect, title=str(title))


def downscale(image: Image.Image, max_long_edge: int) -> Image.Image:
    """Resize so the long edge is at most max_long_edge (preserving aspect)."""
    w, h = image.size
    long_edge = max(w, h)
    if long_edge <= max_long_edge:
        return image
    scale = max_long_edge / long_edge
    new = (int(round(w * scale)), int(round(h * scale)))
    return image.resize(new, Image.LANCZOS)


def _is_window_visible(hwnd: int) -> bool:
    if _backend is None:
        return False
    try:
        return _backend.is_window_visible(hwnd)
    except Exception:
        return False


class ForegroundTracker(QtCore.QObject):
    """Polls the foreground window so we remember what the user was looking at.

    Holler's own panel/overlay/toast windows are excluded — when the user
    clicks holler to interact with it, we keep the previous "real" foreground.
    On Windows the exclusion is by HWND (passed to `set_exclude`); on macOS the
    backend already skips holler's own process, so the exclude set is a
    harmless no-op there.
    """

    def __init__(self, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._exclude: set[int] = set()
        self._last_hwnd: Optional[int] = None
        self._timer = QtCore.QTimer(self)
        # 400ms is plenty for "last app the operator was looking at" tracking
        # while costing ~half the GUI-thread wakeups of the previous 150ms.
        # Capture latency at PTT is unchanged — target_hwnd() reads the
        # foreground hwnd live, not the polled cache.
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._poll)

    def set_exclude(self, hwnds: Iterable[int]) -> None:
        self._exclude = {int(h) for h in hwnds if h}

    def start(self) -> None:
        cur = get_foreground_hwnd()
        if cur is not None and cur not in self._exclude:
            self._last_hwnd = cur
        self._timer.start()
        log.info("foreground tracker started (excluding %d hwnds)", len(self._exclude))

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        cur = get_foreground_hwnd()
        if cur is None or cur in self._exclude:
            return
        if not _is_window_visible(cur):
            return
        self._last_hwnd = cur

    def target_hwnd(self) -> Optional[int]:
        """Best hwnd to capture: current foreground if non-self, else last-seen."""
        cur = get_foreground_hwnd()
        if cur is not None and cur not in self._exclude:
            return cur
        return self._last_hwnd
