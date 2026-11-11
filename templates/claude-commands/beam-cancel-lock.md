---
description: DEPRECATED — the session lockfile is no longer used. This command is a no-op.
---

`/beam-cancel-lock` is **retired as of 2026-05-12**.

This skill existed only to clear `<your-project>/.session-lock`, a
coordination lockfile that `/beam-start` wrote and `/beam-end` deleted.
Both of those skills are retired (Holler worktrees handle isolation;
landing is plain git), so the lockfile is no longer written.

**What to do:**

If a stale `.session-lock` somehow still exists from before
2026-05-12, just delete it by hand — no skill needed:

```bash
rm -f <your-project>/.session-lock
```

Tell the user:

> `/beam-cancel-lock` is retired. The session lock isn't written
> anymore (it belonged to the old `/beam-start` flow). If a stale
> `.session-lock` is still on disk from before 2026-05-12, delete it
> by hand: `rm -f <your-project>/.session-lock`.

Then stop. Do not run any commands.
