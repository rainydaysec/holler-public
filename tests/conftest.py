"""Shared pytest fixtures.

QT_QPA_PLATFORM and QT_SCALE_FACTOR must both be set before any PySide6 import
so Qt tests run headless at a fixed scale.
"""
from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Pinned rather than setdefault: the A+/A− buttons relaunch holler with
# QT_SCALE_FACTOR exported, so a suite run from inside holler inherits whatever
# text size the operator last picked. Qt reads it once at QApplication
# construction, and it scales both things this suite asserts on — grabbed image
# size, and the offscreen plugin's virtual screen (800x800 at 1.0, 571x571 at
# 1.4). Inheriting it fails every visual golden on size and clamps the geometry
# tests. Goldens are recorded at 1.0.
os.environ["QT_SCALE_FACTOR"] = "1.0"
# Never read or rewrite the operator's real Claude login (holler/accounts.py);
# the Sessions header polls it on construction.
os.environ["HOLLER_ACCOUNTS_DISABLED"] = "1"

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def app():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


# ── Fail on exceptions swallowed by Qt ──────────────────────────────────────
#
# An exception raised inside a Qt slot / event handler / timer callback never
# propagates to the test: PySide routes it to sys.excepthook (which by default
# just prints it) and carries on. That is how a real crash slipped past a fully
# green suite. Every test therefore runs with a recording excepthook, and any
# exception that reaches it fails the test with the full traceback.
#
# A test that deliberately installs its own excepthook (e.g. via
# monkeypatch.setattr(sys, "excepthook", ...)) takes precedence for its own
# duration — those exceptions are then the test's responsibility.

_HITS_ATTR = "_qt_excepthook_hits"


def _format_hits(hits: list[str]) -> str:
    sep = "\n" + "-" * 70 + "\n"
    return (
        f"{len(hits)} exception(s) reached sys.excepthook (swallowed by Qt — "
        f"raised in a slot, event handler or timer callback):\n"
        + sep
        + sep.join(hits)
    )


@pytest.fixture(autouse=True)
def _fail_on_qt_swallowed_exceptions(request):
    hits: list[str] = []
    setattr(request.node, _HITS_ATTR, hits)

    def _hook(exc_type, exc, tb):
        hits.append("".join(traceback.format_exception(exc_type, exc, tb)))

    prev = sys.excepthook
    sys.excepthook = _hook
    try:
        yield
    finally:
        # Exceptions raised during other fixtures' teardown (window close,
        # deleteLater, final processEvents) land here — this fixture is
        # set up first, so it is torn down last.
        if sys.excepthook is _hook:
            sys.excepthook = prev
    if hits:
        msg = _format_hits(list(hits))
        hits.clear()
        pytest.fail(msg, pytrace=False)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    # Report exceptions swallowed during the test body as a test FAILURE (not a
    # teardown error) when the body itself otherwise passed.
    result = yield
    hits = getattr(item, _HITS_ATTR, None)
    if hits:
        msg = _format_hits(list(hits))
        hits.clear()
        pytest.fail(msg, pytrace=False)
    return result



@pytest.fixture(autouse=True)
def _restore_visual_app_style(request):
    """The visual harness applies holler's app style once and keeps it across
    golden scenarios (re-applying per scenario re-polished every live window
    and made the suite ~8× slower). Any other test that borrowed the harness
    must not leak that style into the tests after it."""
    yield
    if "tests/visual/" in str(request.node.fspath).replace("\\", "/"):
        return
    scenarios = sys.modules.get("tests.visual.scenarios")
    if scenarios is not None:
        scenarios.restore_app_style()


@pytest.fixture(autouse=True)
def _reset_toast_stack():
    """Defense in depth: no toast card (or queued toast) outlives the test
    that posted it. A card left up keeps a live auto-hide timer and anchors
    the stacking position of later tests' toasts. (Product-side lifetime
    safety is holler.panel._ToastStack._retire; tests/test_toast_lifetime.py
    checks it without this fixture.)"""
    yield
    panel = sys.modules.get("holler.panel")
    stack = getattr(panel, "_STACK", None)
    if stack is None:
        return
    stack.queue.clear()
    for card in list(stack.visible):
        if panel._alive(card):
            card._timer.stop()
            card.hide()
    stack.visible.clear()
    # hide() runs to completion above, so dropping the references now is safe.
    stack._release_retired()


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    """Run the deletions tests asked for. Fixtures close windows with
    deleteLater(), but pytest never returns to an event loop, so without this
    those windows — and the app-wide event filters they own — live for the
    whole session, piling up (hundreds) and slowing every later event."""
    yield
    from PySide6 import QtCore, QtWidgets

    if QtWidgets.QApplication.instance() is not None:
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
