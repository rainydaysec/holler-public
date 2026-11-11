"""Phase 2 runner: PTT triggers active-window screenshot + scribble overlay.

On release, composites the snapshot + strokes and writes:
    %LOCALAPPDATA%\\holler\\captures\\last.png
No audio, no Claude. Validates the visual pipeline.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from holler.composite import compose
from holler.config import load as load_config
from holler.hotkey import HotkeyListener
from holler.overlay import OverlayWindow
from holler.screen import capture_active_window, set_dpi_awareness


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    set_dpi_awareness()
    cfg = load_config()

    # Qt must own the main thread for the overlay to paint.
    app = QtWidgets.QApplication(sys.argv)
    overlay = OverlayWindow(stroke_color=cfg.stroke_color, stroke_width=cfg.stroke_width)

    # State shared between hotkey thread and Qt thread.
    pending = {"snapshot": None}

    def on_press_qt() -> None:
        snap = capture_active_window()
        if snap is None:
            print("[phase2] no foreground window; skipping")
            return
        pending["snapshot"] = snap
        overlay.show_for(snap.image, snap.rect)
        print(f"[phase2] overlay shown ({snap.title!r}) — scribble away")

    def on_release_qt() -> None:
        snap = pending.pop("snapshot", None)
        overlay.hide()
        if snap is None:
            return
        strokes = overlay.get_strokes()
        out = compose(snap.image, strokes, cfg.stroke_color, cfg.stroke_width)
        out_path: Path = cfg.captures_dir / "last.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(out_path)
        print(f"[phase2] composite saved -> {out_path} ({len(strokes)} strokes)")

    # Hotkey callbacks fire on a non-Qt thread — marshal back via a queued signal.
    invoker = _InvokeOnUiThread()
    invoker.requested_press.connect(on_press_qt)
    invoker.requested_release.connect(on_release_qt)

    listener = HotkeyListener(
        cfg.hotkey,
        on_press=lambda: invoker.requested_press.emit(),
        on_release=lambda: invoker.requested_release.emit(),
    )
    listener.start()
    print(f"[phase2] hold {cfg.hotkey} on any window to capture+scribble. Ctrl+C to quit.")

    try:
        sys.exit(app.exec())
    finally:
        listener.stop()


class _InvokeOnUiThread(QtCore.QObject):
    requested_press = QtCore.Signal()
    requested_release = QtCore.Signal()


if __name__ == "__main__":
    main()
