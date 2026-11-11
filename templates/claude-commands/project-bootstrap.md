---
description: Scaffold per-project guardrails (CLAUDE.md + lint/format config + BACKLOG.md) tailored to the detected stack. Idempotent — proposes before writing and never overwrites existing files.
---

You are bootstrapping **local guardrails** for the project in the current
working directory, so every future session in this repo is pulled toward the
expert engineering standard instead of pattern-copying whatever bad file is
nearest. This is the per-project half of that standard: the global prompt sets
intent, a local `CLAUDE.md` + linter make it concrete and enforceable here.

Follow these steps in order. This command is **idempotent and additive**: never
overwrite or delete anything that already exists — only fill gaps, and only
after the user confirms.

# 1. Detect (report before touching anything)

Read, don't assume:

- **Existing guardrails.** Is there a `CLAUDE.md` at repo root? A `BACKLOG.md`?
  A lint/format config (`ruff`/`.ruff.toml`, `.eslintrc*`, `.prettierrc*`,
  `[tool.ruff]` in `pyproject.toml`)? If any exist, read them — you'll append
  missing pieces only, never replace.
- **Stack**, from whichever manifests are present:
  - `pyproject.toml` / `setup.py` / `requirements.txt` → Python
  - `package.json` (+ lockfile → package manager) → JS/TS
  - `go.mod` → Go, `Cargo.toml` → Rust, etc.
- **Shape**, from a quick skim: entry points, top-level packages, test layout,
  any obvious god-files worth noting.

Report what you found: stack, what guardrails already exist, what's missing.

# 2. Propose (get an explicit yes before writing)

Summarize exactly what you would create and show the user. Ask to proceed.
Offer the pieces individually — they may want the `CLAUDE.md` but not the lint
config, or vice versa.

# 3. Scaffold (only the missing pieces, after confirmation)

- **`CLAUDE.md`** — tailored to THIS repo, not boilerplate:
  - A short architecture map (from your step-1 skim).
  - Local conventions worth pinning (naming, structure, the "use X not the
    deprecated Y" rules you can infer).
  - A "quality floor" section restating the pillars in this repo's terms:
    understand before touching, match the standard not the surrounding rot,
    no god-files, errors never swallowed, verify before done, no new debt,
    backlog rot instead of absorbing it.
- **Lint/format config** (only if wanted, start non-strict):
  - Python → a `[tool.ruff]` block in `pyproject.toml` (or `.ruff.toml`).
  - JS/TS → eslint + prettier, wired into `package.json` scripts.
- **`BACKLOG.md`** — a stub if absent; this is the standard's paper-trail
  target for spotted-but-deferred debt.

# 4. Verify

- Run the linter once against the repo to capture a **baseline** violation
  count. Report it — don't try to drive it to zero now.
- Confirm idempotency: re-running this command on the now-scaffolded repo would
  detect everything present and do nothing.

# Guardrails on this command itself

- Minimal and revertible. No CI/CD pipelines, no dependency installs beyond the
  linter the user approves, no `git push`, no commits unless asked.
- If the repo already has a solid `CLAUDE.md`, say so and stop — don't manufacture
  busywork.
