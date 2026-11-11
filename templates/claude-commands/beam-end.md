---
description: DEPRECATED — use plain git to land your worktree branch. This command is a no-op with a recipe.
---

`/beam-end` is **retired as of 2026-05-12**, following `/beam-start`'s
retirement the day prior. Both halves of the ceremony are gone; landing a
worktree branch is now plain git.

## What to do instead — the recipe `/beam-end` used to run

Run these by hand, in order. They mirror what the skill did and preserve
the safety rules.

### From inside a Holler worktree session

```bash
# 1. Commit any pending work. NEVER blind `git add .` — list files explicitly.
git status --short
git diff
git add path/to/file1 path/to/file2 ...
git commit -m "<imperative subject under 70 chars>"

# 2. Capture the branch name + main-repo path before leaving the worktree.
FEATURE=$(git branch --show-current)
MAIN_REPO=$(git rev-parse --git-common-dir)/..
MAIN_REPO=$(cd "$MAIN_REPO" && pwd)
WORKTREE=$(git rev-parse --show-toplevel)

# 3. Refresh master in the MAIN checkout (worktree can't switch to master).
git -C "$MAIN_REPO" fetch
git -C "$MAIN_REPO" switch master
git -C "$MAIN_REPO" pull --ff-only        # HALT if this fails

# 4. Merge --no-ff so the worktree-branch lineage stays visible.
git -C "$MAIN_REPO" merge --no-ff "$FEATURE"   # HALT on conflict, resolve, then continue from step 5

# 5. Push.
git -C "$MAIN_REPO" push

# 6. Tear down the worktree (must run from main repo, not from inside the worktree dir).
git -C "$MAIN_REPO" worktree remove "$WORKTREE"
git -C "$MAIN_REPO" branch -d "$FEATURE"
git -C "$MAIN_REPO" ls-remote --exit-code --heads origin "$FEATURE" \
    && git -C "$MAIN_REPO" push origin --delete "$FEATURE"
```

### From the shared main checkout (non-worktree session on a feature branch)

```bash
FEATURE=$(git branch --show-current)
git add path/to/file1 path/to/file2
git commit -m "<msg>"

git switch master
git pull --ff-only          # HALT if this fails
git merge "$FEATURE"        # HALT on conflict
git push

git branch -d "$FEATURE"                                          # lowercase -d only
git ls-remote --exit-code --heads origin "$FEATURE" \
    && git push origin --delete "$FEATURE"
```

## Non-negotiable safety rules (carry these forward — they did not retire)

- **NEVER** `git push --force` to master. Not under any circumstance.
- **NEVER** blind `git add .` / `git add -A`. List paths explicitly.
- **NEVER** auto-stash. If state is unexpected, stop and ask.
- **HALT** on merge conflicts. Resolve by hand, then continue.
- **NEVER** use `git branch -D` to delete a feature branch. If `-d`
  refuses, that's the signal something is wrong upstream.
- Skip the cleanup steps if any earlier step halted — the branch needs to
  survive until the conflict / push reject is resolved.

## What this skill should do when invoked

Tell the user:

> `/beam-end` is retired. Worktree branches now land via plain git — see
> the recipe in this file. I won't run it
> automatically; tell me when you want me to land the worktree.

Then stop. Do not run any git commands.
