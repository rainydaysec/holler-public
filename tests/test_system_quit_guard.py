"""⌘Q / Dock → Quit must confirm when turns are in flight, while holler's own
quits (relaunch, confirmed ⋯ → Quit, panel Quit) never prompt. On Qt 6 both
arrive as a *spontaneous* QEvent.Quit — the event can't tell them apart — so
internal quits mark themselves via `holler.quitting.quit_app()`.
"""
import pytest
from PySide6 import QtCore, QtWidgets

from holler import quitting
from holler.runners.full import _block_system_quit


class _Win:
    def __init__(self, ok):
        self.ok, self.asked = ok, 0

    def _confirm_quit_if_busy(self):
        self.asked += 1
        return self.ok


@pytest.fixture(autouse=True)
def _no_stale_internal_quit_mark(app):
    # Other tests call quit_app() with QApplication.quit stubbed out, which
    # leaves the one-shot mark set on the shared QApplication.
    quitting.consume_internal_quit()
    yield
    quitting.consume_internal_quit()


def _quit_event():
    return QtCore.QEvent(QtCore.QEvent.Quit)


def test_system_quit_cancelled_by_operator_is_blocked(app):
    win = _Win(ok=False)
    assert _block_system_quit(_quit_event(), win) is True
    assert win.asked == 1


def test_system_quit_confirmed_proceeds(app):
    assert _block_system_quit(_quit_event(), _Win(ok=True)) is False


def test_internal_quit_never_prompts_and_is_consumed_once(app, monkeypatch):
    # quit_app() marks the quit, then calls QApplication.quit(); don't let the
    # test process actually quit.
    monkeypatch.setattr(QtWidgets.QApplication, "quit", staticmethod(lambda: None))
    win = _Win(ok=False)
    quitting.quit_app()
    assert _block_system_quit(_quit_event(), win) is False
    assert win.asked == 0
    # The mark is one-shot: a later ⌘Q prompts again.
    assert _block_system_quit(_quit_event(), win) is True
    assert win.asked == 1


def test_other_events_and_no_window_pass_through(app):
    assert _block_system_quit(QtCore.QEvent(QtCore.QEvent.Close), _Win(ok=False)) is False
    assert _block_system_quit(_quit_event(), None) is False


def test_confirm_failure_allows_quit(app):
    class _Boom:
        def _confirm_quit_if_busy(self):
            raise RuntimeError("x")
    assert _block_system_quit(_quit_event(), _Boom()) is False
