"""Human-readable summaries of tool calls for the live ticker.

`summarize(name, input_obj)` returns a short string like:
    Read("...auth.py")          -> "Reading auth.py"
    Bash("cd && pytest -x")     -> "Running pytest -x"
    Edit("...layout.tsx", ...)  -> "Editing layout.tsx"
    Task(subagent="Plan", ...)  -> "Delegating to Plan: <prompt>"

`summarize_result(content, is_error)` returns a 200-char preview of a tool
result for tooltips and the detail window's tool log.
"""

from __future__ import annotations

import difflib
import re
import os
from typing import Any, Optional


def _basename(p: str) -> str:
    if not p:
        return p
    p = str(p).replace("\\", "/")
    return p.rsplit("/", 1)[-1] or p


def summarize(name: str, input_obj: Any, max_len: int = 80) -> str:
    """Return a single-line summary; never exceeds max_len chars."""
    if input_obj is None:
        input_obj = {}
    if not isinstance(input_obj, dict):
        return _truncate(f"{name}({_short_repr(input_obj)})", max_len)

    n = name or "Tool"

    if n in ("Read", "NotebookRead"):
        path = input_obj.get("file_path") or input_obj.get("notebook_path") or ""
        return _truncate(f"Reading {_basename(path) or '<file>'}", max_len)

    if n in ("Edit", "MultiEdit", "NotebookEdit"):
        path = input_obj.get("file_path") or input_obj.get("notebook_path") or ""
        return _truncate(f"Editing {_basename(path) or '<file>'}", max_len)

    if n == "Write":
        path = input_obj.get("file_path") or ""
        return _truncate(f"Writing {_basename(path) or '<file>'}", max_len)

    if n == "Bash":
        cmd = str(input_obj.get("command") or "")
        # strip leading "cd ... && "
        cmd = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd)
        cmd = cmd.split("\n", 1)[0]
        return _truncate(f"Running {cmd}", max_len)

    if n == "PowerShell":
        cmd = str(input_obj.get("command") or "")
        cmd = cmd.split("\n", 1)[0]
        return _truncate(f"Running {cmd}", max_len)

    if n == "Grep":
        q = input_obj.get("pattern") or ""
        return _truncate(f"Searching for “{q}”", max_len)

    if n == "Glob":
        q = input_obj.get("pattern") or ""
        return _truncate(f"Finding files {q}", max_len)

    if n in ("Task", "Agent"):
        sub = input_obj.get("subagent_type") or "agent"
        prompt = input_obj.get("prompt") or input_obj.get("description") or ""
        return _truncate(f"Delegating to {sub}: {prompt}", max_len)

    if n == "WebFetch":
        url = str(input_obj.get("url") or "")
        # take host only
        m = re.match(r"https?://([^/]+)", url)
        host = m.group(1) if m else url
        return _truncate(f"Fetching {host}", max_len)

    if n == "WebSearch":
        return _truncate(f"Searching the web: {input_obj.get('query', '')}", max_len)

    if n == "TodoWrite":
        return "Updating to-do list"

    if n == "Skill":
        skill = input_obj.get("skill") or ""
        return _truncate(f"Using skill {skill}", max_len)

    return _truncate(f"{n}({_short_repr(input_obj)})", max_len)


def summarize_args(name: str, input_obj: Any, max_len: int = 120) -> str:
    """Return the args portion of a Claude-Code style call, e.g.

        Read("...auth.py")          -> "holler/auth.py"
        Bash("cd && pytest -x")     -> "pytest -x"
        Grep(pattern, glob)         -> 'pattern: "foo", glob: "*.py"'
        Edit(path, ...)             -> "holler/layout.tsx"

    Pair with the tool name to mirror Claude Code's `● Name(args)` rendering.
    """
    if input_obj is None:
        input_obj = {}
    if not isinstance(input_obj, dict):
        return _truncate(_short_repr(input_obj), max_len)

    n = name or "Tool"

    if n in ("Read", "NotebookRead"):
        path = input_obj.get("file_path") or input_obj.get("notebook_path") or ""
        return _truncate(_short_path(path) or "<file>", max_len)

    if n in ("Edit", "MultiEdit", "NotebookEdit", "Write"):
        path = input_obj.get("file_path") or input_obj.get("notebook_path") or ""
        return _truncate(_short_path(path) or "<file>", max_len)

    if n in ("Bash", "PowerShell"):
        cmd = str(input_obj.get("command") or "")
        cmd = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd)
        cmd = cmd.split("\n", 1)[0]
        return _truncate(cmd, max_len)

    if n == "Grep":
        bits = []
        pat = input_obj.get("pattern")
        if pat is not None:
            bits.append(f'pattern: "{pat}"')
        for key in ("path", "glob", "type", "output_mode"):
            v = input_obj.get(key)
            if v:
                bits.append(f'{key}: "{v}"')
        return _truncate(", ".join(bits) if bits else "", max_len)

    if n == "Glob":
        pat = input_obj.get("pattern") or ""
        path = input_obj.get("path")
        if path:
            return _truncate(f'pattern: "{pat}", path: "{path}"', max_len)
        return _truncate(pat, max_len)

    if n in ("Task", "Agent"):
        sub = input_obj.get("subagent_type") or ""
        desc = input_obj.get("description") or input_obj.get("prompt") or ""
        bg = ", background" if input_obj.get("run_in_background") else ""
        if sub and desc:
            return _truncate(f'"{desc}", {sub}{bg}', max_len)
        return _truncate((desc or sub) + bg, max_len)

    if n == "WebFetch":
        url = str(input_obj.get("url") or "")
        return _truncate(url, max_len)

    if n == "WebSearch":
        return _truncate(f'query: "{input_obj.get("query", "")}"', max_len)

    if n == "TodoWrite":
        todos = input_obj.get("todos") or []
        count = len(todos) if isinstance(todos, list) else 0
        return _truncate(f"{count} todo{'s' if count != 1 else ''}", max_len)

    if n == "Skill":
        skill = input_obj.get("skill") or ""
        args = input_obj.get("args")
        if args:
            return _truncate(f'skill: "{skill}", args: "{args}"', max_len)
        return _truncate(skill, max_len)

    return _truncate(_short_repr(input_obj), max_len)


def _short_path(p: str) -> str:
    """Trim a file path to a Claude-Code-friendly form: drop leading repo root
    and home prefix; keep the rest as-is (forward slashes)."""
    if not p:
        return p
    s = str(p).replace("\\", "/")
    cwd = os.getcwd().replace("\\", "/")
    if cwd and s.lower().startswith(cwd.lower() + "/"):
        return s[len(cwd) + 1 :]
    home = os.path.expanduser("~").replace("\\", "/")
    if home and s.lower().startswith(home.lower() + "/"):
        return "~/" + s[len(home) + 1 :]
    return s


def summarize_result(content: Any, is_error: bool = False, max_len: int = 200) -> str:
    """Extract a short preview of a tool result. Handles list-of-blocks too."""
    if content is None:
        return "No output"
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # MCP-shaped: [{"type": "text", "text": "..."}, ...]
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
                if text:
                    break
            elif hasattr(block, "text"):
                text = str(getattr(block, "text", ""))
                if text:
                    break
        if not text:
            text = _short_repr(content)
    elif isinstance(content, dict):
        text = str(content.get("text") or content.get("content") or _short_repr(content))
    else:
        text = _short_repr(content)
    text = text.strip().replace("\n", " ")
    if is_error:
        text = "ERROR: " + text
    return _truncate(text, max_len)


def _short_repr(obj: Any, n: int = 60) -> str:
    s = repr(obj).replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# Tools whose input carries the change itself, so a diff can be shown from
# the stored input alone (no file access).
DIFF_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")


def _diff_pairs(name: str, input_obj: Any) -> list[tuple[str, str]]:
    """(old, new) text pairs a file-editing tool call applies."""
    if not isinstance(input_obj, dict):
        return []
    if name == "Edit":
        return [(str(input_obj.get("old_string") or ""), str(input_obj.get("new_string") or ""))]
    if name == "MultiEdit":
        edits = input_obj.get("edits")
        if not isinstance(edits, list):
            return []
        return [(str(e.get("old_string") or ""), str(e.get("new_string") or ""))
                for e in edits if isinstance(e, dict)]
    if name == "Write":
        return [("", str(input_obj.get("content") or ""))]
    if name == "NotebookEdit":
        new = str(input_obj.get("new_source") or "")
        if input_obj.get("edit_mode") == "delete":
            return [(new, "")]
        return [("", new)]
    return []


def edit_diff(name: str, input_obj: Any) -> Optional[list[tuple[str, str]]]:
    """Unified-diff rows for an Edit / MultiEdit / Write / NotebookEdit call,
    built from its input. Each row is ``(kind, text)`` with kind ``"+"``
    (added), ``"-"`` (removed), ``" "`` (context) or ``"@"`` (hunk break
    between separate edits). ``None`` for other tools or an input with
    nothing to show."""
    if name not in DIFF_TOOLS:
        return None
    rows: list[tuple[str, str]] = []
    for old, new in _diff_pairs(name, input_obj):
        if old == new:
            continue
        if rows:
            rows.append(("@", "…"))
        a, b = old.splitlines(), new.splitlines()
        for n, line in enumerate(difflib.unified_diff(a, b, lineterm="", n=2)):
            if n < 2:
                # The "--- / +++" file headers — always exactly the first two
                # lines. (Body lines may start with them too: a removed
                # "-- sql comment" is "--- sql comment".)
                continue
            if line.startswith("@@"):
                if rows and rows[-1][0] != "@":
                    rows.append(("@", "…"))
                continue
            kind, text = line[:1], line[1:]
            if kind in ("+", "-", " "):
                rows.append((kind, text))
    while rows and rows[0][0] == "@":
        rows.pop(0)
    return rows or None


def diff_stats(rows: list[tuple[str, str]]) -> tuple[int, int]:
    """(added, removed) line counts of :func:`edit_diff` rows."""
    return (sum(1 for k, _ in rows if k == "+"), sum(1 for k, _ in rows if k == "-"))
