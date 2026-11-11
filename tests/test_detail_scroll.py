"""Regression tests for DetailView entry-scroll behavior.

Bug: entering a large DONE/idle session landed the viewport at the TOP instead
of the latest turn. On a done session sticky-tailing is off, so the layout-
driven bottom-snap (`_on_doc_size_changed`) was disabled, leaving only a single
post-`setHtml` snap that fires against Qt's stale `maximum()` and lands short.

These tests run a real (offscreen) QTextBrowser so the scrollbar/layout timing
is exercised for real, not mocked.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. .venv/bin/python tests/test_detail_scroll.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from holler.detail import DetailView  # noqa: E402

_TALL_HTML = "<html><body>" + "".join(
    f"<p style='margin:0 0 24px 0'>line {i} — some content to force the "
    f"document taller than the viewport so a real scrollbar exists</p>"
    for i in range(400)
) + "</body></html>"


def _new_view(app: QtWidgets.QApplication) -> DetailView:
    view = DetailView()
    view.resize(420, 300)
    view.show()
    app.processEvents()
    return view


def _settle(app: QtWidgets.QApplication, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def _sb(view: DetailView):
    return view._reply_browser.verticalScrollBar()


def test_force_entry_on_done_session_lands_at_bottom(app):
    """The core regression: tailing OFF (done session) + forced entry must end
    at the bottom, not the top."""
    view = _new_view(app)
    # Mimic show_session for a DONE session: force bottom, tailing off.
    view._tailing = False
    view._force_bottom_next = True
    view._set_browser_html(_TALL_HTML)
    _settle(app)

    sb = _sb(view)
    assert sb.maximum() > 0, "test setup: content must overflow the viewport"
    dist = sb.maximum() - sb.value()
    assert dist <= 2, f"expected bottom, landed {dist}px from bottom (max={sb.maximum()})"
    print("PASS test_force_entry_on_done_session_lands_at_bottom")


def test_doc_size_change_snaps_when_pending_even_if_tailing_off(app):
    """documentSizeChanged must snap to bottom during the entry settle window
    even with tailing off — this is what corrects setHtml's stale maximum."""
    view = _new_view(app)
    view._reply_browser.setHtml(_TALL_HTML)
    _settle(app)
    sb = _sb(view)
    sb.setValue(0)  # simulate the stale snap having landed at the top
    app.processEvents()
    assert sb.value() == 0

    view._tailing = False
    view._force_bottom_pending = True
    view._on_doc_size_changed(None)
    assert sb.value() == sb.maximum(), "pending window should snap to bottom"
    print("PASS test_doc_size_change_snaps_when_pending_even_if_tailing_off")


def test_doc_size_change_ignored_when_idle(app):
    """With tailing off AND no pending window, layout changes must NOT move the
    operator's scroll position (respect their read position on done sessions)."""
    view = _new_view(app)
    view._reply_browser.setHtml(_TALL_HTML)
    _settle(app)
    sb = _sb(view)
    sb.setValue(0)
    app.processEvents()

    view._tailing = False
    view._force_bottom_pending = False
    view._on_doc_size_changed(None)
    assert sb.value() == 0, "idle view should not be yanked to the bottom"
    print("PASS test_doc_size_change_ignored_when_idle")


def test_user_scroll_away_cancels_pending_force(app):
    """If the operator scrolls up during the settle window, cancel the pending
    force so we don't yank them back down."""
    view = _new_view(app)
    view._reply_browser.setHtml(_TALL_HTML)
    _settle(app)
    sb = _sb(view)

    view._force_bottom_pending = True
    sb.setValue(sb.maximum() // 3)  # operator scrolls up, away from bottom
    view._update_tailing_from_scroll()

    assert view._force_bottom_pending is False, "scroll-away must clear pending force"
    assert view._tailing is False
    print("PASS test_user_scroll_away_cancels_pending_force")


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    failures = 0
    for fn in (
        test_force_entry_on_done_session_lands_at_bottom,
        test_doc_size_change_snaps_when_pending_even_if_tailing_off,
        test_doc_size_change_ignored_when_idle,
        test_user_scroll_away_cancels_pending_force,
    ):
        try:
            fn(app)
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
