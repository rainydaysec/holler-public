"""Rotating-file logging configuration.

When holler runs under `pythonw.exe` (autostart shortcut), stdout/stderr are
discarded — without a file sink we lose all diagnostic logs. This module sets
up a rotating handler at `%LOCALAPPDATA%\\holler\\logs\\holler.log` plus a
stderr handler that's still useful for `holler.cmd` console launches.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def configure() -> None:
    from holler.appdir import app_data_dir
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "holler.log"

    fmt = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    # Drop any handlers that an earlier configure() call (or basicConfig) installed.
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)

    # Console handler — works for `holler.cmd` launches; harmless under pythonw.
    try:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        stream_handler.setLevel(logging.INFO)
        root.addHandler(stream_handler)
    except Exception:
        pass

    root.setLevel(logging.INFO)
    # Tame the SDK transport's chatter at INFO; we only care about WARNING+.
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
