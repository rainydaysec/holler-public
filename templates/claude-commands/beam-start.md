---
description: DEPRECATED — Holler's worktree mode handles branch isolation automatically. This command is a no-op.
---

`/beam-start` is **retired as of 2026-05-11**.

Holler now spawns every session in its own git worktree on a pre-created
`work-YYYYMMDD-HHMM-...` branch (Stage 1 worktree mode, holler `8754e1f`).
That gives you the same branch isolation `/beam-start` was inventing — for
free, before the first turn fires.

**What to do instead:**

- **Starting work:** nothing. The worktree is already on a fresh
  branch off master. Just start editing. The branch lands via plain
  git when you're done.
- **Not running inside Holler:** `git switch -c work-…` from fresh master
  by hand.

Tell the user:

> `/beam-start` is retired. Holler worktree mode already put you on a
> dedicated branch — just start editing. Landing is plain git.

Then stop. Do not run any git commands. Do not branch. Do not pull.
