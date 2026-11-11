#!/usr/bin/env bash
# Dev launcher for holler (macOS / Linux). Activates the local venv and runs
# the package. Mirrors scripts/holler.cmd on Windows.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
    echo "[holler] no venv at .venv/, run:"
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

# python.org's framework Python is a universal2 binary. If the launching shell
# is x86_64 — e.g. iTerm/Terminal running under Rosetta — on an Apple Silicon
# Mac, the interpreter inherits x86_64 and can't load holler's native arm64
# wheels (numpy, PySide6, faster-whisper, pyobjc). Pin it to arm64.
# (hw.optional.arm64 reports 1 on Apple Silicon even from a Rosetta process.)
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    exec arch -arm64 .venv/bin/python -m holler "$@"
fi

exec .venv/bin/python -m holler "$@"
