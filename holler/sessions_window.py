"""Two-pane Sessions window — left rail of session rows, right pane of detail.

Opened via `Ctrl+Shift+H` or the panel's `⋯ → Open Sessions window` menu item.
Not always-on-top; the user works with it like any other window. PTT while it's
foreground deliberately captures it (that's the point — talk about a session
while looking at the right pane).
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional


def html_escape(s: str) -> str:
    return html.escape(s or "")


from PySide6 import QtCore, QtGui, QtWidgets

from holler import iconset
from holler import state as _state
from holler import theme
from holler.agents_panel import PANE_DEFAULT_W, AgentsPanel
from holler.detail import DetailView, _live_group_ids
from holler.domain import Session
from holler.jobs import JobStore
from holler.model_prefix import format_model_label
from holler.profiles import ProfileSpec, builtin_dir, ensure_user_dir, get_profile, list_profiles, user_dir
from holler.quitting import quit_app
from holler.uifont import ui_font

log = logging.getLogger("holler.sessions_window")


_BG = theme.CANVAS
_BG_ROW = theme.SURFACE
_BG_ROW_HOVER = theme.SURFACE_HOVER
_BG_ROW_SELECTED = theme.SELECTION_BG  # translucent accent-emphasis (rgba, never 8-digit hex)
_FG = theme.FG
_FG_DIM = theme.FG_MUTED
_FG_FAINT = theme.FG_FAINT  # one shade dimmer than _FG_DIM, used for stale rows
_ACCENT = theme.ACCENT
_RED = theme.DANGER
_GREEN = theme.SUCCESS
_GREEN_DIM = theme.SUCCESS_DIM
_YELLOW = theme.WARNING
_PIN = theme.PIN

# ─── Holler brand tokens ────────────────────────────────────────────────────
# "HOLLER" reads as a CB-radio call sign / megaphone — voice-first PTT to an
# AI. Brand identity leans warm + confident vs the cold-grey default of every
# other dev tool. Amber = the wordmark + the signature stripe under it; the
# blue _ACCENT stays in service of state (selection, focus rings) so the
# brand color isn't diluted by being used for everything.
_BRAND_AMBER = theme.BRAND_AMBER        # primary brand color — wordmark + stripe
_BRAND_AMBER_SOFT = theme.BRAND_AMBER_SOFT   # darker amber for tints/borders
_BRAND_AMBER_GLOW = theme.BRAND_AMBER_GLOW  # 13% amber for subtle bg washes
_HEADER_BG = theme.HEADER          # warmer-by-a-hair than _BG, anchors header
_HEADER_BORDER = theme.BORDER_SUBTLE      # divider under the header bar
# Stuck-job threshold — a running job stuck on the same tool for this many
# seconds gets a yellow `⏱` badge in its row meta line.
_STUCK_SECONDS = 120

# Recency buckets — boundaries in seconds since last activity. Matched in
# order; first match wins. Header label is shown above the first row in
# each bucket. Sessions older than the last bound fall into "OLDER".
# Empty-dashboard table cell padding.
_DASH_PAD_V = theme.SPACE_SM
_DASH_PAD_H = theme.SPACE_MD

_BUCKETS = [
    ("TODAY", 24 * 3600),
    ("YESTERDAY", 48 * 3600),
    ("THIS WEEK", 7 * 86400),
    ("THIS MONTH", 30 * 86400),
    ("OLDER", float("inf")),
]


_QSS = f"""
QMainWindow {{ background: {_BG}; color: {_FG}; }}
QWidget {{ background: {_BG}; color: {_FG}; }}
QLineEdit#searchBox {{ background: {_BG_ROW}; color: {_FG}; padding: {theme.SPACE_SM}px;
                        border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; font-size: {theme.FONT_SM}pt; }}
QLineEdit#searchBox:focus {{ border-color: {_ACCENT}; }}
QPushButton.chip {{ background: {_BG_ROW}; color: {_FG_DIM}; border: 1px solid {theme.BORDER};
                    padding: 0 {theme.SPACE_SM}px; border-radius: {theme.RADIUS_PILL}px; font-size: {theme.FONT_XS}pt;
                    {theme.PILL_BOX} }}
QPushButton.chip[active="true"] {{ background: {_BG_ROW_SELECTED}; color: {_FG}; border-color: {_ACCENT};
                                   font-weight: {theme.WEIGHT_SEMIBOLD}; }}
QPushButton.chip:hover {{ color: {_FG}; border-color: {theme.BORDER_STRONG}; }}
QPushButton.chip:pressed {{ background: {_BG_ROW_HOVER}; }}
QPushButton.chip:focus {{ border-color: {_ACCENT}; }}
QListWidget {{ background: {_BG}; color: {_FG}; border: none; outline: none; }}
QListWidget::item {{ padding: 0; margin: 0; border-radius: {theme.RADIUS_MD}px; }}
QListWidget::item:hover {{ background: {_BG_ROW}; }}
QListWidget::item:selected {{ background: {_BG_ROW_SELECTED}; }}
QSplitter::handle:horizontal {{ background: {theme.BORDER_SUBTLE}; width: 8px; margin: 0;
                                border-left: 3px solid {_BG}; border-right: 3px solid {_BG}; }}
QSplitter::handle:horizontal:hover {{ background: {_ACCENT}; }}
QSplitter::handle:horizontal:pressed {{ background: {_ACCENT}; }}
""" + theme.menu_qss()


def _color_for_profile(name: str) -> str:
    """Stable HSL hash per profile name — same profile always gets the same
    color across runs. Saturation/lightness pinned so no profile lands too
    bright (clashes with the accent) or too dark (invisible on the row bg).

    Hues are drawn from 55°–345° only: the red band (345°–15°) reads as an
    error and the amber band (15°–55°) is reserved for the brand (BRAND.md).

    Returns a hex color. "No profile" returns the row background (transparent
    effect — no stripe).
    """
    if not name:
        return _BG_ROW
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)
    hue = _PROFILE_HUE_MIN + h % (_PROFILE_HUE_MAX - _PROFILE_HUE_MIN)
    color = QtGui.QColor.fromHsl(hue, 180, 130)
    return color.name()


# Profile hue window (degrees): everything outside it is red or brand amber.
_PROFILE_HUE_MIN = 55
_PROFILE_HUE_MAX = 345


def _key_hint(portable: str) -> str:
    """Platform-correct shortcut text for hand-written hints: "Ctrl+N" →
    "⌘N" on macOS (Qt maps Ctrl to Cmd there), "Ctrl+N" elsewhere."""
    return QtGui.QKeySequence(portable).toString(QtGui.QKeySequence.NativeText)


# Link schemes in the empty-state dashboard (handled by
# SessionsWindow._on_dashboard_anchor; DetailView ignores unknown schemes).
_DASH_SESSION_SCHEME = "holler-session"
_DASH_ACTION_SCHEME = "holler-action"

_ICON_IMG_CACHE: dict[tuple, str] = {}


def _icon_img(name: str, color: str, size: int) -> str:
    """An iconset icon as an inline <img> (PNG data URI, rendered at 2× so it
    stays crisp on Retina) for QTextDocument HTML."""
    key = (name, color, size)
    tag = _ICON_IMG_CACHE.get(key)
    if tag is None:
        pm = iconset.pixmap(name, color, size, 2.0)
        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba)
        buf.open(QtCore.QIODevice.WriteOnly)
        pm.toImage().save(buf, "PNG")
        buf.close()
        b64 = base64.b64encode(bytes(ba)).decode("ascii")
        tag = (f"<img src='data:image/png;base64,{b64}' width='{size}' height='{size}' "
               f"style='vertical-align: middle;'>")
        _ICON_IMG_CACHE[key] = tag
    return tag


def _html_to_plain(html: str) -> str:
    """Rich-text tooltip → plain text, for an accessible description (the
    tooltip itself is only reachable by mouse hover)."""
    if not html:
        return ""
    doc = QtGui.QTextDocument()
    doc.setHtml(html)
    return " ".join(doc.toPlainText().split())


def _truncate(s: str, limit: int) -> str:
    """`limit` chars max; ellipsis if cut."""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


class HollerWordmark(QtWidgets.QWidget):
    """Brand wordmark: ascending-bars 'shout' glyph + 'HOLLER' set in tracked
    uppercase + a 2px amber underline that runs the width of the wordmark.

    Painted by hand (no font dependency, no SVG) so the brand reads identically
    on every machine. The glyph is three vertical bars stepping up — a stripped-
    down speaker-output / megaphone sound visualizer — and is rendered in the
    same amber as the wordmark fill. The underline is the visual signature: it
    repeats below the wordmark whenever Holler asserts itself in the UI (header,
    splash, about box).
    """

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._text = "HOLLER"
        self.setAccessibleName("Holler")
        self._bars = 3
        self._font = ui_font(theme.FONT_WORDMARK)
        self._font.setWeight(QtGui.QFont.Black)
        self._font.setLetterSpacing(QtGui.QFont.PercentageSpacing, theme.TRACKING_BRAND_PCT)
        self.setFixedHeight(34)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        self._recalc_width()

    def _recalc_width(self) -> None:
        fm = QtGui.QFontMetrics(self._font)
        text_w = fm.horizontalAdvance(self._text)
        glyph_w = 22  # 3 bars + spacing
        gap = 9
        self.setFixedWidth(glyph_w + gap + text_w + 2)

    def paintEvent(self, _e: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        amber = QtGui.QColor(_BRAND_AMBER)
        amber_soft = QtGui.QColor(_BRAND_AMBER_SOFT)

        # ── Glyph: 3 ascending bars (6/11/16 px tall) — abstract "shout"/EQ.
        # Tops are rounded (1.5 px radius) so the silhouette reads acoustic
        # rather than digital-signal/wifi.
        bar_w = 4
        bar_gap = 3
        bar_x0 = 0
        baseline_y = 24
        bar_heights = [6, 11, 16]
        for i, h in enumerate(bar_heights):
            x = bar_x0 + i * (bar_w + bar_gap)
            y = baseline_y - h
            # Subtle tonal step — front bar full amber, back bars slightly muted
            # so the glyph reads as having depth without a second hue.
            if i == 0:
                color = QtGui.QColor(_BRAND_AMBER_SOFT).lighter(140)
            elif i == 1:
                color = QtGui.QColor(_BRAND_AMBER).darker(115)
            else:
                color = amber
            rect = QtCore.QRectF(x, y, bar_w, h)
            r = theme.RADIUS_GLYPH_BAR
            path = QtGui.QPainterPath()
            path.moveTo(rect.bottomLeft())
            path.lineTo(rect.left(), rect.top() + r)
            path.quadTo(rect.left(), rect.top(), rect.left() + r, rect.top())
            path.lineTo(rect.right() - r, rect.top())
            path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + r)
            path.lineTo(rect.bottomRight())
            path.closeSubpath()
            p.fillPath(path, color)

        # ── Wordmark
        text_x = bar_x0 + 3 * bar_w + 2 * bar_gap + 9
        p.setFont(self._font)
        p.setPen(amber)
        fm = QtGui.QFontMetrics(self._font)
        text_y = 22
        p.drawText(QtCore.QPointF(text_x, text_y), self._text)

        # ── Signature underline: 2px amber stripe below the wordmark, full
        # wordmark width minus a small inset on the right so it doesn't feel
        # cut off. Slight transparency to keep it from overpowering.
        text_w = fm.horizontalAdvance(self._text)
        stripe = QtGui.QColor(_BRAND_AMBER)
        stripe.setAlphaF(0.85)
        p.fillRect(
            QtCore.QRectF(text_x, text_y + 5, text_w - 4, 2),
            stripe,
        )

        p.end()


class _ElideRightLabel(QtWidgets.QLabel):
    """One-line QLabel that ends in "…" when it's narrower than its text
    (a stock QLabel clips mid-word) and then carries the full text in its
    tooltip. ``text()`` / ``setText()`` speak the full text."""

    def __init__(self, text: str = "", parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        # Take whatever width the layout leaves; never push the buttons out.
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt name)
        self._full_text = text or ""
        # Screen readers read QLabel's own (elided) text otherwise.
        self.setAccessibleName(self._full_text)
        self._apply_elide()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._apply_elide()

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.StyleChange):
            self._apply_elide()

    def _apply_elide(self) -> None:
        full = self._full_text
        shown = self.fontMetrics().elidedText(full, QtCore.Qt.ElideRight, max(0, self.contentsRect().width()))
        if QtWidgets.QLabel.text(self) != shown:
            super().setText(shown)
        tip = full if shown != full else ""
        if self.toolTip() != tip:
            self.setToolTip(tip)


class ElidedLabel(QtWidgets.QLabel):
    """QLabel that wraps to at most `max_lines` and elides the final line with
    "…" instead of clipping when the widget is too narrow for the full text.

    Qt's stock QLabel just clips — bad for the session rail where the narrower
    it gets the fewer glyphs survive. Single-line elide (max_lines=1) still
    chopped most titles to a few characters; allowing two lines lets a title
    stay readable when the operator keeps the rail narrow. We re-flow on every
    resize and on `setFullText()`. Height is pinned to `max_lines` so rows keep
    a predictable size regardless of rail width — or, with ``fit=True``, to the
    number of lines the text actually uses at the current width (1…max_lines),
    so a short title doesn't reserve an empty second line."""

    def __init__(
        self,
        parent: Optional[QtWidgets.QWidget] = None,
        *,
        max_lines: int = 2,
        fit: bool = False,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._max_lines = max(1, max_lines)
        self._fit = fit
        self._lines = 1 if fit else self._max_lines
        self.setWordWrap(True)
        # We compute width-based eliding ourselves, so collapse the natural
        # size hint to "as narrow as our parent layout will give us."
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setMinimumWidth(0)
        self.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.setFixedHeight(self._lines * self.fontMetrics().lineSpacing())

    def setFullText(self, text: str) -> None:
        self._full_text = text or ""
        self._apply_elide()

    def fullText(self) -> str:
        return self._full_text

    def lines(self) -> int:
        """Lines the label is currently sized for."""
        return self._lines

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._apply_elide()

    def changeEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(event)
        # The fixed height was computed from the font at construction time —
        # before the window stylesheet (font-size) / UI scale applied. Recompute
        # whenever the effective font changes so two-line titles aren't clipped.
        if event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.StyleChange):
            self._pin_height()
            self._apply_elide()

    def _pin_height(self) -> None:
        h = self._lines * self.fontMetrics().lineSpacing()
        if h != self.height() or self.minimumHeight() != h:
            self.setFixedHeight(h)

    def _set_lines(self, n: int) -> None:
        if self._fit and n != self._lines:
            self._lines = n
            self._pin_height()

    def _apply_elide(self) -> None:
        avail = max(0, self.width() - 2)
        text = self._full_text
        if avail <= 0 or not text:
            if not text:
                self._set_lines(1)
            self._show_text(text)
            return
        # Lay the text out at the current width to discover its line breaks.
        layout = QtGui.QTextLayout(text, self.font())
        layout.beginLayout()
        line_starts: list[int] = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(avail)
            line_starts.append(line.textStart())
        layout.endLayout()
        self._set_lines(max(1, min(self._max_lines, len(line_starts))))
        if len(line_starts) <= self._max_lines:
            self._show_text(text)  # fits within the allowed lines — show it all
            return
        # Overflows: keep the whole-line prefix, then elide the final visible
        # line so the "…" lands at the end of line `max_lines`.
        tail_start = line_starts[self._max_lines - 1]
        fm = self.fontMetrics()
        head = text[:tail_start]
        elided_tail = fm.elidedText(text[tail_start:], QtCore.Qt.ElideRight, avail)
        self._show_text(head + elided_tail)

    def _show_text(self, shown: str) -> None:
        """Display the (possibly elided) text. Subclasses may decorate it."""
        super().setText(shown)


class _StatusMark(QtWidgets.QWidget):
    """Leading status glyph of a rail row, painted (no stylesheet churn).

    States: ``running`` (accent dot inside a rotating ring and a pulsing halo —
    `set_phase` only repaints this widget), ``unread`` (accent dot: a reply you haven't marked
    Done), ``error`` (danger triangle), ``done`` (muted check) and ``idle``
    (nothing). The glyph is centred on the first line of `line_ref` (the row
    title), whatever height the row ends up."""

    def __init__(self, line_ref: QtWidgets.QWidget, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._line_ref = line_ref
        self._state = "idle"
        self._phase = 0
        self.setFixedWidth(theme.ROW_ICON)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)

    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.update()

    def set_phase(self, phase: int) -> None:
        self._phase = phase
        if self._state == "running":
            self.update()

    def paintEvent(self, _e: QtGui.QPaintEvent) -> None:  # noqa: N802 (Qt name)
        state = self._state
        if state == "idle":
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        cy = self._line_ref.fontMetrics().lineSpacing() / 2.0
        cx = self.width() / 2.0
        if state in ("running", "unread"):
            r = theme.ROW_DOT / 2.0
            p.setPen(QtCore.Qt.NoPen)
            if state == "running":
                ring_r = r + theme.SPACE_2XS
                halo = theme.RUN_HALO_ALPHAS[self._phase % len(theme.RUN_HALO_ALPHAS)]
                p.setBrush(theme.qcolor(_ACCENT, halo))
                p.drawEllipse(QtCore.QPointF(cx, cy), ring_r + 1.0, ring_r + 1.0)
                # Three-quarter ring whose gap rotates a quarter-turn per pulse
                # phase. The second shape is what separates `running` from
                # `unread` when the eye catches a single frame.
                pen = QtGui.QPen(theme.qcolor(_ACCENT), theme.RUN_RING_W)
                pen.setCapStyle(QtCore.Qt.RoundCap)
                p.setPen(pen)
                p.setBrush(QtCore.Qt.NoBrush)
                start = 90 - 90 * (self._phase % 4)
                p.drawArc(
                    QtCore.QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2),
                    start * 16, 270 * 16,
                )
                p.setPen(QtCore.Qt.NoPen)
            p.setBrush(theme.qcolor(_ACCENT))
            p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        else:
            name, color = ("alert-triangle", _RED) if state == "error" else ("check", _FG_DIM)
            size = theme.ROW_ICON - theme.SPACE_2XS
            pm = iconset.pixmap(name, color, size, self.devicePixelRatioF())
            p.drawPixmap(QtCore.QPointF(cx - size / 2.0, cy - size / 2.0), pm)
        p.end()


def _icon_button(icon: str, name: str, tip: str, *, size: int, glyph: int,
                 color: str = _FG_DIM, qss: str = "") -> QtWidgets.QPushButton:
    """An icon-only push button with a tooltip and an accessible name (an
    icon alone says nothing to a screen reader)."""
    b = QtWidgets.QPushButton()
    b.setIcon(iconset.icon(icon, color, glyph))
    b.setIconSize(QtCore.QSize(glyph, glyph))
    b.setFixedSize(size, size)
    b.setToolTip(tip)
    b.setAccessibleName(name)
    b.setCursor(QtCore.Qt.PointingHandCursor)
    if qss:
        b.setStyleSheet(qss)
    return b


def _text_size_icon(*, bigger: bool, color: str, size: int) -> QtGui.QIcon:
    """The A− / A+ glyph as an icon: a bold "A" with a small − / + at its top
    right, painted in `color` (crisp at 1×/2×/3×; no icon file needed)."""
    ic = QtGui.QIcon()
    qc = iconset.to_qcolor(color)
    for dpr in (1.0, 2.0, 3.0):
        px = max(1, round(size * dpr))
        img = QtGui.QImage(px, px, QtGui.QImage.Format_ARGB32_Premultiplied)
        img.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing)
        p.scale(dpr, dpr)
        f = ui_font()
        f.setWeight(QtGui.QFont.DemiBold)
        # The "A" shrinks a step for A− so the pair reads as small / large.
        f.setPixelSize(max(1, round(size * (0.82 if bigger else 0.64))))
        p.setFont(f)
        p.setPen(qc)
        # Centre the glyph's ink (not its advance box) a little left of
        # middle, bottom-aligned with the other icon glyphs.
        ink = QtGui.QFontMetricsF(f).tightBoundingRect("A")
        bottom = size * 0.86
        p.drawText(QtCore.QPointF(size * 0.4 - ink.center().x(), bottom - ink.bottom()), "A")
        # − / + drawn as strokes so it never depends on font glyph metrics.
        pen = QtGui.QPen(qc, max(1.0, size / 10.0))
        pen.setCapStyle(QtCore.Qt.RoundCap)
        p.setPen(pen)
        cx, cy, r = size * 0.84, size * 0.3, size * 0.13
        p.drawLine(QtCore.QPointF(cx - r, cy), QtCore.QPointF(cx + r, cy))
        if bigger:
            p.drawLine(QtCore.QPointF(cx, cy - r), QtCore.QPointF(cx, cy + r))
        p.end()
        pm = QtGui.QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        ic.addPixmap(pm)
    return ic


def _tab_focus_only(*widgets: QtWidgets.QWidget) -> None:
    """Keyboard (Tab) focus only — a mouse click never moves focus to these,
    so no focus ring lingers on a clicked button and typing focus stays where
    it was (the list or the composer)."""
    for w in widgets:
        w.setFocusPolicy(QtCore.Qt.TabFocus)


def _sort_icon(color: str, size: int) -> QtGui.QIcon:
    """Three left-aligned bars, longest on top — "sorted" (painted; there's
    no sort glyph in the icon set)."""
    ic = QtGui.QIcon()
    qc = iconset.to_qcolor(color)
    for dpr in (1.0, 2.0, 3.0):
        px = max(1, round(size * dpr))
        img = QtGui.QImage(px, px, QtGui.QImage.Format_ARGB32_Premultiplied)
        img.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.scale(dpr, dpr)
        pen = QtGui.QPen(qc, max(1.0, size / 9.0))
        pen.setCapStyle(QtCore.Qt.RoundCap)
        p.setPen(pen)
        x0 = size * 0.14
        for frac, y in ((0.72, 0.3), (0.5, 0.52), (0.28, 0.74)):
            p.drawLine(QtCore.QPointF(x0, size * y), QtCore.QPointF(x0 + size * frac, size * y))
        p.end()
        pm = QtGui.QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        ic.addPixmap(pm)
    return ic


# Row action buttons (✓ / stop / archive): flat until hovered, then a quiet
# rounded fill; the accent ring marks keyboard focus.
_ROW_ACTION_QSS = (
    f"QPushButton {{ background: transparent; border: 1px solid transparent; "
    f"border-radius: {theme.RADIUS_SM}px; padding: 0; margin: 0; }}"
    f"QPushButton:hover {{ background: {_BG_ROW_HOVER}; border-color: {theme.BORDER}; }}"
    f"QPushButton:pressed {{ background: {theme.BORDER}; }}"
    f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
)
_ROW_ACTION_SIZE = theme.PILL_HEIGHT
_QWIDGETSIZE_MAX = (1 << 24) - 1  # Qt's QWIDGETSIZE_MAX (not exported by PySide6)


class SessionListItem(QtWidgets.QFrame):
    """Custom row widget shown via QListWidget.setItemWidget.

    Calm by design: the row never fills itself (the list paints hover and
    selection behind it). State reads from a small painted glyph plus text —
    unread = accent dot + semibold title, running = pulsing dot + the live
    tool verb in the meta line, error = danger triangle + "Failed", Done =
    muted title + check — never from colour alone."""

    # Emitted when the user clicks the per-row archive button. The row
    # archives itself via the parent SessionsWindow which owns the Store.
    archive_requested = QtCore.Signal(int)  # session_id
    # Emitted when the user clicks the per-row Mark Done / Reopen button.
    review_toggled = QtCore.Signal(int)  # session_id
    # Emitted when the user clicks the per-row stop (cancel running turn) button.
    cancel_requested = QtCore.Signal(int)  # session_id

    _ROW_QSS = "QFrame#sessionListItem { background: transparent; }"

    def __init__(
        self,
        session: Session,
        totals: Optional[dict] = None,
        *,
        is_running: bool = False,
        live_verb: Optional[str] = None,
        live_elapsed_s: Optional[float] = None,
        stuck: bool = False,
        now: Optional[float] = None,
    ) -> None:
        super().__init__()
        self.setObjectName("sessionListItem")
        self._session = session
        self._is_running = is_running
        self._now = now
        self._pulse_phase = 0
        self.setMouseTracking(True)
        # Tracks "is the cursor over me OR am I selected?" — both reveal the
        # row actions.
        self._hovered = False
        self._selected = False
        self.setStyleSheet(self._ROW_QSS)

        # Two-column container: 3px profile-color stripe on the left + content.
        wrap = QtWidgets.QHBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(0)
        stripe = QtWidgets.QFrame()
        stripe.setFixedWidth(3)
        stripe_color = _color_for_profile(getattr(session, "profile", "") or "")
        stripe.setStyleSheet(f"background: {stripe_color}; border: none;")
        wrap.addWidget(stripe)

        body = QtWidgets.QWidget()
        body.setStyleSheet("background: transparent;")
        outer = QtWidgets.QVBoxLayout(body)
        outer.setContentsMargins(theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_XS, theme.SPACE_XS)
        outer.setSpacing(theme.SPACE_2XS)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(theme.SPACE_XS)
        _top = QtCore.Qt.AlignTop
        # Identify sessions still on the auto-generated transcript-head
        # placeholder so we can visually distinguish them: faded color +
        # leading ellipsis. Imported lazily to avoid a circular import at
        # module load.
        from holler.auto_title import is_placeholder_title as _is_placeholder
        placeholder = _is_placeholder(session)
        self._placeholder = placeholder
        raw_title = session.title or "Untitled session"
        title_text = f"…{raw_title}" if placeholder else raw_title
        # Title: one or two lines, sized to what it actually needs at the
        # current rail width (the window re-measures rows on resize).
        title = ElidedLabel(max_lines=2, fit=True)
        title.setFullText(title_text)
        title.setTextInteractionFlags(QtCore.Qt.NoTextInteraction)
        self._title_lbl = title
        self._mark = _StatusMark(title)
        head.addWidget(self._mark)
        # Title tooltip — full title + last-activity timestamp + profile +
        # transcript snippet. With ElidedLabel doing visual eliding, the
        # tooltip remains the authoritative way to see the full string.
        snippet = _truncate(_display_transcript(session.transcript or ""), 220)
        ts_iso = time.strftime("%Y-%m-%d %H:%M", time.localtime(session.last_activity_at))
        tip_lines = [f"<b>{html_escape(raw_title)}</b>", f"<span style='color:{theme.FG_MUTED}'>{ts_iso}</span>"]
        self._profile_label = ""
        if getattr(session, "profile", None):
            spec = get_profile(session.profile)
            self._profile_label = spec.display if spec is not None else session.profile
            tip_lines.append(
                f"<span style='color:{stripe_color}'>■</span> Profile: {html_escape(self._profile_label)}"
                f"<br><span style='color:{theme.FG_MUTED}'>A session keeps its profile. "
                f"Start a new session to use a different one.</span>"
            )
        if placeholder:
            tip_lines.append(
                f"<span style='color:{theme.WARNING}'>Title pending — right-click → Regenerate title</span>"
            )
        if snippet:
            tip_lines.append(f"<i>{html_escape(snippet)}</i>")
        title.setToolTip("<br>".join(tip_lines))
        head.addWidget(title, 1)
        if session.pinned:
            pin = QtWidgets.QLabel()
            pin.setPixmap(iconset.pixmap("star-filled", _PIN, theme.ROW_ICON - theme.SPACE_2XS,
                                         self.devicePixelRatioF()))
            pin.setToolTip("Pinned to the top")
            pin.setAccessibleName("Pinned")
            head.addWidget(pin, 0, _top)
        outer.addLayout(head)

        # Meta line + row actions. The actions sit at the end of the meta line
        # (never beside the title), so revealing them on hover / selection
        # only re-elides the one-line meta — the title never reflows and the
        # row never changes height. The line is pinned to the button height.
        meta_row = QtWidgets.QWidget()
        meta_row.setFixedHeight(_ROW_ACTION_SIZE)
        meta_lay = QtWidgets.QHBoxLayout(meta_row)
        meta_lay.setContentsMargins(theme.ROW_ICON + theme.SPACE_XS, 0, 0, 0)
        meta_lay.setSpacing(theme.SPACE_2XS)
        # One line, elided with "…" on a narrow rail (a plain QLabel clipped
        # mid-word, e.g. "1.3k toke"); the full breakdown is in the tooltip.
        self._meta = ElidedLabel(max_lines=1)
        self._meta.setTextFormat(QtCore.Qt.PlainText)
        meta_lay.addWidget(self._meta, 1, QtCore.Qt.AlignVCenter)

        # Mark Done / Reopen: shown on hover OR selection. One click moves a
        # session out of (or back into) the default "Active" view.
        self._review_btn = _icon_button("check", "Mark Done", "Mark Done (Space)",
                                        size=_ROW_ACTION_SIZE, glyph=theme.ROW_ICON, qss=_ROW_ACTION_QSS)
        self._review_btn.setVisible(False)
        self._review_btn.clicked.connect(
            lambda: self.review_toggled.emit(self._session.id)
        )
        meta_lay.addWidget(self._review_btn, 0, QtCore.Qt.AlignVCenter)
        # Stop for running turns. Mirrors the panel.py JobRow behavior so the
        # rail and the detail pane agree.
        self._cancel_btn = _icon_button("stop", "Stop turn", "Stop this turn",
                                        size=_ROW_ACTION_SIZE, glyph=theme.ROW_ICON, qss=_ROW_ACTION_QSS)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(
            lambda: self.cancel_requested.emit(self._session.id)
        )
        meta_lay.addWidget(self._cancel_btn, 0, QtCore.Qt.AlignVCenter)
        # Archive: hidden until hover OR selection. Suppressed while a job is
        # running for this session, so we can't archive mid-flight and orphan
        # a live response.
        self._close_btn = _icon_button("archive", "Archive session",
                                       "Archive (you can undo it from Archived)",
                                       size=_ROW_ACTION_SIZE, glyph=theme.ROW_ICON, qss=_ROW_ACTION_QSS)
        self._close_btn.setVisible(False)
        self._close_btn.clicked.connect(
            lambda: self.archive_requested.emit(self._session.id)
        )
        meta_lay.addWidget(self._close_btn, 0, QtCore.Qt.AlignVCenter)
        _tab_focus_only(self._review_btn, self._cancel_btn, self._close_btn)
        outer.addWidget(meta_row)

        # Context-window meter: thin 2px bar under the meta line tinted by
        # ctx_in / 200k. Hidden when ctx_in is zero. Yellow ≥80%, red ≥95%.
        self._ctx_bar = QtWidgets.QProgressBar()
        self._ctx_bar.setRange(0, 200_000)
        self._ctx_bar.setFixedHeight(2)
        self._ctx_bar.setTextVisible(False)
        self._ctx_bar.setVisible(False)
        outer.addWidget(self._ctx_bar)

        wrap.addWidget(body, 1)

        self._live_verb = live_verb
        self._live_elapsed_s = live_elapsed_s
        self._stuck = stuck
        self._apply_state()
        self.set_totals(totals)
        self._sync_close_btn()

    def enterEvent(self, ev) -> None:  # type: ignore[override]
        self._hovered = True
        self._sync_close_btn()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:  # type: ignore[override]
        self._hovered = False
        self._sync_close_btn()
        super().leaveEvent(ev)

    def set_selected(self, selected: bool) -> None:
        """Called by the parent SessionsWindow when selection changes."""
        self._selected = selected
        self._sync_close_btn()
        self._sync_text_colors()

    _TITLE_QSS = f"color: {{color}}; font-size: {theme.FONT_SM}pt; font-weight: {{weight}};"
    _META_QSS = f"color: {{color}}; font-size: {theme.FONT_SM}pt;"

    def state(self) -> str:
        """``running`` / ``done`` / ``error`` / ``unread`` / ``idle``."""
        sess = self._session
        if self._is_running or sess.status == "running":
            return "running"
        if sess.reviewed_at is not None:
            return "done"
        if sess.status == "error":
            return "error"
        if _is_stale(sess, now=self._now):
            return "idle"
        # Not Done yet: there's a reply here you haven't marked Done.
        return "unread"

    def _apply_state(self) -> None:
        """Status glyph, title weight / colour, meta colour, the Mark Done ↔
        Reopen toggle and accessible text for the current state."""
        sess = self._session
        st = self.state()
        self._mark.set_state(st)
        self._mark.setToolTip(_dot_tooltip(sess, now=self._now, running=self._is_running))
        self._mark.setAccessibleName(_STATE_NAMES.get(st, ""))
        if self._placeholder:
            title_color = _FG_FAINT
        elif st == "done":
            title_color = _FG_DIM
        else:
            title_color = _title_color_for(sess, now=self._now)
        self._title_color = title_color
        self._title_weight = (theme.WEIGHT_SEMIBOLD if st in ("unread", "running", "error")
                              else theme.WEIGHT_REGULAR)
        meta_color = _FG_DIM
        if st == "running":
            # The live tool verb leads the meta line, but in the shared grey it
            # scanned as ordinary metadata. Accent makes the line itself say
            # "this row is working".
            meta_color = _ACCENT
        elif sess.reviewed_at is not None:
            # Done → faint meta so the row visually steps back even when it's
            # still listed (Done filter, or pinned-still-visible).
            meta_color = _FG_FAINT
        elif _is_stale(sess, now=self._now):
            meta_color = _FG_FAINT
        self._meta_color = meta_color
        done = sess.reviewed_at is not None
        self._review_btn.setIcon(iconset.icon("inbox" if done else "check", _FG_DIM, theme.ROW_ICON))
        self._review_btn.setAccessibleName("Reopen" if done else "Mark Done")
        self._review_btn.setToolTip("Reopen (Space)" if done else "Mark Done (Space)")
        self._sync_text_colors()

    def _sync_text_colors(self) -> None:
        """FG_FAINT (placeholder title, stale / Done meta) only clears AA
        contrast on the plain row backgrounds; on the selection fill or a
        running row those labels step up to FG_MUTED."""
        tinted = self._selected or self._is_running

        def pick(color: str) -> str:
            return _FG_DIM if (tinted and color == _FG_FAINT) else color

        want = self._TITLE_QSS.format(color=pick(self._title_color), weight=self._title_weight)
        if self._title_lbl.styleSheet() != want:
            self._title_lbl.setStyleSheet(want)
        want = self._META_QSS.format(color=pick(self._meta_color))
        if self._meta.styleSheet() != want:
            self._meta.setStyleSheet(want)

    def set_is_running(self, is_running: bool) -> None:
        """Called by the parent when a job for this session starts/stops.
        Suppresses the archive button while a turn is in flight."""
        if self._is_running != is_running:
            self._is_running = is_running
            self._sync_close_btn()
            self._apply_state()
            self.update()  # start / clear the running wash

    def pulse_dot(self, phase: int) -> None:
        """Advance the running-row halo. Phase is a small integer (0..N) that
        the parent SessionsWindow advances on a fast timer. Only the status
        glyph repaints — no stylesheet is touched. No-op when the session
        isn't running."""
        if not self._is_running:
            return
        self._pulse_phase = phase
        self._mark.set_phase(phase)
        self.update()  # the row-wide wash in paintEvent

    def paintEvent(self, e: QtGui.QPaintEvent) -> None:  # noqa: N802 (Qt name)
        # While a turn is in flight the whole row breathes an accent wash, so
        # a working session reads from across the room, not just from its 6px
        # dot. Painted (not QSS) so the 280ms tick never re-polishes children;
        # alphas are capped in theme.PULSE_ALPHAS to keep text at AA contrast.
        if self._is_running:
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setPen(QtCore.Qt.NoPen)
            alpha = theme.PULSE_ALPHAS[self._pulse_phase % len(theme.PULSE_ALPHAS)]
            p.setBrush(theme.qcolor(_ACCENT, alpha))
            p.drawRoundedRect(QtCore.QRectF(self.rect()), 4, 4)
            p.end()
        super().paintEvent(e)

    def _sync_close_btn(self) -> None:
        # Mark Done is shown on hover/selection always (even running). An
        # operator can mark Done while a turn is still running — it just
        # means "I've already reviewed what's queued up here."
        show_quick = self._hovered or self._selected
        self._review_btn.setVisible(show_quick)
        # Stop: only when a turn is in flight. Archive: only when no turn is
        # in flight (so we can't orphan a live response).
        self._cancel_btn.setVisible(show_quick and self._is_running)
        if self._is_running:
            self._close_btn.setVisible(False)
            return
        self._close_btn.setVisible(show_quick)

    def set_live(self, verb: Optional[str], elapsed_s: Optional[float], stuck: bool) -> None:
        """Called by the parent to update the live-tool ticker on the meta line.
        verb=None clears the live segment (no in-flight job)."""
        self._live_verb = verb
        self._live_elapsed_s = elapsed_s
        self._stuck = stuck
        self.set_totals(getattr(self, "_last_totals", None))

    def set_totals(self, totals: Optional[dict]) -> None:
        sess = self._session
        self._last_totals = totals
        parts = []
        # Leading state text, so state never rests on colour alone: the live
        # tool verb while running, "Failed" after an error.
        if self._is_running and self._live_verb:
            verb = _truncate(self._live_verb, 40)
            if self._live_elapsed_s is not None:
                verb = f"{verb} · {_fmt_elapsed(self._live_elapsed_s)}"
            if self._stuck:
                verb = f"Stuck — {verb}"
            parts.append(verb)
        elif self.state() == "error":
            parts.append("Failed")
        if sess.archived:
            parts.append("Archived")
        if self._profile_label:
            parts.append(self._profile_label)
        parts.append(format_model_label(sess.model) or sess.model)
        parts.append(_relative_time(sess.last_activity_at))
        fresh_in = int((totals or {}).get("fresh_in", 0))
        ctx_in = int((totals or {}).get("ctx_in", 0))
        output = int((totals or {}).get("output", 0))
        cache_pct = int((totals or {}).get("cache_pct", 0))
        # Headline: only NEW content moved through this session (no cache double-count).
        total = fresh_in + output
        if total:
            tok = f"{_fmt_k(total)} tokens"
            if cache_pct >= 1:
                tok += f" · {cache_pct}% cached"
            parts.append(tok)
        text = " · ".join(parts)
        self._meta.setFullText(text)
        # Context window bar — disabled for now. The per-turn input_tokens
        # column stacks cache_read across many tool messages, so a single
        # "turn" can have 5-10M ctx_in even though the actual context window
        # is 200k. Need a better signal (e.g. a dedicated last_compaction_ctx
        # field) before this is useful. Hidden so it doesn't mislead.
        self._ctx_bar.setVisible(False)
        cost = float((totals or {}).get("cost", 0.0))
        if ctx_in or output or cost:
            rows = [
                "<b>Session totals</b> &middot; all turns",
                "<table cellspacing='0' cellpadding='2'>",
                f"<tr><td><b>{_fmt_k(total)}</b></td>"
                f"<td>&nbsp;new tokens</td></tr>",
                f"<tr><td>&uarr;&nbsp;{_fmt_k(fresh_in)}</td>"
                f"<td>&nbsp;input (prompts, screenshots, tool results)</td></tr>",
                f"<tr><td>&darr;&nbsp;{_fmt_k(output)}</td>"
                f"<td>&nbsp;output (replies and tool calls)</td></tr>",
                f"<tr><td colspan='2'><hr style='background-color:{theme.BORDER};'/></td></tr>",
                f"<tr><td>{_fmt_k(ctx_in)}</td>"
                f"<td>&nbsp;total processed, including cache re-reads</td></tr>",
            ]
            if cost:
                rows.append(
                    f"<tr><td><b>${cost:.3f}</b></td>"
                    f"<td>&nbsp;API-equivalent cost (not billed on a Claude subscription)</td></tr>"
                )
            rows.append("</table>")
            self._meta.setToolTip("".join(rows))
        else:
            self._meta.setToolTip("")

    def fit_to_width(self, width: int) -> int:
        """Lay the row out at `width` and return the height it needs (the
        title takes one or two lines depending on the width). Works before
        the row is shown — it doesn't wait for resize events."""
        if width > 0 and self.width() != width:
            self.resize(width, max(1, self.height()))
        lay = self.layout()
        lay.activate()
        self._title_lbl._apply_elide()
        lay.activate()
        return self.sizeHint().height()

    def accessible_text(self) -> str:
        """One spoken line for the row: title, state, age."""
        s = self._session
        title = s.title or "Untitled session"
        if self._placeholder:
            title = f"{title} (title pending)"
        bits = [title, _STATE_NAMES.get(self.state(), "")]
        if s.pinned:
            bits.append("Pinned")
        if self._profile_label:
            bits.append(self._profile_label)
        bits.append(_relative_time(s.last_activity_at))
        return ", ".join(b for b in bits if b)

    def session(self) -> Session:
        return self._session


def _open_profiles_folder() -> None:
    """Reveal the user profiles folder (created on first use)."""
    try:
        ensure_user_dir()
    except Exception:
        log.exception("failed to ensure user profile dir")
    try:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(user_dir())))
    except Exception:
        log.exception("failed to open profiles folder")


class ProfilePickerDialog(QtWidgets.QDialog):
    """Modal list of session profiles. Returns the chosen profile name via
    `selected_name` after exec() returns Accepted, or "" for "No profile".

    "No profile" is pinned at the top so the default path is one click.
    """

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New session")
        self.setStyleSheet(_QSS + f"""
            QDialog {{ background: {_BG}; }}
            QListWidget {{ background: {_BG_ROW}; border: 1px solid {theme.BORDER};
                           border-radius: {theme.RADIUS_MD}px; padding: {theme.SPACE_XS}px; }}
            QListWidget::item {{ padding: {ProfilePickerDialog._ITEM_PAD_V}px {ProfilePickerDialog._ITEM_PAD_H}px;
                                 margin: 1px 0; border-radius: {theme.RADIUS_SM}px; }}
            QListWidget::item:selected {{ background: {_BG_ROW_SELECTED}; color: {_FG}; }}
            QListWidget::item:hover {{ background: {_BG_ROW_HOVER}; }}
        """)
        self.setMinimumSize(520, 460)
        self.selected_name: str = ""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_LG, theme.SPACE_LG, theme.SPACE_LG, theme.SPACE_MD)
        layout.setSpacing(theme.SPACE_SM)

        heading = QtWidgets.QLabel("Choose a profile")
        heading.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_LG}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        layout.addWidget(heading)

        sub = QtWidgets.QLabel(
            "A profile adds instructions to every turn in this session. You "
            "can't change it later — start a new session to switch."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt;")
        layout.addWidget(sub)

        self._list = QtWidgets.QListWidget()
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._list.setUniformItemSizes(False)
        self._list.itemActivated.connect(lambda _it: self._accept())
        self._list.setAccessibleName("Profiles")
        layout.addWidget(self._list, 1)

        # Pinned top row = "No profile".
        none_item = QtWidgets.QListWidgetItem()
        none_item.setData(QtCore.Qt.UserRole, "")  # empty string = no profile
        none_widget = self._make_row(
            "No profile",
            "Holler's standard behavior.",
            is_default=True,
        )
        none_item.setSizeHint(none_widget.sizeHint())
        none_item.setData(QtCore.Qt.AccessibleTextRole, "No profile (default): Holler's standard behavior.")
        self._list.addItem(none_item)
        self._list.setItemWidget(none_item, none_widget)

        # Built-in + user profiles.
        try:
            specs: List[ProfileSpec] = list_profiles()
        except Exception:
            log.exception("failed to list profiles")
            specs = []
        for spec in specs:
            it = QtWidgets.QListWidgetItem()
            it.setData(QtCore.Qt.UserRole, spec.name)
            w = self._make_row(
                spec.display,
                spec.description or "No description",
                source=spec.source,
            )
            it.setSizeHint(w.sizeHint())
            it.setData(QtCore.Qt.AccessibleTextRole,
                       f"{spec.display}: {spec.description or 'No description'}")
            self._list.addItem(it)
            self._list.setItemWidget(it, w)

        # Default selection = "No profile". Hit Enter to take the default
        # path in one keystroke.
        self._list.setCurrentRow(0)

        # Footer row: Open folder + Cancel/OK
        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(theme.SPACE_SM)
        open_btn = QtWidgets.QPushButton("Open profiles folder…")
        open_btn.setCursor(QtCore.Qt.PointingHandCursor)
        open_btn.setStyleSheet(theme.button_qss("secondary", pad_v=theme.SPACE_XS, pad_h=theme.SPACE_MD,
                                            font_size=theme.FONT_SM, weight=None))
        open_btn.clicked.connect(self._open_profiles_folder)
        footer.addWidget(open_btn)
        footer.addStretch(1)

        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.setCursor(QtCore.Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(theme.button_qss("secondary", pad_v=theme.SPACE_XS, pad_h=theme.SPACE_LG,
                                              font_size=theme.FONT_SM, weight=None))
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)

        ok_btn = QtWidgets.QPushButton("Start session")
        ok_btn.setCursor(QtCore.Qt.PointingHandCursor)
        ok_btn.setStyleSheet(theme.button_qss("primary", pad_v=theme.SPACE_XS, pad_h=theme.SPACE_LG,
                                          font_size=theme.FONT_SM))
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept)
        footer.addWidget(ok_btn)
        layout.addLayout(footer)

    # Item padding (QSS ``QListWidget::item``) — the row widget sits inside it.
    _ITEM_PAD_V = theme.SPACE_SM
    _ITEM_PAD_H = theme.SPACE_MD

    def _fit_rows(self) -> None:
        """Size each row to its word-wrapped content at the list's current
        width. A row widget's plain sizeHint() is measured before it has a
        width, so wrapped descriptions came out too tall or too short."""
        avail = self._list.viewport().width() - 2 * self._ITEM_PAD_H
        if avail <= 0:
            return
        for i in range(self._list.count()):
            it = self._list.item(i)
            w = self._list.itemWidget(it)
            lay = w.layout() if w is not None else None
            if lay is None:
                continue
            h = lay.totalHeightForWidth(avail) if lay.hasHeightForWidth() else lay.sizeHint().height()
            want = QtCore.QSize(0, h + 2 * self._ITEM_PAD_V + 2)
            if it.sizeHint() != want:
                it.setSizeHint(want)

    def showEvent(self, ev: QtGui.QShowEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(ev)
        self._fit_rows()

    def resizeEvent(self, ev: QtGui.QResizeEvent) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(ev)
        self._fit_rows()

    def _make_row(self, title: str, description: str, *, source: str = "", is_default: bool = False) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setStyleSheet("background: transparent;")
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(theme.SPACE_2XS, theme.SPACE_2XS, theme.SPACE_2XS, theme.SPACE_2XS)
        v.setSpacing(theme.SPACE_XS)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(theme.SPACE_SM)
        title_lbl = QtWidgets.QLabel(title)
        title_lbl.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        head.addWidget(title_lbl)
        if source == "user":
            # Outline accent tag — state-ish metadata, never brand amber.
            tag = QtWidgets.QLabel("Custom")
            tag.setStyleSheet(
                f"color: {_ACCENT}; background: transparent; border: 1px solid {theme.ACCENT_PILL_BORDER}; {theme.PILL_BOX} "
                f"padding: 0 {theme.SPACE_SM}px; border-radius: {theme.RADIUS_PILL}px; font-size: {theme.FONT_XS}pt;"
            )
            head.addWidget(tag)
        elif is_default:
            tag = QtWidgets.QLabel("Default")
            tag.setStyleSheet(
                f"color: {_FG_DIM}; background: {theme.BORDER_SUBTLE}; border: 1px solid {theme.BORDER}; {theme.PILL_BOX} "
                f"padding: 0 {theme.SPACE_SM}px; border-radius: {theme.RADIUS_PILL}px; font-size: {theme.FONT_XS}pt;"
            )
            head.addWidget(tag)
        head.addStretch(1)
        v.addLayout(head)
        desc = QtWidgets.QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt;")
        v.addWidget(desc)
        return w

    def _open_profiles_folder(self) -> None:
        _open_profiles_folder()

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item is not None:
            data = item.data(QtCore.Qt.UserRole)
            self.selected_name = str(data) if data else ""
        else:
            self.selected_name = ""
        self.accept()


class _ViewportResizeWatch(QtCore.QObject):
    """Event filter calling `callback()` whenever the watched widget resizes
    (the session list's viewport: rows re-fit, overlays re-pin)."""

    def __init__(self, callback: Callable[[], None], parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Resize:
            self._callback()
        return False


class _RenameEdit(QtWidgets.QLineEdit):
    """In-row title editor (double-click, F2, or the row menu's Rename…).
    Return commits, Esc cancels, clicking away commits."""

    finished = QtCore.Signal(bool)  # True = commit, False = cancel

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._done = False
        self._original_text = ""  # the title editing started from
        self.setAccessibleName("Session title")
        self.setStyleSheet(
            f"QLineEdit {{ background: {_BG_ROW}; color: {_FG}; border: 1px solid {_ACCENT}; "
            f"border-radius: {theme.RADIUS_SM}px; padding: 0 {theme.SPACE_XS}px; "
            f"font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; "
            f"selection-background-color: {theme.ACCENT_EMPHASIS}; selection-color: {theme.FG_ON_EMPHASIS}; }}"
        )
        self.returnPressed.connect(lambda: self._finish(True))

    def _finish(self, commit: bool) -> None:
        if self._done:
            return
        self._done = True
        self.finished.emit(commit)

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:  # noqa: N802 (Qt name)
        if ev.key() == QtCore.Qt.Key_Escape:
            self._finish(False)
            return
        super().keyPressEvent(ev)

    def focusOutEvent(self, ev: QtGui.QFocusEvent) -> None:  # noqa: N802 (Qt name)
        super().focusOutEvent(ev)
        # A context menu / popup taking focus isn't "clicking away".
        if ev.reason() != QtCore.Qt.PopupFocusReason:
            self._finish(True)


class _FocusInputOnClick(QtCore.QObject):
    """Event filter that bounces keyboard focus onto a target widget after a
    mouse press on whatever it watches. Used to keep typing focus on the
    bottom text input even when the operator clicks the conversation
    transcript above it. The press itself is not consumed, so selection
    drag and link clicks still work normally."""

    def __init__(self, target: QtWidgets.QWidget, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._target = target

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.MouseButtonPress:
            tgt = self._target
            QtCore.QTimer.singleShot(0, tgt, lambda: tgt.setFocus(QtCore.Qt.MouseFocusReason))
        return False


class _MousePressFlag(QtCore.QObject):
    """Event filter that raises `owner._list_mouse_select` for the duration of
    a mouse press on the watched viewport (reset on the next event-loop tick,
    after the synchronous selection change has run). Lets the selection slot
    tell a click apart from keyboard navigation."""

    def __init__(self, owner: QtCore.QObject) -> None:
        super().__init__(owner)
        self._owner = owner

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.MouseButtonPress:
            owner = self._owner
            owner._list_mouse_select = True
            QtCore.QTimer.singleShot(0, owner, lambda: setattr(owner, "_list_mouse_select", False))
        return False


class _TypeToComposer(QtCore.QObject):
    """Event filter on the session list: a printable keystroke (no Ctrl/⌘/Alt)
    moves focus to the composer and is delivered there, so typing after
    arrow-key navigation lands in the message box instead of the list's
    type-ahead. Space / Backspace / Delete / Return keep their list meaning."""

    _LIST_KEYS = {
        QtCore.Qt.Key_Space, QtCore.Qt.Key_Backspace, QtCore.Qt.Key_Delete,
        QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Tab,
        QtCore.Qt.Key_Backtab, QtCore.Qt.Key_Escape,
    }

    def __init__(self, owner: QtCore.QObject) -> None:
        super().__init__(owner)
        self._owner = owner

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() != QtCore.QEvent.KeyPress:
            return False
        # Option types ordinary characters on macOS (@ [ { € on many
        # layouts), so only Ctrl/⌘ mark a shortcut there.
        shortcut_mods = QtCore.Qt.ControlModifier | QtCore.Qt.MetaModifier
        if sys.platform != "darwin":
            shortcut_mods |= QtCore.Qt.AltModifier
        mods = event.modifiers() & shortcut_mods
        text = event.text()
        if mods or event.key() in self._LIST_KEYS or not text or not text.isprintable():
            return False
        target = getattr(self._owner, "_text_input", None)
        if target is None or not target.isEnabled():
            return False
        target.setFocus(QtCore.Qt.OtherFocusReason)
        QtCore.QCoreApplication.sendEvent(target, QtGui.QKeyEvent(
            QtCore.QEvent.KeyPress, event.key(), event.modifiers(), text,
        ))
        return True


class _GlobalCopyFilter(QtCore.QObject):
    """App-level Cmd/Ctrl+C interceptor for the conversation surfaces.

    The reply browser + header transcript are held NoFocus so clicks bounce
    typing focus back to the composer — which means the composer (a
    QPlainTextEdit) is virtually always the focus widget. Qt input widgets
    consume Cmd/Ctrl+C in their own keyPressEvent BEFORE any window-level
    QShortcut sees it, and with no local selection that consumption is a silent
    no-op — so selecting transcript text and pressing Cmd+C copies nothing.
    This filter runs ahead of the focused widget: if that widget has its own
    selection we let Qt copy it normally; otherwise we route to the window's
    conversation-copy logic and swallow the event. Scoped to the owning window
    so dialogs/popouts are untouched."""

    def __init__(self, window: "SessionsWindow") -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() != QtCore.QEvent.KeyPress:
            return False
        try:
            if event.key() != QtCore.Qt.Key_C:
                return False
            if event.modifiers() != QtCore.Qt.ControlModifier:
                return False
        except AttributeError:
            return False
        focus = QtWidgets.QApplication.focusWidget()
        if focus is None:
            return False
        try:
            if not self._window.isAncestorOf(focus) and focus is not self._window:
                return False
        except RuntimeError:
            return False
        # Focused editor with its own selection → let Qt's built-in copy run.
        if isinstance(focus, QtWidgets.QLineEdit):
            if focus.hasSelectedText():
                return False
        elif isinstance(focus, (QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit)):
            try:
                if focus.textCursor().hasSelection():
                    return False
            except Exception:
                return False
        try:
            self._window._copy_selection()
        except Exception:
            log.exception("global Cmd/Ctrl+C filter: copy failed")
            return False
        return True


class _GlobalUndoFilter(QtCore.QObject):
    """App-level ⌘Z / Ctrl+Z → the newest toast's Undo (Mark Done, Archive…).

    The toast is a no-focus tool window, so its Undo button can't be reached
    from the keyboard; this filter gives it the platform undo shortcut while
    an Undo toast is up. It never steals the key from a text field that has
    its own undo history (the composer, search box or rename editor), and it
    stands aside during a capture hold, where ⌘Z removes the last stroke.
    Scoped to the owning window like _GlobalCopyFilter."""

    def __init__(self, window: "SessionsWindow") -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() != QtCore.QEvent.KeyPress:
            return False
        if event.isAutoRepeat():
            return False  # holding ⌘Z must not walk back through older toasts
        try:
            if not event.matches(QtGui.QKeySequence.Undo):
                return False
        except (AttributeError, TypeError):
            return False
        try:
            win = self._window
            toast = getattr(win, "_toast", None)
            focus = QtWidgets.QApplication.focusWidget()
            if focus is None:
                if QtWidgets.QApplication.activeWindow() is not win:
                    return False
            elif not win.isAncestorOf(focus) and focus is not win:
                # A toast window that took focus (e.g. after a click on it)
                # still answers to this window's undo.
                owner = getattr(focus.window(), "_owner", None) or focus.window()
                if toast is None or owner is not toast:
                    return False
        except RuntimeError:
            return False
        if focus is not None and _has_text_undo(focus):
            return False  # the field's own undo wins
        if toast is None:
            return False
        try:
            return bool(toast.trigger_undo())
        except Exception:
            log.exception("global undo filter: toast undo failed")
            return False


def _has_text_undo(widget: QtWidgets.QWidget) -> bool:
    """True when `widget` is an editable text field with something to undo."""
    if isinstance(widget, QtWidgets.QLineEdit):
        return not widget.isReadOnly() and widget.isUndoAvailable()
    if isinstance(widget, (QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit)):
        return not widget.isReadOnly() and widget.document().isUndoAvailable()
    return False


class _TextInput(QtWidgets.QPlainTextEdit):
    """Bottom-of-pane text box. Enter submits; Shift+Enter inserts a newline.

    Auto-grows up to ~180px tall on multi-line content, then internal scroll
    kicks in. Matches the visual rhythm of Slack/Discord/etc.
    """

    submit_requested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(56)
        self._min_height = 56
        self._max_height = 180
        # Debounce timer so per-keystroke documentSizeChanged signals collapse
        # into a single layout pass — fires on the NEXT event loop tick.
        self._fit_timer = QtCore.QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit_height_now)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)

    def _fit_height(self) -> None:
        if not self._fit_timer.isActive():
            self._fit_timer.start()

    def _fit_height_now(self) -> None:
        doc_h = int(self.document().size().height())
        margins = self.contentsMargins()
        frame = 2 * self.frameWidth()
        target = max(
            self._min_height,
            min(self._max_height, doc_h + margins.top() + margins.bottom() + frame + 12),
        )
        if target != self.height():
            self.setFixedHeight(target)

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:
        if ev.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            if ev.modifiers() & QtCore.Qt.ShiftModifier:
                # Shift+Enter → newline; pass through to base.
                super().keyPressEvent(ev)
                return
            # Plain Enter → submit.
            self.submit_requested.emit()
            return
        super().keyPressEvent(ev)


# ---------- Window-sizing helpers (monitor-adaptive) ----------
#
# The sessions window historically resized to a hardcoded 1100x700, which
# either overflowed small laptops or looked lost on 4K externals. These
# helpers size against the monitor the operator is actually on, with a
# readable floor so we never collapse the layout.

# Minimum the user is allowed to drag the window down to — the splitter +
# session-row content stop being usable below this. Kept at 720 so a portrait
# 1080-wide monitor at 125–150% Windows DPI scaling can still host the window.
SW_MIN_W = 720
SW_MIN_H = 600

# Preferred maximums when computing a default — beyond this the window
# just looks empty on big displays.
SW_PREF_MAX_W = 1280
SW_PREF_MAX_H = 820

# Fraction of available screen we'll consume by default.
SW_DEFAULT_W_FRAC = 0.80
SW_DEFAULT_H_FRAC = 0.85


def _screen_for_cursor() -> QtGui.QScreen:
    """Return the screen the cursor is currently on, falling back to primary.

    Using the cursor's screen means the window opens where the operator is
    actually working — important on multi-monitor setups where the primary
    isn't necessarily the active one.
    """
    pos = QtGui.QCursor.pos()
    scr = QtGui.QGuiApplication.screenAt(pos)
    return scr or QtGui.QGuiApplication.primaryScreen()


def compute_default_sw_geometry() -> tuple[int, int, int, int]:
    """Pick a sensible (x, y, w, h) for the sessions window on first launch.

    Centers in the available geometry of the cursor's screen, sized as a
    fraction of that screen but clamped to readable floor / preferred max.
    """
    scr = _screen_for_cursor()
    if scr is None:
        return (100, 100, SW_PREF_MAX_W, SW_PREF_MAX_H)
    avail = scr.availableGeometry()
    w = max(SW_MIN_W, min(SW_PREF_MAX_W, int(avail.width() * SW_DEFAULT_W_FRAC)))
    h = max(SW_MIN_H, min(SW_PREF_MAX_H, int(avail.height() * SW_DEFAULT_H_FRAC)))
    # If the floor itself is larger than the screen (tiny display), shrink
    # below the floor rather than refusing to render — Qt will scrollbar
    # internal widgets where it can.
    w = min(w, avail.width())
    h = min(h, avail.height())
    x = avail.left() + (avail.width() - w) // 2
    y = avail.top() + (avail.height() - h) // 2
    return (x, y, w, h)


def fit_saved_sw_geometry(x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """Validate + shrink-to-fit a saved geometry against current screens.

    If the saved rect doesn't intersect any screen (monitor unplugged), or
    is larger than the available screen it lands on (laptop undocked into
    a smaller display), produce a fitted rect rather than restoring blindly.
    """
    screens = QtGui.QGuiApplication.screens() or []
    if not screens:
        return compute_default_sw_geometry()

    rect = QtCore.QRect(x, y, max(SW_MIN_W, w), max(SW_MIN_H, h))

    # Find a screen the saved rect intersects.
    host = None
    for scr in screens:
        if scr.availableGeometry().intersects(rect):
            host = scr
            break
    if host is None:
        # Saved rect is off-screen entirely — fall back to a fresh default.
        return compute_default_sw_geometry()

    avail = host.availableGeometry()
    # Apply the readable floor, then cap at avail. Order matters: capping last
    # means a monitor smaller than the floor (tiny laptop, portrait at high DPI)
    # shrinks below the floor rather than overshooting the screen edge.
    new_w = min(max(SW_MIN_W, rect.width()), avail.width())
    new_h = min(max(SW_MIN_H, rect.height()), avail.height())

    # Re-anchor if shrinking pushed it off-edge.
    new_x = rect.x()
    new_y = rect.y()
    if new_x + new_w > avail.right():
        new_x = avail.right() - new_w
    if new_y + new_h > avail.bottom():
        new_y = avail.bottom() - new_h
    new_x = max(avail.left(), new_x)
    new_y = max(avail.top(), new_y)
    return (new_x, new_y, new_w, new_h)


def _link_root(sess: Session, fallback: Optional[Path]) -> Optional[Path]:
    """Directory relative file links in a session's transcript resolve against:
    its live worktree when it has one, else the configured repo."""
    if sess.worktree_state == "active" and sess.worktree_path:
        wt = Path(sess.worktree_path)
        if wt.is_dir():
            return wt
    return fallback


class SessionsWindow(QtWidgets.QMainWindow):
    # The New-session split button's main half squares its right corners
    # against the profile-menu arrow.
    _NEW_MAIN_CORNERS = "border-top-right-radius: 0; border-bottom-right-radius: 0;"

    # Routing
    session_selected = QtCore.Signal(int)
    new_session_requested = QtCore.Signal()
    profile_selected = QtCore.Signal(str)  # "" = no profile
    cancel_job_requested = QtCore.Signal(int)
    # Stop button on an Agents-pane card: (local_session_id, tool_use_id).
    stop_agent_requested = QtCore.Signal(int, str)
    # Build-status poll result, hopped from its worker thread (see
    # holler/release_status.py).
    _release_status_ready = QtCore.Signal(object)
    # (current Account | None, [saved Account]) from a worker thread — see
    # holler/accounts.py.
    _account_ready = QtCore.Signal(object, object)
    request_open_in_claude_code = QtCore.Signal(str)
    # Header controls (replaces the killed mini panel)
    model_cycle_requested = QtCore.Signal()
    settings_requested = QtCore.Signal()
    # Text-size buttons: emits +1 (bigger) or -1 (smaller). The runner owns the
    # config + persistence and applies the new UI scale.
    text_scale_bumped = QtCore.Signal(int)
    restart_requested = QtCore.Signal()
    # Text input at the bottom of the right pane → submit-as-turn (no screenshot).
    text_submitted = QtCore.Signal(str)
    # Superintendent Chain bar button pressed — payload is "arm" | "file" |
    # "discard". The runner mutates the draft/armed state and pushes the
    # resulting (armed, count) back via set_superintendent_chain_status.
    superintendent_chain_action = QtCore.Signal(str)
    # Evidence-lane toggle in the inbox toolbar — payload is "deploy" |
    # "investigation". The runner records it as state["superintendent_mode"] so
    # the next dump/chain is filed into that lane.
    superintendent_mode_action = QtCore.Signal(str)
    # Clicking the worker-model badge in the inbox toolbar — the runner cycles
    # cfg.superintendent_model and persists it, so the next worker adopts it.
    superintendent_model_cycle_requested = QtCore.Signal()

    def __init__(
        self,
        store,                              # holler.store.Store
        job_store: JobStore,
        captures_dir: Path,
        repo_cwd: Path,
        hotkey: str = "right alt",
        parent: Optional[QtWidgets.QWidget] = None,
        auto_titler: Optional[object] = None,  # holler.auto_title.AutoTitler
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet(_QSS)
        self.setWindowTitle("Holler")
        # Monitor-adaptive default — runner.full will replace this with the
        # saved (fitted) geometry if one exists. Floor lets the operator
        # drag-resize but not into an unreadable footprint.
        self.setMinimumSize(SW_MIN_W, SW_MIN_H)
        _dx, _dy, _dw, _dh = compute_default_sw_geometry()
        self.setGeometry(_dx, _dy, _dw, _dh)

        self._store = store
        self._job_store = job_store
        self._captures_dir = captures_dir
        self._repo_cwd = repo_cwd
        self._hotkey_label = _format_hotkey(hotkey)
        # AutoTitler reference — optional. Used by the "Regenerate title"
        # right-click action and the titler status footer. Type-annotated as
        # `object` to avoid a Qt-time circular import of `auto_title`.
        self._auto_titler = auto_titler
        # Restore last-used filter + sort from persisted state (falls back to
        # "active" + "recent" if state.json is missing or older).
        _persisted = _state.load()
        self._current_filter = str(_persisted.get("sessions_filter", "active"))
        if self._current_filter not in ("active", "done", "running", "pinned", "archived"):
            self._current_filter = "active"
        self._sort_key = str(_persisted.get("sessions_sort", "recent"))
        if self._sort_key not in ("recent", "tokens", "cost", "turns", "title"):
            self._sort_key = "recent"
        self._active_session_id: Optional[int] = None
        self._row_widgets: dict[int, "SessionListItem"] = {}
        # Tracks the timestamp we first saw a tool become "current" per job —
        # lets us flag stuck (>120s same tool) rows without a per-tool join.
        self._tool_first_seen: dict[int, tuple[str, float]] = {}
        # Detached pop-out detail windows (Ctrl+Shift+D).
        self._detached_windows: list[QtWidgets.QMainWindow] = []
        # MRU recents stack — last-N session ids selected, freshest first.
        recents_raw = _persisted.get("sessions_recents", [])
        try:
            self._recents = [int(x) for x in recents_raw][:8]
        except Exception:
            self._recents = []

        # Central widget: header bar above a horizontal splitter (rail | detail).
        central = QtWidgets.QWidget()
        central_layout = QtWidgets.QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self.setCentralWidget(central)

        # ---- Header bar: brand wordmark + warmth status + model badge + ⋯ ----
        # Header is the only place the Holler brand asserts itself in the
        # main UI — warmer near-black background (vs the cold _BG body),
        # a 2px amber bottom edge as the brand signature, and the wordmark
        # widget on the left followed by a thin vertical divider before
        # the live status chips. Anything brand-colored stops at this bar
        # so the workspace below stays neutral.
        header = QtWidgets.QFrame()
        # Scoped by objectName: a bare `QFrame` rule also matched every QLabel
        # (a QFrame subclass) in the bar, underlining each chip.
        header.setObjectName("sessionsHeader")
        header.setStyleSheet(
            f"QFrame#sessionsHeader {{ background: {_HEADER_BG}; "
            f"border-bottom: 1px solid {_HEADER_BORDER}; }}"
            f"QFrame#sessionsHeader QLabel {{ background: transparent; }}"
        )
        header.setFixedHeight(52)
        h_layout = QtWidgets.QHBoxLayout(header)
        h_layout.setContentsMargins(theme.SPACE_LG, theme.SPACE_SM, theme.SPACE_MD, theme.SPACE_SM)
        h_layout.setSpacing(theme.SPACE_SM)

        wordmark = HollerWordmark()
        h_layout.addWidget(wordmark, 0, QtCore.Qt.AlignVCenter)

        sep = QtWidgets.QFrame()
        sep.setFixedWidth(1)
        sep.setFixedHeight(22)
        sep.setStyleSheet(f"background: {_HEADER_BORDER};")
        h_layout.addWidget(sep, 0, QtCore.Qt.AlignVCenter)

        # Status chips — each is a small dot + label, mono-numerals for any
        # numeric content. Three semantic clusters separated by thin vertical
        # dividers so the eye lands instead of scanning a wall of " · " chips:
        #   runtime (warmth) | session (usage) | budget (quota)
        # Each divider tracks the visibility of the chip it precedes.
        chip_qss = (
            f"QLabel {{ color: {_FG_DIM}; font-size: {theme.FONT_SM}pt; padding: 0; }}"
        )

        def _chip_divider() -> QtWidgets.QFrame:
            d = QtWidgets.QFrame()
            d.setFixedWidth(1)
            d.setFixedHeight(14)
            d.setStyleSheet(f"background: {_HEADER_BORDER};")
            return d

        self._warmth = QtWidgets.QLabel("Starting…")
        self._warmth.setStyleSheet(chip_qss)
        self._warmth.setTextFormat(QtCore.Qt.RichText)
        self._warmth.setAccessibleName("Status: Starting")
        h_layout.addWidget(self._warmth, 0, QtCore.Qt.AlignVCenter)

        self._usage_div = _chip_divider()
        self._usage_div.setVisible(False)
        h_layout.addWidget(self._usage_div, 0, QtCore.Qt.AlignVCenter)
        self._usage = QtWidgets.QLabel("")
        self._usage.setStyleSheet(chip_qss)
        h_layout.addWidget(self._usage, 0, QtCore.Qt.AlignVCenter)
        # Narrow windows (< HEADER_COMPACT_W): usage + quota fold into this
        # one chip; its tooltip carries both breakdowns.
        self._usage_compact = QtWidgets.QLabel("")
        self._usage_compact.setStyleSheet(chip_qss)
        self._usage_compact.setVisible(False)
        h_layout.addWidget(self._usage_compact, 0, QtCore.Qt.AlignVCenter)
        self._header_compact = False
        self._usage_short = ""
        self._quota_short = ""
        self._quota_color = _FG_DIM

        # Anthropic quota readout — pulled from SDK RateLimitEvent (last-known
        # value per window type, persisted across restarts so the operator
        # doesn't see a blank label between transition events).
        self._quota_div = _chip_divider()
        self._quota_div.setVisible(False)
        h_layout.addWidget(self._quota_div, 0, QtCore.Qt.AlignVCenter)
        self._quota = QtWidgets.QLabel("")
        self._quota.setStyleSheet(chip_qss)
        self._quota.setVisible(False)
        h_layout.addWidget(self._quota, 0, QtCore.Qt.AlignVCenter)
        # Titler activity badge — surfaces silent auto-title failures and live
        # backfill progress. Hidden when everything is idle and successful.
        self._titler_status = QtWidgets.QLabel("")
        self._titler_status.setStyleSheet(chip_qss)
        self._titler_status.setToolTip(
            "Automatic session titles. Right-click a session → Regenerate title to retry."
        )
        self._titler_status.setVisible(False)
        h_layout.addWidget(self._titler_status, 0, QtCore.Qt.AlignVCenter)
        # Build chip — which build is running vs. what's installed. Hidden while
        # running the newest tested build with nothing uncommitted; otherwise it
        # says a restart will pick up an update, that one failed its tests, or
        # that edits in the source checkout won't ship until committed.
        self._release_chip = QtWidgets.QPushButton("")
        self._release_chip.setCursor(QtCore.Qt.PointingHandCursor)
        self._release_chip.setFlat(True)
        self._release_chip.setVisible(False)
        self._release_chip.setAccessibleName("Build status")
        self._release_chip.setToolTip("Build status")
        self._release_chip.clicked.connect(self._on_release_chip)
        h_layout.addWidget(self._release_chip, 0, QtCore.Qt.AlignVCenter)
        self._release_status = None
        self._release_status_ready.connect(self._apply_release_status)
        self._release_timer = QtCore.QTimer(self)
        self._release_timer.setInterval(20_000)
        self._release_timer.timeout.connect(self._poll_release_status)
        self._release_timer.start()
        QtCore.QTimer.singleShot(1500, self, self._poll_release_status)
        h_layout.addStretch(1)
        # Routing pill — "Next PTT → continues / starts fresh". The single most
        # important piece of state that used to be implicit. Lives in the header
        # so it's always visible regardless of which session you're scrolled to.
        # Outline-only so it reads as STATE; the amber model pill stays the only
        # filled pill on the right and never competes with the brand.
        # Account pill — which Claude account every session runs as. Click to
        # switch, add or forget accounts (holler/accounts.py).
        self._account_btn = QtWidgets.QPushButton("Claude account")
        self._account_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._account_btn.setIcon(iconset.icon("user", _FG_DIM, theme.ROW_ICON))
        self._account_btn.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        self._account_btn.setAccessibleName("Claude account")
        self._account_btn.setToolTip("Switch Claude account")
        self._account_btn.setStyleSheet(
            f"QPushButton {{ color: {_FG_DIM}; background: transparent; "
            f"border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_PILL}px; {theme.PILL_BOX} "
            f"padding-left: {theme.SPACE_SM}px; padding-right: {theme.SPACE_MD}px; "
            f"font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; }}"
            f"QPushButton:hover {{ border-color: {theme.BORDER_STRONG}; color: {_FG}; }}"
            f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
        )
        self._account_btn.clicked.connect(self._show_account_menu)
        h_layout.addWidget(self._account_btn, 0, QtCore.Qt.AlignVCenter)
        self._account = None
        self._accounts: list = []
        self._account_restart_pending = False
        self._account_login_from: Optional[str] = None
        self._account_login_deadline = 0.0
        self._account_ready.connect(self._apply_accounts)
        self._account_timer = QtCore.QTimer(self)
        self._account_timer.setInterval(60_000)
        self._account_timer.timeout.connect(self._poll_accounts)
        self._account_timer.start()
        # Fast poll while a Terminal sign-in is in progress.
        self._account_login_timer = QtCore.QTimer(self)
        self._account_login_timer.setInterval(2_000)
        self._account_login_timer.timeout.connect(self._poll_accounts)
        QtCore.QTimer.singleShot(800, self, self._poll_accounts)

        # Agents toggle — shows/hides the Agents pane; its label carries the
        # live count of running subagents across every session.
        self._agents_btn = QtWidgets.QPushButton("Agents")
        self._agents_btn.setCheckable(True)
        self._agents_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._agents_btn.setIcon(iconset.icon("agents", _FG_DIM, theme.ROW_ICON))
        self._agents_btn.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        self._agents_btn.setToolTip("Show the agents Claude is running in parallel")
        self._agents_btn.setAccessibleName("Agents")
        self._agents_btn.setStyleSheet(
            f"QPushButton {{ color: {_FG_DIM}; background: transparent; "
            f"border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_PILL}px; {theme.PILL_BOX} "
            f"padding-left: {theme.SPACE_SM}px; padding-right: {theme.SPACE_MD}px; "
            f"font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; }}"
            f"QPushButton:hover {{ border-color: {theme.BORDER_STRONG}; color: {_FG}; }}"
            f"QPushButton:checked {{ color: {_ACCENT}; border-color: {theme.ACCENT_PILL_BORDER}; "
            f"background: {theme.SELECTION_BG}; }}"
            f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
        )
        self._agents_btn.clicked.connect(self._on_agents_btn)
        h_layout.addWidget(self._agents_btn, 0, QtCore.Qt.AlignVCenter)
        self._routing_pill = QtWidgets.QLabel("")
        self._routing_pill.setStyleSheet(
            f"color: {_ACCENT}; background: transparent; "
            f"border: 1px solid {theme.ACCENT_PILL_BORDER}; border-radius: {theme.RADIUS_PILL}px; {theme.PILL_BOX} "
            f"padding-left: {theme.SPACE_MD}px; padding-right: {theme.SPACE_MD}px; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};"
        )
        self._routing_pill.setVisible(False)
        h_layout.addWidget(self._routing_pill, 0, QtCore.Qt.AlignVCenter)
        # Model badge — brand-tinted pill so the active model reads at a
        # glance. Tracked uppercase echoes the wordmark voice (HOLLER → OPUS).
        # Soft border + glow bg keep it grounded without competing visually.
        self._model_badge = QtWidgets.QPushButton("")
        self._model_badge.setCursor(QtCore.Qt.PointingHandCursor)
        _model_font = ui_font(theme.FONT_MODEL_PILL)
        _model_font.setWeight(QtGui.QFont.Bold)
        _model_font.setLetterSpacing(QtGui.QFont.PercentageSpacing, theme.TRACKING_BRAND_PCT)
        self._model_badge.setFont(_model_font)
        self._model_badge.setStyleSheet(
            f"QPushButton {{ color: {_BRAND_AMBER}; "
            f"background: {_BRAND_AMBER_GLOW}; "
            f"border: 1px solid {_BRAND_AMBER_SOFT}; border-radius: {theme.RADIUS_PILL}px; {theme.PILL_BOX} "
            f"padding-left: {theme.SPACE_MD}px; padding-right: {theme.SPACE_MD}px; }}"
            f"QPushButton:hover {{ color: {theme.FG_ON_EMPHASIS}; "
            f"background: {theme.BRAND_AMBER_HOVER_WASH}; "
            f"border-color: {_BRAND_AMBER}; }}"
            f"QPushButton:pressed {{ background: {_BRAND_AMBER_GLOW}; }}"
            f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
        )
        self._model_badge.setToolTip("Model for new turns — click to switch")
        self._model_badge.setAccessibleName("Model")
        self._model_badge.clicked.connect(self.model_cycle_requested.emit)
        h_layout.addWidget(self._model_badge, 0, QtCore.Qt.AlignVCenter)

        # Header icon buttons: A− / A+ (text size), restart, menu. Outline
        # squares with neutral hover — brand amber stays on the wordmark and
        # the model pill only (BRAND.md).
        _icon_btn_qss = (
            f"QPushButton {{ background: transparent; "
            f"border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; padding: 0; }}"
            f"QPushButton:hover {{ background: {_BG_ROW}; border-color: {theme.BORDER_STRONG}; }}"
            f"QPushButton:pressed {{ background: {_BG_ROW_HOVER}; }}"
            f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
        )
        # Text-size controls — A− / A+ bump the global UI scale (Holler
        # restarts to apply). Grouped tight so they read as one control.
        _btn = theme.ICON_BUTTON
        self._text_smaller_btn = _icon_button(
            "refresh", "Smaller text", "Smaller text (restarts Holler)",
            size=_btn, glyph=theme.ICON_GLYPH, qss=_icon_btn_qss)
        self._text_smaller_btn.setIcon(_text_size_icon(bigger=False, color=_FG_DIM, size=theme.ICON_GLYPH))
        self._text_smaller_btn.clicked.connect(lambda: self.text_scale_bumped.emit(-1))
        h_layout.addWidget(self._text_smaller_btn, 0, QtCore.Qt.AlignVCenter)

        self._text_bigger_btn = _icon_button(
            "refresh", "Larger text", "Larger text (restarts Holler)",
            size=_btn, glyph=theme.ICON_GLYPH, qss=_icon_btn_qss)
        self._text_bigger_btn.setIcon(_text_size_icon(bigger=True, color=_FG_DIM, size=theme.ICON_GLYPH))
        self._text_bigger_btn.clicked.connect(lambda: self.text_scale_bumped.emit(1))
        h_layout.addWidget(self._text_bigger_btn, 0, QtCore.Qt.AlignVCenter)

        # Restart — relaunches Holler in place with one click.
        self._restart_btn = _icon_button(
            "refresh", "Restart Holler", "Restart Holler",
            size=_btn, glyph=theme.ICON_GLYPH, qss=_icon_btn_qss)
        self._restart_btn.clicked.connect(self.restart_requested.emit)
        h_layout.addWidget(self._restart_btn, 0, QtCore.Qt.AlignVCenter)

        self._menu_btn = _icon_button(
            "more-horizontal", "Menu", "Menu",
            size=_btn, glyph=theme.ICON_GLYPH,
            qss=_icon_btn_qss.replace(f"border: 1px solid {theme.BORDER};", "border: 1px solid transparent;"))
        self._menu_btn.clicked.connect(self._show_header_menu)
        h_layout.addWidget(self._menu_btn, 0, QtCore.Qt.AlignVCenter)
        central_layout.addWidget(header)

        # Splitter: left rail | right detail.
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        # An 8px handle with a visible resting color + accent-on-hover gives the
        # operator an actual grab target; the stock 4px near-background handle
        # read as "the slider doesn't work" because it was invisible and too
        # thin to reliably hit.
        splitter.setHandleWidth(8)
        central_layout.addWidget(splitter, 1)

        # Toast for inline notices (replaces the mini-panel toast).
        from holler.panel import Toast
        self._toast = Toast()

        # Per-event repaints are now coalesced behind a single 250ms timer so
        # that a flood of job_updated signals (e.g. during a fast stream)
        # collapses into one round of warmth / usage / per-row total updates
        # instead of N separate ones. JobStore already coalesces job_updated
        # to ~12.5Hz; this is the second-stage debounce for the heavier
        # GROUP BY + per-widget set_totals work that runs PER ROW.
        self._row_refresh_timer = QtCore.QTimer(self)
        self._row_refresh_timer.setSingleShot(True)
        self._row_refresh_timer.setInterval(250)
        self._row_refresh_timer.timeout.connect(self._coalesced_row_refresh)
        # Live JobStore counts → warmth label.
        self._job_store.job_added.connect(self._schedule_row_refresh)
        self._job_store.job_updated.connect(self._schedule_row_refresh)
        # Anthropic rate-limit transitions → header quota chip. The chip
        # countdown ("resets in 2h 14m") needs to tick independently of the
        # SDK, so a 30-second timer re-renders the label from the cached
        # snapshot. 30s is granular enough for human reading; sub-minute
        # precision on a multi-hour countdown buys nothing.
        self._job_store.rate_limit_changed.connect(self._refresh_quota)
        self._quota_timer = QtCore.QTimer(self)
        self._quota_timer.setInterval(30_000)
        self._quota_timer.timeout.connect(self._refresh_quota)
        self._quota_timer.start()
        # Paint once at boot so any persisted snapshot is visible immediately.
        QtCore.QTimer.singleShot(0, self, self._refresh_quota)
        # _refresh_visible_totals piggy-backs on the same 250ms coalesced
        # repaint scheduler as warmth/usage.

        # Titler status — poll the AutoTitler counters every 2 s. Cheap (just a
        # dict copy under a mutex) and only repaints the header chip when the
        # text actually changes.
        self._titler_timer = QtCore.QTimer(self)
        self._titler_timer.setInterval(2000)
        self._titler_timer.timeout.connect(self._refresh_titler_status)
        self._titler_timer.start()

        # ---- left rail
        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(theme.SPACE_MD, theme.SPACE_MD, theme.SPACE_MD, theme.SPACE_MD)
        left_layout.setSpacing(theme.SPACE_SM)

        # New session — a split button: the main part starts a session with
        # no profile in one click; the arrow opens the profile menu (every
        # profile, plus the full picker dialog).
        new_row = QtWidgets.QHBoxLayout()
        new_row.setContentsMargins(0, 0, 0, 0)
        new_row.setSpacing(0)
        self._new_btn = QtWidgets.QPushButton("New session")
        self._new_btn.setIcon(iconset.icon("plus", theme.FG_ON_EMPHASIS, theme.ROW_ICON))
        self._new_btn.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        self._new_btn.setToolTip(f"Start a new session ({_key_hint('Ctrl+N')})")
        self._new_btn.setAccessibleName("New session")
        self._new_btn.setStyleSheet(theme.button_qss(
            "primary", pad_v=theme.SPACE_SM, pad_h=theme.SPACE_MD, font_size=theme.FONT_SM,
            extra=self._NEW_MAIN_CORNERS))
        # Resting style, captured once: the click-confirm flash restores this
        # (re-reading styleSheet() mid-flash would capture the green flash).
        self._new_btn_qss = self._new_btn.styleSheet()
        self._new_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._new_btn.clicked.connect(self._new_session_default)
        new_row.addWidget(self._new_btn, 1)
        self._new_menu_btn = _icon_button(
            "chevron-down", "Choose a profile for the new session",
            f"New session with a profile… ({_key_hint('Ctrl+Shift+N')})",
            size=theme.ICON_BUTTON, glyph=theme.ROW_ICON, color=theme.FG_ON_EMPHASIS,
            qss=theme.button_qss(
                "primary", pad_v=0, pad_h=0,
                extra=(f"border-top-left-radius: 0; border-bottom-left-radius: 0; "
                       f"border-left: 1px solid {theme.ACCENT_EMPHASIS_PRESSED};")))
        # Fixed width; the height stretches to the main half's so the two
        # read as one button.
        self._new_menu_btn.setMinimumSize(theme.ICON_BUTTON + theme.SPACE_XS, 0)
        self._new_menu_btn.setMaximumSize(theme.ICON_BUTTON + theme.SPACE_XS, _QWIDGETSIZE_MAX)
        self._new_menu_btn.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)
        self._new_menu_btn.clicked.connect(self._show_new_session_menu)
        new_row.addWidget(self._new_menu_btn, 0)
        left_layout.addLayout(new_row)

        self._search = QtWidgets.QLineEdit()
        self._search.setObjectName("searchBox")
        self._search.setPlaceholderText(f"Search sessions ({_key_hint('Ctrl+F')})")
        self._search.setAccessibleName("Search sessions")
        self._search.setClearButtonEnabled(True)
        # Qt's built-in clear (×) button is icon-only and unnamed.
        for _clear in self._search.findChildren(QtWidgets.QToolButton):
            _clear.setAccessibleName("Clear search")
            _clear.setToolTip("Clear search")
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._refresh_list)
        self._search.textChanged.connect(lambda _: self._search_timer.start())
        # Esc inside the search box clears it (back to full list).
        esc_clear = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self._search)
        esc_clear.setContext(QtCore.Qt.WidgetShortcut)
        esc_clear.activated.connect(self._clear_search)
        # Search + sort share a row, so the filter chips below get the full
        # rail width (all five fit at the default split).
        search_row = QtWidgets.QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(theme.SPACE_XS)
        search_row.addWidget(self._search, 1)
        left_layout.addLayout(search_row)

        # Filter chips live inside a horizontal QScrollArea so they don't
        # pin the left rail to ~360 px (the natural width of all five
        # chips). When the user drags the splitter narrow, chips scroll
        # horizontally instead of forcing the rail wider.
        chips_row = QtWidgets.QHBoxLayout()
        chips_row.setSpacing(theme.SPACE_SM)
        chips_row.setContentsMargins(0, 0, 0, 0)

        chips_scroll = QtWidgets.QScrollArea()
        chips_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        chips_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        chips_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        chips_scroll.setWidgetResizable(True)
        # Thinner than the app-wide scrollbar so the strip stays compact.
        chips_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:horizontal { height: 6px; background: transparent; }"
            f"QScrollBar::handle:horizontal {{ margin: 1px; border-radius: {theme.SPACE_2XS}px; }}"
        )
        chips_scroll.setMinimumWidth(0)

        chips_container = QtWidgets.QWidget()
        chips_container.setStyleSheet("background: transparent;")
        chips_inner = QtWidgets.QHBoxLayout(chips_container)
        chips_inner.setContentsMargins(0, 0, 0, 0)
        chips_inner.setSpacing(theme.SPACE_XS)

        self._chips: dict[str, QtWidgets.QPushButton] = {}
        for key, label in (
            ("active", "Active"),
            ("done", "Done"),
            ("running", "Running"),
            ("pinned", "Pinned"),
            ("archived", "Archived"),
        ):
            btn = QtWidgets.QPushButton(label)
            btn.setProperty("class", "chip")
            btn.setProperty("active", "true" if key == self._current_filter else "false")
            # Checkable so assistive tech hears which filter is on (the
            # active chip is also set semibold, not only recoloured).
            btn.setCheckable(True)
            btn.setChecked(key == self._current_filter)
            btn.setAccessibleName(f"{label} filter")
            btn.setStyleSheet(_QSS)  # reapply so the property selector matches
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, k=key: self._set_filter(k))
            self._chips[key] = btn
            chips_inner.addWidget(btn)
        chips_inner.addStretch(1)
        chips_scroll.setWidget(chips_container)
        # Height the scroll area to the natural chip height plus a few
        # pixels for the thin horizontal scrollbar so the layout doesn't
        # jump when the bar appears.
        _chip_h = next(iter(self._chips.values())).sizeHint().height()
        chips_scroll.setFixedHeight(_chip_h + 8)

        # Sort dropdown button sits at the end of the search row (outside the
        # chips' scroll area), so it remains visible at every rail width.
        # Same height as the search box (it stretches to the row).
        self._sort_btn = QtWidgets.QPushButton()
        self._sort_btn.setIcon(_sort_icon(_FG_DIM, theme.ROW_ICON))
        self._sort_btn.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        self._sort_btn.setStyleSheet(
            f"QPushButton {{ background: {_BG_ROW}; color: {_FG_DIM}; border: 1px solid {theme.BORDER}; "
            f"border-radius: {theme.RADIUS_MD}px; padding: 0 {theme.SPACE_SM}px; font-size: {theme.FONT_XS}pt; }}"
            f"QPushButton:hover {{ color: {_FG}; border-color: {theme.BORDER_STRONG}; }}"
            f"QPushButton:pressed {{ background: {_BG_ROW_HOVER}; }}"
            f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
        )
        self._sort_btn.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)
        self._sort_btn.setMinimumHeight(0)
        self._sort_btn.setMaximumHeight(_QWIDGETSIZE_MAX)
        self._sort_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._sort_btn.setToolTip("Sort sessions")
        self._sync_sort_button()
        self._sort_btn.clicked.connect(self._show_sort_menu)
        search_row.addWidget(self._sort_btn, 0)

        chips_row.addWidget(chips_scroll, 1)
        left_layout.addLayout(chips_row)

        # Result count strip — shows during search to remove the "did it
        # find anything?" guesswork.
        self._results_label = QtWidgets.QLabel("")
        self._results_label.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; padding: {theme.SPACE_2XS}px {theme.SPACE_2XS}px 0 {theme.SPACE_2XS}px;")
        self._results_label.setVisible(False)
        left_layout.addWidget(self._results_label)

        self._list = QtWidgets.QListWidget()
        self._list.setAccessibleName("Sessions")
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self._list.setMouseTracking(True)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        # Track whether a selection change came from a mouse click (focus jumps
        # to the composer) or keyboard navigation (focus stays on the list so
        # arrow keys keep walking rows).
        self._list_mouse_select = False
        self._programmatic_select = False
        self._list_press_flag = _MousePressFlag(self)
        self._list.viewport().installEventFilter(self._list_press_flag)
        # Arrow keys keep focus on the list now; plain typing there still
        # belongs to the composer (as it did when selection stole focus).
        self._list_type_forward = _TypeToComposer(self)
        self._list.installEventFilter(self._list_type_forward)
        self._list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        # Double-click on a row title → inline rename in the row (same
        # affordance as F2). Falls through if the clicked item is a header.
        self._list.itemDoubleClicked.connect(self._rename_item)
        self._rename_editor: Optional[_RenameEdit] = None
        self._rename_sid: Optional[int] = None
        self._refresh_after_rename = False
        # Scroll position drives the sticky-bucket label.
        self._list.verticalScrollBar().valueChanged.connect(self._refresh_sticky_bucket)
        # The view has already scrolled the new current row into view (its
        # own currentChanged runs first) — nudge it out from under the label.
        self._list.currentItemChanged.connect(lambda cur, _prev: self._keep_row_clear_of_sticky(cur))
        left_layout.addWidget(self._list, 1)
        # Sticky bucket label — always shows which time-bucket the topmost
        # visible row falls into so the user never loses context while
        # scrolling deep into TODAY. An overlay pinned to the top of the list
        # viewport (not a layout row), so showing / hiding it never shifts
        # the list. Clicks pass through to the row underneath.
        self._sticky_bucket = QtWidgets.QLabel("", self._list)
        self._sticky_bucket.setStyleSheet(
            f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; font-weight: {theme.WEIGHT_BOLD}; "
            f"letter-spacing: {theme.TRACKING_CAPS_SM}; padding: {theme.SPACE_SM}px {theme.SPACE_MD}px {theme.SPACE_2XS}px {theme.SPACE_MD}px; "
            f"background: {_BG}; border-bottom: 1px solid {theme.BORDER_SUBTLE};"
        )
        self._sticky_bucket.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self._sticky_bucket.setVisible(False)
        # Rows are sized to their content at the current rail width, so the
        # list re-measures them (and re-pins the overlay) on every resize.
        self._list_viewport_watch = _ViewportResizeWatch(self._on_list_viewport_resized, self)
        self._list.viewport().installEventFilter(self._list_viewport_watch)
        self._fit_rows_timer = QtCore.QTimer(self)
        self._fit_rows_timer.setSingleShot(True)
        self._fit_rows_timer.setInterval(0)
        self._fit_rows_timer.timeout.connect(self._fit_row_heights)
        # Delete-key archive — scoped to the list so it doesn't fire when the
        # search box has focus.
        del_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self._list)
        del_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        del_shortcut.activated.connect(self._archive_focused_row)
        # Backspace too: the Mac "delete" key sends Backspace (forward-Delete
        # needs fn). WidgetShortcut → only while the list itself has focus, so
        # Backspace in the search box / composer is untouched.
        bksp_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Backspace), self._list)
        bksp_shortcut.setContext(QtCore.Qt.WidgetShortcut)
        # Selection only (no current-row fallback): after clicking blank list
        # space the current row stays set but nothing looks selected, and a
        # Backspace there must not archive an invisible target.
        bksp_shortcut.activated.connect(
            lambda: self._archive_focused_row(require_selection=True))
        # Space toggles reviewed/done on the focused row.
        space_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), self._list)
        space_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        space_shortcut.activated.connect(self._toggle_reviewed_focused_row)
        # F2 renames focused row (Windows convention).
        f2_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_F2), self._list)
        f2_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        f2_shortcut.activated.connect(self._rename_focused_row)
        # Ctrl+P → pin/unpin focused row.
        pin_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+P"), self._list)
        pin_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        pin_shortcut.activated.connect(self._toggle_pin_focused_row)

        # Allow the splitter handle to drag the rail down to a narrow
        # width — the chips row scrolls horizontally and titles elide,
        # so 160 px still shows the search box and list rows cleanly.
        left.setMinimumWidth(160)
        splitter.addWidget(left)

        # ---- right pane
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Status strip at the top of the right pane. Where the next
        # push-to-talk goes is shown once — the header's routing pill (plus
        # the composer placeholder) — so this strip only appears for
        # transient states the pill can't express (a multi-row selection).
        # `_ptt_status` still tracks the routing sentence for those surfaces.
        self._status_strip = QtWidgets.QFrame()
        self._status_strip.setObjectName("statusStrip")
        self._status_strip.setStyleSheet(
            f"QFrame#statusStrip {{ background: {theme.SUNKEN}; border-bottom: 1px solid {theme.BORDER_SUBTLE}; }}"
            f"QFrame#statusStrip QLabel {{ background: transparent; }}"
        )
        status_bar = QtWidgets.QHBoxLayout(self._status_strip)
        status_bar.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_MD, theme.SPACE_SM)
        status_bar.setSpacing(theme.SPACE_SM)
        self._ptt_status = QtWidgets.QLabel(f"Hold {self._hotkey_label} to start a new session")
        self._ptt_status.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_SM}pt;")
        status_bar.addWidget(self._ptt_status, 1)
        self._status_strip.setVisible(False)
        right_layout.addWidget(self._status_strip)

        # ---- Superintendent Chain bar (visible only on the evidence inbox) ----
        # Deterministic alternative to voice cues: Start chain arms hold-mode
        # (every dump joins one draft), File it flushes the draft as ONE
        # evidence item, Discard drops it.
        self._sup_inbox_sid: Optional[int] = None
        self._sup_chain_armed = False
        self._sup_chain_count = 0
        self._sup_mode = "deploy"
        # Interactive-chain state for the ACTIVE non-inbox session — the same
        # Chain bar, but "File it" bundles the held captures into one turn on the
        # session instead of an evidence item. `_isc_sid` is the session the
        # pushed count belongs to, so switching sessions never shows a stale
        # count before the runner re-pushes.
        self._isc_sid: Optional[int] = None
        self._isc_armed = False
        self._isc_count = 0
        self._chain_bar = QtWidgets.QFrame()
        self._chain_bar.setObjectName("chainBar")
        self._chain_bar.setStyleSheet(
            f"QFrame#chainBar {{ background: {theme.SUNKEN}; border-bottom: 1px solid {theme.BORDER_SUBTLE}; }}"
            f"QFrame#chainBar QLabel {{ background: transparent; }}"
        )
        cb_layout = QtWidgets.QHBoxLayout(self._chain_bar)
        cb_layout.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_MD, theme.SPACE_SM)
        cb_layout.setSpacing(theme.SPACE_SM)
        # ---- Evidence-lane toggle (Deploy vs Investigate/IR) ----
        # A segmented switch on the far left: which superintendent lane the next
        # dump/chain is filed into. Deploy = autonomous remediation → prod;
        # Investigate = IR/forensics worker that reports back, never deploys.
        self._mode_deploy_btn = QtWidgets.QPushButton("⚙ Deploy")
        self._mode_ir_btn = QtWidgets.QPushButton("🔍 Investigate")
        for b in (self._mode_deploy_btn, self._mode_ir_btn):
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setCheckable(True)
            cb_layout.addWidget(b, 0)
        self._mode_deploy_btn.setAccessibleName("Deploy lane")
        self._mode_ir_btn.setAccessibleName("Investigate lane")
        self._mode_deploy_btn.clicked.connect(lambda: self._on_mode_click("deploy"))
        self._mode_ir_btn.clicked.connect(lambda: self._on_mode_click("investigation"))
        # Worker-model badge — picks which model spawned worker sessions run on
        # (independent of the interactive header badge). Click cycles it; the
        # runner persists the choice and the next worker adopts it.
        self._sup_model_btn = QtWidgets.QPushButton("")
        self._sup_model_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._sup_model_btn.setToolTip(
            "Model for Superintendent workers — click to switch"
        )
        _sup_model_font = ui_font(theme.FONT_MODEL_PILL)
        _sup_model_font.setWeight(QtGui.QFont.Bold)
        _sup_model_font.setLetterSpacing(QtGui.QFont.PercentageSpacing, theme.TRACKING_BRAND_PCT)
        self._sup_model_btn.setFont(_sup_model_font)
        self._sup_model_btn.setIcon(iconset.icon("wrench", _BRAND_AMBER, theme.ROW_ICON))
        self._sup_model_btn.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        self._sup_model_btn.setStyleSheet(
            f"QPushButton {{ color: {_BRAND_AMBER}; "
            f"background: {_BRAND_AMBER_GLOW}; "
            f"border: 1px solid {_BRAND_AMBER_SOFT}; border-radius: {theme.RADIUS_PILL}px; {theme.PILL_BOX} "
            f"padding-left: {theme.SPACE_MD}px; padding-right: {theme.SPACE_MD}px; }}"
            f"QPushButton:hover {{ color: {theme.FG_ON_EMPHASIS}; "
            f"background: {theme.BRAND_AMBER_HOVER_WASH}; "
            f"border-color: {_BRAND_AMBER}; }}"
            f"QPushButton:pressed {{ background: {_BRAND_AMBER_GLOW}; }}"
            f"QPushButton:focus {{ border-color: {_ACCENT}; }}"
        )
        self._sup_model_btn.setAccessibleName("Worker model")
        self._sup_model_btn.clicked.connect(
            self.superintendent_model_cycle_requested.emit)
        cb_layout.addWidget(self._sup_model_btn, 0)
        self._chain_label = _ElideRightLabel("")
        self._chain_label.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_SM}pt;")
        cb_layout.addWidget(self._chain_label, 1)
        _chain_btn_qss = theme.button_qss("neutral", font_size=theme.FONT_SM)
        self._chain_start_btn = QtWidgets.QPushButton("🔗 Start chain")
        self._chain_file_btn = QtWidgets.QPushButton("▶ Start session")
        self._chain_file_btn.setStyleSheet(theme.button_qss("primary", font_size=theme.FONT_SM))
        self._chain_discard_btn = QtWidgets.QPushButton("Discard")
        self._chain_discard_btn.setIcon(iconset.icon("close", _FG_DIM, theme.ROW_ICON))
        self._chain_discard_btn.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        for b in (self._chain_start_btn, self._chain_discard_btn):
            b.setStyleSheet(_chain_btn_qss)
        for b in (self._chain_start_btn, self._chain_file_btn, self._chain_discard_btn):
            b.setCursor(QtCore.Qt.PointingHandCursor)
            cb_layout.addWidget(b, 0)
        self._chain_start_btn.setAccessibleName("Start chain")
        self._chain_file_btn.setAccessibleName("Start session from chain")
        self._chain_discard_btn.setAccessibleName("Discard chain")
        self._chain_start_btn.clicked.connect(
            lambda: self.superintendent_chain_action.emit("arm"))
        self._chain_file_btn.clicked.connect(
            lambda: self.superintendent_chain_action.emit("file"))
        self._chain_discard_btn.clicked.connect(
            lambda: self.superintendent_chain_action.emit("discard"))
        right_layout.addWidget(self._chain_bar)

        self._detail = DetailView()
        self._detail.set_captures_dir(captures_dir)
        # Esc / × cancel button in the live bar → emit cancel_requested upward.
        self._detail.cancel_requested.connect(self._on_cancel_requested)
        # Transcript Copy links confirm through our notice (optional signal).
        if hasattr(self._detail, "copy_notice"):
            self._detail.copy_notice.connect(lambda text: self.show_inline_notice(text))
        # Transcript | Agents pane. Nested in the right pane so the outer
        # rail/right splitter (and its persisted two-entry sizes) is untouched.
        self._agents_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._agents_split.setChildrenCollapsible(False)
        self._agents_split.setHandleWidth(8)
        self._agents_split.addWidget(self._detail)
        self._agents_panel = AgentsPanel(self._job_store)
        self._agents_panel.stop_agent_requested.connect(self.stop_agent_requested.emit)
        self._agents_panel.close_requested.connect(lambda: self._set_agents_pane(False, user=True))
        self._agents_split.addWidget(self._agents_panel)
        self._agents_split.setStretchFactor(0, 1)
        self._agents_split.setStretchFactor(1, 0)
        self._agents_panel.setVisible(False)
        # Sessions where the operator closed the pane while agents ran — don't
        # pop it open again for them.
        self._agents_dismissed: set[int] = set()
        self._job_store.agents_changed.connect(self._on_agents_changed)
        right_layout.addWidget(self._agents_split, 1)

        # ---- Text input bar (bottom of right pane) ----
        # Lets the user type instead of pressing the hotkey. Routes through
        # the same dispatch as PTT — selected session → continues it,
        # nothing selected → creates new. No screenshot attached (text-only).
        text_bar = QtWidgets.QFrame()
        text_bar.setObjectName("composerBar")
        text_bar.setStyleSheet(
            f"QFrame#composerBar {{ background: {theme.SUNKEN}; border-top: 1px solid {theme.BORDER_SUBTLE}; }}"
        )
        tb_outer = QtWidgets.QVBoxLayout(text_bar)
        tb_outer.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_MD, theme.SPACE_SM)
        tb_outer.setSpacing(theme.SPACE_XS)
        # Transient capture status ("Transcribing…") — set_capture_status().
        self._capture_status = QtWidgets.QLabel("")
        self._capture_status.setObjectName("captureStatus")
        self._capture_status.setStyleSheet(
            f"QLabel#captureStatus {{ color: {_ACCENT}; background: transparent; "
            f"font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; }}"
        )
        self._capture_status.setAccessibleName("Capture status")
        self._capture_status.setVisible(False)
        tb_outer.addWidget(self._capture_status)
        self._capture_status_timer = QtCore.QTimer(self)
        self._capture_status_timer.setSingleShot(True)
        self._capture_status_timer.setInterval(30_000)
        self._capture_status_timer.timeout.connect(self.clear_capture_status)
        self._job_store.job_added.connect(self._on_job_added_capture_status)
        tb_layout = QtWidgets.QHBoxLayout()
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(theme.SPACE_SM)
        tb_outer.addLayout(tb_layout)
        self._text_input = _TextInput()
        self._text_input.setAccessibleName("Message")
        self._text_input.setStyleSheet(
            f"QPlainTextEdit {{ background: {_BG_ROW}; color: {_FG}; "
            f"border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; padding: {theme.SPACE_SM}px; "
            f"font-size: {theme.FONT_SM}pt; }}"
            f"QPlainTextEdit:focus {{ border-color: {_ACCENT}; }}"
        )
        self._text_input.submit_requested.connect(self._on_text_submit)
        tb_layout.addWidget(self._text_input, 1)
        self._send_btn = QtWidgets.QPushButton("Send")
        self._send_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._send_btn.setStyleSheet(theme.button_qss(
            "primary", pad_v=theme.SPACE_SM, pad_h=theme.SPACE_LG, font_size=theme.FONT_SM,
            extra="min-width: 60px;"))
        self._send_btn.setAccessibleName("Send message")
        self._send_btn.clicked.connect(self._on_text_submit)
        tb_layout.addWidget(self._send_btn, 0, QtCore.Qt.AlignBottom)
        self._update_text_input_placeholder()
        right_layout.addWidget(text_bar)

        # Operator habit: tap the conversation transcript expecting to keep
        # typing. Strip click-focus from the transcript surfaces and bounce
        # focus back to the input on press. Mouse-drag selection + right-click
        # → Copy still work; only keyboard focus is redirected.
        self._detail._reply_browser.setFocusPolicy(QtCore.Qt.NoFocus)
        self._detail._reply_browser.viewport().setFocusPolicy(QtCore.Qt.NoFocus)
        self._detail._transcript.setFocusPolicy(QtCore.Qt.NoFocus)
        self._focus_keeper = _FocusInputOnClick(self._text_input, self)
        self._detail._reply_browser.viewport().installEventFilter(self._focus_keeper)
        self._detail._transcript.installEventFilter(self._focus_keeper)

        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # Splitter sizing model:
        #   - If the user has manually moved the splitter in the past
        #     ("sessions_splitter_user_set" is truthy), restore their absolute
        #     sash sizes — that's their explicit choice and shouldn't be
        #     overwritten by window resizes.
        #   - Otherwise, treat the split as a *fraction* of the current
        #     window width (default 30/70) so the rail/pane stays
        #     proportional on different monitor sizes.
        saved_sash = _persisted.get("sessions_splitter")
        self._splitter_user_set = bool(_persisted.get("sessions_splitter_user_set", False))
        if (
            self._splitter_user_set
            and isinstance(saved_sash, list)
            and len(saved_sash) == 2
            and all(isinstance(v, (int, float)) for v in saved_sash)
            and saved_sash[0] >= 160
        ):
            splitter.setSizes([int(saved_sash[0]), int(saved_sash[1])])
        else:
            # Default split: 30% rail / 70% detail of current window width.
            # _apply_splitter_fraction runs after the window is laid out so
            # splitter.width() is meaningful; in __init__ we set provisional
            # sizes from the geometry width we just applied.
            geom = self.geometry()
            self._splitter_fraction = 0.30
            rail_w = max(160, int(geom.width() * self._splitter_fraction))
            splitter.setSizes([rail_w, max(SW_MIN_W - rail_w, geom.width() - rail_w)])
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self._splitter = splitter

        # Listen for live job updates so an open detail view can refresh.
        self._job_store.job_updated.connect(self._on_job_updated)
        self._job_store.session_changed.connect(self._on_session_changed)

        # ----- app-wide shortcuts -----
        # Ctrl+N: new session — same as the split button's main half.
        new_shortcut = QtGui.QShortcut(QtGui.QKeySequence.New, self)
        new_shortcut.activated.connect(self._new_session_default)
        # Ctrl+Shift+N: new session with a profile (the picker dialog).
        new_profile_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+N"), self)
        new_profile_shortcut.activated.connect(self._on_new_session_clicked)
        # Ctrl+F: focus search box.
        find_shortcut = QtGui.QShortcut(QtGui.QKeySequence.Find, self)
        find_shortcut.activated.connect(self._focus_search)
        # Ctrl+K: quick switcher (fuzzy match across titles + transcripts).
        switcher_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+K"), self)
        switcher_shortcut.activated.connect(self._open_quick_switcher)
        # Ctrl+Shift+D: pop out the currently-selected session in a detached window.
        popout_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+D"), self)
        popout_shortcut.activated.connect(self._popout_current_session)
        # Ctrl+, (⌘, on macOS): Settings — same handler as ⋯ → Settings….
        # Explicit sequence rather than StandardKey.Preferences, which has no
        # binding on Windows.
        settings_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+,"), self)
        settings_shortcut.activated.connect(self.settings_requested.emit)
        # Ctrl+Q: quit (advertised in the ⋯ menu). macOS already gets ⌘Q from
        # the application menu Qt installs, so only bind it elsewhere.
        if sys.platform != "darwin":
            quit_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Q"), self)
            quit_shortcut.activated.connect(self._request_quit)
        # Cmd/Ctrl+C: copy the live selection. The transcript surfaces are held
        # NoFocus so typing focus stays on the input, which means their built-in
        # keyboard-copy never fires — so we route Copy through the window and
        # pull from whichever surface actually has a selection.
        copy_shortcut = QtGui.QShortcut(QtGui.QKeySequence.Copy, self)
        copy_shortcut.activated.connect(self._copy_selection)
        # The QShortcut only fires when the focused widget doesn't consume the
        # keypress first — but Qt input widgets consume Cmd/Ctrl+C even with no
        # selection (silent no-op). An app-level filter runs ahead of them so the
        # conversation-copy fires regardless of which input holds focus.
        self._copy_filter = _GlobalCopyFilter(self)
        # ⌘Z / Ctrl+Z: the newest toast's Undo, when no text field claims it.
        self._undo_filter = _GlobalUndoFilter(self)
        _app = QtWidgets.QApplication.instance()
        if _app is not None:
            _app.installEventFilter(self._copy_filter)
            _app.installEventFilter(self._undo_filter)
        # Ctrl+1..5: jump between filter chips.
        for i, key in enumerate(["active", "done", "running", "pinned", "archived"], start=1):
            sc = QtGui.QShortcut(QtGui.QKeySequence(f"Ctrl+{i}"), self)
            sc.activated.connect(lambda k=key: self._set_filter(k))

        # Live tool ticker — drives stuck detection + per-row tickers.
        self._tick_timer = QtCore.QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick_live_rows)
        self._tick_timer.start()

        # Fast pulse timer — animates the dot next to each running session in
        # the rail so the operator can see at a glance which sessions are
        # actually alive without staring at the right pane.
        self._pulse_phase = 0
        self._pulse_timer = QtCore.QTimer(self)
        self._pulse_timer.setInterval(280)
        self._pulse_timer.timeout.connect(self._tick_pulse_dots)
        self._pulse_timer.start()

        # The empty-state dashboard (relative times, this-week totals) is
        # re-rendered while it's showing; unchanged HTML is skipped.
        self._dashboard_html = ""
        self._dashboard_timer = QtCore.QTimer(self)
        self._dashboard_timer.setInterval(30_000)
        self._dashboard_timer.timeout.connect(self._refresh_dashboard_if_visible)
        self._dashboard_timer.start()
        self._dashboard_refresh_timer = QtCore.QTimer(self)
        self._dashboard_refresh_timer.setSingleShot(True)
        self._dashboard_refresh_timer.setInterval(400)
        self._dashboard_refresh_timer.timeout.connect(self._refresh_dashboard_if_visible)
        # Dashboard links (recent sessions, first-run actions).
        self._detail._reply_browser.anchorClicked.connect(self._on_dashboard_anchor)

        # Buttons take focus from Tab only, never from a click: a click-focused
        # button kept its accent focus ring after the mouse left (Windows
        # gives buttons StrongFocus), so e.g. a clicked "Done" chip still
        # looked selected after Ctrl+1 switched to Active. Same on both
        # platforms; Tab still reaches every one of them.
        _tab_focus_only(
            self._new_btn, self._new_menu_btn, self._sort_btn, *self._chips.values(),
            self._model_badge, self._text_smaller_btn, self._text_bigger_btn,
            self._restart_btn, self._menu_btn, self._send_btn,
            self._mode_deploy_btn, self._mode_ir_btn, self._sup_model_btn,
            self._chain_start_btn, self._chain_file_btn, self._chain_discard_btn,
        )
        # Keyboard order through the rail, then the composer.
        chain = [self._new_btn, self._new_menu_btn, self._search, self._sort_btn,
                 *self._chips.values(), self._list, self._text_input, self._send_btn]
        for a, b in zip(chain, chain[1:]):
            QtWidgets.QWidget.setTabOrder(a, b)

        self._refresh_list()
        # Initial empty state — no session selected on first open, so paint
        # the actionable dashboard + routing pill rather than the emoji splash.
        self._render_empty_dashboard()
        self._update_routing_pill()
        self._update_chain_bar()

        # ----- monitor-change awareness -----
        # If the user drags the window to a different monitor (or unplugs the
        # one it's on), re-fit to that screen instead of getting stranded or
        # oversized. screenAdded/Removed handles hot-plug. Signals live on the
        # QGuiApplication *instance*, not the class.
        _app = QtGui.QGuiApplication.instance()
        if _app is not None:
            _app.screenRemoved.connect(self._on_screens_changed)
            _app.screenAdded.connect(self._on_screens_changed)
        self._last_screen_name: Optional[str] = None
        # Drag-aware refit state — see _schedule_screen_refit.
        self._pending_refit_reason: str = ""
        self._refit_retry_count: int = 0

    def showEvent(self, ev: QtGui.QShowEvent) -> None:  # type: ignore[override]
        # windowHandle is only valid after the window is shown; this is the
        # earliest place we can subscribe to its per-window screenChanged
        # signal (firing when the window is dragged to another monitor).
        super().showEvent(ev)
        # The dashboard isn't rebuilt while hidden / minimised: catch up now.
        self._refresh_dashboard_if_visible()
        wh = self.windowHandle()
        if wh is not None and self._last_screen_name is None:
            try:
                wh.screenChanged.connect(self._on_window_screen_changed)
                cur = wh.screen()
                self._last_screen_name = cur.name() if cur else None
            except Exception:
                logging.exception("could not subscribe to screenChanged")

    def _on_window_screen_changed(self, screen) -> None:
        """Window dragged to a different monitor — re-fit to the new screen.

        Critical: if the user is mid-drag (mouse button held), calling
        setGeometry() here yanks the window out of Windows' WM_MOVING grab
        and breaks the drag. Defer until the button is released.
        """
        if screen is None:
            return
        try:
            name = screen.name()
            if name == self._last_screen_name:
                return
            self._last_screen_name = name
            self._schedule_screen_refit(reason="window-screen-changed")
        except Exception:
            logging.exception("screenChanged re-fit failed")

    def _on_screens_changed(self, _screen) -> None:
        """A monitor was added/removed — re-validate current geometry."""
        self._schedule_screen_refit(reason="screens-changed")

    def _schedule_screen_refit(self, *, reason: str) -> None:
        """Run the re-fit now if the user isn't dragging, else poll until idle.

        Polling caps at ~4s so a stuck mouse-button state can't loop forever.
        """
        self._pending_refit_reason = reason
        self._refit_retry_count = 0
        self._try_screen_refit()

    def _try_screen_refit(self) -> None:
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            self._refit_retry_count += 1
            if self._refit_retry_count > 20:  # ~4s ceiling at 200ms ticks
                logging.warning("screen refit gave up waiting for drag to end")
                return
            QtCore.QTimer.singleShot(200, self, self._try_screen_refit)
            return
        try:
            g = self.geometry()
            fx, fy, fw, fh = fit_saved_sw_geometry(g.x(), g.y(), g.width(), g.height())
            if (fx, fy, fw, fh) != (g.x(), g.y(), g.width(), g.height()):
                self.setGeometry(fx, fy, fw, fh)
            if self._pending_refit_reason == "window-screen-changed":
                self._reapply_splitter_fraction()
        except Exception:
            logging.exception("deferred screen refit failed")

    def resizeEvent(self, ev: QtGui.QResizeEvent) -> None:  # type: ignore[override]
        super().resizeEvent(ev)
        # Window resized — keep the rail/pane split proportional unless the
        # user has explicitly dragged the splitter. Deferred one tick: during
        # the window's resizeEvent the splitter still has its old width (the
        # central layout resizes it afterwards), so applying the fraction now
        # sized the rail against the previous window width.
        QtCore.QTimer.singleShot(0, self, self._reapply_splitter_fraction)
        self._sync_header_density()

    # ----- window state (maximised + normal geometry) -----
    #
    # The runner persists `sessions_window` = geometry() on quit and restores
    # it before the first show. A maximised window's geometry() is the
    # maximised rect, so on its own that lost the maximised state and made
    # "un-maximise" a no-op after a relaunch. The window keeps its own two
    # keys: `sessions_window_state` ("normal" | "maximized" | "fullscreen")
    # and `sessions_window_normal` (the rect to return to).

    def changeEvent(self, ev: QtCore.QEvent) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(ev)
        if ev.type() == QtCore.QEvent.WindowStateChange:
            self._persist_window_state()
            self._refresh_dashboard_if_visible()  # un-minimised: catch up
            if (getattr(self, "_reset_geometry_on_normal", False)
                    and self._window_state_name() == "normal"):
                self._reset_geometry_on_normal = False
                # Next tick: the platform is still settling the frame here.
                QtCore.QTimer.singleShot(0, self, self._apply_default_geometry)

    def _reset_geometry_fallback(self) -> None:
        if getattr(self, "_reset_geometry_on_normal", False):
            self._reset_geometry_on_normal = False
            if self._window_state_name() == "normal":
                self._apply_default_geometry()

    def _apply_default_geometry(self) -> None:
        self.setGeometry(*compute_default_sw_geometry())
        QtCore.QTimer.singleShot(0, self, self._reapply_splitter_fraction)

    def _window_state_name(self) -> str:
        st = self.windowState()
        if st & QtCore.Qt.WindowFullScreen:
            return "fullscreen"
        if st & QtCore.Qt.WindowMaximized:
            return "maximized"
        return "normal"

    def _persist_window_state(self) -> None:
        name = self._window_state_name()
        if name == getattr(self, "_persisted_window_state", None) and name == "normal":
            return
        self._persisted_window_state = name
        update: dict = {"sessions_window_state": name}
        if name != "normal" and not self.isMinimized():
            g = self.normalGeometry()
            if g.isValid() and g.width() > 0 and g.height() > 0:
                update["sessions_window_normal"] = {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}
        _state.merged_save_debounced(update)

    def _restore_window_state(self) -> bool:
        """First show only: re-apply a persisted maximised state (from its
        saved normal rect, so un-maximising lands somewhere sensible). A
        full-screen window comes back as its normal rect — opening straight
        into a full-screen Space is disorienting. True if it showed the
        window itself."""
        if getattr(self, "_window_state_restored", False):
            return False
        self._window_state_restored = True
        persisted = _state.load()
        name = persisted.get("sessions_window_state")
        if name not in ("maximized", "fullscreen"):
            return False
        normal = persisted.get("sessions_window_normal") or {}
        if all(isinstance(normal.get(k), (int, float)) for k in ("x", "y", "w", "h")):
            try:
                fx, fy, fw, fh = fit_saved_sw_geometry(
                    int(normal["x"]), int(normal["y"]), int(normal["w"]), int(normal["h"]))
                self.setGeometry(fx, fy, fw, fh)
            except Exception:
                log.exception("could not restore the normal window rect")
        if name == "maximized":
            self.showMaximized()
            return True
        # Full screen came back as its normal rect: record that, or every
        # later launch would re-apply this old rect over newer geometry.
        self._persisted_window_state = "normal"
        _state.merged_save_debounced({"sessions_window_state": "normal"})
        return False

    def reset_layout(self) -> None:
        """⋯ → Reset layout: default window size and position on this
        screen, not maximised, and the 30/70 rail split again."""
        if (sys.platform == "darwin" and self.windowState() & QtCore.Qt.WindowFullScreen
                and self.isVisible()):
            # Leaving a macOS full-screen Space is animated: a setGeometry now
            # is overridden when the window lands back at its old rect. Apply
            # the default rect once the state change to normal arrives.
            self._reset_geometry_on_normal = True
            self.showNormal()
            # Fallback if that state change never arrives: don't leave the
            # flag armed for some later, unrelated return to normal.
            QtCore.QTimer.singleShot(2000, self, self._reset_geometry_fallback)
        else:
            if self.windowState() & (QtCore.Qt.WindowMaximized | QtCore.Qt.WindowFullScreen):
                self.showNormal()
            self.setGeometry(*compute_default_sw_geometry())
        self._splitter_user_set = False
        self._splitter_fraction = 0.30
        _state.merged_save_debounced({
            "sessions_splitter_user_set": False,
            "sessions_window_state": "normal",
        })
        self._persisted_window_state = "normal"
        QtCore.QTimer.singleShot(0, self, self._reapply_splitter_fraction)

    # ----- public API -----

    def show_and_focus(self) -> None:
        if self.isMinimized():
            self.showNormal()
        elif not self._restore_window_state():
            self.show()
        self.raise_()
        self.activateWindow()

    @QtCore.Slot(int)
    def select_session(self, local_session_id: int) -> None:
        # Explicit select from elsewhere (quick switcher, panel, new session)
        # lands focus on the composer like a click does.
        self._programmatic_select = True
        try:
            self._select_session(local_session_id)
        finally:
            self._programmatic_select = False

    def _select_session(self, local_session_id: int) -> None:
        # If the row isn't in the list yet (e.g. just-created session before
        # the next refresh tick), refresh once and retry. If it's still absent
        # it's hidden by the current filter chip / search — switch to a view
        # that shows it rather than silently doing nothing.
        if self._select_row_by_id(local_session_id):
            return
        self._refresh_list()
        if self._select_row_by_id(local_session_id):
            return
        sess = self._store.get_session(local_session_id)
        if sess is None:
            return
        if self._search.text():
            self._search_timer.stop()
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        self._set_filter(self._filter_showing(sess))
        self._select_row_by_id(local_session_id)

    def _select_row_by_id(self, session_id: int) -> bool:
        """Make the row for `session_id` current. False if it isn't listed."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None or _is_header_item(item):
                continue
            sess: Session = item.data(QtCore.Qt.UserRole)
            if sess and sess.id == session_id:
                self._list.setCurrentItem(item)
                return True
        return False

    @staticmethod
    def _filter_showing(sess: Session) -> str:
        """The filter chip whose (search-less) listing includes `sess` —
        mirrors the predicates `_refresh_list` passes to list_sessions."""
        if sess.archived:
            return "archived"
        if (
            sess.reviewed_at is not None
            and not sess.pinned
            and sess.status != "running"
        ):
            return "done"
        return "active"

    def active_session_id(self) -> Optional[int]:
        return self._active_session_id

    # ----- list management -----

    def _set_filter(self, key: str) -> None:
        self._current_filter = key
        for k, btn in self._chips.items():
            btn.setProperty("active", "true" if k == key else "false")
            btn.setChecked(k == key)
            btn.setStyleSheet(_QSS)
        _state.merged_save_debounced({"sessions_filter": key})
        self._refresh_list()

    def _refresh_list(self) -> None:
        # A rebuild replaces every row widget: land an in-progress inline
        # rename first (background churn defers instead — _on_session_changed).
        if self._rename_editor is not None:
            self._finish_rename(commit=True)
        self._refresh_after_rename = False
        prev_id = self._active_session_id
        q = self._search.text().strip()
        include_archived = self._current_filter == "archived"
        if q:
            # Search applies across the WHOLE library so we surface a result
            # even if it's filtered out by the current chip. Reviewed sessions
            # are always returned by search — the operator typed something, we
            # don't second-guess.
            sessions = self._store.search(q, include_archived=True)
            bucket_groups = False
        else:
            sessions = self._store.list_sessions(
                include_archived=(self._current_filter == "archived"),
                pinned_only=(self._current_filter == "pinned"),
                running_only=(self._current_filter == "running"),
                reviewed_only=(self._current_filter == "done"),
                exclude_reviewed=(self._current_filter == "active"),
                limit=500,
            )
            if self._current_filter == "running":
                listed = {x.id for x in sessions}
                for sid in self._working_session_ids() - listed:
                    extra = self._store.get_session(sid)
                    if extra is not None and not extra.archived:
                        sessions.append(extra)
            # Bucket grouping only on the "Active" + "Done" views. Running /
            # Pinned / Archived are short enough that grouping just adds chrome
            # without aiding navigation. Age buckets only make sense when the
            # rows are in recency order — under tokens/cost/title sort they'd
            # repeat ("Today", "Older", "Today", …).
            bucket_groups = (
                self._current_filter in ("active", "done")
                and self._sort_key == "recent"
            )

        # Apply non-default sort orders in Python (lets us sort by computed
        # values like token totals without touching the underlying SQL).
        if not q:
            sessions = self._apply_sort(sessions)
        self._list.blockSignals(True)
        self._list.clear()
        self._row_widgets = {}
        target_item = None
        totals = self._compute_session_totals([s.id for s in sessions])
        running_session_ids = self._working_session_ids()
        now = time.time()

        # Pinned floats above the buckets in their own implicit "PINNED" group
        # (the underlying SQL already sorted them first). When bucketing, lift
        # them out and present a dedicated header so they're visually anchored.
        pinned = [s for s in sessions if s.pinned] if bucket_groups else []
        unpinned = [s for s in sessions if not s.pinned] if bucket_groups else sessions

        last_bucket: Optional[str] = None
        if bucket_groups and pinned:
            self._add_header_row("PINNED")
            for sess in pinned:
                ti = self._add_session_row(sess, totals, running_session_ids, now)
                if prev_id is not None and sess.id == prev_id:
                    target_item = ti

        for sess in unpinned:
            if bucket_groups:
                bucket = _age_bucket(sess.last_activity_at, now=now)
                if bucket != last_bucket:
                    self._add_header_row(bucket)
                    last_bucket = bucket
            ti = self._add_session_row(sess, totals, running_session_ids, now)
            if prev_id is not None and sess.id == prev_id:
                target_item = ti
        self._list.blockSignals(False)
        if target_item is not None:
            self._list.setCurrentItem(target_item)

        # Result-count strip — shown during search OR when sort/filter has
        # winnowed things meaningfully. Keep it informational.
        session_count = len(sessions)
        if q:
            archived_n = sum(1 for s in sessions if s.archived)
            done_n = sum(1 for s in sessions if s.reviewed_at is not None)
            bits = [f"{session_count} result{'s' if session_count != 1 else ''}"]
            if archived_n:
                bits.append(f"{archived_n} archived")
            if done_n:
                bits.append(f"{done_n} Done")
            self._results_label.setText(" · ".join(bits))
            self._results_label.setVisible(True)
        else:
            self._results_label.setVisible(False)
        self._update_list_empty_state(q, session_count)
        # Rows were measured as they were added; re-fit once the list has
        # settled (a scrollbar appearing narrows every row).
        self._fit_rows_timer.start()
        # Hide sticky bucket label outside bucketed views.
        if not bucket_groups:
            self._sticky_bucket.setVisible(False)
        else:
            self._refresh_sticky_bucket()

    def _update_list_empty_state(self, query: str, count: int) -> None:
        """A quiet line in the empty list saying why it's empty."""
        lbl = getattr(self, "_list_empty", None)
        if lbl is None:
            lbl = QtWidgets.QLabel("", self._list.viewport())
            lbl.setObjectName("listEmpty")
            lbl.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_SM}pt; background: transparent; "
                              f"padding: {theme.SPACE_XL}px {theme.SPACE_MD}px;")
            lbl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
            self._list_empty = lbl
        if count:
            lbl.setVisible(False)
            return
        if query:
            text = f"No sessions match “{query}”"
        else:
            text = {
                "active": f"No active sessions. Hold {self._hotkey_label} to start one.",
                "done": "Nothing in Done yet",
                "running": "No sessions are working right now",
                "pinned": "No pinned sessions",
                "archived": "No archived sessions",
            }.get(self._current_filter, "No sessions")
        lbl.setText(text)
        lbl.setGeometry(self._list.viewport().rect())
        lbl.setVisible(True)
        lbl.raise_()

    def _add_header_row(self, label: str) -> None:
        item = QtWidgets.QListWidgetItem()
        item.setFlags(QtCore.Qt.NoItemFlags)  # not selectable, not focusable
        item.setData(QtCore.Qt.UserRole + 1, True)  # marks this as a header
        item.setSizeHint(QtCore.QSize(0, 26))
        item.setData(QtCore.Qt.AccessibleTextRole, label.capitalize())
        self._list.addItem(item)
        widget = QtWidgets.QLabel(label)
        widget.setStyleSheet(
            f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; font-weight: {theme.WEIGHT_BOLD}; "
            f"letter-spacing: {theme.TRACKING_CAPS_SM}; padding: {theme.SPACE_SM}px {theme.SPACE_MD}px {theme.SPACE_2XS}px {theme.SPACE_MD}px; background: transparent;"
        )
        self._list.setItemWidget(item, widget)

    def _add_session_row(
        self,
        sess: Session,
        totals: dict,
        running_session_ids: set,
        now: float,
    ) -> QtWidgets.QListWidgetItem:
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.UserRole, sess)
        self._list.addItem(item)
        is_running = sess.id in running_session_ids
        live_verb, live_elapsed_s, stuck = self._live_state_for(sess.id, now) if is_running else (None, None, False)
        widget = SessionListItem(
            sess,
            totals.get(sess.id),
            is_running=is_running,
            live_verb=live_verb,
            live_elapsed_s=live_elapsed_s,
            stuck=stuck,
            now=now,
        )
        widget.archive_requested.connect(self._on_archive_requested)
        widget.review_toggled.connect(self._on_review_toggled)
        widget.cancel_requested.connect(self._on_row_cancel_requested)
        self._row_widgets[sess.id] = widget
        item.setData(QtCore.Qt.AccessibleTextRole, widget.accessible_text())
        # Height comes from the row's own content: one or two title lines at
        # the current rail width (re-measured on resize by _fit_row_heights).
        # Parent + polish the widget first so the stylesheet fonts apply
        # (ElidedLabel re-pins its height on FontChange) before we measure.
        self._list.setItemWidget(item, widget)
        widget.ensurePolished()
        h = widget.fit_to_width(self._list.viewport().width())
        item.setSizeHint(QtCore.QSize(0, h))
        return item

    def _fit_row_heights(self) -> None:
        """Re-measure every session row at the list's current width (titles
        take one or two lines depending on it) and resize the items that
        changed. Runs (coalesced) after every list rebuild and viewport
        resize."""
        vw = self._list.viewport().width()
        if vw <= 0:
            return
        for i in range(self._list.count()):
            it = self._list.item(i)
            w = self._list.itemWidget(it)
            if not isinstance(w, SessionListItem):
                continue
            h = w.fit_to_width(vw)
            if it.sizeHint().height() != h:
                it.setSizeHint(QtCore.QSize(0, h))
        if self._rename_editor is not None:
            # Rows above may have changed height: follow the renamed title.
            self._list.doItemsLayout()
            self._place_rename_editor()

    def _on_list_viewport_resized(self) -> None:
        self._fit_rows_timer.start()
        self._place_sticky_bucket()
        lbl = getattr(self, "_list_empty", None)
        if lbl is not None:
            lbl.setGeometry(self._list.viewport().rect())
        if self._rename_editor is not None:
            self._place_rename_editor()

    def _live_job_for(self, session_id: int):
        """The job actually executing for this session: the newest
        status=="running" job, else the newest "queued" one. A follow-up turn
        queued behind a running one must not shadow it (ticker / ⏹ target)."""
        best = None
        for j in self._job_store.all():
            if j.local_session_id != session_id:
                continue
            if j.status not in ("queued", "running"):
                continue
            if best is None:
                best = j
                continue
            rank = (j.status == "running", j.id)
            if rank > (best.status == "running", best.id):
                best = j
        return best

    def _working_session_ids(self) -> set[int]:
        """Sessions with work in flight: a queued/running turn, or subagents
        still running after their turn ended (background agents)."""
        ids = {
            j.local_session_id for j in self._job_store.all()
            if j.status in ("queued", "running") and j.local_session_id is not None
        }
        ids.update(a.local_session_id for a in self._job_store.running_agents()
                   if a.local_session_id is not None)
        return ids

    def _session_has_live_job(self, session_id: int) -> bool:
        return self._live_job_for(session_id) is not None

    def _live_state_for(self, session_id: int, now: float) -> tuple[Optional[str], Optional[float], bool]:
        """Return (verb, elapsed_seconds, is_stuck) for the most recent running
        job belonging to this session, or (None, None, False) if none.
        """
        job = self._live_job_for(session_id)
        if job is None:
            # No turn in flight, but the session's background agents may be.
            agents = [a for a in self._job_store.agents_for_session(session_id) if a.running]
            if not agents:
                return None, None, False
            n = len(agents)
            verb = f"{n} agent{'s' if n != 1 else ''} running"
            latest = max(agents, key=lambda a: a.started_ts)
            if latest.doing:
                verb += f" \u00b7 {latest.doing}"
            return verb, max(0.0, now - min(a.started_ts for a in agents)), False
        try:
            from holler.tool_format import summarize as summarize_tool
        except Exception:
            summarize_tool = None
        cur = getattr(job, "current_tool", None)
        if cur is not None and summarize_tool is not None:
            verb = summarize_tool(cur.name, cur.input, max_len=60)
        elif getattr(job, "streaming_text", ""):
            verb = "Responding…"
        else:
            verb = "Thinking…"
        elapsed = None
        if job.started_at:
            elapsed = max(0.0, now - job.started_at)
        # Stuck detection: same tool name pinned for ≥_STUCK_SECONDS.
        cur_name = cur.name if cur is not None else "(none)"
        first = self._tool_first_seen.get(job.id)
        if first is None or first[0] != cur_name:
            self._tool_first_seen[job.id] = (cur_name, now)
            stuck = False
        else:
            stuck = (now - first[1]) >= _STUCK_SECONDS and cur is not None
        return verb, elapsed, stuck

    def _compute_session_totals(self, session_ids: list) -> dict:
        """DB totals plus any currently-running jobs whose tokens haven't
        persisted yet — so the rail updates live during a turn.

        Returns {sid: {ctx_in, output, cost, cache_pct}}. cache_pct is the
        share of input tokens served from prompt cache, useful as a row-level
        savings signal.
        """
        if not session_ids:
            return {}
        base = self._store.session_token_totals(session_ids)
        # Per-session cache split for cache_pct. Compute from turns separately
        # since session_token_totals already collapses input/cache columns.
        try:
            placeholders = ",".join("?" * len(session_ids))
            rows = self._store._conn().execute(  # type: ignore[attr-defined]
                f"SELECT session_id, "
                f"  COALESCE(SUM(cache_read_input_tokens), 0) AS cache_r, "
                f"  COALESCE(SUM(input_tokens + cache_creation_input_tokens + cache_read_input_tokens), 0) AS ctx_in "
                f"FROM turns WHERE session_id IN ({placeholders}) GROUP BY session_id",
                tuple(session_ids),
            ).fetchall()
            cache_data = {int(r["session_id"]): (int(r["cache_r"] or 0), int(r["ctx_in"] or 0)) for r in rows}
            last_turn_rows = self._store._conn().execute(  # type: ignore[attr-defined]
                f"SELECT t.session_id, "
                f"  COALESCE(t.input_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens, 0) AS lctx "
                f"FROM turns t "
                f"WHERE t.session_id IN ({placeholders}) "
                f"  AND t.seq = (SELECT MAX(seq) FROM turns WHERE session_id = t.session_id)",
                tuple(session_ids),
            ).fetchall()
            last_ctx = {int(r["session_id"]): int(r["lctx"] or 0) for r in last_turn_rows}
        except Exception:
            cache_data = {}
            last_ctx = {}
        for j in self._job_store.all():
            if j.status != "running":
                continue
            sid = j.local_session_id
            if sid not in base:
                continue
            base[sid]["fresh_in"] = base[sid].get("fresh_in", 0) + j.input_tokens
            base[sid]["ctx_in"] += (
                j.input_tokens + j.cache_creation_input_tokens + j.cache_read_input_tokens
            )
            base[sid]["output"] += j.output_tokens
            base[sid]["cost"] += j.total_cost_usd
            r, ctx = cache_data.get(sid, (0, 0))
            cache_data[sid] = (
                r + j.cache_read_input_tokens,
                ctx + j.input_tokens + j.cache_creation_input_tokens + j.cache_read_input_tokens,
            )
        for sid, entry in base.items():
            r, ctx = cache_data.get(sid, (0, 0))
            entry["cache_pct"] = int(round((r / ctx) * 100)) if ctx else 0
            entry["last_ctx_in"] = int(last_ctx.get(sid, 0))
        return base

    def _refresh_visible_totals(self, *_args) -> None:
        """Update token totals + live-job flags on existing rows without
        rebuilding them.

        Cheap enough to run on every job_updated tick — single GROUP BY query
        plus in-memory adjustments, no widget recreation."""
        widgets = getattr(self, "_row_widgets", None)
        if not widgets:
            return
        totals = self._compute_session_totals(list(widgets.keys()))
        running_session_ids = self._working_session_ids()
        now = time.time()
        flipped = False
        for sid, widget in widgets.items():
            widget.set_totals(totals.get(sid))
            flipped |= widget._is_running != (sid in running_session_ids)
            widget.set_is_running(sid in running_session_ids)
            if sid in running_session_ids:
                verb, elapsed, stuck = self._live_state_for(sid, now)
                widget.set_live(verb, elapsed, stuck)
            else:
                widget.set_live(None, None, False)
        # A row that starts / stops running changes title weight, which can
        # re-wrap it: re-measure (coalesced) — only when that happened.
        if flipped:
            self._fit_rows_timer.start()

    def _tick_pulse_dots(self) -> None:
        """Fast tick (~280ms) — animates the running-session dot in the rail
        so the list view also has a visible heartbeat, not just the detail
        pane. No-op when no sessions are running."""
        widgets = getattr(self, "_row_widgets", None)
        if not widgets:
            return
        running_session_ids = self._working_session_ids()
        if not running_session_ids:
            return
        self._pulse_phase = (self._pulse_phase + 1) % 4
        for sid in running_session_ids:
            w = widgets.get(sid)
            if w is not None:
                w.pulse_dot(self._pulse_phase)

    def _tick_live_rows(self) -> None:
        """1Hz tick — refreshes elapsed seconds + stuck detection on running
        rows without rebuilding their widgets."""
        widgets = getattr(self, "_row_widgets", None)
        if not widgets:
            return
        running_session_ids = self._working_session_ids()
        if not running_session_ids:
            return
        now = time.time()
        for sid in running_session_ids:
            w = widgets.get(sid)
            if w is None:
                continue
            verb, elapsed, stuck = self._live_state_for(sid, now)
            w.set_live(verb, elapsed, stuck)

    @QtCore.Slot(int)
    def _on_review_toggled(self, session_id: int) -> None:
        """Wired from SessionListItem.review_toggled. Mark/unmark Done."""
        sess = self._store.get_session(session_id)
        if sess is None:
            return
        reviewed = sess.reviewed_at is None  # if currently unreviewed, mark reviewed
        self._store.mark_reviewed(session_id, reviewed)
        title = _truncate(sess.title or "Untitled session", 40)
        if reviewed:
            # The callback carries its own session: notices stack, and Undo
            # on an older one must reopen that session, not the newest.
            self.show_inline_notice(
                f"Marked “{title}” Done",
                undo_callback=lambda sid=session_id: self._undo_last_review(sid),
                kind="info",
            )
        else:
            self.show_inline_notice(f"Reopened “{title}”", kind="info")
        self._refresh_list()

    @QtCore.Slot(int)
    def _on_row_cancel_requested(self, session_id: int) -> None:
        """Wired from SessionListItem.cancel_requested. Find the live job and
        emit cancel upward (same signal the detail-pane Esc uses). Targets the
        running job, not a follow-up queued behind it."""
        job = self._live_job_for(session_id)
        if job is not None:
            self.cancel_job_requested.emit(job.id)

    def _undo_last_review(self, sid: int) -> None:
        self._store.mark_reviewed(sid, False)
        self._refresh_list()

    @QtCore.Slot(int)
    def _on_archive_requested(self, session_id: int) -> None:
        """Wired from SessionListItem.archive_requested. Soft-archive (recoverable
        from the Archived tab via right-click → Unarchive)."""
        sess = self._store.get_session(session_id)
        if sess is None:
            return
        # Belt-and-suspenders: even though the row hides the X while a job is
        # running for that session, an event in flight could still reach here.
        if self._session_has_live_job(session_id):
            self.show_inline_notice(
                f"Can't archive “{_truncate(sess.title or 'Untitled session', 40)}” while it's working. "
                f"Stop the turn first.",
                kind="error",
            )
            return
        self._store.archive(session_id, True)
        if self._active_session_id == session_id:
            self._clear_active_session()
        title_short = _truncate(sess.title or "Untitled session", 40)
        self.show_inline_notice(
            f"Archived “{title_short}”",
            undo_callback=lambda sid=session_id: self._undo_last_archive(sid),
            kind="info",
        )
        self._refresh_list()

    def _clear_active_session(self) -> None:
        """Drop the current selection/routing target — the single path every
        "the active session went away" action (×, Delete key, context-menu
        Archive/Delete, bulk archive, empty selection) funnels through, so
        routing (session_selected(-1)), the detail pane, routing pill, chain
        bar, text-input placeholder and row highlight never go stale."""
        self._active_session_id = None
        self._clear_detail()
        self._sync_agents_pane(None)
        self._render_empty_dashboard()
        self._ptt_status.setText(f"Hold {self._hotkey_label} to start a new session")
        self._ptt_status.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_SM}pt;")
        self._status_strip.setVisible(False)
        self._refresh_usage()
        self._sync_row_selection()  # also refreshes routing pill + chain bar
        self._update_text_input_placeholder()
        self.session_selected.emit(-1)

    def _undo_last_archive(self, sid: int) -> None:
        self._store.archive(sid, False)
        self._refresh_list()

    def _archive_focused_row(self, require_selection: bool = False) -> None:
        """Delete-key handler — archives the focused row(s). For multi-select,
        archives the whole selection; for single selection, advances focus to
        the next non-header row so the user can chain Deletes."""
        items = [it for it in self._list.selectedItems() if not _is_header_item(it)]
        if not items and require_selection:
            return
        if not items:
            item = self._list.currentItem()
            if item is None or _is_header_item(item):
                return
            items = [item]
        if len(items) > 1:
            self._bulk_archive(items)
            return
        item = items[0]
        sess: Optional[Session] = item.data(QtCore.Qt.UserRole)
        if sess is None:
            return
        # Remember the NEXT session by id (not row index): archiving rebuilds
        # the list, so a pre-computed index lands one row too far (and header
        # rows can appear/disappear).
        next_id: Optional[int] = None
        for r in range(self._list.row(item) + 1, self._list.count()):
            it = self._list.item(r)
            if it is None or _is_header_item(it):
                continue
            nxt: Optional[Session] = it.data(QtCore.Qt.UserRole)
            if nxt is not None:
                next_id = nxt.id
                break
        self._on_archive_requested(sess.id)
        # Land on the next row so a user can chain Deletes (no-op if the
        # archive was refused — the row is still listed and still current).
        after = self._store.get_session(sess.id)
        if next_id is not None and (after is None or after.archived):
            self._select_row_by_id(next_id)

    def _toggle_reviewed_focused_row(self) -> None:
        """Space-key handler — toggle reviewed/done on the focused row(s)."""
        items = [it for it in self._list.selectedItems() if not _is_header_item(it)]
        if not items:
            return
        sessions = [it.data(QtCore.Qt.UserRole) for it in items]
        sessions = [s for s in sessions if s is not None]
        if not sessions:
            return
        if len(sessions) == 1:
            self._on_review_toggled(sessions[0].id)
            return
        # Bulk: if any row in the selection is un-reviewed, mark all as done;
        # otherwise un-mark all. Mirrors the inbox "mark read" gesture. Undo
        # restores each session's previous state (no modal confirm).
        any_unreviewed = any(s.reviewed_at is None for s in sessions)
        before = {s.id: s.reviewed_at is not None for s in sessions}
        for s in sessions:
            self._store.mark_reviewed(s.id, any_unreviewed)
        if any_unreviewed:
            msg = f"Marked {len(sessions)} sessions Done"
        else:
            msg = f"Reopened {len(sessions)} sessions"
        # The callback carries its own batch: Undo on an older notice undoes
        # that batch, not the latest one.
        self.show_inline_notice(msg, undo_callback=lambda b=before: self._undo_bulk_review(b), kind="info")
        self._refresh_list()

    def _undo_bulk_review(self, before: dict) -> None:
        if not before:
            return
        for sid, was_done in before.items():
            if self._store.get_session(sid) is not None:
                self._store.mark_reviewed(sid, was_done)
        self._refresh_list()

    def _toggle_pin_focused_row(self) -> None:
        items = [it for it in self._list.selectedItems() if not _is_header_item(it)]
        for it in items:
            sess: Optional[Session] = it.data(QtCore.Qt.UserRole)
            if sess is None:
                continue
            self._store.pin(sess.id, not sess.pinned)
        self._refresh_list()

    def _rename_focused_row(self) -> None:
        item = self._list.currentItem()
        if item is None or _is_header_item(item):
            return
        self._rename_item(item)

    def _rename_session(self, session_id: int) -> None:
        """Inline-rename a session by id (⌘K "Rename this session…", the row
        menu) — not whatever row happens to be the list's current item, which
        in a multi-select or after Mark Done hid the active row is another
        session. A row the current filter hides is brought back first."""
        item = self._item_for_session(session_id)
        if item is None:
            self.select_session(session_id)
            item = self._item_for_session(session_id)
        if item is not None:
            self._rename_item(item)

    def _rename_item(self, item: QtWidgets.QListWidgetItem) -> None:
        """Inline rename: a line edit over the row's title. Return commits,
        Esc cancels, clicking away commits; an empty title changes nothing."""
        if item is None or _is_header_item(item):
            return
        sess: Optional[Session] = item.data(QtCore.Qt.UserRole)
        if sess is None:
            return
        if self._rename_editor is not None:
            if self._rename_sid == sess.id:
                self._rename_editor.setFocus(QtCore.Qt.OtherFocusReason)
                return
            self._finish_rename(commit=True)
            item = self._item_for_session(sess.id)
            if item is None:
                return
        self._list.scrollToItem(item)
        ed = _RenameEdit(self._list.viewport())
        ed.setText(sess.title or "")
        ed.selectAll()
        # What the editor started from: only a title the operator actually
        # changed is saved (an auto-title landing meanwhile must not be
        # overwritten by the stale text on click-away).
        ed._original_text = ed.text().strip()
        ed.finished.connect(lambda commit: self._finish_rename(commit=commit))
        self._rename_editor = ed
        self._rename_sid = sess.id
        self._refresh_sticky_bucket()  # hidden while renaming: it would cover the editor
        self._place_rename_editor()
        ed.show()
        ed.raise_()
        ed.setFocus(QtCore.Qt.OtherFocusReason)

    def _item_for_session(self, session_id: int) -> Optional[QtWidgets.QListWidgetItem]:
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it is None or _is_header_item(it):
                continue
            s: Optional[Session] = it.data(QtCore.Qt.UserRole)
            if s is not None and s.id == session_id:
                return it
        return None

    def _place_rename_editor(self) -> None:
        """Lay the editor over the renamed row's title (viewport coords)."""
        ed = self._rename_editor
        if ed is None:
            return
        row = self._row_widgets.get(getattr(self, "_rename_sid", -1))
        vp = self._list.viewport()
        if row is None:
            return
        title = row._title_lbl
        top_left = title.mapTo(vp, QtCore.QPoint(0, 0))
        h = max(ed.sizeHint().height(), title.fontMetrics().lineSpacing() + theme.SPACE_SM)
        x = top_left.x() - theme.SPACE_XS
        y = top_left.y() + title.fontMetrics().lineSpacing() // 2 - h // 2
        right = row.mapTo(vp, QtCore.QPoint(row.width(), 0)).x() - theme.SPACE_XS
        ed.setGeometry(x, y, max(theme.SPACE_XL * 3, right - x), h)

    def _refresh_unless_renaming(self) -> None:
        """Deferred post-rename refresh; if another inline rename has opened
        meanwhile, run it when that one finishes instead of closing it."""
        if self._rename_editor is not None:
            self._refresh_after_rename = True
        else:
            self._refresh_list()

    def _finish_rename(self, commit: bool) -> None:
        """Close the inline editor; on commit save a changed, non-empty title."""
        ed = self._rename_editor
        if ed is None:
            return
        sid = getattr(self, "_rename_sid", None)
        self._rename_editor = None
        self._rename_sid = None
        new_title = ed.text().strip()
        had_focus = ed.hasFocus()
        ed._done = True  # its focus-out (from hide) must not finish again
        ed.hide()
        ed.deleteLater()
        self._refresh_sticky_bucket()
        changed = False
        if commit and sid is not None and new_title:
            sess = self._store.get_session(sid)
            if (sess is not None and new_title != ed._original_text
                    and new_title != (sess.title or "")):
                self._store.set_title(sid, new_title)
                changed = True
        if changed or self._refresh_after_rename:
            # Next tick: a commit from focus-out runs inside the click that
            # moved focus (e.g. a press on another row) — don't rebuild the
            # rows under that press.
            QtCore.QTimer.singleShot(0, self, self._refresh_unless_renaming)
        if had_focus:
            # Back to the list on the renamed row, ready for the next key.
            self._list.setFocus(QtCore.Qt.OtherFocusReason)

    def _regenerate_title(self, sess: Session) -> None:
        """Force a fresh auto-title pass on this session. Resets the auto_titled
        flag so the AutoTitler doesn't bail thinking the user renamed it, and
        zeros the attempt counter so it isn't immediately stopped by the
        lifetime cap. Image is omitted — re-title without screenshot context
        is fine for this manual path."""
        if self._auto_titler is None:
            return
        try:
            # Re-mark as auto-titled so the AutoTitler will overwrite, and
            # reset the lifetime counter so an exhausted session gets fresh
            # retries.
            self._store.set_title(sess.id, sess.title, auto=True)
            self._store.reset_title_attempts(sess.id)
        except Exception:
            pass
        try:
            self._auto_titler.request(sess.id, sess.transcript or "", None)  # type: ignore[attr-defined]
        except Exception:
            pass
        self.show_inline_notice(
            f"Generating a new title for “{_truncate(sess.title or 'Untitled session', 40)}”…", kind="info")

    def _bulk_archive(self, items: list) -> None:
        sessions = [it.data(QtCore.Qt.UserRole) for it in items]
        sessions = [s for s in sessions if s is not None]
        if not sessions:
            return
        # Don't bulk-archive a running session — silently skip it.
        running_ids = self._working_session_ids()
        eligible = [s for s in sessions if s.id not in running_ids]
        if not eligible:
            self.show_inline_notice(
                "Can't archive while these sessions are working. Stop them first.", kind="error")
            return
        # Archive is recoverable, so no modal confirm — the toast's Undo
        # brings the whole batch back (Delete keeps its confirm).
        for s in eligible:
            self._store.archive(s.id, True)
        if self._active_session_id in {s.id for s in eligible}:
            self._clear_active_session()
        batch = [s.id for s in eligible]
        n = len(eligible)
        self.show_inline_notice(
            f"Archived {n} session{'s' if n != 1 else ''}",
            undo_callback=lambda ids=batch: self._undo_bulk_archive(ids),
            kind="info",
        )
        self._refresh_list()

    def _undo_bulk_archive(self, ids: list) -> None:
        if not ids:
            return
        for sid in ids:
            if self._store.get_session(sid) is not None:
                self._store.archive(sid, False)
        self._refresh_list()

    def _on_selection_changed(self) -> None:
        # With ExtendedSelection, multi-row selection shouldn't change routing.
        # Only single selection drives the right pane + PTT routing.
        selected = [it for it in self._list.selectedItems() if not _is_header_item(it)]
        if len(selected) > 1:
            # Multi-select: surface a "N selected" cue in the status strip,
            # don't blow away the right pane.
            delete_key = "⌫" if sys.platform == "darwin" else "Delete"
            self._ptt_status.setText(
                f"{len(selected)} selected — {delete_key} to archive, Space to mark Done"
            )
            self._ptt_status.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
            self._status_strip.setVisible(True)
            return
        self._status_strip.setVisible(False)
        item = self._list.currentItem()
        # Header rows are non-selectable, but defensive check anyway.
        if item is None or _is_header_item(item) or not selected:
            self._clear_active_session()
            return
        sess: Session = item.data(QtCore.Qt.UserRole)
        if sess is None:
            return
        # _refresh_list calls setCurrentItem(<same row>) on every list rebuild
        # (e.g. when a sibling session updates and emits session_changed). That
        # re-fires this slot even though the selection didn't actually change.
        # Skip the redundant (expensive) detail re-render so the operator's
        # scroll position in the detail pane isn't yanked to the bottom by
        # background activity in other sessions — but still restore the cheap
        # cosmetic state: a multi-select collapsing back to this row left the
        # "N selected" status behind, and a list rebuild recreated the row
        # widgets without their selected (✓/×) state.
        if self._active_session_id == sess.id:
            self._apply_selected_status(sess)
            return
        # MRU recents — promote this session to the top of the stack.
        if sess.id in self._recents:
            self._recents.remove(sess.id)
        self._recents.insert(0, sess.id)
        self._recents = self._recents[:8]
        _state.merged_save_debounced({"sessions_recents": self._recents})
        self._active_session_id = sess.id
        # A profile picked for "the next new session" was consumed (the
        # runner clears its copy when the PTT creates the session) or
        # abandoned — either way the pill / placeholder mustn't keep showing it.
        self._pending_profile = ""
        self._render_session_detail(sess)
        self._apply_selected_status(sess)
        # Selection IS the routing decision now — no separate Continue button.
        self.session_selected.emit(sess.id)
        # Land focus on the message input so the operator can immediately type
        # a continuation without clicking. Defer one tick so the list-widget
        # selection finishes processing before we steal focus. Not for keyboard
        # navigation in the list — arrow keys / chained Delete must keep focus
        # on the list.
        if (
            self._list.hasFocus()
            and not self._list_mouse_select
            and not self._programmatic_select
        ):
            return
        QtCore.QTimer.singleShot(0, self, lambda: self._text_input.setFocus(QtCore.Qt.OtherFocusReason))

    def _apply_selected_status(self, sess: Session) -> None:
        """Cheap per-selection chrome: PTT label, usage, row highlight, routing
        pill / chain bar, text-input placeholder."""
        title_short = _truncate(sess.title or "Untitled session", 60)
        profile_suffix = ""
        if getattr(sess, "profile", None):
            spec = get_profile(sess.profile)
            profile_suffix = f" · {spec.display if spec else sess.profile}"
        self._ptt_status.setText(
            f"Hold {self._hotkey_label} to continue “{title_short}”{profile_suffix}"
        )
        self._ptt_status.setStyleSheet(f"color: {_ACCENT}; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        self._status_strip.setVisible(False)
        self._refresh_usage()
        self._sync_row_selection()
        self._update_text_input_placeholder()

    def _sync_row_selection(self) -> None:
        """Tell each row widget whether it's the selected one (so its X
        button shows even without hover)."""
        for sid, w in self._row_widgets.items():
            w.set_selected(sid == self._active_session_id)
        # Routing pill reflects selection state too.
        self._update_routing_pill()
        self._update_chain_bar()

    # ----- Superintendent Chain bar -----

    def set_superintendent_inbox_sid(self, sid: int) -> None:
        """Runner tells us which local session is the pinned evidence inbox."""
        self._sup_inbox_sid = sid if sid > 0 else None
        self._update_chain_bar()
        self._update_text_input_placeholder()

    @QtCore.Slot(bool, int)
    def set_superintendent_chain_status(self, armed: bool, count: int) -> None:
        """Runner pushes the live draft state (armed flag + held-item count)."""
        self._sup_chain_armed = bool(armed)
        self._sup_chain_count = max(0, int(count))
        self._update_chain_bar()

    @QtCore.Slot(int, bool, int)
    def set_session_chain_status(self, sid: int, armed: bool, count: int) -> None:
        """Runner pushes the active session's interactive-chain state."""
        self._isc_sid = sid if sid > 0 else None
        self._isc_armed = bool(armed)
        self._isc_count = max(0, int(count))
        self._update_chain_bar()

    def _on_mode_click(self, mode: str) -> None:
        """Toolbar lane toggle: record locally, restyle, and tell the runner."""
        self._sup_mode = "investigation" if mode == "investigation" else "deploy"
        self._update_chain_bar()
        self.superintendent_mode_action.emit(self._sup_mode)

    def _style_mode_toggle(self) -> None:
        ir = self._sup_mode == "investigation"
        # The selected lane is a filled accent button (state, not brand —
        # BRAND.md) in bold; the other is an outline. Checkable, so the
        # selection is exposed to assistive tech as well.
        on_deploy = theme.button_qss("primary", font_size=theme.FONT_SM, weight=theme.WEIGHT_BOLD)
        on_ir = on_deploy
        off = theme.button_qss("secondary", font_size=theme.FONT_SM)
        self._mode_deploy_btn.setChecked(not ir)
        self._mode_ir_btn.setChecked(ir)
        self._mode_deploy_btn.setStyleSheet(off if ir else on_deploy)
        self._mode_ir_btn.setStyleSheet(on_ir if ir else off)

    def _update_chain_bar(self) -> None:
        # One bar, two contexts: the pinned evidence inbox renders the
        # superintendent draft controls (with the lane toggle); anything else
        # renders an interactive chain (file bundles into a turn). With no
        # selection the chain still applies — it files into the new session the
        # next hold would otherwise have created on its own.
        if not hasattr(self, "_chain_bar"):
            return
        # Show inbox controls only when the inbox is the active session.
        # Viewing any other session shows that session's own chain controls.
        active = self._active_session_id
        inbox = self._sup_inbox_sid
        if inbox is not None and (active is None or active == inbox):
            self._render_inbox_chain_bar()
        else:
            self._render_session_chain_bar()
        self._chain_bar.show()

    def _render_inbox_chain_bar(self) -> None:
        self._mode_deploy_btn.setVisible(True)
        self._mode_ir_btn.setVisible(True)
        # Worker-model badge stays hidden: the header badge is the one model
        # control, and workers follow it. Pin workers via Settings if needed.
        self._sup_model_btn.setVisible(False)
        self._style_mode_toggle()
        ir = self._sup_mode == "investigation"
        chaining = self._sup_chain_armed or self._sup_chain_count > 0
        self._chain_start_btn.setVisible(not chaining)
        self._chain_file_btn.setVisible(chaining)
        self._chain_discard_btn.setVisible(chaining)
        lane = "Investigate — report findings, never deploys" if ir \
            else "Deploy — fixes and ships to production"
        if chaining:
            n = self._sup_chain_count
            self._chain_file_btn.setText(f"▶ Start session ({n})" if n else "▶ Start session")
            self._chain_file_btn.setAccessibleName(
                f"Start session from chain ({n})" if n else "Start session from chain")
            held = (f"{n} capture{'s' if n != 1 else ''} held" if n
                    else "Waiting for the first capture")
            self._chain_label.setText(
                f"Chaining — {held} · every hold is added until you start the session"
            )
        else:
            self._chain_label.setText(
                f"Hold {self._hotkey_label} with no session selected → new Superintendent session · "
                f"{lane} · Start a chain to group several captures"
            )

    def _render_session_chain_bar(self) -> None:
        # The lane toggle + worker-model badge are superintendent-only; hide
        # them for normal sessions.
        self._mode_deploy_btn.setVisible(False)
        self._mode_ir_btn.setVisible(False)
        self._sup_model_btn.setVisible(False)
        # Only trust the pushed count when it belongs to the active session —
        # otherwise show the idle "Start chain" state until the runner re-pushes.
        current = self._isc_sid == self._active_session_id
        armed = self._isc_armed and current
        n = self._isc_count if current else 0
        chaining = armed or n > 0
        self._chain_start_btn.setVisible(not chaining)
        self._chain_file_btn.setVisible(chaining)
        self._chain_discard_btn.setVisible(chaining)
        has_session = self._active_session_id is not None
        if chaining:
            verb = "Continue session" if has_session else "Start session"
            self._chain_file_btn.setText(f"▶ {verb} ({n})" if n else f"▶ {verb}")
            self._chain_file_btn.setAccessibleName(
                f"{verb} from chain ({n})" if n else f"{verb} from chain")
            held = (f"{n} capture{'s' if n != 1 else ''} held" if n
                    else "Waiting for the first capture")
            dest = "this session" if has_session else "a new session"
            self._chain_label.setText(f"Chaining — {held} · sent as one message to {dest}")
        elif has_session:
            self._chain_label.setText(
                f"Start a chain to send several captures as one message to this session")
        else:
            self._chain_label.setText(
                f"Hold {self._hotkey_label} with no session selected → new session · "
                f"Start a chain to group several captures into one")

    @QtCore.Slot(int)
    def _on_cancel_requested(self, job_id: int) -> None:
        self.cancel_job_requested.emit(job_id)

    def _on_text_submit(self) -> None:
        text = self._text_input.toPlainText().strip()
        if not text:
            return
        self._text_input.clear()
        # User-initiated send → tail the conversation. Without this, an
        # operator scrolled up reading prior turns wouldn't see their new
        # turn appear because _on_session_changed re-renders with
        # auto_scroll=False (which preserves their scroll position).
        self._detail.request_scroll_to_bottom()
        self.text_submitted.emit(text)

    def _update_text_input_placeholder(self) -> None:
        """Reflect routing state in the text-input placeholder."""
        if not hasattr(self, "_text_input"):
            return
        # This is where the routing sentence lives now that the right pane's
        # status line is gone: what typing (or the push-to-talk key) does,
        # and the keys to send.
        send, newline = _return_keys()
        keys = f"{send} to send, {newline} for a new line"
        if self._active_session_id is None:
            profile = ""
            pending = getattr(self, "_pending_profile", "")
            if pending:
                spec = get_profile(pending)
                profile = f" ({spec.display if spec else pending})"
            self._text_input.setPlaceholderText(
                f"Type, or hold {self._hotkey_label}, to start a new session{profile} — {keys}"
            )
        else:
            # Holding the key from any app continues the selected session
            # (see _ptt_target_sid).
            self._text_input.setPlaceholderText(
                f"Type, or hold {self._hotkey_label}, to continue this session — {keys}"
            )

    # ----- header controls (replacement for the killed mini panel) -----

    def set_model_label(self, text: str) -> None:
        # Tracked uppercase echoes the HOLLER wordmark voice.
        self._model_badge.setText((text or "").upper())
        self._model_badge.setAccessibleName(f"Model: {text}" if text else "Model")

    def set_superintendent_model_label(self, text: str) -> None:
        """Runner pushes the effective worker model onto the inbox badge."""
        if not hasattr(self, "_sup_model_btn"):
            return
        self._sup_model_btn.setText((text or "").upper())
        self._sup_model_btn.setAccessibleName(f"Worker model: {text}" if text else "Worker model")

    def set_warmth(self, text: str) -> None:
        self._warmth.setText(text)

    def set_speech_state(self, state: str) -> None:
        """Runner pushes the speech model's state — "loading", "ready" or
        "failed" — so the header status never says Ready while push-to-talk
        can't transcribe."""
        self._speech_state = state if state in ("loading", "ready", "failed") else "ready"
        self._refresh_warmth()

    @QtCore.Slot()
    @QtCore.Slot(int)
    def _schedule_row_refresh(self, *_args) -> None:
        """Hook for job_added / job_updated — start the debounce timer."""
        if not self._row_refresh_timer.isActive():
            self._row_refresh_timer.start()

    def _coalesced_row_refresh(self) -> None:
        """One pass of all row-affecting refresh work. Runs at most ~4Hz."""
        self._refresh_warmth()
        self._refresh_usage()
        self._refresh_visible_totals()
        self._schedule_dashboard_refresh()

    # ----- Build status -----

    def _poll_release_status(self) -> None:
        import threading

        from holler import release_status

        def _run() -> None:
            try:
                st = release_status.snapshot()
            except Exception:
                log.debug("release status poll failed", exc_info=True)
                return
            self._release_status_ready.emit(st)

        threading.Thread(target=_run, name="holler-release-status", daemon=True).start()

    @QtCore.Slot(object)
    def _apply_release_status(self, st) -> None:
        self._release_status = st
        text = st.chip_text()
        color = {"update_ready": _ACCENT, "failed": _YELLOW}.get(st.state, _FG_DIM)
        self._release_chip.setText(text)
        self._release_chip.setToolTip(st.tooltip())
        self._release_chip.setStyleSheet(
            f"QPushButton {{ color: {color}; background: transparent; border: none; "
            f"font-size: {theme.FONT_SM}pt; padding: 0 {theme.SPACE_SM}px; }}"
            f"QPushButton:hover {{ text-decoration: underline; }}"
        )
        self._release_chip.setVisible(bool(text))

    def _on_release_chip(self) -> None:
        st = self._release_status
        if st is None:
            return
        if st.state == "update_ready":
            self.restart_requested.emit()
        elif st.state == "failed" and st.log:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(st.log))

    # ----- Claude account -----

    def _poll_accounts(self) -> None:
        import threading

        from holler import accounts

        if not accounts.supported():
            self._account_btn.setVisible(False)
            self._account_timer.stop()
            return

        def _run() -> None:
            try:
                # Re-save the active login each poll: its refresh token rotates.
                accounts.remember_current()
                cur, rows = accounts.current(), accounts.saved()
            except Exception:
                log.debug("account poll failed", exc_info=True)
                return
            self._account_ready.emit(cur, rows)

        threading.Thread(target=_run, name="holler-accounts", daemon=True).start()

    @QtCore.Slot(object, object)
    def _apply_accounts(self, cur, rows) -> None:
        prev = self._account
        self._account, self._accounts = cur, list(rows)
        email = cur.email if cur else ""
        if self._account_login_from is not None:
            if email and email.lower() != self._account_login_from.lower():
                self._account_login_from = None
                self._account_login_timer.stop()
                self._account_restart_pending = True
                self._offer_account_restart(f"Signed in as {email}.")
            elif time.monotonic() > self._account_login_deadline:
                self._account_login_from = None
                self._account_login_timer.stop()
        elif prev is not None and cur is not None and prev.email.lower() != email.lower():
            # Switched outside Holler (e.g. `claude auth login` in Terminal):
            # the running sessions still hold the old login until a restart.
            self._account_restart_pending = True
        self._render_account_pill()

    def _render_account_pill(self) -> None:
        cur = self._account
        if cur is None:
            text, tip = "Not signed in", "Claude Code isn't signed in. Click to sign in."
        else:
            text = cur.email if len(cur.email) <= 28 else cur.email[:26] + "…"
            tip = f"Claude account: {cur.email}" + (f"\n{cur.org}" if cur.org else "")
            tip += "\nClick to switch accounts."
        if self._account_restart_pending:
            text += "  ·  restart to apply"
            tip += "\nRunning sessions still use the previous account until Holler restarts."
        if self._header_compact:
            text = "↻" if self._account_restart_pending else ""
        self._account_btn.setText(text)
        self._account_btn.setToolTip(tip)
        self._account_btn.setAccessibleName(f"Claude account: {cur.email if cur else 'not signed in'}")

    def _show_account_menu(self) -> None:
        from holler import accounts

        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(_QSS)
        title = menu.addAction("Claude account — every session runs as:")
        title.setEnabled(False)
        cur = (self._account.email.lower() if self._account else "")
        switch_acts = {}
        for a in self._accounts:
            act = menu.addAction(a.email + (f"   ({a.org})" if a.org and a.org != a.email + "'s Organization" else ""))
            act.setCheckable(True)
            act.setChecked(a.email.lower() == cur)
            switch_acts[act] = a
        if not self._accounts:
            none = menu.addAction("No saved accounts yet")
            none.setEnabled(False)
        menu.addSeparator()
        add_act = menu.addAction("Add another account…")
        forget_menu = None
        forget_acts = {}
        others = [a for a in self._accounts if a.email.lower() != cur]
        if others:
            forget_menu = menu.addMenu("Forget an account")
            forget_menu.setStyleSheet(_QSS)
            for a in others:
                forget_acts[forget_menu.addAction(a.email)] = a
        restart_act = None
        if self._account_restart_pending:
            menu.addSeparator()
            restart_act = menu.addAction("Restart Holler to apply")
        chosen = menu.exec(self._account_btn.mapToGlobal(self._account_btn.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen in switch_acts:
            a = switch_acts[chosen]
            if a.email.lower() != cur:
                self._switch_account(a.email)
        elif chosen is add_act:
            self._add_account()
        elif chosen in forget_acts:
            accounts.forget(forget_acts[chosen].email)
            self._poll_accounts()
        elif chosen is restart_act:
            self.restart_requested.emit()

    def _switch_account(self, email: str) -> None:
        from holler import accounts

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Switch Claude account?")
        box.setText(f"Switch to {email}?")
        box.setInformativeText(
            "Holler restarts so every session uses the new account. "
            "Claude Code in Terminal switches too."
        )
        go = box.addButton("Switch && Restart", QtWidgets.QMessageBox.AcceptRole)
        cancel = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(go)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is not go:
            return
        try:
            acct = accounts.switch_to(email)
        except accounts.AccountError as e:
            QtWidgets.QMessageBox.warning(self, "Couldn't switch account", str(e))
            return
        self._account = acct
        self._account_restart_pending = True
        self._render_account_pill()
        self.restart_requested.emit()

    def _add_account(self) -> None:
        from holler import accounts, claude_cli

        email, ok = QtWidgets.QInputDialog.getText(
            self, "Add Claude account",
            "Email of the account to add (optional).\n\n"
            "Terminal opens and your browser asks you to sign in. "
            "Come back here when it says you're logged in.",
        )
        if not ok:
            return
        self._account_login_from = self._account.email if self._account else ""
        self._account_login_deadline = time.monotonic() + 600
        try:
            accounts.login_in_terminal(claude_cli.cli_path(), email.strip())
        except accounts.AccountError as e:
            self._account_login_from = None
            QtWidgets.QMessageBox.warning(self, "Couldn't start sign-in", str(e))
            return
        self._account_login_timer.start()

    def _offer_account_restart(self, headline: str) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Claude account changed")
        box.setText(headline)
        box.setInformativeText("Restart Holler now so every session uses it?")
        go = box.addButton("Restart Now", QtWidgets.QMessageBox.AcceptRole)
        box.addButton("Later", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(go)
        self.raise_()
        self.activateWindow()
        box.exec()
        if box.clickedButton() is go:
            self.restart_requested.emit()

    # ----- Agents pane -----

    def _set_agents_pane(self, on: bool, *, user: bool = False) -> None:
        was = self._agents_panel.isVisible()
        self._agents_panel.setVisible(on)
        self._agents_btn.setChecked(on)
        if on and not was:
            total = max(1, sum(self._agents_split.sizes()))
            pane = min(PANE_DEFAULT_W, max(1, total // 2))
            self._agents_split.setSizes([max(1, total - pane), pane])
        sid = self._active_session_id
        if user and sid is not None:
            if on:
                self._agents_dismissed.discard(sid)
            elif any(a.running for a in self._job_store.agents_for_session(sid)):
                self._agents_dismissed.add(sid)

    def _on_agents_btn(self) -> None:
        self._set_agents_pane(self._agents_btn.isChecked(), user=True)

    def _sync_agents_pane(self, local_session_id: Optional[int]) -> None:
        if self._agents_panel.session_id() != local_session_id:
            self._agents_panel.set_session(local_session_id)
        self._refresh_agents_btn()
        self._maybe_open_agents_pane()

    def _maybe_open_agents_pane(self) -> None:
        """Open the pane while the session on screen has running agents —
        unless the operator closed it during those agents' run."""
        sid = self._active_session_id
        if sid is None or self._agents_panel.isVisible() or sid in self._agents_dismissed:
            return
        if any(a.running for a in self._job_store.agents_for_session(sid)):
            self._set_agents_pane(True)

    def _refresh_agents_btn(self) -> None:
        running = len(self._job_store.running_agents())
        if running:
            self._agents_btn.setText(f"{running} running")
        else:
            # A narrow header keeps just the icon until agents are live.
            self._agents_btn.setText("" if self._header_compact else "Agents")
        self._agents_btn.setAccessibleName(
            f"Agents, {running} running" if running else "Agents")

    @QtCore.Slot(int)
    def _on_agents_changed(self, local_session_id: int) -> None:
        self._refresh_agents_btn()
        self._schedule_row_refresh()
        if local_session_id == self._active_session_id:
            self._maybe_open_agents_pane()

    def _refresh_warmth(self, *_args) -> None:
        running = sum(1 for j in self._job_store.all() if j.status == "running")
        queued = sum(1 for j in self._job_store.all() if j.status == "queued")
        # Status dot color mirrors what the operator cares about glance-wise:
        # dim green when nothing's running (idle/ready), green when work is
        # in flight, warning-yellow when there's a queue waiting on capacity
        # (state colour — brand amber stays on the wordmark + model pill).
        # The words carry the same information.
        speech = getattr(self, "_speech_state", "ready")
        if speech == "failed":
            dot = _RED
        elif queued or speech == "loading":
            dot = _YELLOW
        elif running:
            dot = _GREEN
        else:
            dot = _GREEN_DIM
        parts = [{"failed": "Speech unavailable — see Settings",
                  "loading": "Loading speech model…"}.get(speech, "Ready")]
        if running:
            parts.append(f"{running} running")
        if queued:
            parts.append(f"{queued} queued")
        agents_running = len(self._job_store.running_agents())
        if agents_running:
            parts.append(f"{agents_running} agent{'s' if agents_running != 1 else ''}")
        body = html_escape(" · ".join(parts))
        self._warmth.setText(
            f"<span style='color:{dot}'>●</span> "
            f"<span style='color:{_FG_DIM}'>{body}</span>"
        )
        self._warmth.setAccessibleName("Status: " + ", ".join(parts))

    def _render_empty_dashboard(self) -> None:
        """Replace the giant emoji splash with an actionable dashboard:
        recent sessions (click to open), this-week token + cost rollup,
        errored sessions to triage — or, before the first session exists, a
        short how-to. Pulls everything from `self._store`. Unchanged HTML is
        not re-set (the periodic refresh must not reset the scroll)."""
        try:
            # ---- recents (click to open) ----
            recents_rows = []
            for sid in self._recents[:6]:
                s = self._store.get_session(sid)
                if s is None or s.archived:
                    continue
                title = html_escape(_truncate(s.title or "Untitled session", 50))
                age = html_escape(_relative_time(s.last_activity_at))
                profile = getattr(s, "profile", "") or ""
                stripe = _color_for_profile(profile)
                done = s.reviewed_at is not None
                badge = (f" <span style='color:{theme.FG_MUTED}; font-weight:{theme.WEIGHT_REGULAR};'>"
                         f"· ✓ Done</span>") if done else ""
                prof_txt = ""
                if profile:
                    spec = get_profile(profile)
                    prof_txt = html_escape(spec.display if spec else profile) + " · "
                recents_rows.append(
                    f"<tr>"
                    f"<td width='3' bgcolor='{stripe}' style='padding:0;'></td>"
                    f"<td style='padding:{_DASH_PAD_V}px {_DASH_PAD_H}px; font-size:{theme.FONT_SM}pt; "
                    f"font-weight:{theme.WEIGHT_SEMIBOLD};'>"
                    f"<a href='{_DASH_SESSION_SCHEME}:{s.id}' style='color:{theme.FG}; text-decoration:none;'>"
                    f"{title}</a>{badge}</td>"
                    f"<td align='right' style='padding:{_DASH_PAD_V}px {_DASH_PAD_H}px; color:{theme.FG_MUTED}; "
                    f"font-size:{theme.FONT_XS}pt;'>{prof_txt}{age}</td>"
                    f"</tr>"
                )

            # ---- week token rollup ----
            now = time.time()
            week_cutoff = now - 7 * 86400
            c = self._store._conn()  # type: ignore[attr-defined]
            week_row = c.execute(
                "SELECT COUNT(DISTINCT session_id) AS n_sess, "
                "  COALESCE(SUM(input_tokens), 0) AS fresh_in, "
                "  COALESCE(SUM(input_tokens + cache_creation_input_tokens + cache_read_input_tokens), 0) AS ctx_in, "
                "  COALESCE(SUM(output_tokens), 0) AS output, "
                "  COALESCE(SUM(cache_read_input_tokens), 0) AS cache_r, "
                "  COALESCE(SUM(total_cost_usd), 0.0) AS cost "
                "FROM turns WHERE started_at >= ?",
                (week_cutoff,),
            ).fetchone()
            week_sessions = int(week_row["n_sess"] or 0)
            week_fresh_in = int(week_row["fresh_in"] or 0)
            week_ctx_in = int(week_row["ctx_in"] or 0)
            week_out = int(week_row["output"] or 0)
            week_cache_r = int(week_row["cache_r"] or 0)
            week_cost = float(week_row["cost"] or 0.0)
            cache_pct = int(round((week_cache_r / week_ctx_in) * 100)) if week_ctx_in else 0

            # ---- errored sessions (in last 24h, needs triage) ----
            err_cutoff = now - 24 * 3600
            errored_n = int(c.execute(
                "SELECT COUNT(*) FROM sessions WHERE status = 'error' "
                "AND last_activity_at >= ? AND archived = 0",
                (err_cutoff,),
            ).fetchone()[0] or 0)

            # ---- active vs done split (lifetime, archived excluded) ----
            split = c.execute(
                "SELECT "
                "  SUM(CASE WHEN reviewed_at IS NULL THEN 1 ELSE 0 END) AS active_n, "
                "  SUM(CASE WHEN reviewed_at IS NOT NULL THEN 1 ELSE 0 END) AS done_n "
                "FROM sessions WHERE archived = 0"
            ).fetchone()
            active_n = int(split["active_n"] or 0)
            done_n = int(split["done_n"] or 0)
            # Any session at all (archived included) — none means first run.
            any_session = c.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None
        except Exception:
            log.exception("failed to build empty-state dashboard")
            return

        # Build the HTML. QTextDocument quirks shape this markup: heading
        # tags ignore CSS font-size (Qt applies its own heading scale on top),
        # so section titles are styled <p>s; tables take width / bgcolor as
        # attributes (CSS width on <table> is ignored, and border-collapse
        # drops the background).
        def _section(label: str, top: int) -> str:
            return (
                f"<p style='color:{theme.FG_MUTED}; font-size:{theme.FONT_XS}pt; font-weight:{theme.WEIGHT_BOLD}; "
                f"letter-spacing:{theme.TRACKING_CAPS_MD}; text-transform:uppercase; "
                f"margin:{top}px 0 {theme.SPACE_SM}px 0;'>{label}</p>"
            )

        def _table(rows: str) -> str:
            return (
                f"<table width='100%' cellspacing='0' cellpadding='0' bgcolor='{theme.SURFACE}'>{rows}</table>"
            )

        key = html_escape(self._hotkey_label)
        mic = _icon_img("mic", _ACCENT, theme.ICON_GLYPH)
        shortcuts = (
            f"Switch session {_key_hint('Ctrl+K')} · New session {_key_hint('Ctrl+N')} · "
            f"Search {_key_hint('Ctrl+F')}"
        )

        if not any_session:
            # First run: nothing to resume or total yet — say how Holler works.
            def _step(n: int, head: str, body: str) -> str:
                return (
                    f"<tr><td valign='top' width='28' style='padding:{_DASH_PAD_V}px 0 {_DASH_PAD_V}px {_DASH_PAD_H}px; "
                    f"color:{theme.ACCENT}; font-size:{theme.FONT_SM}pt; font-weight:{theme.WEIGHT_BOLD};'>{n}</td>"
                    f"<td style='padding:{_DASH_PAD_V}px {_DASH_PAD_H}px; font-size:{theme.FONT_SM}pt;'>"
                    f"<span style='color:{theme.FG}; font-weight:{theme.WEIGHT_SEMIBOLD};'>{head}</span><br>"
                    f"<span style='color:{theme.FG_MUTED};'>{body}</span></td></tr>"
                )

            steps = _table("".join([
                _step(1, f"Hold {key} and say what you need",
                      "Holler sends what you say, plus a screenshot of the window in front of you, to Claude."),
                _step(2, "Point while you talk",
                      "Draw on the screen while the key is held to circle what you mean."),
                _step(3, "Read the reply here",
                      "Each conversation becomes a session on the left. Hold the key again to continue it."),
            ]))
            body = f"""
                <html><body style='background: {theme.CANVAS}; color: {theme.FG};'>
                <div style='margin: {theme.SPACE_LG}px {theme.SPACE_SM}px {theme.SPACE_XL}px {theme.SPACE_SM}px;'>
                <p style='margin: 0 0 {theme.SPACE_SM}px 0;'>{_icon_img("mic", _ACCENT, theme.SPACE_XL)}</p>
                <p style='color: {theme.FG}; font-size: {theme.FONT_DISPLAY}pt; font-weight: {theme.WEIGHT_SEMIBOLD};
                          margin: 0;'>Welcome to Holler</p>
                <p style='color: {theme.FG_MUTED}; font-size: {theme.FONT_MD}pt; margin: {theme.SPACE_SM}px 0 0 0;'>
                    Talk to Claude about whatever is on your screen. Or type below to start.</p>
                {_section("How it works", theme.SPACE_XL)}
                {steps}
                <p style='color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt; margin: {theme.SPACE_LG}px 0 0 0;'>
                    {shortcuts} · <a href='{_DASH_ACTION_SCHEME}:settings' style='color:{theme.ACCENT}; text-decoration:none;'>Settings</a></p>
                </div>
                </body></html>
            """
            self._set_dashboard_html(body)
            return

        recents_html = ""
        if recents_rows:
            recents_html = _section("Recent sessions", theme.SPACE_XL) + _table("".join(recents_rows))

        def _stat(label: str, value: str, *, label_color: str = theme.FG_MUTED,
                  value_color: str = theme.FG, indent: bool = False, strong: bool = False,
                  title: str = "") -> str:
            pad_l = _DASH_PAD_H + (theme.SPACE_MD if indent else 0)
            weight = f" font-weight:{theme.WEIGHT_SEMIBOLD};" if strong else ""
            tip = f" title='{title}'" if title else ""
            return (
                f"<tr><td style='padding:{_DASH_PAD_V}px {_DASH_PAD_H}px {_DASH_PAD_V}px {pad_l}px; "
                f"color:{label_color}; font-size:{theme.FONT_SM}pt;{weight}'{tip}>{label}</td>"
                f"<td align='right' style='padding:{_DASH_PAD_V}px {_DASH_PAD_H}px; color:{value_color}; "
                f"font-size:{theme.FONT_SM}pt; font-weight:{theme.WEIGHT_SEMIBOLD};'>{value}</td></tr>"
            )

        stats_rows = [
            _stat("Active", str(active_n)),
            _stat("Done", str(done_n), value_color=theme.SUCCESS),
        ]
        if errored_n:
            stats_rows.append(_stat("⚠ Failed in the last 24 hours", str(errored_n),
                                    label_color=theme.DANGER, value_color=theme.DANGER))

        week_html = ""
        if week_fresh_in or week_out or week_ctx_in:
            headline_tokens = week_fresh_in + week_out
            divider = (
                f"<tr><td colspan='2' style='padding:0 {_DASH_PAD_H}px;'>"
                f"<hr style='background-color:{theme.BORDER}; margin:{theme.SPACE_XS}px 0;'/></td></tr>"
            )
            week_html = _section("This week", theme.SPACE_XL) + _table("".join([
                _stat("Sessions", str(week_sessions)),
                _stat("New tokens", _fmt_k(headline_tokens), label_color=theme.FG, strong=True),
                _stat("↑ Input", _fmt_k(week_fresh_in), value_color=theme.FG_MUTED, indent=True),
                _stat("↓ Output", _fmt_k(week_out), value_color=theme.FG_MUTED, indent=True),
                divider,
                _stat("Total processed", _fmt_k(week_ctx_in), value_color=theme.FG_MUTED,
                      title="Includes cache re-reads"),
                _stat("Cache hit rate", f"{cache_pct}%", value_color=theme.SUCCESS),
                _stat("API-equivalent cost", f"${week_cost:.2f}", value_color=theme.ACCENT),
            ]))

        body = f"""
            <html><body style='background: {theme.CANVAS}; color: {theme.FG};'>
            <div style='margin: {theme.SPACE_LG}px {theme.SPACE_SM}px {theme.SPACE_XL}px {theme.SPACE_SM}px;'>
            <p style='color: {theme.FG}; font-size: {theme.FONT_DISPLAY}pt; font-weight: {theme.WEIGHT_SEMIBOLD};
                      margin: 0;'>Hold {key} to start talking, or pick a session</p>
            <p style='color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt; margin: {theme.SPACE_SM}px 0 0 0;'>
                {mic}&nbsp; {shortcuts}</p>
            {recents_html}
            {_section("At a glance", theme.SPACE_XL)}
            {_table(''.join(stats_rows))}
            {week_html}
            </div>
            </body></html>
        """
        self._set_dashboard_html(body)

    def _set_dashboard_html(self, body: str) -> None:
        """Show `body` as the dashboard. A refresh that changes nothing is
        skipped; a changed refresh keeps the dashboard's scroll position."""
        browser = self._detail._reply_browser
        showing = getattr(self, "_dashboard_showing", False)
        if showing and body == self._dashboard_html:
            return
        keep = browser.verticalScrollBar().value() if showing else 0
        self._dashboard_html = body
        self._detail.show_dashboard(body)
        self._dashboard_showing = True
        if keep:
            browser.verticalScrollBar().setValue(keep)

    def _schedule_dashboard_refresh(self) -> None:
        """Coalesced dashboard re-render (session / job churn while nothing
        is selected). Not restarted while pending, so a steady stream of
        updates can't starve it."""
        if self._active_session_id is None and not self._dashboard_refresh_timer.isActive():
            self._dashboard_refresh_timer.start()

    def set_hotkey(self, raw: str) -> None:
        """Push-to-talk key changed in Settings: refresh every place that
        names it (dashboard, status line, composer placeholder)."""
        self._hotkey_label = _format_hotkey(raw)
        if self._active_session_id is None:
            self._ptt_status.setText(f"Hold {self._hotkey_label} to start a new session")
            if getattr(self, "_dashboard_showing", False):
                self._render_empty_dashboard()
        else:
            sess = self._store.get_session(self._active_session_id)
            if sess is not None:
                self._apply_selected_status(sess)
        self._update_text_input_placeholder()

    def _refresh_dashboard_if_visible(self) -> None:
        """Periodic / change-driven re-render while nothing is selected (the
        dashboard is what the right pane shows then)."""
        # Not while hidden / minimised (showEvent / un-minimise catch up).
        if (getattr(self, "_dashboard_showing", False) and self._active_session_id is None
                and self.isVisible() and not self.isMinimized()):
            self._render_empty_dashboard()

    def _on_dashboard_anchor(self, url: QtCore.QUrl) -> None:
        """Dashboard links: a recent session opens it; first-run actions.
        Only while the dashboard is what the pane shows (a transcript can't
        smuggle in a link that switches sessions)."""
        if not getattr(self, "_dashboard_showing", False):
            return
        scheme = url.scheme()
        if scheme == _DASH_SESSION_SCHEME:
            try:
                sid = int(url.path())
            except ValueError:
                return
            self.select_session(sid)
        elif scheme == _DASH_ACTION_SCHEME and url.path() == "settings":
            self.settings_requested.emit()

    def _update_routing_pill(self) -> None:
        """Reflect the implicit routing decision visibly: where will the next
        PTT go? Selected session → "→ Continue: title". No selection → "→ New".
        """
        if not hasattr(self, "_routing_pill"):
            return
        if self._active_session_id is not None:
            sess = self._store.get_session(self._active_session_id)
            if sess is not None:
                title_short = _truncate(sess.title or "Untitled session", 28)
                self._routing_pill.setText(f"→ {title_short}")
                # Outline-only (BRAND.md: the pill is STATE, never filled).
                # Continue = full-strength accent border; New = soft border.
                self._routing_pill.setStyleSheet(
                    f"color: {_ACCENT}; background: transparent; "
                    f"border: 1px solid {_ACCENT}; border-radius: {theme.RADIUS_PILL}px; {theme.PILL_BOX} "
                    f"padding-left: {theme.SPACE_MD}px; padding-right: {theme.SPACE_MD}px; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};"
                )
                self._routing_pill.setToolTip(
                    f"Next push-to-talk continues “{sess.title or 'Untitled session'}”"
                )
                self._routing_pill.setAccessibleName(
                    f"Next push-to-talk: continues “{sess.title or 'Untitled session'}”")
                self._routing_pill.setVisible(True)
                return
        # No selection → new-session routing.
        profile_suffix = ""
        pending = getattr(self, "_pending_profile", "")
        if pending:
            spec = get_profile(pending)
            profile_suffix = f" · {spec.display if spec else pending}"
        self._routing_pill.setText(f"→ New session{profile_suffix}")
        self._routing_pill.setStyleSheet(
            f"color: {theme.ACCENT}; background: transparent; "
            f"border: 1px solid {theme.ACCENT_PILL_BORDER}; border-radius: {theme.RADIUS_PILL}px; {theme.PILL_BOX} "
            f"padding-left: {theme.SPACE_MD}px; padding-right: {theme.SPACE_MD}px; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};"
        )
        self._routing_pill.setToolTip(
            f"Next push-to-talk starts a new session{profile_suffix}"
        )
        self._routing_pill.setAccessibleName(f"Next push-to-talk: starts a new session{profile_suffix}")
        self._routing_pill.setVisible(True)
        self._update_text_input_placeholder()

    def _refresh_titler_status(self) -> None:
        """Poll the AutoTitler's in-memory stats and show a small chip when
        anything is in flight or has failed. Idle + healthy = hidden so the
        header doesn't get noisy."""
        titler = self._auto_titler
        if titler is None:
            self._titler_status.setVisible(False)
            return
        try:
            stats = titler.stats()  # type: ignore[attr-defined]
        except Exception:
            self._titler_status.setVisible(False)
            return
        retrying = int(stats.get("retrying", 0))
        failed = int(stats.get("failed", 0))
        fallback = int(stats.get("fallback", 0))
        ok = int(stats.get("ok", 0))
        # Only render when there's something worth surfacing.
        bits = []
        color = _FG_DIM
        if retrying > 0:
            bits.append(f"Naming {retrying}…")
            color = _ACCENT
        if failed > 0:
            bits.append(f"{failed} title{'s' if failed != 1 else ''} failed")
            color = _RED
        if fallback > 0:
            bits.append(f"{fallback} basic title{'s' if fallback != 1 else ''}")
            if color == _FG_DIM:
                color = _YELLOW
        if not bits:
            self._titler_status.setVisible(False)
            return
        self._titler_status.setStyleSheet(f"color: {color}; font-size: {theme.FONT_SM}pt;")
        self._titler_status.setText(" · ".join(bits))
        self._titler_status.setToolTip(
            f"Titles: {ok} done, {fallback} basic, {retrying} retrying, {failed} failed.\n"
            "Right-click a session → Regenerate title to retry."
        )
        self._titler_status.setVisible(True)

    def _refresh_usage(self, *_args) -> None:
        """Update the header token-usage label.

        Selected session: cumulative across that session's in-memory jobs.
        No selection: live totals across any currently-running jobs.
        Otherwise: blank.
        """
        sid = self._active_session_id
        if sid is not None:
            jobs = [j for j in self._job_store.all() if j.local_session_id == sid]
        else:
            jobs = [j for j in self._job_store.all() if j.status == "running"]
        if not jobs:
            self._usage.setText("")
            self._usage.setToolTip("")
            self._usage_short = ""
            self._sync_header_density()
            return

        inp = sum(j.input_tokens for j in jobs)
        out = sum(j.output_tokens for j in jobs)
        cache_create = sum(j.cache_creation_input_tokens for j in jobs)
        cache_read = sum(j.cache_read_input_tokens for j in jobs)
        cost = sum(j.total_cost_usd for j in jobs)
        ctx_in = inp + cache_create + cache_read

        if ctx_in == 0 and out == 0:
            self._usage.setText("")
            self._usage.setToolTip("")
            self._usage_short = ""
            self._sync_header_density()
            return

        cache_pct = int(round((cache_read / ctx_in) * 100)) if ctx_in else 0
        # Headline = new content only; cache re-sends are bookkeeping, not usage.
        total = inp + out
        parts = [f"{_fmt_k(total)} tokens"]
        if cache_pct > 0:
            parts.append(f"{cache_pct}% cached")
        if cost > 0:
            parts.append(f"${cost:.3f}")

        running_here = any(j.status == "running" for j in jobs)
        prefix = "● " if running_here else ""
        accent = _ACCENT if running_here else _FG_DIM
        self._usage.setStyleSheet(f"color: {accent}; font-size: {theme.FONT_SM}pt;")
        self._usage.setText(prefix + " · ".join(parts))
        self._usage_short = parts[0]
        self._usage_color = accent

        scope = "this session" if self._active_session_id is not None else "running turns"
        rows = [
            f"<b>Token usage</b> &middot; {scope}",
            "<table cellspacing='0' cellpadding='2'>",
            f"<tr><td><b>{_fmt_k(total)}</b></td>"
            f"<td>&nbsp;new tokens</td></tr>",
            f"<tr><td>&uarr;&nbsp;{_fmt_k(inp)}</td>"
            f"<td>&nbsp;input (prompts, screenshots, tool results)</td></tr>",
            f"<tr><td>&darr;&nbsp;{_fmt_k(out)}</td>"
            f"<td>&nbsp;output (replies and tool calls)</td></tr>",
            f"<tr><td colspan='2'><hr style='background-color:{theme.BORDER};'/></td></tr>",
            f"<tr><td>{_fmt_k(ctx_in)}</td>"
            f"<td>&nbsp;total processed, including cache re-reads</td></tr>",
        ]
        if cache_pct > 0:
            rows.append(
                f"<tr><td><b>{cache_pct}%</b></td>"
                f"<td>&nbsp;served from cache (about 10% of the normal price)</td></tr>"
            )
        if cost > 0:
            rows.append(
                f"<tr><td><b>${cost:.3f}</b></td>"
                f"<td>&nbsp;API-equivalent cost (not billed on a Claude subscription)</td></tr>"
            )
        rows.append("</table>")
        self._usage.setToolTip("".join(rows))
        self._usage.setAccessibleDescription(_html_to_plain(self._usage.toolTip()))
        self._sync_header_density()

    def _sync_header_density(self) -> None:
        """Below HEADER_COMPACT_W the usage and quota chips fold into one
        compact chip (headline numbers; both breakdowns in its tooltip), so a
        narrow window's header doesn't crowd the routing pill."""
        if not hasattr(self, "_usage_compact"):
            return
        compact = self.width() < theme.HEADER_COMPACT_W
        self._header_compact = compact
        if hasattr(self, "_agents_btn"):
            self._refresh_agents_btn()
        if hasattr(self, "_account_btn"):
            self._render_account_pill()
        usage_on = bool(self._usage.text())
        quota_on = bool(self._quota.text())
        if not compact:
            self._usage_compact.setVisible(False)
            self._usage.setVisible(usage_on)
            self._usage_div.setVisible(usage_on)
            self._quota.setVisible(quota_on)
            self._quota_div.setVisible(quota_on)
            return
        self._usage.setVisible(False)
        self._quota.setVisible(False)
        self._quota_div.setVisible(False)
        bits, tips = [], []
        if usage_on:
            bits.append(self._usage_short)
            tips.append(self._usage.toolTip())
        if quota_on:
            bits.append(self._quota_short)
            tips.append(self._quota.toolTip())
        if not bits:
            self._usage_compact.setVisible(False)
            self._usage_div.setVisible(False)
            return
        # Quota warnings outrank the running accent (they need action).
        color = self._quota_color if (quota_on and self._quota_color != _FG_DIM) \
            else getattr(self, "_usage_color", _FG_DIM) if usage_on else _FG_DIM
        want = f"color: {color}; font-size: {theme.FONT_SM}pt;"
        if self._usage_compact.styleSheet() != want:
            self._usage_compact.setStyleSheet(want)
        self._usage_compact.setText(" · ".join(bits))
        self._usage_compact.setToolTip("<br>".join(tips))
        self._usage_compact.setAccessibleName("Usage: " + " · ".join(bits))
        self._usage_compact.setAccessibleDescription(_html_to_plain(self._usage_compact.toolTip()))
        self._usage_compact.setVisible(True)
        self._usage_div.setVisible(True)

    def _refresh_quota(self, *_args) -> None:
        """Render the Anthropic rate-limit chip from cached snapshots.

        The SDK's ``RateLimitEvent`` only fires on state transitions, so we
        render whatever was last seen (potentially from a prior process via
        ``state.json``). The "resets in" countdown is computed live from the
        cached ``resets_at`` timestamps and re-rendered every 30s by a timer
        in ``__init__``.

        Display contract:
          - empty / no data → label hidden
          - one or more windows → ``5h 64% · 7d 31%`` style, color by worst
            status across all known windows. The SDK frequently sends
            ``utilization: None`` (status only) — those windows render their
            status instead of a percentage (``7d limit hit``).
          - a window whose ``resets_at`` has passed is stale: whatever status
            it last reported (e.g. ``rejected``) no longer applies, so it's
            shown as ``reset`` and doesn't color the chip.
          - tooltip → per-window breakdown with reset time + last-seen age
        """
        snapshots = self._job_store.rate_limits()
        # Filter out the catch-all "_unknown" bucket and any snapshot with
        # neither a utilization nor a status to render. Keep "overage"
        # separate — it's surfaced in the tooltip but not in the headline chip.
        usable = {
            rtype: s for rtype, s in snapshots.items()
            if rtype and rtype != "_unknown" and isinstance(s, dict)
            and (s.get("utilization") is not None or s.get("status"))
        }
        if not usable:
            self._quota.setText("")
            self._quota.setToolTip("")
            self._quota_short = ""
            self._quota_color = _FG_DIM
            self._sync_header_density()
            return

        now = time.time()

        def _fmt_window(rtype: str) -> str:
            return {
                "five_hour": "5h",
                "seven_day": "7d",
                "seven_day_opus": "7d Opus",
                "seven_day_sonnet": "7d Sonnet",
                "overage": "Extra usage",
            }.get(rtype, rtype)

        def _fmt_resets_in(resets_at) -> str:
            if not resets_at:
                return ""
            secs = int(resets_at) - int(now)
            if secs <= 0:
                return "resetting"
            if secs < 3600:
                return f"{secs // 60}m"
            if secs < 86400:
                h = secs // 3600
                m = (secs % 3600) // 60
                return f"{h}h {m}m" if m else f"{h}h"
            d = secs // 86400
            h = (secs % 86400) // 3600
            return f"{d}d {h}h" if h else f"{d}d"

        def _window_stale(s: dict) -> bool:
            ra = s.get("resets_at")
            try:
                return bool(ra) and float(ra) <= now
            except (TypeError, ValueError):
                return False

        def _effective_status(s: dict) -> str:
            if _window_stale(s):
                return "allowed"
            return s.get("status") or "allowed"

        status_label = {
            "allowed": "OK",
            "allowed_warning": "near limit",
            "rejected": "limit reached",
        }

        def _chip_value(s: dict) -> str:
            if _window_stale(s):
                return "reset"
            util = s.get("utilization")
            if util is not None:
                return f"{int(round(util * 100))}%"
            st = s.get("status") or ""
            return status_label.get(st, st or "—")

        # Ordering: five_hour first (most actionable), then 7-day variants.
        order = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
        ordered_types = [t for t in order if t in usable]
        ordered_types += [t for t in usable.keys() if t not in order and t != "overage"]

        chip_parts: list[str] = []
        worst_status = "allowed"
        for rtype in ordered_types:
            s = usable[rtype]
            chip_parts.append(f"{_fmt_window(rtype)} {_chip_value(s)}")
            status = _effective_status(s)
            if status == "rejected":
                worst_status = "rejected"
            elif status == "allowed_warning" and worst_status != "rejected":
                worst_status = "allowed_warning"

        if not chip_parts:
            self._quota.setText("")
            self._quota_short = ""
            self._quota_color = _FG_DIM
            self._sync_header_density()
            return

        # Tail with the soonest reset of any tracked window so the operator
        # always sees "when does anything change" at a glance.
        soonest_reset_secs: Optional[int] = None
        for s in usable.values():
            ra = s.get("resets_at")
            if ra:
                delta = int(ra) - int(now)
                if delta > 0 and (soonest_reset_secs is None or delta < soonest_reset_secs):
                    soonest_reset_secs = delta
        headline = chip_parts[0]
        if soonest_reset_secs is not None:
            chip_parts.append(f"resets in {_fmt_resets_in(int(now) + soonest_reset_secs)}")

        color = {
            "allowed": _FG_DIM,
            "allowed_warning": _YELLOW,
            "rejected": _RED,
        }.get(worst_status, _FG_DIM)
        # Near / over a limit says so in words too (⚠), not only in colour.
        warn = "⚠ " if worst_status in ("allowed_warning", "rejected") else ""
        self._quota.setStyleSheet(f"color: {color}; font-size: {theme.FONT_SM}pt;")
        self._quota.setText(warn + " · ".join(chip_parts))
        self._quota_short = warn + headline
        self._quota_color = color

        # Tooltip: full per-window detail with status, reset time, and
        # how stale the last-known snapshot is.
        tip_rows = ["<b>Usage limits</b> &middot; last reported",
                    "<table cellspacing='0' cellpadding='3'>"]
        for rtype in ordered_types:
            s = usable[rtype]
            util = s.get("utilization")
            pct = f"{int(round(util * 100))}%" if util is not None else "—"
            stat = status_label.get(s.get("status") or "", s.get("status") or "")
            if _window_stale(s):
                stat = f"{stat} (window has reset)" if stat else "window has reset"
            resets_str = _fmt_resets_in(s.get("resets_at"))
            seen_secs = int(now - float(s.get("seen_at") or now))
            seen_label = _fmt_resets_in(int(now) + seen_secs) if seen_secs >= 60 else "just now"
            tip_rows.append(
                f"<tr>"
                f"<td><b>{_fmt_window(rtype)}</b></td>"
                f"<td>&nbsp;{pct} used</td>"
                f"<td>&nbsp;<i>{stat}</i></td>"
                f"<td>&nbsp;resets in {resets_str or '—'}</td>"
                f"<td>&nbsp;<span style='color:{theme.FG_MUTED}'>(updated {seen_label}{'' if seen_label == 'just now' else ' ago'})</span></td>"
                f"</tr>"
            )
        # Overage row if present.
        overage_snap = snapshots.get("overage") if isinstance(snapshots.get("overage"), dict) else None
        if overage_snap is not None:
            o_util = overage_snap.get("utilization")
            pct = f"{int(round(o_util * 100))}%" if o_util is not None else "—"
            stat = status_label.get(overage_snap.get("status") or "", overage_snap.get("status") or "")
            resets_str = _fmt_resets_in(overage_snap.get("resets_at"))
            disabled_reason = overage_snap.get("overage_disabled_reason")
            extra = f" — {html_escape(disabled_reason)}" if disabled_reason else ""
            tip_rows.append(
                f"<tr><td><b>Extra usage</b></td>"
                f"<td>&nbsp;{pct}</td>"
                f"<td>&nbsp;<i>{stat}</i></td>"
                f"<td>&nbsp;resets in {resets_str or '—'}{extra}</td>"
                f"<td></td></tr>"
            )
        tip_rows.append("</table>")
        tip_rows.append(
            f"<div style='color:{theme.FG_MUTED}; font-size:{theme.FONT_XS}pt; margin-top:{theme.SPACE_XS}px;'>"
            "Updated only when your usage level changes, so values may lag."
            "</div>"
        )
        self._quota.setToolTip("".join(tip_rows))
        self._quota.setAccessibleDescription(_html_to_plain(self._quota.toolTip()))
        self._sync_header_density()

    @QtCore.Slot(str)
    def show_inline_notice(
        self,
        text: str,
        *,
        undo_callback: Optional[Callable] = None,
        action: Optional[tuple] = None,
        on_click: Optional[Callable] = None,
        kind: Optional[str] = None,
    ) -> None:
        """In-window toast — anchors to the bottom-right of THIS window's
        geometry, not the primary screen (which on multi-monitor setups was
        the wrong display).

        If `undo_callback` is set, the toast shows an `Undo` link the user can
        click within the toast's lifetime to reverse the action. `action` is
        an optional (label, callable) button, `on_click` makes the notice
        clickable, and `kind` ("info" / "success" / "error") picks its styling
        and duration (errors 10 s, else 6 s, 8 s with a button) — all passed
        straight to panel.Toast. Text-only and undo calls are unchanged.
        """
        # Anchor inside the sessions window.
        anchor_global = self.mapToGlobal(QtCore.QPoint(self.width(), self.height()))
        extra = {}
        if action is not None:
            extra["action"] = action
        if on_click is not None:
            extra["on_click"] = on_click
        if kind is not None:
            extra["kind"] = kind
        self._toast.show_message(text, (anchor_global.x(), anchor_global.y()),
                                 undo_callback=undo_callback, **extra)

    # ----- capture status (composer area) -----

    @QtCore.Slot(str)
    def set_capture_status(self, text: str) -> None:
        """Transient status line above the composer (e.g. "Transcribing…").
        Cleared by `clear_capture_status`, when a new job for the active
        session (or, with nothing selected, any new job) appears, or after
        30 s as a safety net."""
        text = (text or "").strip()
        if not text:
            self.clear_capture_status()
            return
        self._capture_status.setText(text)
        self._capture_status.setVisible(True)
        self._capture_status_timer.start()

    @QtCore.Slot()
    def clear_capture_status(self) -> None:
        self._capture_status_timer.stop()
        self._capture_status.setText("")
        self._capture_status.setVisible(False)

    @QtCore.Slot(int)
    def _on_job_added_capture_status(self, job_id: int) -> None:
        if not self._capture_status.isVisible():
            return
        job = self._job_store.get(job_id)
        if job is None:
            return
        active = self._active_session_id
        if active is None or job.local_session_id == active:
            self.clear_capture_status()

    def _show_header_menu(self) -> None:
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(_QSS)
        new_act = menu.addAction(f"New session\t{_key_hint('Ctrl+N')}")
        new_profile_act = menu.addAction(f"New session with profile…\t{_key_hint('Ctrl+Shift+N')}")
        switch_act = menu.addAction(f"Switch session…\t{_key_hint('Ctrl+K')}")
        menu.addSeparator()
        reset_act = menu.addAction("Reset layout")
        menu.addSeparator()
        account_act = menu.addAction("Switch Claude account…")
        settings_act = menu.addAction(f"Settings…\t{_key_hint('Ctrl+,')}")
        menu.addSeparator()
        quit_act = menu.addAction(f"Quit Holler\t{_key_hint('Ctrl+Q')}")
        chosen = menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))
        if chosen is new_act:
            self._new_session_default()
        elif chosen is new_profile_act:
            self._on_new_session_clicked()
        elif chosen is switch_act:
            self._open_quick_switcher()
        elif chosen is reset_act:
            self.reset_layout()
        elif chosen is account_act:
            self._show_account_menu()
        elif chosen is settings_act:
            self.settings_requested.emit()
        elif chosen is quit_act:
            self._request_quit()

    def _request_quit(self) -> None:
        """⋯ → Quit Holler / Ctrl+Q: confirm first if turns are in flight."""
        if self._confirm_quit_if_busy():
            quit_app()

    def _confirm_quit_if_busy(self) -> bool:
        """True if it's OK to quit. Quitting stops every queued/running turn,
        so ask first when there are any (default Cancel); otherwise True
        without a prompt."""
        if getattr(self, "_quit_confirmed", False):
            return True  # already confirmed for this quit (e.g. ⌘Q, then the close it triggers)
        live = [j for j in self._job_store.all() if j.status in ("queued", "running")]
        if not live:
            return True
        # Count sessions, not jobs (a running turn + its queued follow-up is
        # one session); a job without a session yet counts on its own.
        n = len({
            j.local_session_id if j.local_session_id is not None else ("job", j.id)
            for j in live
        })
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Quit Holler?")
        box.setText(
            f"{n} session{'s are' if n != 1 else ' is'} still working. "
            f"Quitting stops {'them' if n != 1 else 'it'}."
        )
        quit_btn = box.addButton("Quit", QtWidgets.QMessageBox.DestructiveRole)
        cancel_btn = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        if box.clickedButton() is quit_btn:
            self._quit_confirmed = True
            return True
        return False

    def _new_session_default(self) -> None:
        """New session, no profile — the split button's main half and ⌘N."""
        log.info("'New session' — fresh routing, no profile")
        self._start_fresh_routing("")

    def _show_new_session_menu(self) -> None:
        """The split button's arrow: start the new session with a profile
        straight from the list, or open the full picker dialog."""
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(_QSS)
        none_act = menu.addAction("No profile")
        none_act.setToolTip("Holler's standard behavior")
        try:
            specs = list_profiles()
        except Exception:
            log.exception("failed to list profiles")
            specs = []
        acts = []
        if specs:
            menu.addSeparator()
            for spec in specs:
                act = menu.addAction(spec.display)
                if spec.description:
                    act.setToolTip(spec.description)
                acts.append((act, spec.name))
        menu.addSeparator()
        picker_act = menu.addAction(f"Choose profile…\t{_key_hint('Ctrl+Shift+N')}")
        folder_act = menu.addAction("Open profiles folder…")
        menu.setToolTipsVisible(True)
        btn = self._new_btn
        chosen = menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen is none_act:
            self._start_fresh_routing("")
        elif chosen is picker_act:
            self._on_new_session_clicked()
        elif chosen is folder_act:
            _open_profiles_folder()
        else:
            for act, name in acts:
                if chosen is act:
                    self._start_fresh_routing(name)
                    return

    def _on_new_session_clicked(self) -> None:
        log.info("New session with profile — opening profile picker")
        # Open the profile picker. Cancel = abort; the existing routing stays
        # untouched (the operator can keep the previous selection).
        dlg = ProfilePickerDialog(self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        profile_name = dlg.selected_name  # "" = no profile
        self._start_fresh_routing(profile_name)

    def _start_fresh_routing(self, profile_name: str) -> None:
        """Clear current selection and announce that the next PTT begins a
        fresh session, optionally with `profile_name` layered in."""
        # Clear selection → next PTT creates a fresh session.
        # Block signals so itemSelectionChanged doesn't fire mid-clear and
        # transiently re-route to the previously selected session.
        self._list.blockSignals(True)
        try:
            self._list.clearSelection()
            self._list.setCurrentItem(None)
        finally:
            self._list.blockSignals(False)
        self._active_session_id = None
        self._pending_profile = profile_name or ""
        self._clear_detail()
        self._render_empty_dashboard()
        self._refresh_usage()
        self._update_routing_pill()
        self._update_chain_bar()
        spec = get_profile(profile_name) if profile_name else None
        profile_suffix = f" · {spec.display}" if spec else ""
        # Routing sentence (the header pill + composer placeholder show it;
        # the status strip itself stays hidden).
        self._ptt_status.setText(
            f"Ready — hold {self._hotkey_label} to start a new session{profile_suffix}"
        )
        self._ptt_status.setStyleSheet(f"color: {_ACCENT}; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        self._status_strip.setVisible(False)
        # Flash the button briefly to confirm the click was received.
        orig = self._new_btn_qss
        self._new_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.SUCCESS}; color: {_FG}; border: 1px solid {theme.SUCCESS}; "
            f"padding: {theme.SPACE_SM}px {theme.SPACE_MD}px; border-radius: {theme.RADIUS_MD}px; font-size: {theme.FONT_SM}pt; "
            f"font-weight: {theme.WEIGHT_SEMIBOLD}; {self._NEW_MAIN_CORNERS} }}"
        )
        # Context object: the restore is dropped if the window is destroyed first.
        QtCore.QTimer.singleShot(450, self._new_btn, lambda: self._new_btn.setStyleSheet(orig))
        self._update_text_input_placeholder()
        # Two signals: new_session_requested clears the previous active session
        # in app state; profile_selected stashes the profile for the next PTT.
        self.new_session_requested.emit()
        self.profile_selected.emit(profile_name)

    def _clear_detail(self) -> None:
        """Blank the detail pane (its "ready" splash) — the dashboard, if one
        follows, must be re-set even when its HTML hasn't changed."""
        self._dashboard_showing = False
        self._detail.clear()

    def _render_session_detail(self, sess: Session, auto_scroll: bool = True) -> None:
        self._dashboard_showing = False
        # Pull every committed turn + tool calls. The tool_calls fetch is a
        # single batched query (one SELECT for all turns) rather than the
        # legacy N+1 — meaningful on long sessions where it fired on every
        # _on_session_changed.
        # The in-flight Job (if any) streams into its turn; follow-ups queued
        # behind it are held back until they start (see _turns_up_to_live).
        live_job = self._find_live_job(sess.id)
        turns = _turns_up_to_live(self._store.get_turns(sess.id), live_job)
        tool_calls_by_turn = self._store.get_tool_calls_for_turns(
            [t.id for t in turns]
        )
        if not turns and live_job is None:
            self._clear_detail()
            return
        self._sync_agents_pane(sess.id)
        self._detail.set_repo_cwd(_link_root(sess, self._repo_cwd))
        self._detail.show_session(
            sess, turns, tool_calls_by_turn, live_job, self._captures_dir,
            auto_scroll=auto_scroll,
        )

    def _find_live_job(self, local_session_id: int):
        """Return the most recent Job from JobStore for this session, or None.

        We include `done`/`error` jobs as long as they're still in memory because
        their `blocks` list preserves the chronological interleaving of text +
        tool calls — exactly how Claude Code displays them. Only when the Job
        falls out of JobStore (e.g. after app restart) do we fall back to the
        less-accurate DB-reconstructed committed view.
        """
        return _pick_live_job(self._job_store.all(), local_session_id)

    @QtCore.Slot(int)
    def _on_session_changed(self, local_session_id: int) -> None:
        # Title/status may have changed — refresh the list. Not under an
        # inline rename (the rebuild would replace the row being edited):
        # that refresh runs when the rename finishes.
        if self._rename_editor is not None:
            self._refresh_after_rename = True
        else:
            self._refresh_list()
        self._schedule_dashboard_refresh()
        # If the changed session is the one currently selected, re-render —
        # but preserve scroll position. Metadata churn (title/status) and
        # sibling-session activity should never yank the operator's reading
        # position. The _set_browser_html at-bottom heuristic still tails new
        # content when they were at the tail.
        if self._active_session_id == local_session_id:
            sess = self._store.get_session(local_session_id)
            if sess is not None:
                self._render_session_detail(sess, auto_scroll=False)

    @QtCore.Slot(int)
    def _on_job_updated(self, job_id: int) -> None:
        job = self._job_store.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            # Finished (or evicted) job — drop its stuck-detection entry so
            # the dict doesn't grow for the life of the process.
            self._tool_first_seen.pop(job_id, None)
        if self._active_session_id is None:
            return
        if job is None:
            return
        if job.local_session_id != self._active_session_id:
            return
        # Only the session's live job (the running one; a queued follow-up
        # never displaces it) streams into the view. Another job's update
        # matters only when it finishes: re-render so its turn commits and
        # the next job takes over as live.
        live = self._find_live_job(self._active_session_id)
        if live is not None and live.id != job.id:
            if job.status not in ("queued", "running"):
                sess = self._store.get_session(self._active_session_id)
                if sess is not None:
                    self._render_session_detail(sess, auto_scroll=False)
            return
        # If the detail view is in session mode (has prior_turns), update via
        # the throttled streaming path; else fall back to single-job render.
        if getattr(self._detail, "_prior_turns", None):
            # If the job represents a NEW turn we don't yet have in prior_turns,
            # do a full re-render from DB so the new turn card appears.
            # auto_scroll=False — the at-bottom heuristic in _set_browser_html
            # auto-tails new content when the operator was already at the tail,
            # and preserves their scroll position when they were reading higher.
            # Same for a turn just smart-batched into the job (merged_in).
            known = {t.id for t in self._detail._prior_turns}  # type: ignore[attr-defined]
            if any(tid not in known for tid in _live_group_ids(job)):
                sess = self._store.get_session(self._active_session_id)
                if sess is not None:
                    self._render_session_detail(sess, auto_scroll=False)
                    return
            # Otherwise just update the streaming for the live turn.
            self._detail._job = job  # type: ignore[attr-defined]
            self._detail.update_streaming(job)
        else:
            self._detail.show_job(job, self._captures_dir)

    # ----- context menu -----

    def _show_context_menu(self, pos: QtCore.QPoint) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        sess: Session = item.data(QtCore.Qt.UserRole)
        if sess is None:
            return
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(_QSS)
        review_act = menu.addAction(
            "Reopen\tSpace" if sess.reviewed_at is not None else "Mark Done\tSpace"
        )
        pin_hint = _key_hint("Ctrl+P")
        pin_act = menu.addAction(f"Unpin\t{pin_hint}" if sess.pinned else f"Pin\t{pin_hint}")
        rename_act = menu.addAction("Rename…\tF2")
        regen_title_act = menu.addAction("Regenerate title")
        regen_title_act.setEnabled(self._auto_titler is not None)
        menu.addSeparator()
        popout_act = menu.addAction(f"Open in separate window\t{_key_hint('Ctrl+Shift+D')}")
        archive_act = menu.addAction("Unarchive" if sess.archived else "Archive")
        delete_act = menu.addAction("Delete…")
        menu.addSeparator()
        copy_id_act = menu.addAction("Copy Claude session ID")
        copy_id_act.setEnabled(bool(sess.sdk_session_id))
        open_cc_act = menu.addAction("Open in Claude Code")
        open_cc_act.setEnabled(bool(sess.sdk_session_id))

        chosen = menu.exec(self._list.mapToGlobal(pos))
        if chosen is review_act:
            self._on_review_toggled(sess.id)
        elif chosen is pin_act:
            self._store.pin(sess.id, not sess.pinned)
            self._refresh_list()
        elif chosen is rename_act:
            # Re-resolve: a background refresh while the menu was open
            # rebuilt the rows and deleted `item`.
            fresh = self._item_for_session(sess.id)
            if fresh is not None:
                self._rename_item(fresh)
        elif chosen is regen_title_act and self._auto_titler is not None:
            self._regenerate_title(sess)
        elif chosen is popout_act:
            self._popout_session(sess)
        elif chosen is archive_act:
            if sess.archived:
                self._store.archive(sess.id, False)
                self.show_inline_notice(f"Unarchived “{_truncate(sess.title or 'Untitled session', 40)}”",
                                        kind="info")
                self._refresh_list()
            else:
                # Same path as the row × — running guard, active-session
                # cleanup, undo toast.
                self._on_archive_requested(sess.id)
        elif chosen is delete_act:
            self._delete_session(sess)
        elif chosen is copy_id_act and sess.sdk_session_id:
            QtWidgets.QApplication.clipboard().setText(sess.sdk_session_id)
        elif chosen is open_cc_act and sess.sdk_session_id:
            self._open_in_claude_code(sess.sdk_session_id)

    def _delete_session(self, sess: Session) -> None:
        """Context-menu Delete… — permanent. Refused while a turn is running
        (same guard as archive): the runner would keep writing turns/events
        for a session row that no longer exists."""
        if self._session_has_live_job(sess.id):
            self.show_inline_notice(
                f"Can't delete “{_truncate(sess.title or 'Untitled session', 40)}” while it's working. "
                f"Stop the turn first.",
                kind="error",
            )
            return
        confirm = QtWidgets.QMessageBox.question(
            self, "Delete session?",
            f"“{sess.title or 'Untitled session'}” will be permanently deleted from Holler. "
            f"This can't be undone. (Claude Code's own copy is kept.)",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,  # Return must not confirm
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        self._store.delete(sess.id)
        if self._active_session_id == sess.id:
            self._clear_active_session()
        self._refresh_list()

    # ----- splitter / search / sort helpers -----

    def _on_splitter_moved(self, *_args) -> None:
        sizes = self._splitter.sizes()
        if sizes and all(s >= 0 for s in sizes):
            # User has manually adjusted the split — switch to absolute-size
            # mode permanently. We persist a flag so future launches respect
            # the user's choice instead of resetting to the 30/70 fraction.
            self._splitter_user_set = True
            _state.merged_save_debounced({
                "sessions_splitter": list(sizes),
                "sessions_splitter_user_set": True,
            })

    def _reapply_splitter_fraction(self) -> None:
        """Re-apply the 30/70 fractional split against current window width.

        No-op if the user has manually adjusted the splitter (their absolute
        sizes win). Called from resizeEvent and on monitor changes.
        """
        if getattr(self, "_splitter_user_set", False):
            return
        sp = getattr(self, "_splitter", None)
        if sp is None:
            return
        total = sp.width()
        if total <= 0:
            return
        frac = getattr(self, "_splitter_fraction", 0.30)
        rail_w = max(160, int(total * frac))
        right_w = max(1, total - rail_w)
        cur = sp.sizes()
        # Avoid an infinite-tick loop: only setSizes if it would actually
        # change (within 2px tolerance).
        if not cur or abs(cur[0] - rail_w) > 2 or abs(cur[1] - right_w) > 2:
            sp.setSizes([rail_w, right_w])

    def _refresh_sticky_bucket(self, *_args) -> None:
        """Update the sticky-bucket strip to match whichever bucket header
        is the most recent one above the topmost visible row. Hidden when not
        in bucketed mode (search results / non-recency filters)."""
        if not hasattr(self, "_sticky_bucket"):
            return
        if self._list.count() == 0 or self._rename_editor is not None:
            self._sticky_bucket.setVisible(False)
            return
        # Find topmost visible row in viewport coords.
        viewport = self._list.viewport()
        if viewport is None:
            self._sticky_bucket.setVisible(False)
            return
        top_idx = self._list.indexAt(QtCore.QPoint(2, 2))
        if not top_idx.isValid():
            self._sticky_bucket.setVisible(False)
            return
        top_row = top_idx.row()
        bucket = None
        # Walk upward until we hit a header row, capturing its text.
        for r in range(top_row, -1, -1):
            item = self._list.item(r)
            if item is not None and _is_header_item(item):
                w = self._list.itemWidget(item)
                if isinstance(w, QtWidgets.QLabel):
                    bucket = w.text().strip()
                break
        if not bucket:
            self._sticky_bucket.setVisible(False)
            return
        # Don't double-show the bucket name if the actual header is already
        # visible at the top.
        first_visible_item = self._list.item(top_row)
        if first_visible_item is not None and _is_header_item(first_visible_item):
            self._sticky_bucket.setVisible(False)
            return
        self._sticky_bucket.setText(bucket)
        self._place_sticky_bucket()
        self._sticky_bucket.setVisible(True)
        self._sticky_bucket.raise_()

    def _keep_row_clear_of_sticky(self, item: Optional[QtWidgets.QListWidgetItem]) -> None:
        """Scroll so `item` isn't (partly) under the sticky-bucket label —
        scrollTo / arrow-up park the row at the very top of the viewport,
        exactly where the overlay sits."""
        if item is None or not hasattr(self, "_sticky_bucket"):
            return
        sb = self._list.verticalScrollBar()
        per_pixel = self._list.verticalScrollMode() == QtWidgets.QAbstractItemView.ScrollPerPixel
        for _ in range(8):  # per-item scrolling moves a row at a time
            self._refresh_sticky_bucket()
            lbl = self._sticky_bucket
            if lbl.isHidden():
                return
            rect = self._list.visualItemRect(item)
            cover = lbl.geometry().bottom() + 1 - self._list.viewport().geometry().y()
            if rect.top() >= cover or rect.bottom() < 0 or sb.value() <= sb.minimum():
                return
            sb.setValue(sb.value() - (cover - rect.top() if per_pixel else 1))

    def _place_sticky_bucket(self) -> None:
        """Pin the sticky bucket overlay across the top of the list viewport."""
        lbl = getattr(self, "_sticky_bucket", None)
        if lbl is None:
            return
        vp = self._list.viewport().geometry()
        lbl.setGeometry(vp.x(), vp.y(), vp.width(), lbl.sizeHint().height())

    def _focus_search(self) -> None:
        self._search.setFocus(QtCore.Qt.ShortcutFocusReason)
        self._search.selectAll()

    def _copy_selection(self) -> None:
        """Copy the current selection to the clipboard for Cmd/Ctrl+C.

        Priority: the focused editor (input/search) if it holds a selection,
        then the conversation browser, then the header transcript. Mouse-drag
        selection populates a widget's cursor even while it is NoFocus, so we
        read the selection directly rather than relying on keyboard focus."""
        clipboard = QtWidgets.QApplication.clipboard()

        focused = QtWidgets.QApplication.focusWidget()
        if isinstance(focused, (QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit)):
            cursor = focused.textCursor()
            if cursor.hasSelection():
                clipboard.setText(cursor.selection().toPlainText())
                return
        elif isinstance(focused, QtWidgets.QLineEdit) and focused.hasSelectedText():
            clipboard.setText(focused.selectedText())
            return

        browser = self._detail._reply_browser
        cursor = browser.textCursor()
        if cursor.hasSelection():
            text = cursor.selection().toPlainText()
            clipboard.setText(text)
            self.show_inline_notice(f"Copied {len(text):,} character{'s' if len(text) != 1 else ''}",
                                    kind="info")
            return

        label = self._detail._transcript
        if label.hasSelectedText():
            text = label.selectedText()
            clipboard.setText(text)
            self.show_inline_notice(f"Copied {len(text):,} character{'s' if len(text) != 1 else ''}",
                                    kind="info")

    def _clear_search(self) -> None:
        if self._search.text():
            self._search.clear()
            self._search_timer.start()
        else:
            # Empty already → bounce focus back to the list so arrow keys work.
            self._list.setFocus(QtCore.Qt.ShortcutFocusReason)

    def _apply_sort(self, sessions: list) -> list:
        """Sort sessions by the current sort key. Pinned always wins."""
        key = self._sort_key
        if key == "tokens":
            totals = self._store.session_token_totals([s.id for s in sessions])
            return sorted(
                sessions,
                key=lambda s: (
                    not s.pinned,
                    -(totals.get(s.id, {}).get("ctx_in", 0) + totals.get(s.id, {}).get("output", 0)),
                ),
            )
        if key == "cost":
            totals = self._store.session_token_totals([s.id for s in sessions])
            return sorted(
                sessions,
                key=lambda s: (not s.pinned, -totals.get(s.id, {}).get("cost", 0.0)),
            )
        if key == "title":
            return sorted(
                sessions,
                key=lambda s: (not s.pinned, (s.title or "").lower()),
            )
        # Default: recent (already SQL-ordered; nothing to do).
        return sessions

    def _sort_label(self) -> str:
        return {
            "recent": "Sort: Recent",
            "tokens": "Sort: Tokens",
            "cost": "Sort: Cost",
            "title": "Sort: A–Z",
        }.get(self._sort_key, "Sort: Recent")

    def _sync_sort_button(self) -> None:
        """The sort button is a quiet icon at the default (most recent) order
        — leaving the filter chips the room — and names any other order next
        to the icon so a non-default sort is never invisible."""
        label = self._sort_label()
        short = "" if self._sort_key == "recent" else label.split(": ", 1)[-1]
        self._sort_btn.setText(short)
        self._sort_btn.setAccessibleName(label)

    def _show_sort_menu(self) -> None:
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(_QSS)
        options = [
            ("recent", "Most recent"),
            ("tokens", "Most tokens"),
            ("cost", "Highest cost"),
            ("title", "Title A–Z"),
        ]
        acts = []
        group = QtGui.QActionGroup(menu)
        for key, label in options:
            # Checkable (not a "•" prefix) so the current sort is announced.
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(key == self._sort_key)
            group.addAction(act)
            acts.append((act, key))
        chosen = menu.exec(self._sort_btn.mapToGlobal(self._sort_btn.rect().bottomLeft()))
        for act, key in acts:
            if chosen is act:
                self._sort_key = key
                _state.merged_save_debounced({"sessions_sort": key})
                self._sync_sort_button()
                self._refresh_list()
                return

    # ----- quick switcher (Ctrl+K) -----

    def _open_quick_switcher(self) -> None:
        dlg = QuickSwitcherDialog(self._store, self._recents, parent=self,
                                  actions=self._palette_actions())
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        if dlg.selected_action is not None:
            dlg.selected_action.callback()
        elif dlg.selected_id is not None:
            self.select_session(dlg.selected_id)

    def _palette_actions(self) -> list:
        """What the ⌘K palette can do besides switching session."""
        acts = [
            PaletteAction("New session", self._new_session_default,
                          hint=_key_hint("Ctrl+N"), icon="plus", keywords="start create"),
            PaletteAction("New session with profile…", self._on_new_session_clicked,
                          hint=_key_hint("Ctrl+Shift+N"), icon="plus", keywords="start create profile mode"),
            PaletteAction("Settings…", self.settings_requested.emit,
                          hint=_key_hint("Ctrl+,"), icon="settings", keywords="preferences options"),
        ]
        for i, (key, label) in enumerate((("active", "Active"), ("done", "Done"), ("running", "Running"),
                                          ("pinned", "Pinned"), ("archived", "Archived")), start=1):
            acts.append(PaletteAction(
                f"Show {label} sessions", lambda k=key: self._set_filter(k),
                hint=_key_hint(f"Ctrl+{i}"), icon="eye", keywords="filter view"))
        sess = self._store.get_session(self._active_session_id) if self._active_session_id is not None else None
        if sess is not None:
            sid = sess.id

            def _pin(sid=sid, pinned=sess.pinned) -> None:
                self._store.pin(sid, not pinned)
                self._refresh_list()

            def _archive(sid=sid, archived=sess.archived) -> None:
                if archived:
                    self._store.archive(sid, False)
                    self._refresh_list()
                else:
                    self._on_archive_requested(sid)

            acts += [
                PaletteAction("Unpin this session" if sess.pinned else "Pin this session", _pin,
                              hint=_key_hint("Ctrl+P"), icon="pin", keywords="current"),
                PaletteAction("Reopen this session" if sess.reviewed_at is not None else "Mark this session Done",
                              lambda sid=sid: self._on_review_toggled(sid),
                              icon="check", keywords="current finished read"),
                PaletteAction("Unarchive this session" if sess.archived else "Archive this session",
                              _archive, icon="archive", keywords="current"),
                PaletteAction("Rename this session…", lambda sid=sid: self._rename_session(sid),
                              hint="F2", icon="edit", keywords="current title"),
                PaletteAction("Open this session in a separate window", self._popout_current_session,
                              hint=_key_hint("Ctrl+Shift+D"), icon="external-link", keywords="current pop out"),
            ]
        acts.append(PaletteAction("Reset layout", self.reset_layout, icon="refresh",
                                  keywords="window size splitter"))
        return acts

    # ----- pop-out detail (Ctrl+Shift+D) -----

    def _popout_current_session(self) -> None:
        if self._active_session_id is None:
            self.show_inline_notice("Select a session to open it in a separate window", kind="info")
            return
        sess = self._store.get_session(self._active_session_id)
        if sess is not None:
            self._popout_session(sess)

    def _popout_session(self, sess: Session) -> None:
        """Open a detached read-only viewer for `sess`. Useful for side-by-side
        comparison without losing the current rail selection."""
        viewer = SessionPopoutWindow(
            sess,
            store=self._store,
            job_store=self._job_store,
            captures_dir=self._captures_dir,
            parent=self,
        )
        self._detached_windows.append(viewer)
        viewer.destroyed.connect(lambda _=None, v=viewer: self._detached_windows.remove(v) if v in self._detached_windows else None)
        viewer.show()
        viewer.raise_()

    def _open_in_claude_code(self, sdk_session_id: str) -> None:
        try:
            if sys.platform == "darwin":
                # `claude` is an interactive TUI — it needs a real terminal, so
                # hand the command to Terminal.app via osascript rather than
                # spawning it headless.
                import shlex
                shell_cmd = (
                    f"cd {shlex.quote(str(self._repo_cwd))} && "
                    f"claude --resume {shlex.quote(sdk_session_id)}"
                )
                osa = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
                subprocess.Popen([
                    "osascript",
                    "-e", f'tell application "Terminal" to do script "{osa}"',
                    "-e", 'tell application "Terminal" to activate',
                ])
                return
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
            subprocess.Popen(
                ["claude", "--resume", sdk_session_id],
                cwd=str(self._repo_cwd),
                **kwargs,
            )
        except Exception:
            log.exception("failed to open in Claude Code")

    # ----- close handling -----

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        # Sessions window IS the main UI now — closing it quits holler. A
        # user close from the window system (title-bar ×, Alt+F4) confirms
        # first while turns are in flight. Non-spontaneous closes are app
        # quits already in progress (⋯ → Quit confirmed above, panel Quit,
        # self-relaunch — which has already spawned its replacement, so it
        # must never be cancelled here).
        if ev.spontaneous():
            if not self._confirm_quit_if_busy():
                ev.ignore()
                return
            quit_app()
        # A non-spontaneous close is Qt closing windows for a quit already in
        # progress. Quitting again from inside it makes Cocoa exit() on the
        # spot — skipping aboutToQuit, state flush and session.stop()'s SDK
        # subprocess reaping (orphaned `claude` workers).
        self._remove_app_filters()
        super().closeEvent(ev)

    def _remove_app_filters(self) -> None:
        """A closed window no longer answers app-wide ⌘C / ⌘Z. (The filters
        are children of this window and leave the app's filter list when it is
        destroyed; this covers a closed-but-not-yet-deleted window.)"""
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        for f in (getattr(self, "_copy_filter", None), getattr(self, "_undo_filter", None)):
            if f is not None:
                try:
                    app.removeEventFilter(f)
                except RuntimeError:
                    pass


def _safe_json_loads(s: str):
    import json
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# Push-to-talk key names: pynput-style spec word → (macOS glyph, macOS name).
_MAC_KEY_NAMES = {
    "alt": ("⌥", "Option"), "option": ("⌥", "Option"), "opt": ("⌥", "Option"),
    "alt_gr": ("⌥", "Option"),
    "cmd": ("⌘", "Command"), "command": ("⌘", "Command"), "super": ("⌘", "Command"),
    "ctrl": ("⌃", "Control"), "control": ("⌃", "Control"),
    "shift": ("⇧", "Shift"),
}


def _format_hotkey(raw: str) -> str:
    """Push-to-talk key spec → the name shown in the UI.

    Elsewhere: `right alt` → `Right Alt`; `ctrl+shift+space` →
    `Ctrl+Shift+Space`. On macOS the modifier keys use their Mac names —
    `right alt` → `Right ⌥ Option`, `cmd` → `⌘ Command` — and a chord is
    written the Mac way, `ctrl+shift+space` → `⌃⇧Space`."""
    if not raw:
        return "your push-to-talk key"
    parts = [p.strip() for p in raw.replace("_", " ").split("+") if p.strip()]
    if not parts:
        return "your push-to-talk key"
    if sys.platform == "darwin":
        def _mac(part: str, chord: bool) -> str:
            words = part.lower().split()
            key = words[-1] if words else ""
            if key == "gr" and len(words) > 1 and words[-2] == "alt":
                words, key = words[:-1], "alt"
            if key in _MAC_KEY_NAMES:
                glyph, name = _MAC_KEY_NAMES[key]
                side = " ".join(w.capitalize() for w in words[:-1])
                if chord:
                    return glyph
                return f"{side} {glyph} {name}".strip()
            return " ".join(w.capitalize() for w in words)

        if len(parts) == 1:
            return _mac(parts[0], chord=False)
        return "".join(_mac(p, chord=True) for p in parts)
    out = []
    for p in parts:
        words = [w.capitalize() for w in p.split()]
        out.append(" ".join(words))
    return "+".join(out)


def _return_keys() -> tuple[str, str]:
    """(send, new line) key names for the composer hint: ↩ / ⇧↩ on macOS,
    Enter / Shift+Enter elsewhere."""
    if sys.platform == "darwin":
        return "↩", "⇧↩"
    return "Enter", "Shift+Enter"


# Model-facing marker the runner appends to a turn's transcript when the
# screenshot didn't change (holler.runners.full). It's meant for Claude, not
# for the operator's own message — strip it from row snippets. The transcript
# view gets the raw turns: it strips the marker itself and shows "Same screen".
_SCREEN_UNCHANGED_SUFFIX = " (screen unchanged since last turn)"


def _display_transcript(text: str) -> str:
    """`text` without the model-only "(screen unchanged…)" marker."""
    if text and text.endswith(_SCREEN_UNCHANGED_SUFFIX):
        return text[: -len(_SCREEN_UNCHANGED_SUFFIX)]
    return text


def _pick_live_job(jobs, session_id: int):
    """The job a session's view streams: its running job, else its newest
    queued one, else (finished jobs still in memory keep their interleaved
    blocks) the newest one. A queued follow-up must never displace the
    running turn — that rendered turns out of order and pointed Stop at the
    wrong job."""
    best = None
    latest = None
    for j in jobs:
        if j.local_session_id != session_id:
            continue
        if latest is None or j.id > latest.id:
            latest = j
        if j.status in ("queued", "running"):
            rank = (j.status == "running", j.id)
            if best is None or rank > (best.status == "running", best.id):
                best = j
    return best if best is not None else latest


def _turns_up_to_live(turns: list, live_job) -> list:
    """While a job is in flight, drop the turns after its turn: those belong
    to follow-ups still queued behind it (no content yet). Rendering them
    would put them above the live turn, which the view appends last. Turns
    smart-batched into the job (``merged_turns``) stay: the view renders them
    with the live turn, which answers them too."""
    if live_job is None or live_job.status not in ("queued", "running"):
        return turns
    tid = getattr(live_job, "local_turn_id", None)
    merged = {mt.get("local_turn_id") for mt in getattr(live_job, "merged_turns", None) or ()
              if isinstance(mt, dict)}
    for i, t in enumerate(turns):
        if t.id == tid:
            return turns[: i + 1] + [m for m in turns[i + 1:] if m.id in merged]
    return turns


def _fmt_k(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    if n < 1_000_000_000:
        return f"{n/1_000_000:.2f}M"
    return f"{n/1_000_000_000:.2f}B"


def _relative_time(ts: float) -> str:
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _fmt_elapsed(seconds: float) -> str:
    """Compact elapsed time for the live ticker: `12s`, `1m 30s`, `5m`."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        if s:
            return f"{m}m {s}s"
        return f"{m}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def _age_bucket(last_activity_at: float, now: Optional[float] = None) -> str:
    """Map a session's last_activity_at to one of the recency bucket labels."""
    if now is None:
        now = time.time()
    delta = max(0.0, now - last_activity_at)
    for label, bound in _BUCKETS:
        if delta < bound:
            return label
    return _BUCKETS[-1][0]


def _is_stale(session: Session, now: Optional[float] = None) -> bool:
    """A session is "stale" once it's been idle longer than 7 days."""
    if session.pinned:
        return False
    if now is None:
        now = time.time()
    return (now - session.last_activity_at) >= 7 * 86400


# Spoken / tooltip names of a rail row's status glyph (SessionListItem.state).
_STATE_NAMES = {
    "running": "Working",
    "error": "Last turn failed",
    "done": "Done",
    "unread": "Not Done yet",
    "idle": "Idle",
}


def _dot_tooltip(session: Session, now: Optional[float] = None, *, running: bool = False) -> str:
    """Tooltip for a rail row's status glyph."""
    ago = _relative_time(session.last_activity_at)
    if running or session.status == "running":
        return "Working"
    if session.reviewed_at is not None:
        return f"Done · {ago}"
    if session.status == "error":
        return f"Last turn failed · {ago}"
    if _is_stale(session, now=now):
        return f"Idle · {ago}"
    return f"Not Done yet — mark it Done when you've read the reply · {ago}"


def _title_color_for(session: Session, now: Optional[float] = None) -> str:
    """Pinned rows always full-bright; stale rows fade to dim."""
    if session.pinned:
        return _FG
    if _is_stale(session, now=now):
        return _FG_DIM
    return _FG


def _is_header_item(item: Optional[QtWidgets.QListWidgetItem]) -> bool:
    if item is None:
        return False
    return bool(item.data(QtCore.Qt.UserRole + 1))


class _HighlightLabel(ElidedLabel):
    """One-line ElidedLabel that colours the parts of the (elided) text
    matching the palette query. Plain text when there's nothing to mark."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent, max_lines=1)
        self._terms: list[str] = []

    def set_terms(self, terms: list[str]) -> None:
        self._terms = [t.lower() for t in terms if t]
        self._apply_elide()

    def _show_text(self, shown: str) -> None:
        spans = _match_spans(shown, self._terms)
        if not spans:
            self.setTextFormat(QtCore.Qt.PlainText)
            super()._show_text(shown)
            return
        out, pos = [], 0
        for a, b in spans:
            out.append(html_escape(shown[pos:a]))
            out.append(f"<span style='color:{_ACCENT};'>{html_escape(shown[a:b])}</span>")
            pos = b
        out.append(html_escape(shown[pos:]))
        self.setTextFormat(QtCore.Qt.RichText)
        super()._show_text("".join(out))


def _match_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    """Merged, sorted (start, end) ranges where any of `terms` (lower-case)
    occurs in `text`, case-insensitively."""
    low = text.lower()
    hits: list[tuple[int, int]] = []
    for t in terms:
        start = 0
        while t:
            i = low.find(t, start)
            if i < 0:
                break
            hits.append((i, i + len(t)))
            start = i + len(t)
    hits.sort()
    merged: list[tuple[int, int]] = []
    for a, b in hits:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


class PaletteAction:
    """A command offered by the ⌘K palette (besides switching session)."""

    def __init__(self, label: str, callback: Callable[[], None], *, hint: str = "",
                 icon: str = "chevron-right", keywords: str = "") -> None:
        self.label = label
        self.callback = callback
        self.hint = hint
        self.icon = icon
        self.keywords = keywords

    def matches(self, terms: list[str]) -> bool:
        """Every query term starts a word of the label or keywords ("re"
        finds "Rename" / "Reset", not "New session" via "create")."""
        words = re.findall(r"\w+", f"{self.label} {self.keywords}".lower())
        return all(any(w.startswith(t) for w in words) for t in terms)


class QuickSwitcherDialog(QtWidgets.QDialog):
    """⌘K command palette: switch session, or run an action.

    A frameless card centred over the Sessions window. Type to filter across
    title + transcript + last reply (FTS-backed when available) — matches
    are highlighted in the titles — and the window's actions (New session,
    Settings, filters, Pin / Archive / Mark Done for the current session).
    ↑ / ↓ move from the search field, ⏎ opens, Esc closes. With no query,
    recent sessions come first so "jump back to what I was on" is one ⏎.

    After `exec()` returns Accepted, either `selected_id` (a session) or
    `selected_action` (a PaletteAction) is set.
    """

    # Item padding. The delegate places each row's label widget inside the
    # padded rect, so the row's size hint must add it back or the two-line
    # title + subtitle get squeezed and clip.
    _ITEM_PAD_V = theme.SPACE_SM
    _ITEM_PAD_H = theme.SPACE_MD
    _HEADER = QtCore.Qt.UserRole + 1
    _ACTION = QtCore.Qt.UserRole + 2

    def __init__(self, store, recents: list, parent: Optional[QtWidgets.QWidget] = None,
                 actions: Optional[list] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._recents = list(recents or [])
        self._actions: list[PaletteAction] = list(actions or [])
        self.selected_id: Optional[int] = None
        self.selected_action: Optional[PaletteAction] = None
        self._was_active = False
        self._closing = False
        self.setWindowTitle("Switch session")
        self.setWindowFlags(QtCore.Qt.Dialog | QtCore.Qt.FramelessWindowHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setStyleSheet(_QSS + f"""
            QDialog {{ background: transparent; }}
            QFrame#paletteCard {{ background: {_BG}; border: 1px solid {theme.BORDER_STRONG};
                                  border-radius: {theme.RADIUS_MD + theme.SPACE_2XS}px; }}
            QFrame#paletteCard QLabel {{ background: transparent; }}
            QFrame#paletteRule {{ background: {theme.BORDER}; border: none; }}
            QLineEdit#paletteInput {{ background: transparent; color: {_FG}; border: none;
                          padding: {theme.SPACE_XS}px 0; font-size: {theme.FONT_MD}pt; }}
            QListWidget {{ background: transparent; border: none; }}
            QListWidget::item {{ padding: {self._ITEM_PAD_V}px {self._ITEM_PAD_H}px; border-radius: {theme.RADIUS_SM}px; }}
            QListWidget::item:selected {{ background: {_BG_ROW_SELECTED}; color: {_FG}; }}
            QListWidget::item:hover:!selected {{ background: {_BG_ROW}; }}
        """)
        self.setModal(True)
        self.setMinimumSize(640, 480)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QtWidgets.QFrame()
        card.setObjectName("paletteCard")
        outer.addWidget(card)
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM)
        layout.setSpacing(theme.SPACE_SM)

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(theme.SPACE_SM, 0, theme.SPACE_SM, 0)
        head.setSpacing(theme.SPACE_SM)
        glass = QtWidgets.QLabel()
        glass.setPixmap(iconset.pixmap("search", _FG_DIM, theme.ICON_GLYPH, self.devicePixelRatioF()))
        head.addWidget(glass, 0, QtCore.Qt.AlignVCenter)
        self._input = QtWidgets.QLineEdit()
        self._input.setObjectName("paletteInput")
        send, _newline = _return_keys()
        self._input.setPlaceholderText(f"Search sessions or actions — {send} to open")
        self._input.setAccessibleName("Search sessions and actions")
        head.addWidget(self._input, 1)
        layout.addLayout(head)
        rule = QtWidgets.QFrame()
        rule.setObjectName("paletteRule")
        rule.setFixedHeight(1)
        layout.addWidget(rule)

        self._results = QtWidgets.QListWidget()
        self._results.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._results.setAccessibleName("Results")
        # Keyboard stays in the search field (↑ / ↓ / ⏎ are forwarded); the
        # list is still clickable.
        self._results.setFocusPolicy(QtCore.Qt.NoFocus)
        # Rows elide long titles to the list width; never scroll sideways.
        self._results.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        layout.addWidget(self._results, 1)

        self._empty = QtWidgets.QLabel("")
        self._empty.setAlignment(QtCore.Qt.AlignCenter)
        self._empty.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_SM}pt;")
        self._empty.setVisible(False)
        layout.addWidget(self._empty, 1)

        hint = QtWidgets.QLabel(f"↑ ↓ to move · {send} to open · Esc to close")
        hint.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; padding: 0 {theme.SPACE_SM}px;")
        layout.addWidget(hint)

        self._input.textChanged.connect(self._refresh)
        self._input.returnPressed.connect(self._accept_current)
        self._input.installEventFilter(self)
        self._results.itemClicked.connect(self._accept_item)
        self._results.itemActivated.connect(lambda _it: self._accept_current())

        self._refresh("")

    # ----- placement -----

    def showEvent(self, ev: QtGui.QShowEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(ev)
        self._place()
        self._input.setFocus(QtCore.Qt.OtherFocusReason)

    def _place(self) -> None:
        """Centred horizontally over the parent window, in its upper third
        (or over the cursor's screen without one)."""
        parent = self.parentWidget()
        if parent is not None and parent.isVisible():
            area = parent.frameGeometry()
        else:
            area = _screen_for_cursor().availableGeometry()
        x = area.x() + (area.width() - self.width()) // 2
        y = area.y() + max(theme.SPACE_XL, (area.height() - self.height()) // 5)
        self.move(x, y)

    def changeEvent(self, ev: QtCore.QEvent) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(ev)
        # A palette closes when you click elsewhere (it has no title bar).
        # Not while it's already closing: accept() hides the window, which
        # deactivates it — rejecting then would undo the choice.
        if ev.type() == QtCore.QEvent.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active and self.isVisible() and not self._closing:
                self.reject()

    def done(self, r: int) -> None:  # noqa: D401 (Qt override)
        self._closing = True
        super().done(r)

    # ----- keyboard -----

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        if watched is self._input and event.type() == QtCore.QEvent.KeyPress:
            key = event.key()
            if key in (QtCore.Qt.Key_Down, QtCore.Qt.Key_Up):
                self._move(1 if key == QtCore.Qt.Key_Down else -1)
                return True
            if key in (QtCore.Qt.Key_PageDown, QtCore.Qt.Key_PageUp):
                self._move(8 if key == QtCore.Qt.Key_PageDown else -8)
                return True
        return super().eventFilter(watched, event)

    def _selectable_rows(self) -> list[int]:
        return [i for i in range(self._results.count())
                if not self._results.item(i).data(self._HEADER)]

    def _move(self, delta: int) -> None:
        rows = self._selectable_rows()
        if not rows:
            return
        cur = self._results.currentRow()
        if cur in rows:
            idx = max(0, min(len(rows) - 1, rows.index(cur) + delta))
        else:
            idx = 0 if delta > 0 else len(rows) - 1
        self._results.setCurrentRow(rows[idx])
        self._results.scrollToItem(self._results.item(rows[idx]))

    def _focus_results(self) -> None:
        """Back-compat: move to the first result."""
        rows = self._selectable_rows()
        if rows:
            self._results.setCurrentRow(rows[0])

    # ----- rows -----

    def _add_header(self, label: str) -> None:
        item = QtWidgets.QListWidgetItem()
        item.setFlags(QtCore.Qt.NoItemFlags)
        item.setData(self._HEADER, True)
        item.setData(QtCore.Qt.AccessibleTextRole, label)
        lbl = QtWidgets.QLabel(label.upper())
        lbl.setStyleSheet(
            f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; font-weight: {theme.WEIGHT_BOLD}; "
            f"letter-spacing: {theme.TRACKING_CAPS_SM}; background: transparent;")
        lbl.ensurePolished()
        # The QSS item padding applies to header rows too: add it back.
        item.setSizeHint(QtCore.QSize(0, lbl.sizeHint().height() + 2 * self._ITEM_PAD_V))
        self._results.addItem(item)
        self._results.setItemWidget(item, lbl)

    def _add_action_row(self, act: PaletteAction, terms: list[str]) -> None:
        item = QtWidgets.QListWidgetItem()
        item.setData(self._ACTION, act)
        item.setData(QtCore.Qt.AccessibleTextRole, act.label)
        w = QtWidgets.QWidget()
        w.setStyleSheet("background: transparent;")
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(theme.SPACE_SM)
        ic = QtWidgets.QLabel()
        ic.setPixmap(iconset.pixmap(act.icon, _FG_DIM, theme.ROW_ICON, self.devicePixelRatioF()))
        h.addWidget(ic, 0, QtCore.Qt.AlignVCenter)
        t = _HighlightLabel()
        t.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_SM}pt;")
        t.set_terms(terms)
        t.setFullText(act.label)
        h.addWidget(t, 1, QtCore.Qt.AlignVCenter)
        if act.hint:
            k = QtWidgets.QLabel(act.hint)
            k.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt;")
            h.addWidget(k, 0, QtCore.Qt.AlignVCenter)
        w.ensurePolished()
        t.ensurePolished()
        item.setSizeHint(QtCore.QSize(0, w.sizeHint().height() + 2 * self._ITEM_PAD_V))
        self._results.addItem(item)
        self._results.setItemWidget(item, w)

    def _add_session_row(self, s: Session, terms: list[str]) -> None:
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.UserRole, s.id)
        title = (s.title or "Untitled session").strip()
        sub_bits = [_relative_time(s.last_activity_at)]
        if getattr(s, "profile", None):
            spec = get_profile(s.profile)
            sub_bits.append(spec.display if spec is not None else s.profile)
        if s.reviewed_at is not None:
            sub_bits.append("Done")
        sub = " · ".join(sub_bits)
        item.setData(QtCore.Qt.AccessibleTextRole, f"{title}, {sub}")
        label_widget = QtWidgets.QWidget()
        # Let the item's selection fill show through behind the text
        # (a bare declaration cascades to the labels too).
        label_widget.setStyleSheet("background: transparent;")
        v = QtWidgets.QVBoxLayout(label_widget)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(theme.SPACE_2XS)
        # Single-line, elided to the row width (full title in the tooltip)
        # — a plain QLabel reports its whole text width as its size hint,
        # which put a horizontal scrollbar under the list.
        t = _HighlightLabel()
        t.set_terms(terms)
        t.setFullText(title)
        t.setToolTip(title)
        t.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        m = QtWidgets.QLabel(sub)
        m.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt;")
        v.addWidget(t)
        v.addWidget(m)
        # Polish first so the labels' QSS font sizes count in the hint.
        label_widget.ensurePolished()
        t.ensurePolished()
        hint = label_widget.sizeHint()
        item.setSizeHint(QtCore.QSize(0, hint.height() + 2 * self._ITEM_PAD_V))
        self._results.addItem(item)
        self._results.setItemWidget(item, label_widget)

    def _refresh(self, query: str) -> None:
        self._results.clear()
        query = (query or "").strip()
        terms = query.lower().split()
        if query:
            sessions = self._store.search(query, include_archived=False, limit=80)
        else:
            recent_set = set(self._recents)
            recents = []
            for sid in self._recents:
                s = self._store.get_session(sid)
                if s is not None and not s.archived:
                    recents.append(s)
            tail = self._store.list_sessions(include_archived=False, limit=40)
            tail = [s for s in tail if s.id not in recent_set]
            sessions = recents + tail
        actions = [a for a in self._actions if a.matches(terms)]
        # A typed query that names an action puts it first (that's the
        # intent); with no query the sessions lead (recents first) and the
        # actions follow.
        if query and actions:
            self._add_header("Actions")
            for a in actions:
                self._add_action_row(a, terms)
        if sessions:
            if actions:
                self._add_header("Sessions")
            for s in sessions[:80]:
                self._add_session_row(s, terms)
        if not query and actions:
            self._add_header("Actions")
            for a in actions:
                self._add_action_row(a, terms)
        rows = self._selectable_rows()
        self._empty.setText(f"No matches for “{query}”" if query else "No sessions yet")
        self._empty.setVisible(not rows)
        self._results.setVisible(bool(rows))
        if rows:
            self._results.setCurrentRow(rows[0])

    def _accept_item(self, item: QtWidgets.QListWidgetItem) -> None:
        """A click: open that row (a click on a section header does nothing)."""
        if item is None or item.data(self._HEADER):
            return
        self._results.setCurrentItem(item)
        self._accept_current()

    def _accept_current(self) -> None:
        item = self._results.currentItem()
        if item is None or item.data(self._HEADER):
            return
        act = item.data(self._ACTION)
        if isinstance(act, PaletteAction):
            self.selected_action = act
            self.accept()
            return
        sid = item.data(QtCore.Qt.UserRole)
        if sid is None:
            return
        self.selected_id = int(sid)
        self.accept()


class SessionPopoutWindow(QtWidgets.QMainWindow):
    """Detached read-only viewer for a session. Used by Ctrl+Shift+D and the
    context-menu Pop out action. Updates live as new turns land."""

    def __init__(
        self,
        session: Session,
        *,
        store,
        job_store: JobStore,
        captures_dir: Path,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._sess_id = session.id
        self._store = store
        self._job_store = job_store
        self._captures_dir = captures_dir
        self.setStyleSheet(_QSS)
        self.setWindowTitle(f"{_truncate(session.title or 'Untitled session', 60)} — Holler")
        self.resize(900, 720)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self._restore_geometry()

        self._view = DetailView(self)
        self._view.set_captures_dir(captures_dir)
        # Copy confirmations go through the main window's notice.
        if hasattr(self._view, "copy_notice") and isinstance(parent, SessionsWindow):
            self._view.copy_notice.connect(lambda text, w=parent: w.show_inline_notice(text))
        self.setCentralWidget(self._view)
        self._render()
        self._job_store.job_updated.connect(self._on_job_updated)
        self._job_store.session_changed.connect(self._on_session_changed)

    def _render(self, auto_scroll: bool = True) -> None:
        sess = self._store.get_session(self._sess_id)
        if sess is None:
            return
        self._view.set_repo_cwd(_link_root(sess, None))
        live_job = _pick_live_job(self._job_store.all(), sess.id)
        turns = _turns_up_to_live(self._store.get_turns(sess.id), live_job)
        tool_calls_by_turn = {t.id: self._store.get_tool_calls(t.id) for t in turns}
        self._view.show_session(
            sess, turns, tool_calls_by_turn, live_job, self._captures_dir,
            auto_scroll=auto_scroll,
        )

    @QtCore.Slot(int)
    def _on_job_updated(self, job_id: int) -> None:
        job = self._job_store.get(job_id)
        if job is None or job.local_session_id != self._sess_id:
            return
        live = _pick_live_job(self._job_store.all(), self._sess_id)
        if live is not None and live.id != job.id:
            if job.status not in ("queued", "running"):
                self._render(auto_scroll=False)
            return
        if live is not None and getattr(self._view, "_job", None) is not None \
                and self._view._job.id != live.id:
            self._render(auto_scroll=False)  # live job changed hands
            return
        prior = getattr(self._view, "_prior_turns", None)
        if prior and any(tid not in {t.id for t in prior} for tid in _live_group_ids(job)):
            self._render(auto_scroll=False)  # a turn was merged into the job
            return
        self._view.update_streaming(job)

    @QtCore.Slot(int)
    def _on_session_changed(self, session_id: int) -> None:
        # Background metadata refresh — preserve operator's scroll position.
        if session_id == self._sess_id:
            self._render(auto_scroll=False)

    # Pop-outs share one remembered rect (the last one closed), re-fitted to
    # the current screens; each later pop-out is cascaded off it a little so
    # two never stack exactly.
    _GEOMETRY_KEY = "sessions_popout_geometry"
    _CASCADE = theme.SPACE_XL

    def _restore_geometry(self) -> None:
        saved = _state.load().get(self._GEOMETRY_KEY) or {}
        if not all(isinstance(saved.get(k), (int, float)) for k in ("x", "y", "w", "h")):
            return
        rect = QtCore.QRect(int(saved["x"]), int(saved["y"]), int(saved["w"]), int(saved["h"]))
        parent = self.parent()
        siblings = 0
        if isinstance(parent, SessionsWindow):
            siblings = sum(1 for w in parent._detached_windows if w is not self and w.isVisible())
        rect.translate(siblings * self._CASCADE, siblings * self._CASCADE)
        screens = QtGui.QGuiApplication.screens() or []
        host = next((sc for sc in screens if sc.availableGeometry().intersects(rect)), None)
        if host is None:
            return  # monitor gone — keep the default size, let Qt place it
        avail = host.availableGeometry()
        w = min(max(rect.width(), self.minimumWidth(), 1), avail.width())
        h = min(max(rect.height(), self.minimumHeight(), 1), avail.height())
        x = max(avail.left(), min(rect.x(), avail.right() - w + 1))
        y = max(avail.top(), min(rect.y(), avail.bottom() - h + 1))
        self.setGeometry(x, y, w, h)

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt name)
        try:
            g = self.normalGeometry() if self.isMaximized() else self.geometry()
            if g.width() > 0 and g.height() > 0:
                _state.merged_save_debounced({
                    self._GEOMETRY_KEY: {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()},
                })
        except Exception:
            log.exception("could not persist the pop-out geometry")
        super().closeEvent(ev)
