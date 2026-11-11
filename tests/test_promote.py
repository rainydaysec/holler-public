"""scripts/promote.py + holler/release_status.py: only tested commits reach
the stable install, and Holler can tell when it is running a stale build."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from holler import release_status
from tests.test_sessions_window_selection import qapp, win  # noqa: F401 (fixtures)

ROOT = Path(__file__).resolve().parent.parent
PROMOTE = ROOT / "scripts" / "promote.py"


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _toy_repo(tmp: Path, passing: bool = True) -> Path:
    repo = tmp / "src"
    (repo / "tests").mkdir(parents=True)
    (repo / "VERSION").write_text("1\n")
    (repo / "tests" / "test_ok.py").write_text(f"def test_x():\n    assert {passing}\n")
    (repo / ".venv").symlink_to(ROOT / ".venv")
    (repo / ".gitignore").write_text(".venv\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _promote(tmp: Path, repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "HOLLER_PROMOTE_REPO": str(repo),
           "HOLLER_STABLE_DIR": str(tmp / "stable"),
           "HOLLER_APP_DATA": str(tmp / "appdata"),
           "HOLLER_PROMOTE_STAGING": str(tmp / "staging"),
           # As inside a git hook: must not leak into the test run.
           "GIT_DIR": str(repo / ".git")}
    return subprocess.run([sys.executable, str(PROMOTE), *args], env=env,
                          capture_output=True, text=True, timeout=300)


def _status(tmp: Path) -> dict:
    return json.loads((tmp / "appdata" / "promote.json").read_text())


def test_passing_commit_is_installed_and_keeps_operator_config(tmp_path):
    repo = _toy_repo(tmp_path)
    stable = tmp_path / "stable"
    stable.mkdir()
    (stable / "config.toml").write_text("claude_model = 'mine'\n")
    (stable / "stale.py").write_text("old")

    r = _promote(tmp_path, repo)
    assert r.returncode == 0, r.stderr
    rel = json.loads((stable / "RELEASE.json").read_text())
    assert rel["sha"] == _git(repo, "rev-parse", "main") and rel["subject"] == "first"
    assert (stable / "VERSION").read_text() == "1\n"
    assert (stable / "config.toml").read_text() == "claude_model = 'mine'\n"
    assert not (stable / "stale.py").exists()
    assert (tmp_path / "stable-prev" / "stale.py").exists()
    assert _status(tmp_path)["state"] == "promoted"

    # Same tip again: nothing to do.
    assert _promote(tmp_path, repo).returncode == 0
    assert _status(tmp_path)["state"] == "current"


def test_uncommitted_edits_never_ship(tmp_path):
    repo = _toy_repo(tmp_path)
    (repo / "VERSION").write_text("dirty\n")
    assert _promote(tmp_path, repo).returncode == 0
    assert (tmp_path / "stable" / "VERSION").read_text() == "1\n"


def test_failing_commit_is_not_installed(tmp_path):
    repo = _toy_repo(tmp_path)
    assert _promote(tmp_path, repo).returncode == 0
    good = _git(repo, "rev-parse", "main")
    (repo / "VERSION").write_text("2\n")
    (repo / "tests" / "test_ok.py").write_text("def test_x():\n    assert False\n")
    _git(repo, "commit", "-qam", "broken")

    r = _promote(tmp_path, repo)
    assert r.returncode == 1
    st = _status(tmp_path)
    assert st["state"] == "failed" and st["subject"] == "broken"
    assert json.loads((tmp_path / "stable" / "RELEASE.json").read_text())["sha"] == good
    assert (tmp_path / "stable" / "VERSION").read_text() == "1\n"
    assert "assert False" in Path(st["log"]).read_text()


def test_hooks_only_fire_on_main_and_are_executable():
    for name in ("post-commit", "post-merge", "post-rewrite"):
        hook = ROOT / "scripts" / "git-hooks" / name
        assert os.access(hook, os.X_OK)
        text = hook.read_text()
        assert '= "main" ] || exit 0' in text and "promote.py" in text and "--background" in text


# ── release_status.evaluate ─────────────────────────────────────────────────


def test_status_dev_checkout_is_quiet():
    st = release_status.evaluate({}, {}, {}, 0)
    assert st.state == "dev" and st.chip_text() == ""


def test_status_update_ready_when_disk_is_newer_than_running():
    st = release_status.evaluate({"sha": "aaaaaaa1"}, {"sha": "bbbbbbb2", "subject": "feat"},
                                 {"state": "promoted", "sha": "bbbbbbb2"}, 0)
    assert st.state == "update_ready"
    assert "restart" in st.chip_text() and "bbbbbbb" in st.chip_text()


def test_status_failed_and_testing_name_the_new_commit():
    run = {"sha": "aaaaaaa1"}
    failed = release_status.evaluate(run, run, {"state": "failed", "sha": "ccccccc3",
                                                "subject": "x", "log": "/l"}, 0)
    assert failed.state == "failed" and "ccccccc" in failed.chip_text()
    testing = release_status.evaluate(run, run, {"state": "testing", "sha": "ccccccc3"}, 0)
    assert testing.state == "testing"


def test_status_current_surfaces_uncommitted_work():
    run = {"sha": "aaaaaaa1", "source": "/src"}
    st = release_status.evaluate(run, run, {"state": "promoted", "sha": "aaaaaaa1"}, 3)
    assert st.state == "current"
    assert st.chip_text() == "3 uncommitted changes"
    assert "Only committed work" in st.tooltip()
    assert release_status.evaluate(run, run, {}, 0).chip_text() == ""


def test_header_chip_offers_restart_when_update_ready(win):  # noqa: F811
    restarts: list[int] = []
    win.restart_requested.connect(lambda: restarts.append(1))
    win._apply_release_status(release_status.evaluate(
        {"sha": "aaaaaaa1"}, {"sha": "bbbbbbb2", "subject": "feat"}, {}, 0))
    assert not win._release_chip.isHidden()
    assert "Update ready" in win._release_chip.text()
    win._release_chip.click()
    assert restarts == [1]
    win._apply_release_status(release_status.evaluate({}, {}, {}, 0))
    assert win._release_chip.isHidden()
