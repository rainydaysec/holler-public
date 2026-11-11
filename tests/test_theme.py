"""holler.theme is the single source of UI design values.

Guards:
  (a) migrated UI modules carry no colour literals outside theme.py (except
      the explicit allowlist below),
  (b) the rgba()/qcolor() helpers format exactly what Qt expects,
  (c) every `theme.X` referenced anywhere in holler/ exists,
  (d) phase 2b scales: type sizes on the 6-step scale (≥ 9pt), spacing on
      the 4-based scale, radii 4 / 6 / pill,
  (e) WCAG AA contrast for the key text / UI token pairs,
  (f) the app-level stylesheet / palette and where they are applied.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from holler import theme

_HOLLER = Path(__file__).resolve().parent.parent / "holler"

# UI modules migrated to read design values from holler.theme.
_MIGRATED = [
    "sessions_window.py",
    "detail.py",
    "panel.py",
    "markdown_render.py",
    "settings_dialog.py",
    "first_run.py",
    "overlay.py",
    "tool_format.py",
    "uifont.py",
    "runners/full.py",
]

# Colour literals deliberately left in place, per file. Keep this short and
# say why — anything else belongs in holler/theme.py.
_ALLOWED_LITERALS = {
    # Annotation stroke colour is a user setting, not a design token: the
    # default mirrors Config.stroke_color in holler/config.py and must stay
    # greppable next to it.
    "overlay.py": {"#FF2E2E"},
    "settings_dialog.py": {"#FF2E2E"},
}

_HEX = re.compile(r"(?<![&\w])#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?(?![0-9a-fA-F\w])")
_RGB = re.compile(r"rgba?\(\s*\d")
_NAMED = re.compile(r"(?:color|background|border-color)\s*:\s*(?:white|black)\b")
_FONT_SIZE = re.compile(r"font-size\s*:\s*\d")


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _string_literals(path: Path):
    """(lineno, text) for every str literal / f-string literal part, minus docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.lineno, node.value


@pytest.mark.parametrize("rel", _MIGRATED)
def test_no_colour_literals_outside_theme(rel):
    path = _HOLLER / rel
    allowed = _ALLOWED_LITERALS.get(rel, set())
    hits = []
    for lineno, text in _string_literals(path):
        for m in _HEX.finditer(text):
            if m.group(0) not in allowed:
                hits.append(f"{rel}:{lineno}: {m.group(0)}")
        for rx in (_RGB, _NAMED, _FONT_SIZE):
            for m in rx.finditer(text):
                hits.append(f"{rel}:{lineno}: {m.group(0)}…")
    # QColor(r, g, b[, a]) with literal ints is a colour literal too.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", None)) == "QColor"
                and node.args and all(isinstance(a, ast.Constant) and isinstance(a.value, int) for a in node.args)):
            hits.append(f"{rel}:{node.lineno}: QColor({', '.join(str(a.value) for a in node.args)})")
    assert not hits, "design literals outside holler/theme.py — add/use a token:\n" + "\n".join(hits)


def test_allowlist_entries_are_still_present():
    # A stale allowlist would silently widen the guard.
    for rel, lits in _ALLOWED_LITERALS.items():
        found = {m.group(0) for _, t in _string_literals(_HOLLER / rel) for m in _HEX.finditer(t)}
        assert lits <= found, f"{rel}: allowlisted {lits - found} no longer used — drop it"


def test_scanner_catches_literals(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        '"""doc #123456 is fine"""\n'
        'A = "color: #2c343c;"\n'
        'B = f"x {y} rgba(1, 2, 3, 4)"\n'
        'C = "QPushButton { color: white; }"\n'
    )
    texts = [t for _, t in _string_literals(p)]
    assert not any("#123456" in t for t in texts)
    assert any(_HEX.search(t) for t in texts)
    assert any(_RGB.search(t) for t in texts)
    assert any(_NAMED.search(t) for t in texts)
    assert not _HEX.search("&#123456;")


# ── (b) helpers ─────────────────────────────────────────────────────────────

def test_rgba_formats_like_the_original_literals():
    assert theme.rgba("#1f6feb", 51) == "rgba(31,111,235,51)"
    assert theme.rgba(theme.BRAND_AMBER, 34) == "rgba(245,165,36,34)"
    assert theme.rgba("#F5A524", 0) == theme.rgba("#f5a524", 0) == "rgba(245,165,36,0)"
    assert theme.rgba("#fff", 255) == "rgba(255,255,255,255)"
    # Washes are all on Qt's 0–255 alpha scale (no fractional rgba).
    assert theme.BRAND_AMBER_GLOW == "rgba(245,165,36,34)"
    assert theme.ACCENT_HOVER_WASH == "rgba(88,166,255,31)"
    assert theme.BRAND_AMBER_HOVER_WASH == "rgba(245,165,36,46)"
    assert not hasattr(theme, "rgba_f")


@pytest.mark.parametrize("bad", [-1, 256, 0.5, True, "51"])
def test_rgba_rejects_non_0_255_alpha(bad):
    with pytest.raises(ValueError):
        theme.rgba("#000000", bad)


@pytest.mark.parametrize("bad", ["0e1216", "#0e12", "#0e121680", "red"])
def test_rgba_rejects_non_hex_colours(bad):
    with pytest.raises(ValueError):
        theme.rgba(bad, 10)


def test_qcolor_helper(app):
    c = theme.qcolor(theme.CANVAS, 200)
    assert c.getRgb() == (14, 18, 22, 200)
    assert theme.qcolor(theme.DANGER).getRgb() == (255, 92, 92, 255)


def test_rgba_tokens_parse_in_qss(app):
    # What Qt actually sees: the rgba() token in a stylesheet paints the same
    # pixel as the equivalent QColor.
    from PySide6 import QtCore, QtWidgets

    w = QtWidgets.QWidget()
    w.setAttribute(QtCore.Qt.WA_StyledBackground)
    w.setStyleSheet(f"QWidget {{ background: {theme.SCRIM}; }} "
                    f"QFrame {{ background: {theme.rgba('#ffffff', 102)}; }}")
    f = QtWidgets.QFrame(w)
    w.resize(8, 8)
    f.resize(8, 8)
    px = w.grab().toImage().pixelColor(4, 4)
    assert abs(px.red() - 102) <= 1 and px.red() == px.green() == px.blue()


def test_colour_tokens_are_valid_and_never_argb_hex(app):
    from PySide6 import QtGui

    for name, val in vars(theme).items():
        if not name.isupper() or not isinstance(val, str):
            continue
        if val.startswith("#"):
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", val), f"{name}={val!r}: use #rrggbb (Qt reads 8-digit as ARGB)"
            assert QtGui.QColor(val).isValid(), name
        elif val.startswith("rgba("):
            assert re.fullmatch(r"rgba\(\d+,\d+,\d+,\d+\)", val), f"{name}={val!r}"


def test_module_aliases_point_at_theme_tokens():
    from holler import detail, markdown_render, panel, sessions_window as sw

    assert sw._RED == panel._RED == detail._RED == theme.DANGER
    assert sw._FG_DIM == panel._FG_DIM == markdown_render._FG_DIM == theme.FG_MUTED
    assert sw._ACCENT == theme.ACCENT
    assert sw._BG_ROW_SELECTED == theme.SELECTION_BG
    assert sw._BRAND_AMBER == theme.BRAND_AMBER
    assert sw._BRAND_AMBER_GLOW == theme.BRAND_AMBER_GLOW
    assert markdown_render._FG_MUTED == theme.FG_FAINT


# ── (c) referenced tokens exist ─────────────────────────────────────────────

def test_every_referenced_theme_token_exists():
    missing = []
    for path in sorted(_HOLLER.rglob("*.py")):
        if path.name == "theme.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "theme" and not hasattr(theme, node.attr)):
                missing.append(f"{path.relative_to(_HOLLER.parent)}:{node.lineno}: theme.{node.attr}")
    assert not missing, "unknown theme tokens:\n" + "\n".join(missing)



# ── (d) scales ──────────────────────────────────────────────────────────────

_TYPE_SCALE = {9, 10, 11, 13, 16, 20}


def test_type_tokens_sit_on_the_scale():
    sizes = {n: v for n, v in vars(theme).items() if n.startswith("FONT_") and isinstance(v, (int, float))}
    assert sizes, "no FONT_ tokens?"
    for name, v in sizes.items():
        if name == "FONT_DISPLAY":
            assert v > max(_TYPE_SCALE), name  # the one display size
        elif name == "FONT_WORDMARK":
            assert v == 13
        else:
            assert v in _TYPE_SCALE, f"{name}={v} is off the 9/10/11/13/16/20 scale"
    assert min(sizes.values()) >= 9, "minimum UI text is 9pt"


def test_spacing_and_radius_tokens_sit_on_the_scale():
    spaces = {n: v for n, v in vars(theme).items() if n.startswith("SPACE_")}
    assert set(spaces.values()) == {2, 4, 8, 12, 16, 24}
    assert (theme.RADIUS_SM, theme.RADIUS_MD) == (4, 6)
    # Qt draws a radius > half the box as a square corner: pills are pinned to
    # PILL_HEIGHT and the pill radius is exactly half of it.
    assert theme.RADIUS_PILL * 2 == theme.PILL_HEIGHT
    assert f"max-height: {theme.PILL_HEIGHT - 2}px" in theme.PILL_BOX


@pytest.mark.parametrize("rel", _MIGRATED)
def test_no_font_point_sizes_below_the_floor(rel):
    text = (_HOLLER / rel).read_text(encoding="utf-8")
    assert not re.search(r"setPointSize(F)?\(\s*[0-8](\.\d+)?\s*\)", text), rel
    assert "UI_FONT_STACK" not in text and "UI_FONT_FAMILY" not in text, \
        f"{rel}: UI text inherits the system font — no hard-coded family"


# ── (e) contrast ────────────────────────────────────────────────────────────

def _cr(fg, bg):
    return theme.contrast_ratio(fg, bg)


def test_contrast_ratio_helper_matches_wcag_reference_values():
    assert round(_cr("#000000", "#ffffff"), 1) == 21.0
    assert round(_cr("#777777", "#ffffff"), 2) == 4.48
    assert _cr("#123456", "#123456") == pytest.approx(1.0)


_TEXT_BGS = {
    "CANVAS": theme.CANVAS,
    "SURFACE": theme.SURFACE,
    "HEADER": theme.HEADER,
    "SUNKEN": theme.SUNKEN,
    # Translucent row states, composited the way they paint.
    "selected row": theme.composite(theme.ACCENT_EMPHASIS, 64, theme.CANVAS),
    "unreviewed (done) card": theme.composite(theme.SUCCESS, theme.CARD_DONE_ALPHA, theme.CANVAS),
    "running card, brightest pulse": theme.composite(theme.ACCENT, max(theme.PULSE_ALPHAS), theme.CANVAS),
}


@pytest.mark.parametrize("fg_name", ["FG", "FG_MUTED", "FG_FAINT"])
@pytest.mark.parametrize("bg_name", sorted(_TEXT_BGS))
def test_contrast_text_tokens_meet_wcag_aa(fg_name, bg_name):
    fg = getattr(theme, fg_name)
    bg = _TEXT_BGS[bg_name]
    if fg_name == "FG_FAINT" and bg_name in ("selected row", "running card, brightest pulse"):
        # Faint labels step up to FG_MUTED on these rows
        # (SessionListItem._sync_text_colors; see test_session_row_* below).
        return
    assert _cr(fg, bg) >= 4.5, (fg_name, bg_name, round(_cr(fg, bg), 2))


def test_contrast_selection_bg_matches_the_token():
    assert theme.SELECTION_BG == theme.rgba(theme.ACCENT_EMPHASIS, 64)
    assert theme.CARD_DONE_BG == theme.rgba(theme.SUCCESS, theme.CARD_DONE_ALPHA)
    assert theme.CARD_PULSE_BRIGHT == theme.rgba(theme.ACCENT, max(theme.PULSE_ALPHAS))


def test_contrast_buttons_and_status_text():
    # Primary buttons: white text on every fill state.
    for fill in (theme.ACCENT_EMPHASIS, theme.ACCENT_EMPHASIS_HOVER, theme.ACCENT_EMPHASIS_PRESSED):
        assert _cr(theme.FG_ON_EMPHASIS, fill) >= 4.5, fill
    # Neutral buttons, menus, tooltips.
    for bg in (theme.CONTROL, theme.SURFACE_HOVER, theme.BORDER):
        assert _cr(theme.FG, bg) >= 4.5, bg
    # Accent links / state text, status and brand text on the canvas.
    for fg in (theme.ACCENT, theme.SUCCESS, theme.WARNING, theme.DANGER, theme.BRAND_AMBER, theme.PIN):
        assert _cr(fg, theme.CANVAS) >= 4.5, fg
        assert _cr(fg, theme.SURFACE) >= 4.5, fg
    # Live bar text.
    assert _cr(theme.FG_MUTED, theme.LIVE_BG) >= 4.5
    # Placeholder text (palette PlaceholderText) on inputs.
    assert _cr(theme.FG_MUTED, theme.SURFACE) >= 4.5


def test_contrast_state_glyphs_and_focus_borders_meet_3_to_1():
    # Focus ring / active chip border, status dots.
    for fg in (theme.ACCENT, theme.SUCCESS, theme.DANGER, theme.WARNING):
        assert _cr(fg, theme.CANVAS) >= 3.0, fg
        assert _cr(fg, theme.SURFACE) >= 3.0, fg
    # A checked checkbox is outlined in ACCENT (its ACCENT_EMPHASIS fill alone
    # is < 3:1 against the surface); the unchecked outline is BORDER_STRONG.
    assert re.search(r"QCheckBox::indicator:checked \{[^}]*border-color: " + re.escape(theme.ACCENT), theme.app_qss())
    assert _cr(theme.BORDER_STRONG, theme.CANVAS) >= 1.5  # decorative outline only


def _row(session_kw, **item_kw):
    from holler.domain import Session
    from holler.sessions_window import SessionListItem

    base = dict(id=1, created_at=1.0, last_activity_at=1.0, model="claude-opus-5",
                transcript="t", title="hey can you look at the logs", status="idle",
                sdk_session_id=None, screenshot_path=None, pinned=False, archived=False, notes="")
    base.update(session_kw)
    return SessionListItem(Session(**base), {}, now=1.0 + 40 * 86400, **item_kw)


def test_session_row_faint_text_steps_up_on_tinted_rows(app):
    """Stale / reviewed meta and placeholder titles are FG_FAINT on plain rows
    but FG_MUTED on the selection fill and a running card's pulse — FG_FAINT
    is under 4.5:1 there."""
    row = _row({"reviewed_at": 2.0})
    assert theme.FG_FAINT in row._meta.styleSheet()
    row.set_selected(True)
    assert theme.FG_MUTED in row._meta.styleSheet() and theme.FG_FAINT not in row._meta.styleSheet()
    row.set_selected(False)
    assert theme.FG_FAINT in row._meta.styleSheet()
    row.set_is_running(True)
    assert theme.FG_FAINT not in row._meta.styleSheet()
    row.set_is_running(False)
    assert theme.FG_FAINT in row._meta.styleSheet()


# ── (f) app style ───────────────────────────────────────────────────────────

def test_app_qss_covers_the_stock_widgets():
    qss = theme.app_qss()
    for sel in ("QMenu", "QMenu::item:selected", "QMenu::separator", "QMenu::item:disabled",
                "QToolTip", "QScrollBar:vertical", "QScrollBar:horizontal",
                "QScrollBar::add-line", "QComboBox", "QComboBox::drop-down", "QComboBox::down-arrow",
                "QComboBox QAbstractItemView", "QSpinBox::up-button", "QSpinBox::down-arrow",
                "QCheckBox::indicator", "QCheckBox::indicator:checked", "QPushButton:hover",
                "QPushButton:pressed", "QPushButton:disabled", "QPushButton:focus",
                "QPushButton#primary", 'QPushButton[variant="secondary"]', "QLineEdit:focus",
                "QPlainTextEdit:focus", "QMessageBox", "QInputDialog", "QScrollArea"):
        assert sel in qss, sel
    assert not re.search(r"#[0-9a-fA-F]{8}\b", qss)
    # Every image the sheet references exists on disk.
    for path in re.findall(r"url\(([^)]+)\)", qss):
        assert Path(path).is_file(), path


def test_app_qss_parses_and_styles_widgets(app):
    from PySide6 import QtWidgets

    w = QtWidgets.QWidget()
    w.setStyleSheet(theme.app_qss())
    lay = QtWidgets.QVBoxLayout(w)
    for cls in (QtWidgets.QComboBox, QtWidgets.QSpinBox, QtWidgets.QCheckBox, QtWidgets.QPushButton,
                QtWidgets.QLineEdit):
        lay.addWidget(cls())
    w.resize(200, 200)
    w.grab()  # would log "Could not parse stylesheet" on a syntax error
    btn = w.findChild(QtWidgets.QPushButton)
    btn.ensurePolished()
    assert btn.palette().color(btn.foregroundRole()).name() == theme.FG.lower()


def test_dark_palette_and_apply_app_style(app):
    from PySide6 import QtCore, QtGui

    class _Hints:
        requested = None

        def setColorScheme(self, scheme):
            self.requested = scheme

    class _App:
        def __init__(self):
            self.hints, self.qss, self.pal = _Hints(), None, None

        def styleHints(self):
            return self.hints

        def setStyleSheet(self, qss):
            self.qss = qss

        def setPalette(self, pal):
            self.pal = pal

    fake = _App()
    theme.apply_app_style(fake)
    assert fake.qss == theme.app_qss()
    if hasattr(QtCore.Qt, "ColorScheme"):
        # The offscreen platform can't report it back, so check the request.
        assert fake.hints.requested == QtCore.Qt.ColorScheme.Dark
    pal = fake.pal
    assert pal.color(QtGui.QPalette.Window).name() == theme.CANVAS
    assert pal.color(QtGui.QPalette.Base).name() == theme.SURFACE
    assert pal.color(QtGui.QPalette.Text).name() == theme.FG
    assert pal.color(QtGui.QPalette.Highlight).name() == theme.ACCENT_EMPHASIS
    assert pal.color(QtGui.QPalette.Disabled, QtGui.QPalette.Text).name() == theme.FG_FAINT

    # Hints without setColorScheme (Qt < 6.8) are tolerated.
    fake.hints = object()
    theme.apply_app_style(fake)


def test_full_runner_applies_app_style_before_any_window(monkeypatch):
    """runners.full applies the app style right after creating the
    QApplication — on the first-run path too — and a failure there is
    logged, never fatal."""
    import inspect

    from holler.runners import full

    src = inspect.getsource(full.main)
    assert src.count("_apply_app_style(") == 2
    first_run = src.index("FirstRunDialog(cfg")
    assert src.index("_apply_app_style(_bootstrap_app)") < first_run
    assert src.index("_apply_app_style(app)") < src.index("OverlayWindow(")

    calls = []
    monkeypatch.setattr(theme, "apply_app_style", lambda a: calls.append(a) or (_ for _ in ()).throw(RuntimeError("x")))
    full._apply_app_style(object())  # must swallow the error
    assert len(calls) == 1


def test_visual_harness_applies_the_same_app_style():
    src = (_HOLLER.parent / "tests" / "visual" / "scenarios.py").read_text(encoding="utf-8")
    assert "theme.apply_app_style(app)" in src


def test_qss_glyphs_ship_in_the_package_and_match_theme():
    """The QSS arrow/tick glyphs must come from the package, not the temp dir
    (macOS/Windows sweep $TMPDIR while holler is running for days, and the
    combo arrows / checkbox ticks vanished). If this fails after a colour
    change, regenerate: write theme.qss_glyphs() into holler/qss_assets/."""
    from holler import theme

    for name, svg in theme.qss_glyphs().items():
        assert (theme._PACKAGED_QSS_ASSETS / name).read_text(encoding="utf-8") == svg, name
    assert theme._asset_dir() == theme._PACKAGED_QSS_ASSETS
    assert str(theme._PACKAGED_QSS_ASSETS.as_posix()) in theme.app_qss()


def test_qss_glyphs_fall_back_to_temp_when_package_copy_missing(tmp_path, monkeypatch):
    from holler import theme

    monkeypatch.setattr(theme, "_PACKAGED_QSS_ASSETS", tmp_path / "missing")
    monkeypatch.setattr(theme.tempfile, "gettempdir", lambda: str(tmp_path))
    d = theme._asset_dir()
    assert d == tmp_path / "holler-qss-v1"
    assert set(p.name for p in d.iterdir()) == set(theme.qss_glyphs())
