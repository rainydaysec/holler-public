# Holler slash commands

## Active

- **`project-bootstrap.md`** — `/project-bootstrap`. Scaffolds per-project
  guardrails (a tailored `CLAUDE.md`, a non-strict linter config, and a
  `BACKLOG.md` stub) for the repo in the current directory. Idempotent:
  proposes before writing, never overwrites. This is the per-project half of
  the global engineering standard — the standard sets intent everywhere, this
  makes it concrete and enforceable in one repo.

  **Loading caveat:** Claude Code reads user commands from `~/.claude/commands/`.
  Standalone CLI sessions load them; holler SDK sessions do **not**
  (`setting_sources` defaults to `None`, and there's no `commands` field on
  `ClaudeAgentOptions`). Inside a holler session, just ask Claude to "bootstrap
  this project" — this file is the canonical spec of what that means.

## Deprecated (branch-workflow commands)

> **All three commands are retired as of 2026-05-12.** Holler's
> worktree mode (Stage 1, `8754e1f`) makes `/beam-start` unnecessary
> (each session opens in its own worktree on a `work-…` branch), and
> `/beam-end` was retired the next day in favor of plain git for
> landing. `/beam-cancel-lock` no longer has a lockfile to clear.

The files in this directory are kept as **deprecation no-ops** — copy
them into `~/.claude/commands/` and they'll print a short explanation
instead of doing anything. The point of keeping them around is so a
slash command typed out of habit (or by a fresh clone) lands somewhere
informative instead of erroring with "unknown command".

## What's here

- **`beam-start.md`** — Deprecation no-op. Tells the user worktree mode
  already handled branch isolation; just start editing.
- **`beam-end.md`** — Deprecation no-op carrying the full landing recipe
  (commit → switch master in main repo → merge --no-ff → push → worktree
  teardown) so anyone reading the file still has the safe procedure.
- **`beam-cancel-lock.md`** — Deprecation no-op. The `.session-lock`
  file isn't written anymore (it belonged to `/beam-start`).

The canonical landing recipe and the non-negotiable safety rules also
live in your project's `git.md`. The skill files mirror
them but are not authoritative — when in doubt, read `git.md`.

## Why they live here

Claude Code reads user-level slash commands from `~/.claude/commands/`,
not from any repository. Keeping copies under Holler version-controls
the deprecation notices so every developer's clone matches.

## Install on a new machine

After cloning the Holler repo for the first time:

```powershell
# Windows (PowerShell) — copy every .md except this README
$src = "tools\holler\templates\claude-commands"
$dst = "$env:USERPROFILE\.claude\commands"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Get-ChildItem "$src\*.md" -Exclude "README.md" | Copy-Item -Destination $dst -Force
```

```bash
# macOS / Linux — copy every .md except this README
mkdir -p ~/.claude/commands
find tools/holler/templates/claude-commands -maxdepth 1 -name '*.md' ! -name 'README.md' \
  -exec cp {} ~/.claude/commands/ \;
```

The `beam-*.md` files are deprecation no-ops; `project-bootstrap.md` is the one
active command. `README.md` is excluded so it doesn't become a stray
`/README` command.

These files won't run any git commands when invoked; they exist so the
slash-command surface stays self-documenting.

## Why not auto-sync?

Two reasons (carried over from the active-skill era):

1. **Symlink fragility on Windows.** `~/.claude/commands/` is in the user
   profile, the repo is wherever the developer cloned it. A symlink from
   one to the other is fine on Unix but flaky on Windows.
2. **Trust boundary.** Claude Code commands have full bash execution
   privilege. The deliberate "copy to install" step keeps the user in
   the loop about what's enabling itself in their `~/.claude/`.
