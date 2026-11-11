"""Toast cards must outlive their own hide, even once their host is gone.

Regression: the full suite segfaulted in QWidgetPrivate::hide_helper, called
from a toast's auto-hide QTimer. A Toast whose host window (panel / Sessions
window) had been dropped was referenced only by the app-wide toast stack;
its timer hid it, Toast.hideEvent told the stack it closed, the stack dropped
that last reference — and Python deleted the C++ widget while Qt was still
inside hide_helper on it. The same happens in the app whenever a toast's host
is destroyed while the toast is up.

Each scenario runs in a subprocess so a segfault is a test failure, not a
suite crash. Run without the conftest toast-reset fixture on purpose: the
product fix must stand on its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = textwrap.dedent(
    """
    import faulthandler, gc, sys, weakref
    faulthandler.enable()
    from PySide6 import QtCore, QtWidgets
    app = QtWidgets.QApplication([])
    from holler import panel

    mode = sys.argv[1]
    refs = []

    def host_goes_away():
        # Like Panel / SessionsWindow: the host holds the owner Toast and
        # posts through it, then the host (and its reference) is gone.
        owner = panel.Toast()
        refs.append(weakref.ref(owner))
        owner.show_message("Copied", (800, 600))
        if mode == "spares":
            # Two more cards come from the owner's spares (owner <-> spare
            # reference cycle): all must hide cleanly and then be freed.
            owner.show_message("Archived 2 sessions", (800, 600), undo_callback=lambda: None)
            owner.show_message("Model failed to load", (800, 600))
            refs.extend(weakref.ref(s) for s in owner._spares)

    host_goes_away()
    assert len(panel._STACK.visible) == (3 if mode == "spares" else 1)
    for card in list(panel._STACK.visible):
        card._timer.start(1)  # auto-hide now
    del card
    end = QtCore.QDeadlineTimer(400)
    while not end.hasExpired():
        app.processEvents()
    gc.collect()
    app.processEvents()
    gc.collect()
    assert panel._STACK.visible == [], panel._STACK.visible
    # The orphaned cards were released, not leaked.
    assert all(r() is None for r in refs), [r() for r in refs]
    print("TOAST-LIFETIME-OK")
    """
)


@pytest.mark.parametrize("mode", ["owner", "spares"])
def test_orphaned_toast_auto_hide_does_not_crash(mode, tmp_path):
    script = tmp_path / "toast_lifetime.py"
    script.write_text(_SCRIPT)
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(script), mode],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"exit {proc.returncode}:\n{out[-4000:]}"
    assert "TOAST-LIFETIME-OK" in proc.stdout, out[-4000:]
