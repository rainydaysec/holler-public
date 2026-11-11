#!/usr/bin/env bash
# Point this clone's git hooks at scripts/git-hooks/ so every commit/merge to
# main is tested and promoted into ~/holler-stable (see scripts/promote.py).
set -euo pipefail
cd "$(dirname "$0")/.."
git config core.hooksPath scripts/git-hooks
echo "hooks installed: $(git config core.hooksPath)"
