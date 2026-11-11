"""Which build is running, and is a newer one waiting?

The stable install (~/holler-stable) is refreshed by scripts/promote.py, which
stamps it with ``RELEASE.json``. This module compares the build this process
started from against what is on disk now, reads the promoter's progress
(``<app data>/promote.json``) and counts uncommitted edits in the source
checkout — work that will never ship until it is committed. The header turns
that into one chip, so "my restart didn't pick it up" is visible instead of
silent.

Pure functions plus ``snapshot()``; no Qt. ``snapshot()`` shells out to git, so
call it off the GUI thread.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

INSTALL_DIR = Path(__file__).resolve().parent.parent


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# The build this process was started from — read once, at import.
RUNNING: dict = _read_json(INSTALL_DIR / "RELEASE.json")


@dataclass(frozen=True)
class Status:
    # "dev" (not a promoted install) | "current" | "update_ready" | "testing"
    # | "failed"
    state: str
    running_sha: str = ""
    available_sha: str = ""
    subject: str = ""
    log: str = ""
    uncommitted: int = 0
    source: str = ""

    def chip_text(self) -> str:
        if self.state == "update_ready":
            return f"↻ Update ready · {self.available_sha[:7]} — restart"
        if self.state == "testing":
            return f"Testing update {self.available_sha[:7]}…"
        if self.state == "failed":
            return f"⚠ Update {self.available_sha[:7]} failed tests"
        if self.uncommitted:
            n = self.uncommitted
            return f"{n} uncommitted change{'s' if n != 1 else ''}"
        return ""

    def tooltip(self) -> str:
        lines = []
        if self.running_sha:
            lines.append(f"Running build {self.running_sha[:7]}.")
        if self.state == "update_ready":
            lines.append(f"A tested build is installed: {self.available_sha[:7]} — {self.subject}. "
                         "Click (or Restart) to switch to it.")
        elif self.state == "testing":
            lines.append(f"Testing {self.available_sha[:7]} — {self.subject} before installing it.")
        elif self.state == "failed":
            lines.append(f"{self.available_sha[:7]} — {self.subject} failed its tests, so it was not "
                         f"installed. Click to open the log ({self.log}).")
        if self.uncommitted:
            lines.append(f"{self.uncommitted} file(s) in {self.source or 'the source checkout'} have "
                         "uncommitted changes. Only committed work is tested and installed.")
        return "\n".join(lines)


def _uncommitted(source: str) -> int:
    if not source or not Path(source, ".git").exists():
        return 0
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        out = subprocess.run(
            ["git", "-C", source, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=5, env=env,
        ).stdout
        return sum(1 for ln in out.splitlines() if ln.strip())
    except Exception:
        return 0


def evaluate(running: dict, on_disk: dict, promote: dict, uncommitted: int) -> Status:
    """Pure decision: what the header should say."""
    run_sha = str(running.get("sha") or "")
    if not run_sha:
        return Status("dev", uncommitted=uncommitted, source=str(on_disk.get("source") or ""))
    disk_sha = str(on_disk.get("sha") or run_sha)
    source = str(on_disk.get("source") or running.get("source") or "")
    p_state = str(promote.get("state") or "")
    p_sha = str(promote.get("sha") or "")
    if p_state == "testing" and p_sha and p_sha != disk_sha:
        return Status("testing", run_sha, p_sha, str(promote.get("subject") or ""),
                      str(promote.get("log") or ""), uncommitted, source)
    if p_state == "failed" and p_sha and p_sha != disk_sha:
        return Status("failed", run_sha, p_sha, str(promote.get("subject") or ""),
                      str(promote.get("log") or ""), uncommitted, source)
    if disk_sha != run_sha:
        return Status("update_ready", run_sha, disk_sha, str(on_disk.get("subject") or ""),
                      "", uncommitted, source)
    return Status("current", run_sha, run_sha, str(running.get("subject") or ""),
                  "", uncommitted, source)


def snapshot(app_data: Optional[Path] = None) -> Status:
    if app_data is None:
        from holler.appdir import app_data_dir
        app_data = app_data_dir()
    on_disk = _read_json(INSTALL_DIR / "RELEASE.json")
    source = str(on_disk.get("source") or RUNNING.get("source") or "")
    return evaluate(RUNNING, on_disk, _read_json(Path(app_data) / "promote.json"),
                    _uncommitted(source))
