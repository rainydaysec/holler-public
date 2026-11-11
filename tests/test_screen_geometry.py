"""Snapshot rect -> overlay geometry mapping (E2 macOS, E3 Windows).

Multi-monitor hardware isn't available in CI, so these pin the coordinate math
with fake screen layouts (mixed DPR) and check single-monitor results match the
old formula exactly.
"""

from __future__ import annotations

import random
import sys

import pytest
from PIL import Image

from holler.screen import native_rect_to_logical


def _old_single_monitor(rect, dpr):
    l, t, r, b = rect
    lw, lh = int(round((r - l) / dpr)), int(round((b - t) / dpr))
    ll, lt = int(round(l / dpr)), int(round(t / dpr))
    return (ll, lt, ll + lw, lt + lh)


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_single_monitor_identical_to_old_mapping(dpr):
    rng = random.Random(1)
    screens = [((0, 0, int(3840 / dpr), int(2160 / dpr)), dpr)]
    for _ in range(500):
        l, t = rng.randrange(-50, 3000), rng.randrange(-50, 1800)
        rect = (l, t, l + rng.randrange(1, 2000), t + rng.randrange(1, 1500))
        assert native_rect_to_logical(rect, screens) == _old_single_monitor(rect, dpr)


def test_window_on_secondary_high_dpi_screen_right_of_primary():
    # Primary 1920x1080 @1.0; secondary 4K @1.5 at native x=1920.
    # Qt6 keeps the native origin: secondary logical geometry (1920,0,2560,1440).
    screens = [((0, 0, 1920, 1080), 1.0), ((1920, 0, 2560, 1440), 1.5)]
    rect = (2880, 216, 3840, 1296)  # native px, 960x1080
    assert native_rect_to_logical(rect, screens) == (2560, 144, 3200, 864)


def test_window_on_secondary_screen_left_of_primary():
    # 2x screen to the LEFT: native x -3840..0, logical geometry (-3840,0,1920,1080).
    screens = [((0, 0, 1920, 1080), 1.0), ((-3840, 0, 1920, 1080), 2.0)]
    rect = (-3000, 400, -1000, 1400)
    assert native_rect_to_logical(rect, screens) == (-3420, 200, -2420, 700)


def test_window_on_primary_unaffected_by_secondary():
    screens = [((0, 0, 1920, 1080), 1.0), ((1920, 0, 2560, 1440), 1.5)]
    assert native_rect_to_logical((100, 100, 900, 700), screens) == (100, 100, 900, 700)


def test_offscreen_rect_falls_back_to_primary():
    screens = [((0, 0, 1280, 720), 1.5)]
    assert native_rect_to_logical((5000, 5000, 5300, 5150), screens) == _old_single_monitor(
        (5000, 5000, 5300, 5150), 1.5)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
@pytest.mark.parametrize("img_scale", [1, 2])
def test_mac_capture_rect_is_in_points(monkeypatch, img_scale):
    """Window on a 2x built-in next to a 1x external: the rect must be the
    CGWindowBounds (points == Qt logical coords), whatever the capture scale."""
    from holler import _screen_mac

    bounds = {"X": 700, "Y": 100, "Width": 600, "Height": 400}
    monkeypatch.setattr(_screen_mac, "_window_info",
                        lambda wid: {"kCGWindowBounds": bounds, "kCGWindowName": "W"})
    monkeypatch.setattr(_screen_mac.Quartz, "CGWindowListCreateImage", lambda *a: object())
    img = Image.new("RGB", (600 * img_scale, 400 * img_scale))
    monkeypatch.setattr(_screen_mac, "_cgimage_to_pil", lambda cg: img)
    image, rect, title = _screen_mac.capture_window(42)
    assert image is img
    assert rect == (700, 100, 1300, 500)
    assert title == "W"


def test_overlay_is_placed_exactly_at_logical_rect(app):
    from holler.overlay import OverlayWindow

    ov = OverlayWindow()
    try:
        img = Image.new("RGB", (400, 300))  # device px (2x)
        ov.show_for(img, (50, 60, 250, 210))
        g = ov.geometry()
        assert (g.x(), g.y(), g.width(), g.height()) == (50, 60, 200, 150)
        # strokes map widget -> source pixels at the image/geometry ratio
        assert ov._widget_to_source(100, 75) == (200, 150)
    finally:
        ov.hide()
        ov.deleteLater()


def test_self_window_rect_is_logical(app, monkeypatch):
    import types
    from PySide6 import QtWidgets
    from holler.screen import capture_self_window

    w = QtWidgets.QWidget()
    w.setWindowTitle("holler-test")
    w.setGeometry(40, 50, 320, 200)
    w.show()
    app.processEvents()
    stub = types.SimpleNamespace(activeWindow=lambda: w, topLevelWidgets=lambda: [w])
    monkeypatch.setattr(QtWidgets.QApplication, "instance", staticmethod(lambda: stub))
    try:
        snap = capture_self_window()
        assert snap is not None
        origin = w.mapToGlobal(w.rect().topLeft())
        assert snap.rect == (origin.x(), origin.y(), origin.x() + w.width(), origin.y() + w.height())
    finally:
        w.close()
        w.deleteLater()
