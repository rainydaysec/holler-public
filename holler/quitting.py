"""Quits holler initiates itself (relaunch, confirmed ⋯ → Quit, panel Quit).

The system-quit guard in runners/full.py confirms ⌘Q / Dock → Quit when turns
are in flight. On Qt 6 those and `QApplication.quit()` all reach the app as a
spontaneous QEvent.Quit, so they can't be told apart by the event — holler's
own quits mark themselves here first and the guard lets a marked quit through.
"""

from __future__ import annotations

from PySide6 import QtWidgets

_PROP = "hollerInternalQuit"


def quit_app() -> None:
    """Quit without the guard's prompt (caller already confirmed, or must not
    be cancelled — a relaunch has already spawned its replacement)."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    app.setProperty(_PROP, True)
    app.quit()


def consume_internal_quit() -> bool:
    """True (once) if the pending quit was started by `quit_app()`."""
    app = QtWidgets.QApplication.instance()
    if app is None or not app.property(_PROP):
        return False
    app.setProperty(_PROP, False)
    return True
