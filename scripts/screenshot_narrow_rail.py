"""Render SessionsWindow headlessly, drag splitter narrow, and grab a PNG.

Used to verify the left rail can now shrink past the previous ~280 px floor.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtWidgets  # noqa: E402

from holler.jobs import JobStore  # noqa: E402
from holler.sessions_window import SessionsWindow  # noqa: E402
from holler.store import Store  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

tmp = Path(tempfile.mkdtemp(prefix="holler-rail-shot-"))
store = Store(tmp / "holler.db")
for i in range(8):
    store.create_session(
        model="claude-opus-4-8",
        transcript=f"Sample transcript body {i+1}. Lorem ipsum dolor sit amet.",
        screenshot_path=None,
        title=f"Sample session {i+1}",
        profile="ui-ux" if i % 2 else "engineering",
    )
job_store = JobStore()

w = SessionsWindow(store=store, job_store=job_store, captures_dir=tmp / "captures", repo_cwd=tmp)
w.resize(1140, 1400)
w.show()
app.processEvents()

# Drag splitter narrow — first force user_set so reapply_fraction can't undo us.
w._splitter_user_set = True
total = w._splitter.width()
narrow = 170
w._splitter.setSizes([narrow, max(1, total - narrow)])
app.processEvents()

out = ROOT / "scripts" / "rail_narrow.png"
pix = w.grab()
pix.save(str(out), "PNG")
print(f"saved: {out}  sizes={w._splitter.sizes()}  window={w.width()}x{w.height()}")
