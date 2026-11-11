"""Pick which `claude` CLI binary the Agent SDK spawns.

The SDK prefers the CLI bundled inside its wheel, and that copy only moves when
the `claude-agent-sdk` package is upgraded. Newly-shipped models are gated on a
minimum CLI version, so a stale bundle fails every turn on the new model with
`API Error: 400 Claude Code 2.1.139 does not support this model; version
2.1.280 or newer is required` — even when the operator's own `claude` (the one
`claude update` maintains) is perfectly current.

`cli_path()` compares the bundled CLI against the installed ones and returns the
newest, so Holler serves whatever the operator's terminal Claude Code can.
Every SDK call site passes it as `ClaudeAgentOptions(cli_path=...)`.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PROBE_TIMEOUT_SECS = 10.0

# A Finder-launched .app gets a minimal PATH (no Homebrew, no ~/.local/bin), so
# `shutil.which` alone would miss the operator's install. Same list the SDK's
# own fallback search uses, plus Homebrew.
_KNOWN_LOCATIONS = (
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
    Path.home() / ".local/bin/claude",
    Path.home() / ".claude/local/claude",
    Path.home() / ".npm-global/bin/claude",
    Path.home() / "node_modules/.bin/claude",
    Path.home() / ".yarn/bin/claude",
)

_lock = threading.Lock()
_cached: Optional[tuple[Optional[str], Optional[tuple[int, int, int]]]] = None


def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """`"2.1.285 (Claude Code)"` → `(2, 1, 285)`; None when absent."""
    m = _VERSION.search(text or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def format_version(v: Optional[tuple[int, int, int]]) -> str:
    return ".".join(map(str, v)) if v else "unknown"


def _bundled_cli() -> Optional[Path]:
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    p = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    return p if p.is_file() else None


def _bundled_version() -> Optional[tuple[int, int, int]]:
    # Read the version the SDK records rather than exec'ing the bundle.
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except ImportError:
        return None
    return parse_version(__cli_version__)


def _probe(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        out = subprocess.run(
            [str(path), "--version"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.info("claude CLI at %s not usable: %s", path, e)
        return None
    return parse_version(out.stdout) if out.returncode == 0 else None


def _candidates() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    which = shutil.which("claude")
    for p in ([Path(which)] if which else []) + list(_KNOWN_LOCATIONS):
        try:
            real = p.resolve()
        except OSError:
            continue
        if real in seen or not p.is_file():
            continue
        seen.add(real)
        out.append(p)
    return out


def _select() -> tuple[Optional[str], Optional[tuple[int, int, int]]]:
    best_path: Optional[str] = None
    best_ver = None
    bundled = _bundled_cli()
    if bundled is not None:
        best_path, best_ver = str(bundled), _bundled_version()
    for p in _candidates():
        v = _probe(p)
        if v is not None and (best_ver is None or v > best_ver):
            best_path, best_ver = str(p), v
    if best_path is None:
        log.warning("no claude CLI found; the SDK will run its own search")
    else:
        log.info("using claude CLI %s at %s", format_version(best_ver), best_path)
    return best_path, best_ver


def _selected() -> tuple[Optional[str], Optional[tuple[int, int, int]]]:
    global _cached
    with _lock:
        if _cached is None:
            _cached = _select()
        return _cached


def cli_path() -> Optional[str]:
    """Path of the newest available `claude` CLI, or None to let the SDK search.

    Probed once per process (a `--version` exec per candidate); call `reset()`
    after the operator updates Claude Code so the next turn re-probes.
    """
    return _selected()[0]


def cli_version() -> Optional[tuple[int, int, int]]:
    """Version of the CLI `cli_path()` returns, when known."""
    return _selected()[1]


def reset() -> None:
    global _cached
    with _lock:
        _cached = None


# `API Error: 400 Claude Code 2.1.139 does not support this model; version
# 2.1.280 or newer is required. ...`
_TOO_OLD = re.compile(
    r"Claude Code (\d+\.\d+\.\d+) does not support this model; "
    r"version (\d+\.\d+\.\d+) or newer is required"
)


def parse_too_old(text: str) -> Optional[tuple[str, str]]:
    """`(running_version, required_version)` from a CLI-too-old API error."""
    m = _TOO_OLD.search(text or "")
    return (m[1], m[2]) if m else None
