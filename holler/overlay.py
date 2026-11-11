"""Frozen-snapshot overlay window.

When PTT is pressed, the caller takes a screenshot of the active window and
shows it via OverlayWindow.show_for(). The overlay is a frameless topmost
window covering the captured rect; the user draws red strokes with the mouse.
On PTT release, the caller invokes hide() and reads the strokes from
get_strokes() to feed into composite.compose().

The overlay deliberately captures input so the underlying app doesn't react to
mouse movement during the PTT hold — the snapshot is what's being annotated.

While it is up, a painted recording HUD shows a pulsing red dot, the elapsed
time, a live mic level bar, where the turn will go ("→ Continuing “Title”")
and the capture-time keys (Esc cancels, ⌘Z / Ctrl+Z removes the last stroke).
"""

from __future__ import annotations

import logging
import math
import sys
import time
from typing import List, Optional, Sequence, Tuple

from PIL import Image
from PySide6 import QtCore, QtGui, QtWidgets

from holler import iconset, theme
from holler.uifont import ui_font

log = logging.getLogger(__name__)

Point = Tuple[float, float]

# Below this distance (logical px) a mouse move doesn't add a stroke point —
# trackpads deliver sub-pixel jitter that only makes the curve wobble.
_MIN_POINT_GAP = 1.0

# Pointer slack (logical px) around the HUD card that counts as "on" it.
_DODGE_SLACK = 8


def undo_key_label() -> str:
    """The capture-time undo shortcut as the operator types it."""
    return "⌘Z" if sys.platform == "darwin" else "Ctrl+Z"


def capture_hint() -> str:
    """The HUD's key hint line."""
    return f"Esc to cancel · {undo_key_label()} to undo"


def format_elapsed(seconds: float) -> str:
    """m:ss for the HUD timer (never negative)."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _quad_point(p0: Point, c: Point, p1: Point, t: float) -> Point:
    u = 1.0 - t
    return (u * u * p0[0] + 2 * u * t * c[0] + t * t * p1[0],
            u * u * p0[1] + 2 * u * t * c[1] + t * t * p1[1])


def _mid(a: Point, b: Point) -> Point:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def smooth_stroke(points: Sequence[Point], step: float = 2.0) -> List[Point]:
    """Quadratic-midpoint smoothing of a raw mouse polyline, sampled back into
    a polyline (every ~`step` units). The curve starts at the first point,
    passes through the midpoints of consecutive input points using each input
    point as the control point, and ends at the last point — exactly the path
    paintEvent draws with quadTo, so the composited screenshot matches what
    the operator saw. Strokes of < 3 points are returned unchanged."""
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 3:
        return pts
    out: List[Point] = [pts[0]]
    start = pts[0]
    for i in range(1, len(pts) - 1):
        ctrl = pts[i]
        end = _mid(pts[i], pts[i + 1])
        chord = math.hypot(ctrl[0] - start[0], ctrl[1] - start[1]) + math.hypot(
            end[0] - ctrl[0], end[1] - ctrl[1])
        n = max(1, min(16, int(chord / max(step, 0.1))))
        for k in range(1, n + 1):
            out.append(_quad_point(start, ctrl, end, k / n))
        start = end
    out.append(pts[-1])
    return out


def _stroke_path(points: Sequence[Point], sx: float, sy: float) -> QtGui.QPainterPath:
    """The quadratic-midpoint path for one stroke, scaled by (sx, sy)."""
    path = QtGui.QPainterPath()
    pts = [QtCore.QPointF(x * sx, y * sy) for x, y in points]
    path.moveTo(pts[0])
    if len(pts) == 2:
        path.lineTo(pts[1])
        return path
    for i in range(1, len(pts) - 1):
        mid = (pts[i] + pts[i + 1]) / 2.0
        path.quadTo(pts[i], mid)
    path.lineTo(pts[-1])
    return path


def _disable_window_animation(widget: QtWidgets.QWidget) -> None:
    """Show/hide the overlay instantly. macOS animates a new panel window in
    (it grows from slightly smaller to full size); with the overlay covering
    the captured window that read as the whole screen zooming in on every
    push-to-talk press. No-op off macOS or without PyObjC."""
    if sys.platform != "darwin" or QtGui.QGuiApplication.platformName() != "cocoa":
        # Only the Cocoa platform's winId() is an NSView; under "offscreen"
        # (tests) it's an unrelated handle and dereferencing it crashes.
        return
    try:
        import AppKit
        import objc
        view = objc.objc_object(c_void_p=int(widget.winId()))
        window = view.window()
        if window is not None:
            window.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorNone)
    except Exception:
        log.debug("could not disable the overlay's window animation", exc_info=True)


class OverlayWindow(QtWidgets.QWidget):
    def __init__(self, stroke_color: str = "#FF2E2E", stroke_width: int = 4) -> None:
        super().__init__(
            None,
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        # Show/raise the overlay WITHOUT making holler the frontmost app. If the
        # overlay stole application focus, holler stayed frontmost after PTT
        # release; the next hold then saw is_self_frontmost()==True and captured
        # holler's own UI instead of the window the operator was still looking at
        # (the Superintendent evidence-chaining bug). WindowDoesNotAcceptFocus +
        # WA_ShowWithoutActivating map Qt.Tool to a macOS non-activating panel,
        # so it draws on top and receives mouse strokes without yanking focus.
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setAccessibleName("Recording — draw on the screenshot to point at things")
        self._stroke_color = QtGui.QColor(stroke_color)
        self._stroke_width = stroke_width
        # Initialize all per-PTT state up-front so set_stroke can never wipe them.
        self._active = False  # a hold is in progress (show_for ran)
        self._snapshot_size: Tuple[int, int] = (0, 0)
        # Strokes in snapshot-pixel coordinates (floats — sub-pixel precision
        # keeps the smoothed curve from stair-stepping on HiDPI captures).
        self._strokes: List[List[Point]] = []
        self._current_stroke: List[Point] = []
        self._last_widget_pt: Optional[QtCore.QPointF] = None
        self._drawing = False
        self._win_w = 1
        self._win_h = 1
        # Mic level meter (0..1) shown during recording. Updated by callers.
        self._mic_level = 0.0
        # Hold timer — seconds since show_for(). Recomputed from a monotonic
        # clock on every HUD tick (a QTimer's ticks drift late under load).
        self._hold_seconds = 0.0
        self._shown_at = 0.0
        self._hold_timer = QtCore.QTimer(self)
        self._hold_timer.setInterval(theme.HUD_TICK_MS)
        self._hold_timer.timeout.connect(self._tick_hold)
        # HUD content, set per capture by show_for().
        self._destination = ""
        self._source = ""
        self._warning = ""
        self._hints = False
        # The HUD hops to the bottom edge when the pointer reaches it, so it
        # never hides what the operator wants to circle.
        self._hud_bottom = False
        self._apply_cursor()

    def set_stroke(self, color: str, width: int) -> None:
        """Live-update stroke style. No state-mutation side effects."""
        self._stroke_color = QtGui.QColor(color)
        self._stroke_width = max(1, int(width))
        self._apply_cursor()

    def set_mic_level(self, level: float) -> None:
        """Push a 0..1 RMS reading from the audio thread (Qt-safe via Slot).

        Rises instantly, falls back smoothly, so the bar reads like a meter
        rather than flickering block to block."""
        lvl = max(0.0, min(1.0, float(level)))
        if lvl >= self._mic_level:
            self._mic_level = lvl
        else:
            self._mic_level = self._mic_level * 0.8 + lvl * 0.2
        if self.isVisible():
            self.update(self._hud_update_rect())

    def _tick_hold(self) -> None:
        # Fires every HUD_TICK_MS while the overlay is shown — only advance the
        # hold clock and repaint the HUD. (Per-PTT state is initialized once in
        # __init__ and reset in show_for(); it must NOT be wiped here, or the
        # snapshot would vanish a moment after the overlay appears.)
        self._hold_seconds = max(0.0, time.monotonic() - self._shown_at)
        self.update(self._hud_update_rect())

    def show_for(
        self,
        snapshot_image: Image.Image,
        rect: Tuple[int, int, int, int],
        *,
        destination: str = "",
        source: str = "",
        warning: str = "",
        hints: bool = False,
    ) -> None:
        """Show the frozen snapshot over `rect` and start the HUD.

        HUD extras (all optional): `destination` ("→ New session"), `source`
        (the captured window's title), `warning` (a one-line problem, e.g.
        Screen Recording is off) and `hints` (show the Esc / undo key line —
        only when the caller actually wired those keys)."""
        left, top, right, bottom = rect
        self._snapshot_size = snapshot_image.size  # (w_px, h_px)
        self._strokes = []
        self._current_stroke = []
        self._last_widget_pt = None
        self._drawing = False
        self._mic_level = 0.0
        self._hold_seconds = 0.0
        self._shown_at = time.monotonic()
        self._destination = destination or ""
        self._source = source or ""
        self._warning = warning or ""
        self._hints = bool(hints)
        self._hud_bottom = False
        self._hold_timer.start()

        # The snapshot is NOT redrawn over the screen: the operator sees the
        # live window plus the recording HUD. A frozen, dimmed copy filling the
        # window read as the screen "zooming in" (2026-10-14). Only its size is
        # kept, to map strokes back onto it.
        self._active = True

        # Place overlay over the captured rect. `rect` is already in Qt logical
        # global coordinates (holler.screen maps each platform's native rect —
        # per-screen, so mixed-DPI multi-monitor setups land on the right
        # screen at the right size). The snapshot image stays in device pixels;
        # strokes are scaled between the two via _snapshot_size / _win_w.
        logical_left = int(left)
        logical_top = int(top)
        logical_w = int(right - left)
        logical_h = int(bottom - top)
        self._win_w = max(1, logical_w)
        self._win_h = max(1, logical_h)

        self.setGeometry(logical_left, logical_top, logical_w, logical_h)
        _disable_window_animation(self)
        self.show()
        self.raise_()
        # Deliberately NOT activateWindow(): activating makes holler the frontmost
        # app and it stays frontmost after PTT release, so the next chained hold
        # mis-targets holler's own window. raise_() alone keeps it topmost.
        self.update()

    def hideEvent(self, ev: QtGui.QHideEvent) -> None:
        self._hold_timer.stop()
        super().hideEvent(ev)

    def get_strokes(self) -> List[List[Point]]:
        """Return strokes in *source-image* coordinates (matches snapshot pixels).

        Each stroke is the smoothed curve the overlay painted, sampled into a
        polyline. Includes the in-progress stroke if the user is still drawing
        — handles the case where they release PTT before releasing the mouse
        button.
        """
        finished = [smooth_stroke(s) for s in self._strokes if len(s) >= 2]
        if len(self._current_stroke) >= 2:
            finished.append(smooth_stroke(self._current_stroke))
        return finished

    def undo_last_stroke(self) -> bool:
        """Remove the stroke being drawn, else the last finished one (⌘Z /
        Ctrl+Z during the hold). Returns True when something was removed."""
        removed = False
        if self._current_stroke:
            # Abandon the in-progress stroke; further drags with the same
            # button held don't resurrect it (a new press starts a new one).
            self._current_stroke = []
            self._drawing = False
            removed = True
        elif self._strokes:
            self._strokes.pop()
            removed = True
        self._last_widget_pt = None
        if removed:
            self.update()
        return removed

    # --- cursor ---

    def _apply_cursor(self) -> None:
        """A round pen-tip cursor in the stroke colour, sized to the stroke,
        with a dark rim so it reads on light and dark content alike."""
        try:
            dpr = 2.0
            d = max(8, int(self._stroke_width) + 6)
            pm = QtGui.QPixmap(int(d * dpr), int(d * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setPen(QtGui.QPen(theme.qcolor(theme.SCRIM, 200), 1.5))
            p.setBrush(self._stroke_color)
            r = max(2.0, self._stroke_width / 2.0 + 1.0)
            c = d / 2.0
            p.drawEllipse(QtCore.QPointF(c, c), r, r)
            p.end()
            self.setCursor(QtGui.QCursor(pm, int(c), int(c)))
        except Exception:
            log.debug("pen cursor unavailable; using a crosshair", exc_info=True)
            self.setCursor(QtCore.Qt.CrossCursor)

    # --- HUD geometry ---

    def _hud_font(self, bold: bool = False) -> QtGui.QFont:
        f = ui_font(theme.FONT_MD)
        if bold:
            f.setWeight(QtGui.QFont.DemiBold)
        return f

    def _hud_rows(self) -> int:
        rows = 2  # status line + level bar
        rows += 1 if self._destination else 0
        rows += 1 if self._warning else 0
        rows += 1 if self._hints else 0
        return rows

    def _hud_metrics(self) -> Tuple[int, int, int, int, int, bool]:
        """(x, w, h, margin, level, can_dodge) for the HUD card.

        The card dodges between top and bottom, so both positions must fit
        without overlapping. `level` is how much of the card is shown: 2 =
        everything, 1 = status line + mic level bar (extras hidden), 0 = the
        single status line — the first that fits twice on this window. If
        not even the status line fits twice the HUD stays at the top
        (`can_dodge` False) instead of flipping."""
        margin = theme.SPACE_LG
        w = min(theme.HUD_WIDTH, self._win_w - 2 * margin)
        if w < theme.HUD_MIN_WIDTH:
            margin = theme.SPACE_XS
            w = max(1, self._win_w - 2 * margin)
        line = QtGui.QFontMetrics(self._hud_font()).height()
        text_rows = self._hud_rows() - 1
        heights = (
            2 * theme.SPACE_MD + line,                                         # status line
            2 * theme.SPACE_MD + line + theme.SPACE_SM + theme.HUD_LEVEL_BAR,  # + level bar
            (2 * theme.SPACE_MD + text_rows * line + theme.HUD_LEVEL_BAR
             + text_rows * theme.SPACE_SM),                                    # everything
        )

        def fits_twice(h: int) -> bool:
            # Both positions plus the pointer slack _maybe_dodge uses.
            return 2 * (h + margin) + 2 * _DODGE_SLACK <= self._win_h
        level = next((i for i in (2, 1) if fits_twice(heights[i])), 0)
        h = heights[level]
        can_dodge = fits_twice(h)
        h = min(h, max(1, self._win_h - 2 * theme.SPACE_XS))
        x = (self._win_w - w) // 2
        return int(x), int(w), int(h), int(margin), level, can_dodge

    def _hud_level(self) -> int:
        return self._hud_metrics()[4]

    def _hud_rect(self, bottom: Optional[bool] = None) -> QtCore.QRect:
        """The HUD card in widget coordinates: top-centre (or bottom-centre
        when dodging the pointer), clamped inside the window. `bottom`
        overrides the current position (used to test the other spot)."""
        x, w, h, margin, _level, can_dodge = self._hud_metrics()
        at_bottom = self._hud_bottom if bottom is None else bottom
        y = margin if not (at_bottom and can_dodge) else self._win_h - h - margin
        y = max(0, min(y, self._win_h - h))
        return QtCore.QRect(int(x), int(y), int(w), int(h))

    def _hud_update_rect(self) -> QtCore.QRect:
        return self._hud_rect().adjusted(-2, -2, 2, 2)

    # --- painting ---

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        if not self._active:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        # Effectively invisible, but not fully transparent: macOS passes clicks
        # straight through fully transparent window pixels, and the overlay
        # must still catch the mouse so the operator can draw on it.
        painter.fillRect(self.rect(), theme.qcolor(theme.SCRIM, theme.CAPTURE_HIT_ALPHA))

        pen = QtGui.QPen(self._stroke_color)
        pen.setWidthF(float(self._stroke_width))
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        sw, sh = self._snapshot_size
        if sw > 0 and sh > 0:
            scale_x = self._win_w / sw
            scale_y = self._win_h / sh
            for stroke in self._strokes + ([self._current_stroke] if self._current_stroke else []):
                if len(stroke) < 2:
                    continue
                painter.drawPath(_stroke_path(stroke, scale_x, scale_y))

        self._paint_hud(painter)
        painter.end()

    def _paint_hud(self, painter: QtGui.QPainter) -> None:
        hud = self._hud_rect()
        painter.save()
        painter.setClipRect(hud.adjusted(-1, -1, 1, 1))
        painter.setPen(QtGui.QPen(theme.qcolor(theme.BORDER), 1))
        painter.setBrush(theme.qcolor(theme.CANVAS, theme.HUD_BG_ALPHA))
        painter.drawRoundedRect(QtCore.QRectF(hud).adjusted(0.5, 0.5, -0.5, -0.5),
                                theme.RADIUS_PILL, theme.RADIUS_PILL)

        pad = theme.SPACE_MD
        inner = hud.adjusted(pad, pad, -pad, -pad)
        font = self._hud_font()
        bold = self._hud_font(bold=True)
        fm = QtGui.QFontMetrics(font)
        fmb = QtGui.QFontMetrics(bold)
        line = fm.height()
        y = inner.top()

        # ---- status line: ● Recording  <source> ............ 0:02 ----
        dot = theme.HUD_REC_DOT
        t = self._hold_seconds
        phase = 0.5 + 0.5 * math.cos(2 * math.pi * t / theme.HUD_PULSE_PERIOD_S)
        # Inset by the halo's maximum growth so the pulse stays inside the card.
        cx = inner.left() + dot / 2.0 + 3.0
        cy = y + line / 2.0
        painter.setPen(QtCore.Qt.NoPen)
        halo = dot / 2.0 + 2.0 + 3.0 * (1.0 - phase)
        painter.setBrush(theme.qcolor(theme.DANGER, int(40 + 50 * phase)))
        painter.drawEllipse(QtCore.QPointF(cx, cy), halo, halo)
        painter.setBrush(theme.qcolor(theme.DANGER, int(150 + 105 * phase)))
        painter.drawEllipse(QtCore.QPointF(cx, cy), dot / 2.0, dot / 2.0)

        timer = format_elapsed(t)
        timer_w = fmb.horizontalAdvance(timer)
        x = int(inner.left() + dot + 3 + theme.SPACE_SM)
        painter.setFont(bold)
        painter.setPen(QtGui.QColor(theme.FG))
        label = "Recording"
        painter.drawText(QtCore.QRect(x, y, inner.right() - x, line),
                         QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, label)
        x += fmb.horizontalAdvance(label) + theme.SPACE_SM
        painter.drawText(QtCore.QRect(inner.right() - timer_w, y, timer_w + 1, line),
                         QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, timer)
        if self._source:
            room = inner.right() - timer_w - theme.SPACE_SM - x
            if room > fm.horizontalAdvance("…") * 3:
                painter.setFont(font)
                painter.setPen(QtGui.QColor(theme.FG_MUTED))
                text = fm.elidedText(self._source, QtCore.Qt.ElideRight, room)
                painter.drawText(QtCore.QRect(x, y, room, line),
                                 QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)
        y += line + theme.SPACE_SM
        level = self._hud_level()  # short windows show less (see _hud_metrics)
        if level == 0:
            painter.restore()
            return

        # ---- mic level bar (tick marks the "loud" threshold) ----
        bar_h = theme.HUD_LEVEL_BAR
        bar = QtCore.QRectF(inner.left(), y, inner.width(), bar_h)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(theme.SURFACE_HOVER))
        painter.drawRoundedRect(bar, bar_h / 2.0, bar_h / 2.0)
        level = min(1.0, self._mic_level)
        fill_w = bar.width() * level
        if fill_w > 0.5:
            loud = level >= theme.HUD_LEVEL_LOUD
            painter.setBrush(QtGui.QColor(theme.DANGER if loud else theme.SUCCESS))
            painter.drawRoundedRect(QtCore.QRectF(bar.left(), bar.top(), max(bar_h, fill_w), bar_h),
                                    bar_h / 2.0, bar_h / 2.0)
        tick_x = bar.left() + bar.width() * theme.HUD_LEVEL_LOUD
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.FG_FAINT), 1))
        painter.drawLine(QtCore.QPointF(tick_x, bar.top() - 2), QtCore.QPointF(tick_x, bar.bottom() + 2))
        y += bar_h + theme.SPACE_SM
        if level == 1:
            painter.restore()
            return

        # ---- destination / warning / key hints ----
        painter.setFont(font)
        if self._destination:
            painter.setPen(QtGui.QColor(theme.ACCENT))
            text = fm.elidedText(self._destination, QtCore.Qt.ElideRight, inner.width())
            painter.drawText(QtCore.QRect(inner.left(), y, inner.width(), line),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)
            y += line + theme.SPACE_SM
        if self._warning:
            isz = min(line, theme.FONT_LG + theme.SPACE_XS)
            pm = iconset.pixmap("alert-triangle", theme.WARNING, isz, self.devicePixelRatioF() or 1.0)
            painter.drawPixmap(inner.left(), int(y + (line - isz) / 2), pm)
            tx = inner.left() + isz + theme.SPACE_XS
            painter.setPen(QtGui.QColor(theme.WARNING))
            text = fm.elidedText(self._warning, QtCore.Qt.ElideRight, inner.right() - tx)
            painter.drawText(QtCore.QRect(tx, y, inner.right() - tx, line),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)
            y += line + theme.SPACE_SM
        if self._hints:
            painter.setPen(QtGui.QColor(theme.FG_MUTED))
            text = fm.elidedText(capture_hint(), QtCore.Qt.ElideRight, inner.width())
            painter.drawText(QtCore.QRect(inner.left(), y, inner.width(), line),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)
        painter.restore()

    # --- mouse ---

    def _widget_to_source(self, x: float, y: float) -> Point:
        sw, sh = self._snapshot_size
        if self._win_w <= 0 or self._win_h <= 0 or sw <= 0 or sh <= 0:
            return (float(x), float(y))
        return (x * sw / self._win_w, y * sh / self._win_h)

    def _maybe_dodge(self, pos: QtCore.QPointF) -> None:
        """Move the HUD to the other end when the pointer reaches it — but
        only if the other spot is clear of the pointer, so it can't flip back
        and forth on every move (and repaint the whole window each time)."""
        pt = pos.toPoint()
        slack = _DODGE_SLACK
        if not self._hud_rect().adjusted(-slack, -slack, slack, slack).contains(pt):
            return
        if not self._hud_metrics()[5]:
            return  # too short to have a second spot
        other = self._hud_rect(bottom=not self._hud_bottom)
        if other.adjusted(-slack, -slack, slack, slack).contains(pt):
            return
        self._hud_bottom = not self._hud_bottom
        self.update()

    def mousePressEvent(self, ev: QtGui.QMouseEvent) -> None:
        if ev.button() != QtCore.Qt.LeftButton:
            return
        pos = ev.position()
        self._drawing = True
        self._last_widget_pt = QtCore.QPointF(pos)
        self._current_stroke = [self._widget_to_source(pos.x(), pos.y())]
        self.update()

    def mouseMoveEvent(self, ev: QtGui.QMouseEvent) -> None:
        pos = ev.position()
        self._maybe_dodge(pos)
        if not self._drawing:
            return
        last = self._last_widget_pt
        if last is not None and (abs(pos.x() - last.x()) + abs(pos.y() - last.y())) < _MIN_POINT_GAP:
            return
        self._last_widget_pt = QtCore.QPointF(pos)
        self._current_stroke.append(self._widget_to_source(pos.x(), pos.y()))
        self.update()

    def mouseReleaseEvent(self, ev: QtGui.QMouseEvent) -> None:
        if ev.button() != QtCore.Qt.LeftButton:
            return
        if self._current_stroke:
            self._strokes.append(self._current_stroke)
        self._current_stroke = []
        self._last_widget_pt = None
        self._drawing = False
        self.update()
