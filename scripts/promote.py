#!/usr/bin/env python3
"""Promote a verified commit of holler-main into the stable install.

Holler.app (and its in-app Restart) runs ~/holler-stable — a snapshot, so
half-finished edits in the dev checkout never reach the running app. Before
this script existed nothing ever refreshed that snapshot: commits piled up in
~/holler-main and every restart reloaded the same old copy (2026-10-02).

What it does, for one commit (default: tip of ``main``):
  1. exports exactly that commit (``git archive``) into a staging dir — never
     the dirty working tree, so only committed work can ship;
  2. runs the full test suite there;
  3. only if it passes, syncs staging into the stable dir (keeping the
     operator's ``config.toml``; previous copy kept in ``<stable>-prev``) and
     writes ``RELEASE.json`` (sha, subject, time) so Holler can tell which
     build it runs and that a newer one is waiting for a restart.

Progress/result goes to ``<app data>/promote.json`` (read by Holler's header)
and the test log to ``<app data>/logs/promote.log``.

Run by the git hooks in scripts/git-hooks/ on every commit/merge to main
(``--background``); safe to run by hand: ``.venv/bin/python scripts/promote.py``.
Runs are serialized with a lock; a commit that lands mid-run triggers one more
run for the newest tip when the current one finishes.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(os.environ.get("HOLLER_PROMOTE_REPO", Path(__file__).resolve().parent.parent))
STABLE = Path(os.environ.get("HOLLER_STABLE_DIR", Path.home() / "holler-stable"))
APP_DATA = Path(os.environ.get(
    "HOLLER_APP_DATA", Path.home() / "Library" / "Application Support" / "holler"))
STATUS = APP_DATA / "promote.json"
LOG = APP_DATA / "logs" / "promote.log"
LOCK = APP_DATA / "promote.lock"
AGAIN = APP_DATA / "promote.again"
STAGING = Path(os.environ.get(
    "HOLLER_PROMOTE_STAGING", Path.home() / "Library" / "Caches" / "holler-promote"))
BRANCH = "main"
# Never synced over (operator state that lives in the install dir) and never
# deleted from it.
KEEP = ("config.toml", ".venv", "RELEASE.json", "__pycache__", ".pytest_cache")
LOCK_STALE_S = 3 * 3600
TEST_TIMEOUT_S = 40 * 60


def _clean_env() -> dict[str, str]:
    # Hooks run with GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE pointing at the
    # live repo; a test that shells out to git would then act on it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("QT_SCALE_FACTOR", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True,
        env=_clean_env(),
    ).stdout.strip()


def _write_status(**fields) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    fields["updated_at"] = time.time()
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields, indent=2))
    os.replace(tmp, STATUS)


def _stable_release() -> dict:
    try:
        return json.loads((STABLE / "RELEASE.json").read_text())
    except Exception:
        return {}


def _acquire_lock() -> bool:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.mkdir()
        (LOCK / "pid").write_text(str(os.getpid()))
        return True
    except FileExistsError:
        try:
            pid = int((LOCK / "pid").read_text())
            os.kill(pid, 0)
            alive = True
        except Exception:
            alive = False
        if not alive or time.time() - LOCK.stat().st_mtime > LOCK_STALE_S:
            shutil.rmtree(LOCK, ignore_errors=True)
            return _acquire_lock()
        return False


def _export(sha: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    blob = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", sha],
                          check=True, capture_output=True, env=_clean_env()).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest, filter="data")
    (dest / ".venv").symlink_to(REPO / ".venv")


def _run_tests(tree: Path, sha: str) -> bool:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("w") as log:
        log.write(f"promote {sha} — tests in {tree}\n\n")
        log.flush()
        try:
            proc = subprocess.run(
                [str(tree / ".venv" / "bin" / "python"), "-m", "pytest",
                 "-p", "no:cacheprovider", "-q"],
                cwd=tree, env=_clean_env(), stdout=log, stderr=subprocess.STDOUT,
                timeout=TEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            log.write(f"\nTIMED OUT after {TEST_TIMEOUT_S}s\n")
            return False
    return proc.returncode == 0


def _rsync(src: Path, dst: Path) -> None:
    # Excluded names are neither copied nor deleted on the destination.
    cmd = ["rsync", "-a", "--delete", "--delay-updates"]
    for name in KEEP:
        cmd.append(f"--exclude={name}")
    subprocess.run([*cmd, f"{src}/", f"{dst}/"], check=True, env=_clean_env())


def _install(tree: Path, sha: str, subject: str) -> None:
    STABLE.mkdir(parents=True, exist_ok=True)
    prev = STABLE.with_name(STABLE.name + "-prev")
    prev.mkdir(exist_ok=True)
    _rsync(STABLE, prev)
    for name in ("config.toml", "RELEASE.json"):
        if (STABLE / name).exists():
            shutil.copy2(STABLE / name, prev / name)
    if not (prev / ".venv").exists() and (STABLE / ".venv").exists():
        (prev / ".venv").symlink_to(os.readlink(STABLE / ".venv"))
    _rsync(tree, STABLE)
    if not (STABLE / ".venv").exists():
        (STABLE / ".venv").symlink_to(REPO / ".venv")
    release = {"sha": sha, "short": sha[:7], "subject": subject,
               "promoted_at": time.time(), "source": str(REPO)}
    tmp = STABLE / "RELEASE.json.tmp"
    tmp.write_text(json.dumps(release, indent=2))
    os.replace(tmp, STABLE / "RELEASE.json")


def promote(sha: str | None, *, force: bool = False) -> int:
    sha = _git("rev-parse", sha or BRANCH)
    subject = _git("log", "-1", "--format=%s", sha)
    if not force and _stable_release().get("sha") == sha:
        _write_status(state="current", sha=sha, subject=subject)
        return 0
    _write_status(state="testing", sha=sha, subject=subject, log=str(LOG))
    tree = STAGING / "tree"
    try:
        _export(sha, tree)
        ok = _run_tests(tree, sha)
        if not ok:
            _write_status(state="failed", sha=sha, subject=subject, log=str(LOG))
            return 1
        _install(tree, sha, subject)
    except Exception as e:  # report, never leave state="testing" behind
        with LOG.open("a") as log:
            log.write(f"\npromote error: {e!r}\n")
        _write_status(state="failed", sha=sha, subject=subject, log=str(LOG), error=repr(e))
        return 1
    _write_status(state="promoted", sha=sha, subject=subject, log=str(LOG))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sha", nargs="?", help=f"commit to promote (default: tip of {BRANCH})")
    ap.add_argument("--force", action="store_true", help="re-test and re-install even if current")
    ap.add_argument("--background", action="store_true",
                    help="hook mode: queue behind a running promotion instead of waiting")
    args = ap.parse_args()

    if not _acquire_lock():
        if args.background:
            AGAIN.touch()  # the running promotion picks up the newest tip next
            return 0
        print("another promotion is running; try again shortly", file=sys.stderr)
        return 2
    try:
        rc = promote(args.sha, force=args.force)
        while AGAIN.exists() and not args.sha:
            AGAIN.unlink(missing_ok=True)
            rc = promote(None)
        return rc
    finally:
        shutil.rmtree(LOCK, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
