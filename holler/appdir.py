"""Platform-aware application data directory.

Holler historically hard-coded `%LOCALAPPDATA%\\holler` (i.e.
`~/AppData/Local/holler`). That path only exists on Windows. This module
returns the right per-user app-data root for the host OS so config, state,
logs, captures, profiles and the SQLite DB all land somewhere sane:

    Windows   ~/AppData/Local/holler
    macOS     ~/Library/Application Support/holler
    Linux     $XDG_DATA_HOME/holler  (default ~/.local/share/holler)

The directory is created on first access. Callers that want a subdir should
use `app_data_dir() / "subdir"` and mkdir it themselves (most already do).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    """Return holler's per-user data directory, creating it if needed."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    d = base / "holler"
    d.mkdir(parents=True, exist_ok=True)
    return d
