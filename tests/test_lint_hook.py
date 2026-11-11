"""lint_hook runs the project-local linter FROM the project root (eslint
resolves config/plugins relative to cwd) and finds Windows-layout binaries."""

from __future__ import annotations

import os
from types import SimpleNamespace

from holler import lint_hook


def _touch_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_eslint_runs_with_project_root_cwd(tmp_path, monkeypatch):
    proj = tmp_path / "web"
    _touch_exe(proj / "node_modules" / ".bin" / "eslint")
    src = proj / "src" / "deep" / "a.ts"
    src.parent.mkdir(parents=True)
    src.write_text("let x = 1\n")

    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lint_hook.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)  # host cwd is NOT the project
    assert lint_hook.lint_file(str(src)) is None
    argv, kw = calls[0]
    assert argv[0].endswith(os.path.join("node_modules", ".bin", "eslint"))
    assert kw["cwd"] == str(proj)
    assert argv[-1] == str(src)


def test_ruff_runs_with_project_root_cwd(tmp_path, monkeypatch):
    proj = tmp_path / "py"
    _touch_exe(proj / ".venv" / "bin" / "ruff")
    src = proj / "pkg" / "m.py"
    src.parent.mkdir(parents=True)
    src.write_text("x=1\n")
    argv, kind, cwd = lint_hook._resolve_linter(str(src))
    assert kind == "ruff" and argv[1] == "check" and cwd == str(proj)


def test_windows_venv_layout_is_found(tmp_path):
    proj = tmp_path / "py"
    _touch_exe(proj / ".venv" / "Scripts" / "ruff.exe")
    src = proj / "m.py"
    src.write_text("x=1\n")
    argv, kind, cwd = lint_hook._resolve_linter(str(src))
    assert argv[0].endswith(os.path.join("Scripts", "ruff.exe"))
    assert cwd == str(proj)


def test_windows_eslint_cmd_shim_is_found(tmp_path):
    proj = tmp_path / "web"
    _touch_exe(proj / "node_modules" / ".bin" / "eslint.cmd")
    src = proj / "a.js"
    src.write_text("1\n")
    argv, kind, cwd = lint_hook._resolve_linter(str(src))
    assert kind == "eslint" and argv[0].endswith("eslint.cmd") and cwd == str(proj)


# ── Canvas coverage (plan task 1.7) ─────────────────────────────────────────
# Canvas has no project-local ruff (backend/.venv lacks it), no installed
# eslint, and its served JS is classic `<script defer>` files under
# frontend/public/legacy — so before this, every Canvas edit linted nothing.

import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

_node = pytest.mark.skipif(shutil.which("node") is None and not os.path.exists(
    "/opt/homebrew/bin/node"), reason="node not installed")


def _exe(path, body="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def no_global_tools(monkeypatch):
    """Isolate from the machine: no PATH tools, no Holler-venv ruff."""
    monkeypatch.setattr(lint_hook, "_which", lambda name: None, raising=False)
    monkeypatch.setattr(lint_hook, "_HOLLER_RUFF_CACHE", {"argv": None}, raising=False)


def test_python_without_any_ruff_stays_silent(tmp_path, no_global_tools):
    src = tmp_path / "m.py"
    src.write_text("import os\n")
    assert lint_hook._resolve_linter(str(src)) is None
    assert lint_hook.lint_file(str(src)) is None


def test_ruff_from_path_when_project_has_none(tmp_path, monkeypatch):
    ruff = _exe(tmp_path / "bin" / "ruff")
    monkeypatch.setattr(lint_hook, "_which", lambda name: str(ruff) if name == "ruff" else None,
                        raising=False)
    proj = tmp_path / "canvas" / "backend" / "app"
    proj.mkdir(parents=True)
    src = proj / "m.py"
    src.write_text("x = 1\n")
    argv, kind, _cwd = lint_hook._resolve_linter(str(src))
    assert argv == [str(ruff), "check"] and kind == "ruff"


def test_ruff_from_holler_venv_python_as_last_resort(tmp_path, monkeypatch):
    monkeypatch.setattr(lint_hook, "_which", lambda name: None, raising=False)
    monkeypatch.setattr(lint_hook, "_HOLLER_RUFF_CACHE", {"argv": ["/hv/python", "-m", "ruff"]},
                        raising=False)
    src = tmp_path / "m.py"
    src.write_text("x = 1\n")
    argv, kind, _cwd = lint_hook._resolve_linter(str(src))
    assert argv == ["/hv/python", "-m", "ruff", "check"] and kind == "ruff"


def test_worktree_uses_main_checkout_venv(tmp_path, no_global_tools):
    """Holler-managed worktrees (<repo>/.claude/worktrees/holler-*) never
    contain the untracked .venv — fall back to the main checkout's."""
    repo = tmp_path / "canvas"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "app.py").write_text("x = 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(git + ["init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(git + ["add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(git + ["commit", "-m", "i"], cwd=repo, check=True, capture_output=True)
    ruff = _exe(repo / "backend" / ".venv" / "bin" / "ruff")
    wt = repo / ".claude" / "worktrees" / "holler-1-x"
    subprocess.run(git + ["worktree", "add", "-b", "holler/1-x", str(wt)],
                   cwd=repo, check=True, capture_output=True)
    argv, kind, cwd = lint_hook._resolve_linter(str(wt / "backend" / "app.py"))
    assert argv[0] == str(ruff) and kind == "ruff"
    assert cwd == str(repo / "backend")


@_node
def test_legacy_classic_script_syntax_error_is_reported(tmp_path, no_global_tools, monkeypatch):
    monkeypatch.setattr(lint_hook, "_which", lambda n: shutil.which(n) or "/opt/homebrew/bin/node"
                        if n == "node" else None, raising=False)
    legacy = tmp_path / "frontend" / "public" / "legacy" / "js" / "modules"
    legacy.mkdir(parents=True)
    src = legacy / "alerts.js"
    src.write_text("function render(a {\n  return a;\n}\n")
    msg = lint_hook.lint_file(str(src))
    assert msg and "node --check" in msg and "SyntaxError" in msg
    assert str(src) in msg and "holler-lint-" not in msg
    assert "    at " not in msg


@_node
def test_classic_script_not_held_to_module_strict_mode(tmp_path, no_global_tools, monkeypatch):
    """Canvas's frontend/package.json says "type": "module", but legacy files
    are classic browser scripts: sloppy-mode syntax must not be flagged."""
    monkeypatch.setattr(lint_hook, "_which", lambda n: shutil.which(n) or "/opt/homebrew/bin/node"
                        if n == "node" else None, raising=False)
    (tmp_path / "package.json").write_text('{"type": "module"}')
    src = tmp_path / "legacy.js"
    src.write_text("var o = {a: 1};\nwith (o) { a = 2; }\nvar n = 010;\n")
    assert lint_hook.lint_file(str(src)) is None
    esm = tmp_path / "mod.js"
    esm.write_text("import { x } from './y.js';\nexport const z = x;\n")
    assert lint_hook.lint_file(str(esm)) is None


@_node
@pytest.mark.parametrize("eslint_body", [
    # Repo eslint whose flat config only covers **/*.{ts,tsx} (Canvas's).
    "#!/bin/sh\necho 'warning  File ignored because no matching configuration was supplied'\nexit 0\n",
    # Repo eslint that can't load its config/plugins.
    "#!/bin/sh\necho 'Oops! Something went wrong!' >&2\nexit 2\n",
])
def test_eslint_not_covering_file_falls_back_to_node_check(tmp_path, monkeypatch, eslint_body):
    monkeypatch.setattr(lint_hook, "_which", lambda n: shutil.which(n) or "/opt/homebrew/bin/node"
                        if n == "node" else None, raising=False)
    proj = tmp_path / "frontend"
    _exe(proj / "node_modules" / ".bin" / "eslint", eslint_body)
    src = proj / "public" / "legacy" / "js" / "bridge.js"
    src.parent.mkdir(parents=True)
    src.write_text("const = 1;\n")
    msg = lint_hook.lint_file(str(src))
    assert msg and "node --check" in msg and "SyntaxError" in msg


def test_working_eslint_findings_still_win(tmp_path, monkeypatch):
    proj = tmp_path / "web"
    _exe(proj / "node_modules" / ".bin" / "eslint", "#!/bin/sh\necho '1:1 error no-undef'\nexit 1\n")
    src = proj / "a.js"
    src.write_text("foo()\n")
    msg = lint_hook.lint_file(str(src))
    assert msg and "Advisory lint (eslint)" in msg and "no-undef" in msg
