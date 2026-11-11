"""Entry point. Parses --phase and dispatches to the right runner."""

from __future__ import annotations

import argparse
import sys


def _reconfigure_io() -> None:
    """Force stdout/stderr to UTF-8 so logs with Unicode (window titles, emoji,
    file paths with non-ASCII) don't crash on the default cp1252 console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def run() -> None:
    _reconfigure_io()
    parser = argparse.ArgumentParser(prog="holler")
    parser.add_argument(
        "--phase",
        type=int,
        default=None,
        help="Run a specific phase test mode (1-4). Omit for full app.",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Suppress console banner (for autostart use).",
    )
    args = parser.parse_args()

    if not args.silent:
        print("[holler] starting", file=sys.stderr)

    if args.phase == 1:
        from holler.runners.phase1 import main as phase_main
    elif args.phase == 2:
        from holler.runners.phase2 import main as phase_main
    elif args.phase == 3:
        from holler.runners.phase3 import main as phase_main
    elif args.phase == 4:
        from holler.runners.phase4 import main as phase_main
    elif args.phase is None:
        from holler.runners.full import main as phase_main
    else:
        print(f"[holler] unknown --phase {args.phase}", file=sys.stderr)
        sys.exit(2)

    phase_main()


if __name__ == "__main__":
    run()
