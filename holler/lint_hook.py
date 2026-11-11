"""Advisory lint-on-edit logic — the single source of truth for both:

  - the in-process SDK ``PostToolUse`` hook registered in ``sdk_session.py``
    (covers every holler session), and
  - the standalone ``~/.claude/hooks/lint_on_edit.py`` shim (covers standalone
    Claude Code CLI sessions, which don't load the SDK-registered hook).

It runs the project's *own* linter against ONLY the file that was just edited,
discovered project-locally, and returns any findings as non-blocking advisory
context. It never blocks or reverts an edit; on any internal error it stays
silent. Pure stdlib on purpose — the CLI shim imports it with no Qt/SDK cost.

Design constraints (this runs on every edit, in every project):
  - Prefer the linter the project itself provides (walk up for
    .venv/venv/node_modules — and, inside a git worktree, also from the same
    spot in the main checkout, since worktrees never carry a .venv). Never
    install anything into the project.
  - Python falls back to a ruff on PATH, then `python -m ruff` from Holler's
    own venv, so repos without a project-local ruff still get linted.
  - JS: ESLint only when the project provides one AND its config covers the
    file; otherwise plain .js/.mjs/.cjs get a `node --check` syntax pass.
  - Lint the single edited file, not the whole tree — cheap and low-noise.
  - Hard timeout so a hung linter can't stall a session.
  - Cap output so a wall of pre-existing violations can't flood context.

Must stay importable on the system Python 3.9 (the CLI shim runs under
/usr/bin/python3).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, List, Optional, Tuple

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
_PY_EXT = {".py"}
_JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
# Plain JavaScript node can parse for a syntax check (no JSX/TS).
_NODE_CHECK_EXT = {".js", ".mjs", ".cjs"}
_TIMEOUT_S = 15
_MAX_OUTPUT_CHARS = 4000


def _find_upwards(start_dir: str, rel_paths) -> Optional[str]:
    """Walk from start_dir to filesystem root; return the first existing
    candidate among rel_paths, or None."""
    d = os.path.abspath(start_dir)
    while True:
        for rel in rel_paths:
            cand = os.path.join(d, rel)
            if os.path.exists(cand):
                return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


# Project-local linter locations, POSIX and Windows layouts. On Windows a venv
# puts executables in `Scripts\` with an `.exe` suffix, and npm writes
# `.cmd` shims into `node_modules\.bin\`.
_RUFF_CANDIDATES = [
    os.path.join(venv, *tail)
    for venv in (".venv", "venv", "env")
    for tail in (("bin", "ruff"), ("Scripts", "ruff.exe"))
]
_ESLINT_CANDIDATES = [
    os.path.join("node_modules", ".bin", name)
    for name in (("eslint.cmd", "eslint") if os.name == "nt" else ("eslint", "eslint.cmd"))
]


def _project_root_for(tool_path: str, depth: int) -> str:
    """Directory `depth` levels above the tool — the project that owns it."""
    d = os.path.dirname(tool_path)
    for _ in range(depth):
        d = os.path.dirname(d)
    return d


# Directories a GUI-launched app's thin PATH often lacks.
_EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.cargo/bin"),
)


def _which(name: str) -> Optional[str]:
    path = os.environ.get("PATH", "")
    extra = os.pathsep.join(d for d in _EXTRA_BIN_DIRS if d)
    return shutil.which(name, path=path + os.pathsep + extra if path else extra)


def _git_out(args: List[str], cwd: str) -> Optional[str]:
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _main_checkout_mirror(start_dir: str) -> Optional[str]:
    """Same relative dir in the main checkout when `start_dir` is inside a
    linked git worktree (worktrees never contain the untracked .venv /
    node_modules); None otherwise."""
    top = _git_out(["rev-parse", "--show-toplevel"], start_dir)
    common = _git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], start_dir)
    if not top or not common:
        return None
    main_root = os.path.dirname(os.path.abspath(common))
    top = os.path.abspath(top)
    if os.path.realpath(main_root) == os.path.realpath(top):
        return None  # already in the main checkout
    rel = os.path.relpath(os.path.abspath(start_dir), top)
    return os.path.normpath(os.path.join(main_root, rel))


def _find_project_tool(start: str, candidates) -> Optional[str]:
    """Project-local tool: walk up from `start`, then from the main-checkout
    mirror of `start` when it lives in a git worktree."""
    found = _find_upwards(start, candidates)
    if found:
        return found
    mirror = _main_checkout_mirror(start)
    return _find_upwards(mirror, candidates) if mirror else None


def _project_root_of(start: str) -> str:
    return _git_out(["rev-parse", "--show-toplevel"], start) or start


_HOLLER_RUFF_CACHE: dict = {}


def _holler_python_ruff() -> Optional[List[str]]:
    """`<python> -m ruff` from Holler's own environment, if ruff is installed
    there (checked once per process)."""
    if "argv" in _HOLLER_RUFF_CACHE:
        return _HOLLER_RUFF_CACHE["argv"]
    holler_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pythons = [sys.executable]
    for venv in (".venv", "venv"):
        for tail in (("bin", "python"), ("Scripts", "python.exe")):
            pythons.append(os.path.join(holler_root, venv, *tail))
    argv = None
    for py in pythons:
        if not py or not os.path.exists(py):
            continue
        try:
            p = subprocess.run(
                [py, "-m", "ruff", "--version"], capture_output=True, text=True, timeout=10,
            )
        except Exception:
            continue
        if p.returncode == 0:
            argv = [py, "-m", "ruff"]
            break
    _HOLLER_RUFF_CACHE["argv"] = argv
    return argv


def _resolve_ruff(start: str) -> Optional[Tuple[List[str], str, str]]:
    """Ruff, in order: project venv (incl. the main checkout's, from a
    worktree) → `ruff` on PATH → `python -m ruff` from Holler's venv."""
    ruff = _find_project_tool(start, _RUFF_CANDIDATES)
    if ruff:
        return ([ruff, "check"], "ruff", _project_root_for(ruff, 2))
    on_path = _which("ruff")
    if on_path:
        return ([on_path, "check"], "ruff", _project_root_of(start))
    py_ruff = _holler_python_ruff()
    if py_ruff:
        return (py_ruff + ["check"], "ruff", _project_root_of(start))
    return None


def _resolve_linter(file_path: str):
    """Return (argv_prefix, kind, cwd) for the file's primary linter, or None.

    `cwd` is the project root that owns the linter (the directory containing
    `.venv` / `node_modules`): eslint resolves its config and plugins relative
    to its cwd, so running it from the host process's cwd fails or picks up
    the wrong config.
    """
    ext = os.path.splitext(file_path)[1].lower()
    start = os.path.dirname(os.path.abspath(file_path))
    if ext in _PY_EXT:
        return _resolve_ruff(start)
    elif ext in _JS_EXT:
        eslint = _find_project_tool(start, _ESLINT_CANDIDATES)
        if eslint:
            return ([eslint], "eslint", _project_root_for(eslint, 2))
    return None


# ESLint's "this file isn't covered by any config" warning (exit 0).
# A project's flat config may only cover **/*.{ts,tsx}, so legacy .js files land here.
_ESLINT_NOT_COVERED = re.compile(
    r"File ignored|no matching configuration|outside of base path", re.IGNORECASE,
)

# Top-level ES-module syntax → check the file as a module, else as a classic
# script (browser `<script defer>` files must not be held to strict mode).
_ESM_SYNTAX = re.compile(r"^\s*(import\s*[\w{*'\"]|export\s)", re.MULTILINE)


def _node_check(file_path: str) -> Optional[Tuple[int, str]]:
    """`node --check` syntax pass. Returns (returncode, output) or None when
    node is unavailable. Checks a temp copy whose extension pins the parse
    mode (.mjs / .cjs), so the nearest package.json's "type" can't flip a
    classic script into strict-mode module parsing."""
    node = _which("node")
    if not node:
        return None
    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return None
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".js":
        ext = ".mjs" if _ESM_SYNTAX.search(src) else ".cjs"
    fd, tmp = tempfile.mkstemp(suffix=ext, prefix="holler-lint-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(src)
        proc = subprocess.run(
            [node, "--check", tmp], capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # Report against the real file (node prints the resolved temp path),
        # and drop node's internal stack frames — only the location, the
        # offending line and the SyntaxError are useful.
        real = os.path.abspath(file_path)
        for alias in sorted({os.path.realpath(tmp), tmp}, key=len, reverse=True):
            out = out.replace(alias, real)
        kept = [
            ln for ln in out.splitlines()
            if not ln.lstrip().startswith("at ") and not ln.startswith("Node.js v")
        ]
        return proc.returncode, "\n".join(kept).strip()
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _format_context(file_path: str, kind: str, output: str) -> str:
    output = output.strip()
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n… (truncated)"
    return (
        "Advisory lint ({kind}) flagged the file you just edited: {path}\n"
        "This is a non-blocking warning — the edit was applied. Fix the issues "
        "you introduced (ignore pre-existing ones outside your change).\n\n"
        "{out}"
    ).format(kind=kind, path=file_path, out=output)


def lint_file(file_path: str) -> Optional[str]:
    """Lint a single file with its project-local linter.

    Returns an advisory context string if the linter reported issues, or None
    for the clean / no-linter / not-applicable / error cases. Never raises.
    """
    try:
        if not file_path or not os.path.isfile(file_path):
            return None
        ext = os.path.splitext(file_path)[1].lower()
        resolved = _resolve_linter(file_path)
        if resolved is not None:
            argv_prefix, kind, cwd = resolved
            try:
                proc = subprocess.run(
                    argv_prefix + [os.path.abspath(file_path)],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT_S,
                )
            except Exception:
                proc = None
            if proc is not None:
                combined = (proc.stdout or "") + (proc.stderr or "")
                usable = not (
                    kind == "eslint"
                    # exit 2 = config/plugin crash: the repo's ESLint isn't
                    # working, so fall through to the syntax floor.
                    and (proc.returncode == 2 or _ESLINT_NOT_COVERED.search(combined))
                )
                if usable:
                    if proc.returncode == 0 or not combined.strip():
                        return None  # clean → stay silent
                    return _format_context(file_path, kind, combined)
            elif kind != "eslint":
                return None
        if ext in _NODE_CHECK_EXT:
            checked = _node_check(file_path)
            if checked is None:
                return None
            rc, out = checked
            if rc == 0 or not out.strip():
                return None
            return _format_context(file_path, "node --check (syntax)", out)
        return None
    except Exception:
        return None


# --- SDK PostToolUse hook entry point (holler sessions) ---------------------

async def post_tool_use_hook(
    input_data: dict,
    tool_use_id: Optional[str],
    context: Any,
) -> dict:
    """claude-agent-sdk PostToolUse callback. Returns {} for no-op, or a
    hookSpecificOutput carrying advisory lint findings as additionalContext.

    Linting runs in a thread so a blocking linter never stalls the event loop.
    """
    try:
        if input_data.get("tool_name") not in _EDIT_TOOLS:
            return {}
        file_path = (input_data.get("tool_input") or {}).get("file_path")
        if not file_path:
            return {}
        msg = await asyncio.to_thread(lint_file, file_path)
        if not msg:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": msg,
            }
        }
    except Exception:
        return {}


# --- CLI entry point (stdin JSON -> stdout JSON) ----------------------------

def main() -> int:
    """Read a PostToolUse payload on stdin; print a hookSpecificOutput JSON on
    stdout if the edited file has lint findings. Always exits 0."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") not in _EDIT_TOOLS:
        return 0
    file_path = (data.get("tool_input") or {}).get("file_path")
    if not file_path:
        return 0
    msg = lint_file(file_path)
    if not msg:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
