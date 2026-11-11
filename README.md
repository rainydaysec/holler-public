# holler

Push-to-talk + screen-scribble input for parallel Claude Agent SDK sessions.

Hold a hotkey (default Right Alt), talk while drawing on whatever window is
focused, release. Image + transcript become a fresh ephemeral SDK session that
has the same tool parity as Claude Code (Read/Edit/Bash/Task/skills) and runs
against your project. Up to 8 sessions in flight concurrently. Sessions persist
to a local SQLite DB so you can resume them later from the Sessions window.

## Quick setup (Windows 11 / 10)

### 1. One-time machine prereqs

- **Python 3.11+** — install from [python.org](https://www.python.org/downloads/) or
  `winget install Python.Python.3.12`
- **Node.js LTS** — install from [nodejs.org](https://nodejs.org/) or
  `winget install OpenJS.NodeJS.LTS`
- **Claude CLI** — install once and log in with your Claude account
  (Max plan recommended — the SDK reuses your CLI's OAuth so holler doesn't
  hit the billed API):

  ```powershell
  npm install -g @anthropic-ai/claude-code
  claude /login
  ```

- **NVIDIA GPU (optional but recommended)** — faster-whisper transcription
  runs ~10x faster on CUDA. No GPU? You can run on CPU — see the config tweak
  in step 4.

### 2. Clone holler

```powershell
git clone https://github.com/rainydaysec/holler-public.git
cd holler
```

### 3. Install Python deps

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

This will pull ~600 MB on the first run (PySide6, faster-whisper, NVIDIA wheels).

### 4. Run

```powershell
.\scripts\holler.cmd
```

On first launch a setup dialog asks for:

- **Project folder** — the directory Claude works against (your repo / codebase)
- **Push-to-talk key** — default Right Alt; change if you prefer
- **Claude model** — Opus 4.8 / Sonnet 4.6 / Haiku 4.5

After saving, the Sessions window opens. **Hold your push-to-talk key on any
window, talk, release.** A new session appears in the left rail, the right
pane shows Claude working live with inline tool calls (Claude Code-style).

#### No GPU? Edit `config.toml` before launching:

```toml
whisper_device = "cpu"
whisper_compute = "int8"
```

## Quick setup (macOS)

### 1. One-time machine prereqs

- **Python 3.11+** — `brew install python` or [python.org](https://www.python.org/downloads/)
- **Node.js LTS** — `brew install node` or [nodejs.org](https://nodejs.org/)
- **Claude CLI** — install once and log in (Max plan recommended — the SDK
  reuses your CLI's OAuth so holler doesn't hit the billed API):

  ```bash
  npm install -g @anthropic-ai/claude-code
  claude /login
  ```

There is no GPU path on macOS — CTranslate2 (faster-whisper) has no Metal
backend, so transcription runs on the CPU. The default config picks the
`small` Whisper model + `int8` CPU compute, which is ~1–2 s per clip on Apple
Silicon.

### 2. Clone + install

```bash
git clone https://github.com/rainydaysec/holler-public.git
cd holler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` uses environment markers, so the Windows-only bits
(`pywin32`, `keyboard`, the CUDA wheels) are skipped and the macOS bits
(`pyobjc`, `pynput`) are pulled in automatically.

### 3. Run

```bash
./scripts/holler.sh
```

First launch shows the setup dialog (project folder, push-to-talk key, model),
then opens the Sessions window.

> **Apple Silicon + Rosetta:** if your terminal runs under Rosetta (x86_64),
> python.org's universal2 interpreter inherits x86_64 and can't load holler's
> native arm64 wheels (`ImportError: incompatible architecture`).
> `scripts/holler.sh` detects this and re-execs arm64, so prefer it over a bare
> `python -m holler`. To fix it at the source, uncheck **Open using Rosetta**
> on your terminal app (Finder → Applications → right-click → Get Info) and
> restart it.

### 4. Grant macOS permissions

holler needs three privacy permissions. The OS prompts for them the first time
each is used; if a prompt doesn't appear, add them by hand in **System Settings
→ Privacy & Security**. Grant them to **the app that launches holler** — your
terminal (e.g. Terminal.app or iTerm2), or the Python framework binary:

- **Screen Recording** — capturing the active window at push-to-talk. Without
  it captures come back blank/None.
- **Microphone** — recording your voice.
- **Accessibility** — the push-to-talk key listener (`pynput`) and the global
  Sessions-window hotkey. Without it the hotkeys silently do nothing.

After granting Screen Recording / Accessibility you usually have to quit and
relaunch holler (and sometimes the terminal) for it to take effect.

holler's data — config, SQLite store, logs, captures, profiles — lives under
`~/Library/Application Support/holler/`.

## How macOS and Windows share one codebase

There is no separate "Mac version" and "Windows version" — it's **one
codebase** that figures out which OS it's on when it starts, and runs the
matching code for the few things that genuinely differ between platforms.

Only a handful of things are OS-specific:

| What | Windows | macOS |
| --- | --- | --- |
| Screenshotting the active window | Win32 / DWM (`pywin32`, ctypes) | Quartz / Cocoa (`pyobjc`) |
| The push-to-talk key listener | the `keyboard` library | `pynput` |
| Where config/logs/DB live | `%LOCALAPPDATA%\holler\` | `~/Library/Application Support/holler/` |
| faster-whisper transcription | NVIDIA GPU (CUDA) | CPU |

Everything else — the UI, the SDK sessions, routing, persistence, the overlay
— is identical on both.

**"Gated by platform"** just means a piece of code is wrapped in an
"if we're on Windows do this, if we're on macOS do that" check, so only the
relevant half ever runs. The other half isn't deleted — it's just skipped.
This happens at two levels:

- **In the code.** A module like `screen.py` is a small switchboard: on
  Windows it hands off to `_screen_win.py`, on macOS to `_screen_mac.py`
  (the leading `_` marks them as internal). Same idea, smaller, inside
  `hotkey.py` and a few others — a plain `if sys.platform == ...` branch.
- **In the dependencies.** `requirements.txt` uses *environment markers*
  (e.g. `pywin32; sys_platform == "win32"`). `pip` reads those and installs
  the Windows-only packages **only** on Windows, the macOS-only packages
  **only** on macOS. Nobody ends up with the other platform's libraries.

So a Windows user clones the repo and it behaves exactly as it always has;
a macOS user clones the same repo and gets the macOS code paths. Adding a
third platform later (Linux) would mostly mean filling in the same seams.

## Customizing Claude's behavior

Holler ships with a sane generic system prompt. Add your own rules in
`sysprompt.md` inside holler's data dir — `%LOCALAPPDATA%\holler\` on Windows,
`~/Library/Application Support/holler/` on macOS. Anything in that file gets
appended to the base. Examples:

- "Always use type hints in new Python code"
- "Before editing files under `backend/`, run `pytest tests/ -x` first"
- "Default git author for this machine is `myname <my@email>`"

Holler also auto-loads `CLAUDE.md` files from your project root and sub-
directories the same way Claude Code does — put project conventions there.

## Branch-workflow slash commands (`/beam-start`, `/beam-end`, `/beam-cancel-lock`)

These commands are **retired deprecation no-ops**. Holler's per-session git
worktree now gives every session branch isolation automatically, and branches
land with plain git. Historically, when two Holler sessions both touched a
shared git checkout they could clobber each other's work — switch branches out
from under each other, stash each other's WIP, lose commits to races — and
these slash commands serialized that coordination. Worktree mode handles it now.

Templates live at `templates/claude-commands/` in this repo. Install once
per developer machine:

```powershell
# Windows (PowerShell), from a holler clone
$src = "templates\claude-commands"
$dst = "$env:USERPROFILE\.claude\commands"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\beam-start.md","$src\beam-end.md","$src\beam-cancel-lock.md" -Destination $dst -Force
```

```bash
# macOS / Linux
mkdir -p ~/.claude/commands
cp templates/claude-commands/beam-*.md ~/.claude/commands/
```

Re-run the same command after pulling updates to the templates. See
`templates/claude-commands/README.md` for the full lock-file mechanism
and worktree-aware merge-back flow.

## Hotkey caveats

- **Windows** — the `keyboard` library uses a low-level Windows hook, which
  can't see input destined for UAC-elevated foreground windows (Task Manager,
  elevated admin PowerShell). Either avoid talking-at those windows or run
  holler elevated (`scripts\holler-admin.cmd`, TODO).
- **macOS** — the `pynput` listener needs the **Accessibility** permission
  (System Settings → Privacy & Security → Accessibility), granted to your
  terminal app. Without it the push-to-talk key and the Sessions-window hotkey
  silently do nothing.

## Sessions window cheat sheet

- **+ New session** — clears routing; next push-to-talk starts a fresh thread
- **Click a session in the rail** — selects it; next push-to-talk continues it
- **Ctrl+Shift+H** — toggle the Sessions window
- **Ctrl+,** — Settings
- **Esc** — cancel the in-flight turn (when an active session is selected)
- **`⋯` menu** — Quit, Settings, New session

## Architecture (one paragraph)

You hold a hotkey: holler grabs the active window's pixels, opens a transparent
red-stroke overlay over it for scribbles, and records mic audio. On release:
faster-whisper (local CUDA) transcribes the audio (~300ms), composites the
screenshot with scribbles, and fires the image + transcript as one turn into
an ephemeral `ClaudeSDKClient` session running on a background asyncio loop.
Up to 8 sessions run in parallel; each session's turns are serialized by a
per-session lock so multi-turn conversations don't corrupt their JSONL store.
Local SQLite + FTS5 persists session metadata + summaries; the SDK's own
JSONL store holds the full conversation. The Sessions window renders an
inline tool/text timeline (Claude-Code-TUI-style) that auto-tails as the model
streams.

## Repo layout

```
holler/
  config.example.toml          settings template
  requirements.txt
  scripts/
    auth_check.py              verify Max-plan OAuth (not API billing)
    holler.cmd                 dev launcher (Windows)
    holler.sh                  dev launcher (macOS / Linux)
    install_autostart.ps1
  holler/
    main.py, __main__.py       entry
    runners/full.py            wires UI + SDK + audio + screen + hotkey
    runners/phase1..4.py       incremental smoke runners
    audio.py                   recorder
    transcriber.py             faster-whisper wrapper + CUDA DLL preload
    screen.py                  active-window capture + foreground tracker
    overlay.py                 frozen-snapshot scribble overlay
    composite.py               PIL composite to PNG
    hotkey.py                  keyboard lib wrapper
    sdk_session.py             per-PTT ephemeral ClaudeSDKClient
    session_routing.py         voice prefix + fuzzy session match
    auto_title.py              Haiku one-shot retitle worker
    sysprompt.py               base + user override
    first_run.py               setup dialog + legacy migration
    config.py                  load/save TOML
    state.py                   atomic JSON state (geometry)
    store.py                   SQLite + FTS5
    domain.py                  dataclasses
    jobs.py                    Job + JobStore + ContentBlock
    sessions_window.py         primary UI (rail + detail)
    detail.py                  DetailView + inline tool rendering
    panel.py                   legacy mini panel (hidden)
    settings_dialog.py         full settings UI
    markdown_render.py         markdown + Pygments
    tool_format.py             tool summarization
    model_prefix.py            voice prefix + model badge
    log_setup.py               rotating file logger
```

## Troubleshooting

**`cublas64_12.dll not found`** — faster-whisper can't find CUDA. Either:
- `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` (already in requirements; reinstall)
- Or set `whisper_device = "cpu"` in config

**Holler doesn't see my push-to-talk** — make sure no other app has the same
hotkey registered, and that you're not focused on a UAC-elevated window.

**Sessions don't resume** — Claude Code's session store lives in
`~/.claude/projects/<sanitized-cwd>/`. If you change `repo_cwd` between
launches, prior session IDs won't resolve. Stick with one project root.

**Whisper transcribes my voice as gibberish** — the medium model expects
English by default. Change `whisper_language` in config to your language
code (e.g. `"es"`, `"fr"`).
