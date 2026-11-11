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

## Install on a new machine

After cloning the Holler repo for the first time:

```powershell
# Windows (PowerShell)
$src = "templates\claude-commands"
$dst = "$env:USERPROFILE\.claude\commands"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\project-bootstrap.md" -Destination $dst -Force
```

```bash
# macOS / Linux
mkdir -p ~/.claude/commands
cp templates/claude-commands/project-bootstrap.md ~/.claude/commands/
```

## Why not auto-sync?

Two reasons:

1. **Symlink fragility on Windows.** `~/.claude/commands/` is in the user
   profile, the repo is wherever the developer cloned it. A symlink from
   one to the other is fine on Unix but flaky on Windows.
2. **Trust boundary.** Claude Code commands have full bash execution
   privilege. The deliberate "copy to install" step keeps the user in
   the loop about what's enabling itself in their `~/.claude/`.
