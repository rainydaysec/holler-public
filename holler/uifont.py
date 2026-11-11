"""The one UI font: the platform's system font.

holler used to hard-code a family per platform ('Segoe UI' / 'Helvetica Neue').
It now follows the system UI font — San Francisco on macOS, Segoe UI on
Windows — which Qt resolves from ``QFontDatabase.systemFont(GeneralFont)``
and installs as the application default font at startup.

Rules for UI code:
- QSS / HTML: don't write ``font-family`` for UI text. Every widget and
  QTextDocument inherits the application font, so leaving it out *is* the
  system font (SF's private ``.AppleSystemUIFont`` name can't be addressed
  from CSS anyway).
- QFont: start from :func:`ui_font` and adjust size / weight / tracking
  instead of ``setFamily()``.
"""

from __future__ import annotations

from typing import Optional


def ui_font(point_size: Optional[float] = None):
    """A copy of the application UI font (the system font unless a caller
    pinned another — e.g. the visual-test harness), optionally resized."""
    from PySide6 import QtGui

    if QtGui.QGuiApplication.instance() is not None:
        font = QtGui.QFont(QtGui.QGuiApplication.font())
    else:
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.GeneralFont)
    if point_size is not None:
        font.setPointSizeF(float(point_size))
    return font


def ui_font_family() -> str:
    """Family name of :func:`ui_font` (for logging / diagnostics)."""
    return ui_font().family()
