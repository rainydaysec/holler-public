"""Icon system: tinted SVG icons + the painted app icon.

The SVGs live in ``holler/icons/`` (24×24 viewBox, stroke-based,
``stroke="currentColor"``). ``icon()`` / ``pixmap()`` swap ``currentColor``
for the requested colour, render with QSvgRenderer at the device pixel ratio
and cache the result, so callers can pass theme tokens freely::

    from holler import iconset, theme
    button.setIcon(iconset.icon("refresh", theme.FG_MUTED))

Colours: a ``QColor``, a hex / named colour string, or the ``rgba(r,g,b,a)``
string ``theme.rgba()`` produces (alpha on Qt's 0–255 scale, like the theme;
a fractional alpha such as ``0.5`` is read as 0–1). Never pass 8-digit hex —
Qt reads it as ``#AARRGGBB``.

``app_icon()`` paints the wordmark's 3-bar "shout" glyph (BRAND.md) on a
rounded dark tile with QPainter — no image files needed.

All functions need a QGuiApplication (QPixmap requires one).
"""

from __future__ import annotations

import re
from importlib import resources
from typing import Union

from PySide6 import QtCore, QtGui, QtSvg

from holler import theme

ColorLike = Union[str, QtGui.QColor]

_ICON_PACKAGE = "holler.icons"
_RGBA = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([\d.]+)\s*)?\)$"
)

_svg_cache: dict[str, str] = {}
_pixmap_cache: dict[tuple, QtGui.QPixmap] = {}
_icon_cache: dict[tuple, QtGui.QIcon] = {}
_app_icon: QtGui.QIcon | None = None

# Device pixel ratios baked into every QIcon so it stays crisp on 1×, 2× and
# 3× screens (Qt picks the closest one for the target window).
_ICON_DPRS = (1.0, 2.0, 3.0)
APP_ICON_SIZES = (16, 32, 64, 128, 256, 512, 1024)


# ── lookup ──────────────────────────────────────────────────────────────────

def names() -> tuple[str, ...]:
    """Every available icon name, sorted."""
    root = resources.files(_ICON_PACKAGE)
    return tuple(sorted(p.name[:-4] for p in root.iterdir() if p.name.endswith(".svg")))


def svg_source(name: str) -> str:
    """Raw SVG text for *name* (still using ``currentColor``)."""
    src = _svg_cache.get(name)
    if src is None:
        res = resources.files(_ICON_PACKAGE).joinpath(f"{name}.svg")
        if not res.is_file():
            raise KeyError(f"unknown icon {name!r}; available: {', '.join(names())}")
        src = _svg_cache[name] = res.read_text(encoding="utf-8")
    return src


def to_qcolor(color: ColorLike) -> QtGui.QColor:
    """Normalise a token / hex / ``rgba()`` string or QColor to a valid QColor."""
    if isinstance(color, QtGui.QColor):
        c = QtGui.QColor(color)
    else:
        m = _RGBA.match(color.strip())
        if m:
            r, g, b = (int(v) for v in m.group(1, 2, 3))
            a = m.group(4)
            if a is None:
                alpha = 255
            elif "." in a:
                alpha = round(float(a) * 255)
            else:
                alpha = int(a)
            c = QtGui.QColor(r, g, b, max(0, min(255, alpha)))
        else:
            c = QtGui.QColor(color)
    if not c.isValid():
        raise ValueError(f"not a colour: {color!r}")
    return c


# ── rendering ───────────────────────────────────────────────────────────────

def pixmap(name: str, color: ColorLike, size: int = 16, dpr: float = 1.0) -> QtGui.QPixmap:
    """*name* tinted *color*, *size* logical px square at device ratio *dpr*.

    The pixmap is ``round(size * dpr)`` physical px with ``devicePixelRatio``
    set, so it paints at *size* logical px. Results are cached; a repeat call
    returns the same object.
    """
    qc = to_qcolor(color)
    dpr = float(dpr)
    key = (name, qc.name(QtGui.QColor.HexArgb), int(size), dpr)
    pm = _pixmap_cache.get(key)
    if pm is not None:
        return pm

    opaque = QtGui.QColor(qc)
    opaque.setAlpha(255)
    svg = svg_source(name).replace("currentColor", opaque.name(QtGui.QColor.HexRgb))
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():  # pragma: no cover — guarded by tests
        raise ValueError(f"icon {name!r} is not valid SVG")

    px = max(1, round(size * dpr))
    img = QtGui.QImage(px, px, QtGui.QImage.Format_ARGB32_Premultiplied)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
    renderer.render(p, QtCore.QRectF(0, 0, px, px))
    if qc.alpha() < 255:
        # Scale alpha after rendering: painter opacity is applied per SVG node,
        # so crossing strokes (plus, close) would come out darker.
        p.setCompositionMode(QtGui.QPainter.CompositionMode_DestinationIn)
        p.fillRect(img.rect(), QtGui.QColor(0, 0, 0, qc.alpha()))
    p.end()

    pm = QtGui.QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    _pixmap_cache[key] = pm
    return pm


def icon(name: str, color: ColorLike, size: int = 16) -> QtGui.QIcon:
    """QIcon of *name* tinted *color* at *size* logical px (1×/2×/3× baked in).

    Qt derives the disabled-mode look from these pixmaps; callers that want a
    token-coloured disabled state can ``addPixmap(pixmap(...), QIcon.Disabled)``.
    """
    qc = to_qcolor(color)
    key = (name, qc.name(QtGui.QColor.HexArgb), int(size))
    ic = _icon_cache.get(key)
    if ic is None:
        ic = QtGui.QIcon()
        for dpr in _ICON_DPRS:
            ic.addPixmap(pixmap(name, qc, size, dpr))
        _icon_cache[key] = ic
    return ic


# ── app icon ────────────────────────────────────────────────────────────────

def _bar_path(rect: QtCore.QRectF, r: float) -> QtGui.QPainterPath:
    """A bar with rounded top corners and a square foot (as the wordmark)."""
    path = QtGui.QPainterPath()
    path.moveTo(rect.bottomLeft())
    path.lineTo(rect.left(), rect.top() + r)
    path.quadTo(rect.left(), rect.top(), rect.left() + r, rect.top())
    path.lineTo(rect.right() - r, rect.top())
    path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + r)
    path.lineTo(rect.bottomRight())
    path.closeSubpath()
    return path


def app_icon_pixmap(size: int) -> QtGui.QPixmap:
    """The app icon painted at *size* px square (device ratio 1)."""
    img = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32_Premultiplied)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)

    # Tile: macOS-style rounded square (~10% margin from 64 px up so it sits
    # on the Dock grid; edge-to-edge at menu/tab sizes where every px counts).
    margin = round(size * 0.1) if size >= 64 else 0
    tile = QtCore.QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    radius = tile.width() * 0.225
    grad = QtGui.QLinearGradient(tile.topLeft(), tile.bottomLeft())
    grad.setColorAt(0.0, QtGui.QColor(theme.SURFACE))
    grad.setColorAt(1.0, QtGui.QColor(theme.HEADER))
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(tile, radius, radius)
    if size >= 32:
        hair = max(1.0, size / 512)
        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), hair))
        inset = hair / 2
        p.drawRoundedRect(tile.adjusted(inset, inset, -inset, -inset), radius - inset, radius - inset)
        p.setPen(QtCore.Qt.NoPen)

    # Glyph: the wordmark's bars (4 wide, 3 gap, 6/11/16 tall → 18×16 box),
    # scaled to ~half the tile (a little more at menu sizes, where it has to
    # carry the icon alone) and centred on its bounding box.
    snap = size <= 32  # small sizes: whole-px edges, or the bars go soft
    unit = tile.height() * (0.62 if snap else 0.52) / 16
    bar_w, step = 4 * unit, 7 * unit
    heights = [h * unit for h in (6, 11, 16)]
    if snap:  # whole-px widths, gaps and heights so all three bars match
        bar_w = max(1, round(bar_w))
        step = bar_w + max(1, round(3 * unit))
        heights = [max(1, round(h)) for h in heights]
    glyph_w, glyph_h = 2 * step + bar_w, heights[-1]
    x0 = tile.center().x() - glyph_w / 2
    base = tile.center().y() + glyph_h / 2
    if snap:
        x0, base = round(x0), round(base)
    colors = (
        QtGui.QColor(theme.BRAND_AMBER_SOFT).lighter(140),
        QtGui.QColor(theme.BRAND_AMBER).darker(115),
        QtGui.QColor(theme.BRAND_AMBER),
    )
    for i, h in enumerate(heights):
        rect = QtCore.QRectF(x0 + i * step, base - h, bar_w, h)
        # At 16 px a bar is 2 px wide: a sub-pixel radius only greys its top row.
        r = 0.0 if size <= 16 else min(theme.RADIUS_GLYPH_BAR * unit, bar_w / 2)
        p.fillPath(_bar_path(rect, r), colors[i])
    p.end()
    return QtGui.QPixmap.fromImage(img)


def app_icon() -> QtGui.QIcon:
    """The Holler app icon at every size in ``APP_ICON_SIZES`` (cached)."""
    global _app_icon
    if _app_icon is None:
        ic = QtGui.QIcon()
        for s in APP_ICON_SIZES:
            ic.addPixmap(app_icon_pixmap(s))
        _app_icon = ic
    return _app_icon
