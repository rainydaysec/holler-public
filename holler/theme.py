"""Design tokens — the single source of holler's UI design values.

Every colour, type size, weight, tracking, spacing and radius used by the Qt UI
modules lives here; the modules read these names instead of writing literals.
Phase 2a captured the values verbatim; phase 2b consolidated near-duplicates,
snapped type / spacing / radii to small scales and raised text contrast to
WCAG AA (see tests/test_theme.py::test_contrast_*).

Conventions
- Colour tokens are plain strings exactly as Qt / QTextDocument receive them
  (``"#rrggbb"`` or ``"rgba(r,g,b,a)"`` with a 0–255 alpha). Never write
  8-digit hex: Qt reads ``#AARRGGBB``, not CSS ``#RRGGBBAA`` (see
  tests/test_no_argb_hex.py).
- Type sizes are numbers in pt; QSS uses ``f"font-size: {FONT_SM}pt"``.
- Spacing and radii are numbers in px.
- Brand amber is brand only (wordmark, signature stripe, model pill); accent
  blue is state (selection, focus, links) — see BRAND.md.

This module is pure Python (no Qt import at module load) so it can be imported
anywhere; the Qt helpers (``qcolor``, ``dark_palette``, ``apply_app_style``)
import QtGui / QtWidgets lazily.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Optional, Union

Number = Union[int, float]

log = logging.getLogger("holler.theme")


# ─── Colour helpers ─────────────────────────────────────────────────────────

def _rgb(color: str) -> tuple[int, int, int]:
    """``"#rrggbb"`` / ``"#rgb"`` (any case) → (r, g, b)."""
    h = color.strip()
    if not h.startswith("#"):
        raise ValueError(f"expected a #hex colour, got {color!r}")
    h = h[1:]
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        raise ValueError(f"expected #rgb or #rrggbb, got {color!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgba(color: str, alpha: int) -> str:
    """``rgba(token_or_hex, 0..255)`` → ``"rgba(r,g,b,a)"`` (no spaces).

    Alpha is Qt's 0–255 integer scale — the form Qt's QSS parser reads
    natively.
    """
    if isinstance(alpha, bool) or not isinstance(alpha, int) or not 0 <= alpha <= 255:
        raise ValueError(f"alpha must be an int in 0..255, got {alpha!r}")
    r, g, b = _rgb(color)
    return f"rgba({r},{g},{b},{alpha})"


def qcolor(color: str, alpha: Optional[int] = None):
    """QtGui.QColor for a token / hex string, optionally with a 0–255 alpha."""
    from PySide6 import QtGui

    c = QtGui.QColor(color)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def composite(color: str, alpha: int, over: str) -> str:
    """Opaque ``#rrggbb`` of ``color`` at ``alpha`` (0–255) painted over
    ``over`` — what a translucent wash actually looks like on screen. Used to
    check text contrast on tinted rows."""
    f, b = _rgb(color), _rgb(over)
    a = alpha / 255.0
    return "#" + "".join(f"{round(f[i] * a + b[i] * (1 - a)):02x}" for i in range(3))


def _rel_luminance(color: str) -> float:
    def ch(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(color)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio between two opaque ``#hex`` colours (1–21)."""
    la, lb = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ─── Colour roles ───────────────────────────────────────────────────────────
# Surfaces (darkest → lightest)
HEADER = "#0c1117"              # sessions-window header bar (warmer than CANVAS)
CANVAS = "#0e1216"              # window / workspace body
SUNKEN = "#11161c"              # composer, chain + routing strips, alt rows
SURFACE = "#161c22"             # rows, inputs, menus, cards
SURFACE_HOVER = "#1c242b"       # hovered row / menu item, code + table-header fill
CONTROL = "#21262d"             # neutral push-button fill
LIVE_BG = "#0d2230"             # detail-pane live/streaming bar
SCRIM = "#000000"               # annotation-overlay dim layer (used with alpha)

# Borders
BORDER_SUBTLE = "#1d242b"       # hairline dividers (header, strips, markdown cells)
BORDER = "#2c343c"              # default 1px border, splitter, scrollbar handle
BORDER_STRONG = "#3a434d"       # hovered borders / scrollbar handle hover
LIVE_BORDER = "#1a3a4f"         # live bar bottom edge

# Text — every text token clears WCAG AA (4.5:1) on CANVAS and SURFACE.
FG = "#e6edf3"                  # primary text
FG_MUTED = "#8b949e"            # secondary text (_FG_DIM)
FG_FAINT = "#808a95"            # tertiary: stale/reviewed rows, strikethrough, disabled
FG_ON_EMPHASIS = "#ffffff"      # text on filled accent / warning buttons

# Accent (state: selection, focus, links)
ACCENT = "#58a6ff"
ACCENT_EMPHASIS = "#1a5fd0"     # primary button fill (white text ≥ 4.5:1)
ACCENT_EMPHASIS_HOVER = "#1f6feb"
ACCENT_EMPHASIS_PRESSED = "#1750b0"

# Status
SUCCESS = "#3fb950"
SUCCESS_DIM = "#2a6b32"
WARNING = "#d29922"
DANGER = "#FF5C5C"
PIN = "#f9c536"
PIN_BG = "#2a210a"
PIN_BORDER = "#4d3b15"

# Brand (wordmark, signature stripe, model pill — see BRAND.md)
BRAND_AMBER = "#F5A524"
BRAND_AMBER_SOFT = "#7a4f0e"
BRAND_AMBER_DEEP = "#b5720b"    # filled amber action button

# Role aliases (one value, several names readers look for)
HEADER_BORDER = BORDER_SUBTLE   # divider under the header bar (BRAND.md)

# Translucent washes (0–255 alpha)
SELECTION_BG = rgba(ACCENT_EMPHASIS, 64)        # selected row / active chip
BRAND_AMBER_GLOW = rgba(BRAND_AMBER, 34)        # 13% — model-pill fill
BRAND_AMBER_HOVER_WASH = rgba(BRAND_AMBER, 46)  # 18% — model-pill hover
# Running-card pulse frames. Capped so FG_MUTED meta text on the brightest
# frame still clears 4.5:1 (tests/test_theme.py).
PULSE_ALPHAS = (6, 25, 44, 25)
CARD_PULSE_DIM = rgba(ACCENT, PULSE_ALPHAS[0])
CARD_PULSE_MID = rgba(ACCENT, PULSE_ALPHAS[1])
CARD_PULSE_BRIGHT = rgba(ACCENT, PULSE_ALPHAS[2])
CARD_DONE_ALPHA = 28
CARD_DONE_BG = rgba(SUCCESS, CARD_DONE_ALPHA)   # unreviewed-reply card
ACCENT_HOVER_WASH = rgba(ACCENT, 31)            # flat-button hover
# Continuation (routing) pill, soft border: accent @ 45% pre-composited over
# the header so the rounded border stays opaque — a translucent QSS border
# double-paints where its edges meet the corner arcs (visible seams at HiDPI).
ACCENT_PILL_BORDER = composite(ACCENT, 115, HEADER)
ROW_STRIPE_WASH = rgba(FG_ON_EMPHASIS, 10)      # profile chip bg
MD_QUOTE_BG = rgba(ACCENT, 10)                  # markdown blockquote fill


# ─── Typography ─────────────────────────────────────────────────────────────
# One UI family (the system font — see holler.uifont) on a six-step scale.
# 9pt is the floor for any UI text.
FONT_XS = 9           # captions, chips, meta lines, table labels
FONT_SM = 10          # UI body, inputs, buttons, code
FONT_MD = 11          # transcript body, section heads
FONT_LG = 13          # sub-titles, h3, icon glyphs
FONT_XL = 16          # dialog / page titles, h2
FONT_2XL = 20         # h1
FONT_DISPLAY = 24     # empty-dashboard headline
FONT_WORDMARK = 13    # painted HOLLER wordmark (BRAND.md)
GLYPH_HERO = 48       # empty-state emoji glyph (not text)

# Role aliases
FONT_CAPTION = FONT_XS
FONT_BODY = FONT_SM
FONT_TRANSCRIPT = FONT_MD
FONT_TITLE = FONT_XL
FONT_MODEL_PILL = FONT_XS

# Line height for QTextDocument bodies. Percentages only: QTextDocument
# treats a unitless value as pixels.
LINE_HEIGHT_BODY = "150%"
LINE_HEIGHT_CODE = "140%"
LINE_HEIGHT_TOOL = "135%"

# Weights (CSS values as written in QSS / HTML)
WEIGHT_SEMIBOLD = 600
WEIGHT_BOLD = 700
WEIGHT_BOLD_KEYWORD = "bold"   # ≡ 700

# Letter-spacing (CSS) and QFont percentage tracking
TRACKING_TIGHT = "-0.01em"     # markdown h1
TRACKING_CAPS_EM = "0.04em"    # markdown h4/h5 small caps
TRACKING_CAPS_SM = "1px"
TRACKING_CAPS_MD = "1.5px"     # section headers
TRACKING_BRAND_PCT = 112       # QFont.PercentageSpacing — wordmark + model pill


# ─── Spacing ────────────────────────────────────────────────────────────────
# Margins / paddings / layout spacing snap to this 4-based scale. SPACE_2XS is
# for hairline gaps inside a single component only (e.g. title → meta line).
SPACE_2XS = 2
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24


# ─── Shape ──────────────────────────────────────────────────────────────────
# Border radii in px. Qt's QSS draws a radius larger than half the box as a
# square corner (so a CSS-style 999 doesn't work): every pill / chip / tag is
# exactly PILL_HEIGHT tall (``PILL_BOX``) and RADIUS_PILL is half of that.
RADIUS_SM = 4          # menu items, code spans, scrollbar handles, tooltips, checkboxes
RADIUS_MD = 6          # buttons, inputs, cards, menus
PILL_HEIGHT = 20
RADIUS_PILL = PILL_HEIGHT // 2   # chips, pills, tags
RADIUS_GLYPH_BAR = 1.5  # wordmark bar tops (QPainter path, BRAND.md)
# QSS fragment pinning a 1px-bordered pill to PILL_HEIGHT (min/max-height size
# the content box, which excludes the border).
PILL_BOX = (f"min-height: {PILL_HEIGHT - 2}px; max-height: {PILL_HEIGHT - 2}px; "
            f"padding-top: 0; padding-bottom: 0;")


# ─── App-level stylesheet ───────────────────────────────────────────────────

def menu_qss() -> str:
    """QMenu rules. Part of :func:`app_qss`; window stylesheets that carry a
    blanket ``QWidget { background: … }`` (which outranks the app sheet for
    their child menus) append this block so their menus match."""
    return f"""
QMenu {{ background: {SURFACE}; color: {FG}; border: 1px solid {BORDER};
         border-radius: {RADIUS_MD}px; padding: {SPACE_XS}px; }}
QMenu::item {{ background: transparent; padding: {SPACE_XS}px {SPACE_XL}px {SPACE_XS}px {SPACE_MD}px;
               border-radius: {RADIUS_SM}px; }}
QMenu::item:selected {{ background: {SELECTION_BG}; color: {FG}; }}
QMenu::item:disabled {{ color: {FG_FAINT}; background: transparent; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: {SPACE_XS}px {SPACE_SM}px; }}
QMenu::indicator {{ width: 12px; height: 12px; }}
"""


def scrollbar_qss() -> str:
    """Thin dark scrollbars in both orientations, no arrow buttons."""
    return f"""
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; border: none; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; border: none; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: {RADIUS_SM}px;
                               min-height: {SPACE_XL}px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: {RADIUS_SM}px;
                                 min-width: {SPACE_XL}px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {BORDER_STRONG}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}
"""


def qss_glyphs() -> dict[str, str]:
    """The few SVG glyphs QSS needs (combo/spin arrows, checkbox tick), drawn
    in theme colours. Shipped pre-rendered in ``holler/qss_assets/``;
    tests/test_theme.py fails if those files drift from this source."""
    return {
        "chevron-down.svg": (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6">'
            f'<path d="M1 1l4 4 4-4" fill="none" stroke="{FG_MUTED}" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        ),
        "chevron-up.svg": (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6">'
            f'<path d="M1 5l4-4 4 4" fill="none" stroke="{FG_MUTED}" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        ),
        "check.svg": (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
            f'<path d="M1.5 5.2l2.3 2.3L8.5 2.5" fill="none" stroke="{FG_ON_EMPHASIS}" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        ),
    }


_PACKAGED_QSS_ASSETS = Path(__file__).resolve().parent / "qss_assets"


def _asset_dir() -> Optional[Path]:
    """Directory holding the QSS glyph files (QSS ``image:`` only reads files).

    The packaged copy is the normal case: holler stays running for days and
    macOS/Windows sweep the temp dir, which used to delete the glyphs from
    under a live app (arrows and checkbox ticks vanished). Only if the
    packaged files are missing/stale do we fall back to writing them to the
    temp dir. ``None`` if that fails too — the rules then omit the images."""
    glyphs = qss_glyphs()
    try:
        if all(
            (_PACKAGED_QSS_ASSETS / name).read_text(encoding="utf-8") == svg
            for name, svg in glyphs.items()
        ):
            return _PACKAGED_QSS_ASSETS
    except OSError:
        pass
    try:
        d = Path(tempfile.gettempdir()) / "holler-qss-v1"
        d.mkdir(parents=True, exist_ok=True)
        for name, svg in glyphs.items():
            p = d / name
            if not p.exists() or p.read_text(encoding="utf-8") != svg:
                p.write_text(svg, encoding="utf-8")
        return d
    except OSError:
        log.warning("could not write QSS glyphs; combo/spin arrows fall back to none", exc_info=True)
        return None


def _url(d: Optional[Path], name: str) -> str:
    return f"image: url({(d / name).as_posix()});" if d is not None else ""


def app_qss() -> str:
    """The application stylesheet, applied once at startup
    (``apply_app_style``). Covers the stock widgets every window shares;
    windows layer only what's specific to them on top."""
    d = _asset_dir()
    return menu_qss() + scrollbar_qss() + f"""
QToolTip {{ background: {SURFACE_HOVER}; color: {FG}; border: 1px solid {BORDER};
            border-radius: {RADIUS_SM}px; padding: {SPACE_XS}px {SPACE_SM}px; font-size: {FONT_SM}pt; }}

QPushButton {{ background: {CONTROL}; color: {FG}; border: 1px solid {BORDER};
               border-radius: {RADIUS_MD}px; padding: {SPACE_XS}px {SPACE_MD}px; min-height: 18px; }}
QPushButton:hover {{ background: {BORDER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background: {SURFACE}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ background: {SURFACE}; color: {FG_FAINT}; border-color: {BORDER_SUBTLE}; }}
QPushButton#primary, QPushButton[variant="primary"] {{
    background: {ACCENT_EMPHASIS}; color: {FG_ON_EMPHASIS}; border-color: {ACCENT_EMPHASIS}; font-weight: {WEIGHT_SEMIBOLD}; }}
QPushButton#primary:hover, QPushButton[variant="primary"]:hover {{
    background: {ACCENT_EMPHASIS_HOVER}; border-color: {ACCENT_EMPHASIS_HOVER}; }}
QPushButton#primary:pressed, QPushButton[variant="primary"]:pressed {{ background: {ACCENT_EMPHASIS_PRESSED}; }}
QPushButton#primary:focus, QPushButton[variant="primary"]:focus {{ border-color: {ACCENT}; }}
QPushButton[variant="secondary"] {{ background: transparent; color: {FG_MUTED}; }}
QPushButton[variant="secondary"]:hover {{ color: {FG}; border-color: {BORDER_STRONG}; background: transparent; }}

QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {SURFACE}; color: {FG}; border: 1px solid {BORDER}; border-radius: {RADIUS_MD}px;
    selection-background-color: {ACCENT_EMPHASIS}; selection-color: {FG_ON_EMPHASIS}; }}
QLineEdit, QSpinBox, QComboBox {{ padding: {SPACE_XS}px {SPACE_SM}px; min-height: 18px; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus,
QComboBox:on {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {FG_FAINT}; border-color: {BORDER_SUBTLE}; }}

QComboBox {{ padding-right: {SPACE_XL}px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
                        width: {SPACE_XL - SPACE_XS}px; border: none; background: transparent; }}
QComboBox::down-arrow {{ {_url(d, "chevron-down.svg")} width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; color: {FG}; border: 1px solid {BORDER};
                               padding: {SPACE_XS}px; outline: none;
                               selection-background-color: {SELECTION_BG}; selection-color: {FG}; }}
QComboBox QAbstractItemView::item {{ min-height: {SPACE_XL}px; padding: 0 {SPACE_SM}px; }}

QSpinBox {{ padding-right: {SPACE_XL - SPACE_XS}px; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: {SPACE_LG + SPACE_XS}px;
                                              border: none; background: transparent; }}
QSpinBox::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {SURFACE_HOVER}; }}
QSpinBox::up-arrow {{ {_url(d, "chevron-up.svg")} width: 8px; height: 5px; }}
QSpinBox::down-arrow {{ {_url(d, "chevron-down.svg")} width: 8px; height: 5px; }}

QCheckBox {{ color: {FG}; spacing: {SPACE_SM}px; background: transparent; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {BORDER_STRONG};
                        border-radius: {RADIUS_SM}px; background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:focus {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT_EMPHASIS}; border-color: {ACCENT};
                                {_url(d, "check.svg")} }}
QCheckBox::indicator:disabled {{ border-color: {BORDER_SUBTLE}; background: {SUNKEN}; }}

QMessageBox, QInputDialog {{ background: {CANVAS}; }}
QMessageBox QLabel, QInputDialog QLabel {{ color: {FG}; background: transparent; }}
QDialogButtonBox QPushButton {{ min-width: 72px; }}

QScrollArea {{ background: transparent; border: none; }}
"""


def button_qss(variant: str = "neutral", *, pad_v: int = SPACE_XS, pad_h: int = SPACE_MD,
               font_size: Optional[Number] = None, weight: Optional[Number] = WEIGHT_SEMIBOLD,
               radius: int = RADIUS_MD, extra: str = "") -> str:
    """Complete QPushButton rule set (rest / hover / pressed / focus /
    disabled) for one button variant. Used as a widget's own stylesheet where
    a window-level ``QWidget { background: … }`` would otherwise outrank the
    app stylesheet's button rules.

    variant: ``"primary"`` (filled accent, white text), ``"secondary"``
    (outline, muted text) or ``"neutral"`` (filled control).
    """
    if variant == "primary":
        rest = f"background: {ACCENT_EMPHASIS}; color: {FG_ON_EMPHASIS}; border: 1px solid {ACCENT_EMPHASIS};"
        hover = f"background: {ACCENT_EMPHASIS_HOVER}; border-color: {ACCENT_EMPHASIS_HOVER};"
        pressed = f"background: {ACCENT_EMPHASIS_PRESSED}; border-color: {ACCENT_EMPHASIS_PRESSED};"
        disabled = f"background: {BORDER}; color: {FG_MUTED}; border-color: {BORDER};"
    elif variant == "secondary":
        rest = f"background: transparent; color: {FG_MUTED}; border: 1px solid {BORDER};"
        hover = f"color: {FG}; border-color: {BORDER_STRONG};"
        pressed = f"background: {SURFACE};"
        disabled = f"color: {FG_FAINT}; border-color: {BORDER_SUBTLE};"
    elif variant == "neutral":
        rest = f"background: {CONTROL}; color: {FG}; border: 1px solid {BORDER};"
        hover = f"background: {BORDER}; border-color: {BORDER_STRONG};"
        pressed = f"background: {SURFACE};"
        disabled = f"background: {SURFACE}; color: {FG_FAINT}; border-color: {BORDER_SUBTLE};"
    else:
        raise ValueError(f"unknown button variant {variant!r}")
    font = f" font-size: {font_size}pt;" if font_size is not None else ""
    wt = f" font-weight: {weight};" if weight is not None else ""
    return (
        f"QPushButton {{ {rest} padding: {pad_v}px {pad_h}px; border-radius: {radius}px;{font}{wt} {extra}}}"
        f"QPushButton:hover {{ {hover} }}"
        f"QPushButton:pressed {{ {pressed} }}"
        f"QPushButton:focus {{ border-color: {ACCENT}; }}"
        f"QPushButton:disabled {{ {disabled} }}"
    )


def dark_palette():
    """QPalette built from the tokens — native dialogs (QFileDialog,
    QColorDialog, QMessageBox, QInputDialog) and any widget without a
    stylesheet read their colours from it."""
    from PySide6 import QtGui

    P = QtGui.QPalette
    pal = QtGui.QPalette()
    roles = {
        P.Window: CANVAS, P.WindowText: FG, P.Base: SURFACE, P.AlternateBase: SUNKEN,
        P.Text: FG, P.Button: CONTROL, P.ButtonText: FG, P.BrightText: FG_ON_EMPHASIS,
        P.Highlight: ACCENT_EMPHASIS, P.HighlightedText: FG_ON_EMPHASIS,
        P.ToolTipBase: SURFACE_HOVER, P.ToolTipText: FG, P.PlaceholderText: FG_MUTED,
        P.Link: ACCENT, P.LinkVisited: ACCENT,
        P.Light: BORDER_STRONG, P.Midlight: BORDER, P.Mid: BORDER, P.Dark: SUNKEN, P.Shadow: HEADER,
    }
    for role, color in roles.items():
        pal.setColor(role, QtGui.QColor(color))
    for role in (P.WindowText, P.Text, P.ButtonText):
        pal.setColor(P.Disabled, role, QtGui.QColor(FG_FAINT))
    pal.setColor(P.Disabled, P.Button, QtGui.QColor(SURFACE))
    pal.setColor(P.Disabled, P.Highlight, QtGui.QColor(BORDER))
    return pal


def apply_app_style(app) -> None:
    """Dark colour scheme hint (Qt ≥ 6.8), token palette and the app
    stylesheet, applied to ``app`` once at startup. Idempotent."""
    from PySide6 import QtCore

    hints = app.styleHints()
    if hasattr(hints, "setColorScheme") and hasattr(QtCore.Qt, "ColorScheme"):
        try:
            hints.setColorScheme(QtCore.Qt.ColorScheme.Dark)
        except Exception:
            log.debug("setColorScheme(Dark) unavailable", exc_info=True)
    app.setPalette(dark_palette())
    app.setStyleSheet(app_qss())


# ── Phase 3 tokens (only the Phase 3 agent edits between these markers) ──
# Sessions window (rail rows, header icon buttons, ⌘K palette).
WEIGHT_REGULAR = 400            # read / Done row titles (unread rows are WEIGHT_SEMIBOLD)
ROW_DOT = 6                     # px — unread / running status dot in a rail row
ROW_ICON = 14                   # px — rail status glyphs (error, Done, pinned) and row actions
ICON_BUTTON = 26                # px — square header icon buttons (A−, A+, restart, menu)
ICON_GLYPH = 16                 # px — glyph size inside an icon button
# Running-row halo around the status dot (alpha frames, advanced by the 280 ms
# pulse tick). Only the glyph repaints — rows are never refilled.
RUN_HALO_ALPHAS = (110, 80, 50, 80)
# Rotating ring drawn around the running dot. `unread` paints the same accent
# dot, so without a second shape the two states are indistinguishable in a
# still glance — the ring is what makes "working" readable without animation.
RUN_RING_W = 1.5                # px — ring stroke
# Header collapses its usage + quota chips into one chip below this width.
HEADER_COMPACT_W = 1000
# ── end Phase 3 tokens ──


# ── Phase 4 tokens (only the Phase 4 agent edits between these markers) ──
# Transcript (holler/detail.py). QTextDocument paints table cells opaquely, so
# tinted rows are pre-composited over the surface they sit on.
TOOL_CARD_BG = SUNKEN                                   # tool-call card fill
DIFF_ADD_BG = composite(SUCCESS, 38, SUNKEN)            # diff: added line
DIFF_DEL_BG = composite(DANGER, 38, SUNKEN)             # diff: removed line
ERROR_BANNER_BG = composite(DANGER, 26, CANVAS)         # turn-failed banner fill
ERROR_BANNER_BORDER = composite(DANGER, 110, CANVAS)    # turn-failed banner frame
TRANSCRIPT_THUMB_W = 160        # px — max inline screenshot thumbnail box
TRANSCRIPT_THUMB_H = 100
TRANSCRIPT_ICON = 12            # px — inline link icons (Copy)
LIVE_BAR_H = 40                 # px — the single live bar under the conversation
# ── end Phase 4 tokens ──


# ── Phase 5 tokens (only the Phase 5 agent edits between these markers) ──
# Capture HUD (painted in holler/overlay.py). Sizes are logical px.
HUD_WIDTH = 400                 # preferred card width; clamped on narrow windows
HUD_MIN_WIDTH = 160             # below this the HUD hugs the window edges instead
HUD_BG_ALPHA = 255            # CANVAS fill alpha (0–255) over the snapshot
HUD_REC_DOT = 10                # recording dot diameter
HUD_LEVEL_BAR = 6               # mic level bar height
HUD_LEVEL_LOUD = 0.7            # level at/above which the bar reads "loud"
HUD_PULSE_PERIOD_S = 1.2        # recording-dot pulse period
HUD_TICK_MS = 50                # HUD repaint cadence while recording
CAPTURE_HIT_ALPHA = 1           # near-invisible overlay fill so macOS routes clicks to it
# Toasts (holler/panel.py Toast).
TOAST_MAX_VISIBLE = 3           # stacked at once; more wait in a queue
TOAST_MS_INFO = 6000
TOAST_MS_ACTION = 8000          # with an Undo / action button
TOAST_MS_ERROR = 10000
TOAST_MS_DRAIN = 1500           # oldest toast's remaining time while others queue
TOAST_MAX_TEXT_WIDTH = 360
# ── end Phase 5 tokens ──


# ── Phase 6 tokens (only the Phase 6 agent edits between these markers) ──
# Settings dialog (holler/settings_dialog.py) and first run (holler/first_run.py).
SETTINGS_NAV_WIDTH = 156        # section list on the left of Settings
SETTINGS_MIN_WIDTH = 660        # dialog floor (nav + a usable form column)
STEP_DOT = 8                    # first-run step indicator dot diameter
PERMISSION_POLL_MS = 1000       # first-run permission re-check while visible
STATUS_GLYPH = 16               # ✓ / ✗ / ! permission + validation icons
# ── end Phase 6 tokens ──
