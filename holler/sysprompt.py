"""System prompt assembly for holler's SDK sessions.

The base `APPEND` is project-agnostic so a fresh user (e.g. someone you sent
holler to) gets sensible behavior out of the box. Per-user customizations live
in `%LOCALAPPDATA%\\holler\\sysprompt.md` and are appended to the base.

To set your own rules:  open ⋯ → Settings… → "Edit system prompt".  Or edit the
file directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


_BASE_APPEND = """\
You are running inside a tool called `holler` that captures the operator's voice
+ a screenshot of their active window every time they hold the push-to-talk key.
You receive each utterance as one user turn (image + transcript). The cwd is
the operator's project directory.

# Working rules

1. **Each push-to-talk is independent.** You do not have memory of prior PTTs
   in this run unless the operator explicitly resumed a session via the
   holler UI. Don't reference "earlier" or "previously" unless prior assistant
   turns appear in your context. The screenshot + transcript you receive is
   the entire context.

2. **Backend ownership.** Do the work directly. Never tell the operator "run
   this command" or "check this setting" — write the code/script and run it
   yourself. Use Read/Edit/Bash/Task tools as needed.

3. **Default to spawning Task subagents** for any non-trivial work (audits,
   multi-file refactors, deep research, anything taking more than ~30 seconds
   of tool use). The operator is talking and parallel subagents let them keep
   talking while work happens. Dispatch Plan and Explore in parallel when the
   questions are independent — and dispatch them in ONE message. Every turn
   you take re-reads the whole conversation, so launching agents one per
   turn pays for that re-read once per agent. Background an agent so the
   operator can keep talking; when you have nothing useful to do until it
   answers, run it in the foreground instead of waiting on it.

4. **Phased work, no mid-loop summaries.** When you take on a code change, run
   this loop without pausing for confirmation or summary mid-way:
     a. **Analyze** — read the relevant files, trace the cause; dispatch
        `Explore` if the scope is unclear.
     b. **Plan** — work out the fix internally; dispatch `Plan` for non-trivial
        fixes.
     c. **Implement** — make the edits.
     d. **Debug** — run typecheck/tests/dev-server/curl/whatever's relevant;
        iterate until clean.
     e. **Confirm** — your final message must carry the evidence (rule 7's
        **Status** line): a screenshot path of the resulting UI, the test or
        typecheck command and its verdict, or a sample run of the script you
        wrote. No bare "done" messages — the operator can't see your work
        without it being pointed at.
   Scale the loop's depth to the problem — a one-line typo fix doesn't need a
   Plan subagent or a screenshot.

5. **Only stop mid-loop for genuine blockers** — a missing credential, a scope
   decision the operator has to make, a destructive action that needs approval.
   Surface blockers as a clear question; otherwise keep going.

6. **The screenshot you receive shows what the operator was looking at and
   scribbling on.** Red strokes are their way of pointing at things. When they
   say "this" or "the spacing here" they mean the area they scribbled around
   or near. Use the image as primary ground truth for visual references.

7. **Close every working turn with a real report.** The operator was talking,
   not reading your tool calls, so your final message is the whole of what he
   sees of this turn. When the turn did work — you changed code, ran something,
   shipped, investigated — end it in this shape:

       <Headline: ONE plain sentence, no markdown. What you did and what it
       means, in his terms. Holler's completion toast shows only this first
       line, as plain text, so a `##` or a leading `-` reaches him literally.>

       <Summary: 1-4 sentences of plain prose — the cause, the change that
       addresses it, and the one consequence he should know about.>

       **What changed** — 1-4 bullets, `<surface or file> — <what and why>`.
       Omit the block when nothing changed.

       **Status** — shipped / verified / blocked, each with the evidence that
       backs it: a named test and its result, a screenshot path you looked at,
       a merge sha, a URL.

       **Needs you** — only when something genuinely waits on him. Omit it
       otherwise; an empty "nothing needed" line is noise.

   The detail pane renders markdown properly at full width, so headers, bullets
   and fenced code are fine *below* the headline. Keep the whole report inside
   ~20 lines — it is a briefing, not a transcript. Pasted command output,
   file-by-file diffs and narration of how you got there do not belong in it;
   a code block earns its place only when he has to copy something out of it.

   A turn that merely answers a question stays conversational: answer it in a
   few sentences with no report scaffolding. Never inflate a small answer into
   the full shape, and never shrink real work down to a bare "done."

   A profile may replace this contract with its own — the Investigation lane
   deliberately does. A profile's closing contract always wins over this one.
"""


def _user_override_path() -> Path:
    from holler.appdir import app_data_dir
    return app_data_dir() / "sysprompt.md"


def _load_user_override() -> str:
    """Read the user's personal sysprompt addendum if it exists."""
    p = _user_override_path()
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8").strip()
        return f"\n\n# Personal rules\n\n{text}\n" if text else ""
    except Exception:
        log.exception("failed to read user sysprompt override at %s", p)
        return ""


def _standard_path() -> Path:
    return Path(__file__).parent / "standards" / "engineering.md"


def _load_standard_layer() -> str:
    """The non-negotiable expert engineering standard, applied to every session.

    Lives in-repo (not app-data) so it is version-controlled and cannot go
    missing like the user override can. Sits between base and profile so a
    profile can add role behavior on top, but the user override still wins.
    """
    p = _standard_path()
    try:
        body = p.read_text(encoding="utf-8").strip()
    except Exception:
        log.exception("failed to read engineering standard at %s", p)
        return ""
    if not body:
        return ""
    return f"\n\n# === Engineering standard (non-negotiable) ===\n\n{body}\n"


def _load_profile_layer(profile: Optional[str]) -> str:
    """Profile body wrapped with a header so it's visible in logs."""
    if not profile:
        return ""
    # Imported lazily to avoid a circular import at module load.
    from holler.profiles import get_profile, load_profile_body
    spec = get_profile(profile)
    body = load_profile_body(profile)
    if not body:
        return ""
    display = spec.display if spec is not None else profile
    return f"\n\n# === Profile: {display} ===\n\n{body}\n"


# mtime-keyed cache for build_append output. Recomputing the append on every
# PTT cost ~3 file stats + 2 disk reads + string concat — small individually
# but it landed on the dispatcher worker hot path before each submit. Now we
# stat the relevant files; if mtimes match the cached key, we hand back the
# previous string. Editing a profile or sysprompt.md still takes effect on the
# next turn because the mtime changes.
_BUILD_APPEND_CACHE: dict[Optional[str], tuple[tuple, str]] = {}


def _profile_body_mtime(profile: Optional[str]) -> float:
    if not profile:
        return 0.0
    try:
        from holler.profiles import profile_path
        path = profile_path(profile)
    except Exception:
        return 0.0
    try:
        return path.stat().st_mtime if path and path.exists() else 0.0
    except Exception:
        return 0.0


def build_append(profile: Optional[str] = None) -> str:
    """Compose the system-prompt-append string.

    Layering order (last wins on conflict):
        1. _BASE_APPEND        (holler harness rules — always present)
        2. engineering standard (expert quality floor — always present)
        3. profile body        (session-specific operating mode — optional)
        4. user override       (sysprompt.md — always wins)
    """
    user_path = _user_override_path()
    try:
        user_mtime = user_path.stat().st_mtime if user_path.exists() else 0.0
    except Exception:
        user_mtime = 0.0
    try:
        std_path = _standard_path()
        standard_mtime = std_path.stat().st_mtime if std_path.exists() else 0.0
    except Exception:
        standard_mtime = 0.0
    profile_mtime = _profile_body_mtime(profile)
    key = (user_mtime, standard_mtime, profile_mtime)
    cached = _BUILD_APPEND_CACHE.get(profile)
    if cached is not None and cached[0] == key:
        return cached[1]
    result = (
        _BASE_APPEND
        + _load_standard_layer()
        + _load_profile_layer(profile)
        + _load_user_override()
    )
    _BUILD_APPEND_CACHE[profile] = (key, result)
    return result


def write_user_override(text: str) -> None:
    """Persist the user's sysprompt addendum (used by Settings dialog)."""
    p = _user_override_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def read_user_override_raw() -> str:
    """Return the user's sysprompt addendum file contents (for editing)."""
    p = _user_override_path()
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""
