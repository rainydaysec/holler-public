"""holler.iconset: SVG icon set + painted app icon."""

import pytest
from PySide6 import QtCore, QtGui, QtSvg

from holler import iconset, theme

REQUIRED = (
    "stop", "refresh", "settings", "search", "mic", "wrench", "inbox", "link",
    "trash", "close", "more-horizontal", "check", "check-circle", "star",
    "star-filled", "pin", "archive", "plus", "chevron-down", "chevron-right",
    "copy", "external-link", "alert-triangle", "clock", "camera", "terminal",
    "file", "edit", "eye",
)


def _strongest_pixel(pm: QtGui.QPixmap) -> QtGui.QColor:
    img = pm.toImage()
    best = QtGui.QColor(0, 0, 0, 0)
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > best.alpha():
                best = c
    return best


def test_every_required_icon_ships():
    assert set(REQUIRED) <= set(iconset.names())


@pytest.mark.parametrize("name", REQUIRED)
def test_icon_svg_is_valid_and_tintable(app, name):
    src = iconset.svg_source(name)
    assert "currentColor" in src
    assert 'viewBox="0 0 24 24"' in src
    assert QtSvg.QSvgRenderer(QtCore.QByteArray(src.encode())).isValid()


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize("size", (16, 32))
def test_icon_renders_in_requested_colour(app, name, size):
    target = QtGui.QColor(theme.DANGER)
    pm = iconset.pixmap(name, theme.DANGER, size)
    assert pm.width() == pm.height() == size
    c = _strongest_pixel(pm)
    assert c.alpha() >= 128, f"{name}@{size} rendered (nearly) empty"
    for got, want in ((c.red(), target.red()), (c.green(), target.green()), (c.blue(), target.blue())):
        assert abs(got - want) <= 8, f"{name}@{size}: {c.name()} != {target.name()}"


def test_rgba_string_and_qcolor_accepted(app):
    pm = iconset.pixmap("plus", theme.rgba(theme.ACCENT, 128), 32)
    c = _strongest_pixel(pm)
    assert 100 <= c.alpha() <= 140
    assert iconset.pixmap("plus", QtGui.QColor(theme.ACCENT), 32) is iconset.pixmap("plus", theme.ACCENT, 32)


def test_cache_returns_same_object(app):
    assert iconset.pixmap("mic", theme.FG, 16) is iconset.pixmap("mic", theme.FG, 16)
    assert iconset.pixmap("mic", theme.FG, 16) is not iconset.pixmap("mic", theme.FG_MUTED, 16)
    assert iconset.icon("mic", theme.FG) is iconset.icon("mic", theme.FG)


def test_dpr2_pixmap_is_double_size(app):
    pm = iconset.pixmap("search", theme.FG, 16, 2)
    assert (pm.width(), pm.height()) == (32, 32)
    assert pm.devicePixelRatio() == 2.0
    assert pm.deviceIndependentSize().toSize() == QtCore.QSize(16, 16)


def test_icon_carries_hidpi_pixmaps(app):
    ic = iconset.icon("check", theme.SUCCESS, 16)
    assert not ic.isNull()
    pm = ic.pixmap(QtCore.QSize(16, 16), 2.0)
    assert pm.width() == 32


def test_unknown_name_raises_keyerror_listing_names(app):
    with pytest.raises(KeyError) as exc:
        iconset.pixmap("no-such-icon", theme.FG)
    assert "refresh" in str(exc.value)
    with pytest.raises(KeyError):
        iconset.icon("no-such-icon", theme.FG)


def test_bad_colour_raises(app):
    with pytest.raises(ValueError):
        iconset.pixmap("plus", "not-a-colour")


def test_app_icon_has_all_sizes(app):
    ic = iconset.app_icon()
    sizes = {(s.width(), s.height()) for s in ic.availableSizes()}
    for s in iconset.APP_ICON_SIZES:
        assert (s, s) in sizes
    assert iconset.app_icon() is ic


def test_app_icon_glyph_is_brand_amber(app):
    img = iconset.app_icon_pixmap(128).toImage()
    amber = QtGui.QColor(theme.BRAND_AMBER)
    found = any(
        img.pixelColor(x, y) == amber
        for y in range(0, 128, 2) for x in range(0, 128, 2)
    )
    assert found, "full-amber front bar missing from the app icon"
