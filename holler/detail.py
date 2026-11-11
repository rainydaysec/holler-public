"""Detail window for a session or single job.

`DetailView` is the embeddable widget that renders either:
- a single Job (used by the popup DetailWindow), or
- a full Session conversation (used by the SessionsWindow's right pane) —
  multi-turn stacked timeline with live streaming into the last turn.

`DetailWindow` is the popup variant (still used when clicking a row in the
mini panel to open a separate window).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional, Union

from PySide6 import QtCore, QtGui, QtWidgets

from holler import claude_cli, iconset, theme
from holler.agents import agent_turn_label, is_agent_turn

Number = Union[int, float]


def _safe_loads(s: str):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    return f"{n/1_000_000:.1f}M"


def _fmt_duration(seconds: float) -> str:
    """Mimic Claude Code's `1m 1s` / `14s` formatting."""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{int(seconds)}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


# ----- Claude Code-style inline rendering -----

_BG = theme.CANVAS
_BG_ROW = theme.SURFACE
_FG = theme.FG
_FG_DIM = theme.FG_MUTED
_ACCENT = theme.ACCENT
_RED = theme.DANGER
_GREEN = theme.SUCCESS

# Tool-result rows indent under the tool name (past the `● ` bullet).
_TOOL_INDENT = theme.SPACE_LG + theme.SPACE_2XS
# Gap between a turn's tool calls and its reply text.
_REPLY_GAP_HTML = f"<div style='height: {theme.SPACE_SM}px;'></div>"
# Rule between turns. QTextDocument paints <hr> in its background-color
# (CSS border on <hr> is ignored — it would fall back to the text colour).
_TURN_DIVIDER_HTML = (
    f"<hr style='background-color:{theme.BORDER}; margin:{theme.SPACE_XL}px 0 {theme.SPACE_LG}px 0;'/>"
)

# Inline-link and image URL schemes the transcript browser understands
# (see DetailView._on_anchor_clicked and _TranscriptBrowser.loadResource).
#   holler://copy/code/<key>   copy a fenced code block
#   holler://copy/msg/<key>    copy an assistant reply
#   holler://expand/<key>      toggle a tool output / diff / thinking block
#   holler://open/shot/<turn>  open a turn's screenshot
#   holler-shot:<turn>         inline screenshot thumbnail (image resource)
#   holler-icon:<name>/<rrggbb>/<px>  tinted icon (image resource)
_SHOT_SCHEME = "holler-shot"
_ICON_SCHEME = "holler-icon"
# Largest icon size (logical px) the icon scheme serves — chrome icons only.
_ICON_MAX_PX = 64
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_ICON_NAMES: Optional[frozenset] = None


def _icon_names() -> frozenset:
    global _ICON_NAMES
    if _ICON_NAMES is None:
        _ICON_NAMES = frozenset(iconset.names())
    return _ICON_NAMES

# Lines shown when a tool output / diff is expanded (stored results are
# already capped at 8 KB by jobs.py; this bounds the row count).
_EXPANDED_MAX_LINES = 400
# Diff rows shown before "… N more lines".
_DIFF_PREVIEW_LINES = 12
# Characters of a thinking block shown while it's collapsed.
_THINKING_PREVIEW_CHARS = 90
# Suffix the capture flow appends to a transcript for the model's benefit —
# must equal holler.runners.full.SCREEN_UNCHANGED_SUFFIX (not imported: that
# module is heavy). Display only: shown as a "Same screen" note in the user's
# message; the stored transcript is never altered.
SCREEN_UNCHANGED_SUFFIX = " (screen unchanged since last turn)"


def _esc(s: str) -> str:
    return html_lib.escape(s or "")


def _mono_css() -> str:
    """Monospace ``font-family`` stack, double-quoted so it can sit inside the
    single-quoted ``style='…'`` attributes used throughout this module."""
    return mono_font_stack('"')


def _icon_src(name: str, color: str, size: int = theme.TRANSCRIPT_ICON) -> str:
    """Image-resource URL for a tinted icon (served by _TranscriptBrowser)."""
    return f"{_ICON_SCHEME}:{name}/{color.lstrip('#')}/{size}"


def _icon_img(name: str, color: str, size: int = theme.TRANSCRIPT_ICON) -> str:
    return (f"<img src='{_icon_src(name, color, size)}' width='{size}' height='{size}' "
            f"style='vertical-align: middle;'/>")


def _link(href: str, text_html: str, *, color: str = _ACCENT, size: Optional[Number] = None) -> str:
    # Inline elements don't reliably inherit a parent block's font-size in
    # QTextDocument, so small contexts pass theirs explicitly.
    fs = f" font-size: {size}pt;" if size is not None else ""
    return f"<a href='{href}' style='color:{color}; text-decoration: none;{fs}'>{text_html}</a>"


def _card(inner: str, *, bg: str, border: str, margin: str, pad_v: int = theme.SPACE_SM,
          pad_h: int = theme.SPACE_MD) -> str:
    """One-cell bordered card. QTextDocument ignores <div> borders and
    backgrounds-with-padding; a table cell paints both. The stylesheet's
    generic data-table rules (cell padding, row rules) are reset inline."""
    return (
        f"<table width='100%' cellspacing='0' cellpadding='0' border='1' "
        f"style='margin: {margin}; border-color: {border}; border-style: solid; border-collapse: collapse;'>"
        f"<tr><td bgcolor='{bg}' style='padding: {pad_v}px {pad_h}px; border-color: {border};'>"
        f"{inner}</td></tr></table>"
    )


class _RenderCtx:
    """Per-render bookkeeping for the interactive bits of the transcript:
    payloads behind Copy links, the screenshot paths behind thumbnails, and
    which collapsible blocks are expanded. Keys are URL-safe and scoped
    (``t<turn id>`` committed, ``j<job id>`` live) so committed and live
    segments never collide. Rendering without a ctx emits none of the
    interactive bits (plain, self-contained HTML)."""

    def __init__(self, scope: str, expanded=frozenset()) -> None:
        self.scope = scope
        self.expanded = expanded
        self.payloads: dict[str, str] = {}
        self.shots: dict[str, str] = {}
        self._n = 0

    def copy_key(self, text: str, kind: str) -> str:
        key = f"{self.scope}-{kind}{self._n}"
        self._n += 1
        self.payloads[key] = text
        return key

    def code_action(self, code: str) -> str:
        """Right side of a code-block header: the Copy link."""
        key = self.copy_key(code, "c")
        return _link(f"holler://copy/code/{key}",
                     f"{_icon_img('copy', _ACCENT)}&nbsp;Copy", size=theme.FONT_XS)


def _split_screen_suffix(transcript: str) -> tuple[str, bool]:
    t = transcript or ""
    if t.endswith(SCREEN_UNCHANGED_SUFFIX):
        return t[: -len(SCREEN_UNCHANGED_SUFFIX)], True
    return t, False


_SHOT_SIZE_CACHE: dict[tuple, tuple[int, int]] = {}


def _thumb_box(path: str) -> Optional[tuple[int, int]]:
    """Logical (w, h) of a screenshot thumbnail fitted into the thumbnail box,
    or None when the file is missing / unreadable. Reads only the header."""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    key = (path, mtime)
    box = _SHOT_SIZE_CACHE.get(key)
    if box is None:
        size = QtGui.QImageReader(path).size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            return None
        s = min(theme.TRANSCRIPT_THUMB_W / size.width(), theme.TRANSCRIPT_THUMB_H / size.height(), 1.0)
        box = (max(1, round(size.width() * s)), max(1, round(size.height() * s)))
        if len(_SHOT_SIZE_CACHE) > 512:
            _SHOT_SIZE_CACHE.clear()
        _SHOT_SIZE_CACHE[key] = box
    return box


def _render_agent_turn_html(idx: int, turn) -> str:
    """A turn Claude started on its own because background agents finished:
    not the operator's words, so no "You" card — a slim accent-ruled label
    naming which agents reported back."""
    label = _esc(agent_turn_label(turn.transcript))
    return (
        f"<p style='margin: {theme.SPACE_SM}px 0 {theme.SPACE_SM}px 0; color:{_ACCENT}; "
        f"font-size: {theme.FONT_XS}pt; letter-spacing: {theme.TRACKING_CAPS_SM}; "
        f"text-transform: uppercase;'>\u27f2 Agents · Turn {idx}"
        f"<span style='color:{_FG_DIM}; text-transform: none; letter-spacing: 0;'>"
        f" &nbsp;{label}</span></p>"
    )


def _render_user_turn_html(idx: int, turn, *, ctx: Optional[_RenderCtx] = None) -> str:
    """The operator's message: a neutral card with a small "You · Turn N"
    label, the transcript, and (when the turn has one) a clickable
    screenshot thumbnail on the right."""
    if is_agent_turn(getattr(turn, "transcript", "")):
        return _render_agent_turn_html(idx, turn)
    raw, same_screen = _split_screen_suffix(turn.transcript)
    transcript = _esc(raw or "(no speech recognized)")
    note = (f" · Same screen" if same_screen else "")
    label = (
        f"<p style='margin: 0 0 {theme.SPACE_XS}px 0; color:{_FG_DIM}; font-size: {theme.FONT_XS}pt; "
        f"letter-spacing: {theme.TRACKING_CAPS_SM}; text-transform: uppercase;'>You · Turn {idx}{note}</p>"
    )
    text = (f"<p style='margin: 0; color:{_FG}; font-size: {theme.FONT_TRANSCRIPT}pt; "
            f"line-height: {theme.LINE_HEIGHT_BODY};'>{transcript}</p>")
    shot = getattr(turn, "screenshot_path", None)
    box = _thumb_box(shot) if (ctx is not None and shot) else None
    if box is None:
        return _card(label + text, bg=_BG_ROW, border=theme.BORDER_SUBTLE,
                     margin=f"{theme.SPACE_SM}px 0 {theme.SPACE_MD}px 0", pad_v=theme.SPACE_MD)
    ctx.shots[str(turn.id)] = shot
    w, h = box
    # Own paragraph at 100% line height: the body's 150% would otherwise
    # pad the image line by half the thumbnail's height.
    thumb = (f"<p style='margin: 0; line-height: 100%;'>"
             + _link(f"holler://open/shot/{turn.id}",
                     f"<img src='{_SHOT_SCHEME}:{turn.id}' width='{w}' height='{h}'/>")
             + "</p>")
    inner = (
        f"<table width='100%' cellspacing='0' cellpadding='0' border='0' style='margin: 0; border: none;'>"
        f"<tr><td valign='top' style='padding: 0 {theme.SPACE_MD}px 0 0; border: none;'>{label}{text}</td>"
        f"<td valign='top' align='right' width='{w}' style='padding: 0; border: none;'>{thumb}</td></tr></table>"
    )
    return _card(inner, bg=_BG_ROW, border=theme.BORDER_SUBTLE,
                 margin=f"{theme.SPACE_SM}px 0 {theme.SPACE_MD}px 0", pad_v=theme.SPACE_MD)


def _render_assistant_text_html(md_text: str, *, highlight_code: bool = True,
                                ctx: Optional[_RenderCtx] = None) -> str:
    """An assistant text block: plain markdown on the canvas — no bullet
    (green marks a successful tool call only) and no card, so the reply
    reads as the primary content between the quieter tool cards.

    ``highlight_code=False`` skips Pygments — used during live streaming where
    the growing buffer would otherwise make highlighting O(N²) per render
    tick. The final post-completion render uses highlight_code=True.
    """
    if not (md_text or "").strip():
        return ""
    return render_body(md_text, highlight_code=highlight_code,
                       code_action=ctx.code_action if ctx is not None else None)


def _result_text_lines(result_text: str) -> tuple[list[str], bool]:
    """Full result lines (without jobs.py's truncation marker) + whether the
    stored result was truncated."""
    text = (result_text or "").strip()
    truncated = text.endswith(_TRUNCATION_MARKER)
    if truncated:
        text = text[: -len(_TRUNCATION_MARKER)].rstrip("\n")
    lines = text.split("\n") if text else []
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines, truncated


def _pre(lines_html: list[str], color: str) -> str:
    return (f"<pre class='out' style='color:{color}; font-family: {_mono_css()};'>"
            + "\n".join(lines_html) + "</pre>")


def _render_diff_html(rows: list[tuple[str, str]], *, expanded: bool, key: str,
                      ctx: Optional[_RenderCtx]) -> str:
    """Red/green unified-diff rows (one table row per line so each paints its
    own fill), capped at _DIFF_PREVIEW_LINES unless expanded."""
    limit = _EXPANDED_MAX_LINES if expanded else _DIFF_PREVIEW_LINES
    shown = rows[:limit]
    out = []
    for kind, text in shown:
        if kind == "+":
            bg, sign, fg = theme.DIFF_ADD_BG, "+", _FG
        elif kind == "-":
            bg, sign, fg = theme.DIFF_DEL_BG, "−", _FG
        elif kind == "@":
            bg, sign, fg = theme.TOOL_CARD_BG, "⋯", _FG_DIM
            text = ""
        else:
            bg, sign, fg = theme.TOOL_CARD_BG, " ", _FG_DIM
        out.append(
            f"<tr><td bgcolor='{bg}' style='padding: 0 {theme.SPACE_SM}px; border: none; "
            f"color:{fg}; white-space: pre-wrap;'>{_esc(sign)} {_esc(text)}</td></tr>"
        )
    more = ""
    hidden = len(rows) - len(shown)
    if ctx is not None and (hidden > 0 or (expanded and len(rows) > _DIFF_PREVIEW_LINES)):
        label = (f"… {hidden} more line{'s' if hidden != 1 else ''}" if hidden > 0 and not expanded
                 else "Show less")
        if expanded and hidden > 0:
            more = f"<span style='color:{_FG_DIM};'>… {hidden} more lines not shown · </span>"
        more = (f"<p class='mono' style='margin: {theme.SPACE_2XS}px 0 0 0; font-size: {theme.FONT_XS}pt;'>"
                f"{more}{_link(f'holler://expand/{key}', label, size=theme.FONT_XS)}</p>")
    elif hidden > 0:
        more = (f"<p class='mono' style='margin: {theme.SPACE_2XS}px 0 0 0; color:{_FG_DIM}; "
                f"font-size: {theme.FONT_XS}pt;'>… {hidden} more line{'s' if hidden != 1 else ''}</p>")
    return (
        f"<table class='mono' width='100%' cellspacing='0' cellpadding='0' border='0' "
        f"style='margin: {theme.SPACE_XS}px 0 0 0; border: none; font-family: {_mono_css()}; "
        f"font-size: {theme.FONT_XS}pt;'>" + "".join(out) + "</table>" + more
    )


def _render_tool_block_html(name: str, input_obj, result_text: str, is_error: bool, running: bool,
                            *, ctx: Optional[_RenderCtx] = None, key: str = "") -> str:
    """Render one tool call as a quiet card: `● Name(args)` head (green done,
    blue running, red failed) and a `⎿ result` preview below — the same
    words Claude Code prints in its terminal. A preview that hides lines
    ends in a "… N more lines" link (holler://expand/<key>) that re-renders
    the full output in place; Edit / MultiEdit / Write / NotebookEdit show a
    red/green diff built from their stored input."""
    # Claude Code's TUI prints up to ~240 chars of args before truncating with
    # `…`; long shell commands wrap on character boundaries (break-all).
    args = _esc(summarize_tool_args(name, input_obj, max_len=240))
    if is_error:
        dot_color = _RED
    elif running:
        dot_color = _ACCENT
    else:
        dot_color = _GREEN
    expandable = ctx is not None and bool(key)
    expanded = expandable and key in ctx.expanded
    # U+25CF BLACK CIRCLE, as Claude Code (U+23FA renders as a record symbol).
    head = (
        f"<p class='mono' style='margin: 0; font-family: {_mono_css()}; "
        f"font-size: {theme.FONT_SM}pt; line-height: {theme.LINE_HEIGHT_TOOL}; word-break: break-all;'>"
        f"<span style='color:{dot_color}; font-weight: {theme.WEIGHT_BOLD_KEYWORD};'>●</span> "
        f"<span style='color:{_FG}; font-weight: {theme.WEIGHT_SEMIBOLD};'>{_esc(name)}</span>"
        f"<span style='color:{_FG_DIM};'>({args})</span></p>"
    )
    parts = [head]
    diff_rows = None if is_error else edit_diff(name, input_obj)
    if running and not result_text:
        parts.append(_pre([f"⎿  <i>running…</i>"], _FG_DIM))
    elif result_text:
        color = _RED if is_error else _FG_DIM
        if diff_rows:
            added, removed = diff_stats(diff_rows)
            counts = []
            if added:
                counts.append(f"<span style='color:{_GREEN};'>+{added}</span>")
            if removed:
                counts.append(f"<span style='color:{_RED};'>−{removed}</span>")
            parts.append(_pre([f"⎿  ✓ " + " ".join(counts)], color))
        elif expanded:
            full, truncated = _result_text_lines(result_text)
            shown = full[:_EXPANDED_MAX_LINES]
            rows = [("⎿  " if i == 0 else "   ") + _esc(ln) for i, ln in enumerate(shown)]
            hidden = len(full) - len(shown)
            tail = []
            if hidden > 0 or truncated:
                tail.append(f"… {hidden}{'+' if truncated else ''} more lines not shown · ")
            rows.append("   " + _esc("".join(tail)) + _link(f"holler://expand/{key}", "Show less", size=theme.FONT_XS))
            parts.append(_pre(rows, color))
        else:
            lines = _compact_tool_result_lines(name, result_text, is_error)
            full, _trunc = _result_text_lines(result_text)
            rows = [("⎿  " if i == 0 else "   ") + _esc(ln) for i, ln in enumerate(lines)]
            if expandable and rows:
                last = lines[-1]
                if last.startswith("… "):
                    rows[-1] = "   " + _link(f"holler://expand/{key}", _esc(last), size=theme.FONT_XS)
                elif len(full) > 1 or (full and full[0] != last and last != "✓"):
                    # A one-line summary ("Read 120 lines", "3 matches")
                    # stands in for the output: offer the output itself.
                    rows[-1] += " · " + _link(f"holler://expand/{key}", "Show output", size=theme.FONT_XS)
            parts.append(_pre(rows, color))
    if diff_rows:
        parts.append(_render_diff_html(diff_rows, expanded=expanded, key=key, ctx=ctx))
    return _card("".join(parts), bg=theme.TOOL_CARD_BG, border=theme.BORDER_SUBTLE,
                 margin=f"{theme.SPACE_XS}px 0", pad_v=theme.SPACE_SM)


def _compact_tool_result(name: str, result_text: str, is_error: bool) -> str:
    """Back-compat single-line summary. New rendering goes through
    `_compact_tool_result_lines`. Kept because external callers / tests still
    expect a single-string return."""
    lines = _compact_tool_result_lines(name, result_text, is_error)
    return " · ".join(lines) if lines else ""


# Number of preview lines to show for verbose tool results (Bash, Grep, etc.).
# Claude Code's TUI shows ~4 lines before `+N more (ctrl+o to expand)`; 3 is
# tighter and reads better in Holler's narrower detail pane.
_PREVIEW_LINES = 3


def _compact_tool_result_lines(name: str, result_text: str, is_error: bool) -> list[str]:
    """Multi-line preview of a tool's result, mirroring Claude Code's TUI.

    Returns up to ``_PREVIEW_LINES`` lines of preview followed by a single
    `… N more lines` line when output was truncated. Returns a single-element
    list for tools that only have a one-line summary (Read, Edit, etc.).
    """
    text = (result_text or "").strip()
    if not text:
        return ["✓"] if not is_error else ["✗ Failed"]

    # jobs.py caps stored results at 8 KB and appends this marker; counts
    # derived from a truncated preview are lower bounds, so render them as
    # approximate ("~N+") instead of exact.
    truncated = text.endswith(_TRUNCATION_MARKER)
    if truncated:
        text = text[: -len(_TRUNCATION_MARKER)].rstrip("\n")

    # Errors: show up to PREVIEW_LINES of the error verbatim.
    if is_error:
        return _take_preview_lines(text, _PREVIEW_LINES, line_label="line", truncated=truncated)

    # Total line count (including truncated tail) — used for the "N more" tail.
    raw_lines = text.split("\n")
    # Strip trailing blank line if the input ended with "\n".
    if raw_lines and raw_lines[-1] == "":
        raw_lines = raw_lines[:-1]
    n_lines = len(raw_lines)

    if name in ("Read", "NotebookRead"):
        if truncated:
            return [f"Read ~{n_lines}+ lines"]
        return [f"Read {n_lines} line{'s' if n_lines != 1 else ''}"]
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return ["✓"]
    if name in ("Bash", "PowerShell"):
        return _take_preview_lines(text, _PREVIEW_LINES, line_label="line", truncated=truncated)
    if name == "Grep":
        # Honor Claude Code's "Found N" summary line if present.
        m = re.search(r"^Found (\d+)", text)
        if m:
            n = int(m.group(1))
            return [f"{n} match{'es' if n != 1 else ''}"]
        return _take_preview_lines(text, _PREVIEW_LINES, line_label="match", truncated=truncated)
    if name == "Glob":
        return _take_preview_lines(text, _PREVIEW_LINES, line_label="file", truncated=truncated)
    if name == "WebFetch":
        return _take_preview_lines(text, _PREVIEW_LINES, line_label="line", truncated=truncated)
    if name == "WebSearch":
        return _take_preview_lines(text, _PREVIEW_LINES, line_label="result", truncated=truncated)
    if name in ("Task", "Agent"):
        if text.startswith("Async agent launched"):
            # The CLI's launch receipt is internal metadata (agent id, output
            # path) — the Agents pane is where the agent actually shows up.
            return ["Running in the background · see the Agents pane"]
        return _take_preview_lines(text, _PREVIEW_LINES, line_label="line", truncated=truncated)
    return _take_preview_lines(text, _PREVIEW_LINES, line_label="line", truncated=truncated)


_TRUNCATION_MARKER = "... [truncated]"


def _take_preview_lines(
    text: str, n: int, line_label: str = "line", *, truncated: bool = False
) -> list[str]:
    """Return up to ``n`` preview lines + a `… K more <label>s` tail when more
    content was elided. Each preview line is hard-capped at 160 chars so a
    single mega-line can't blow up the row height."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        return []
    preview = [ln[:160] for ln in lines[:n]]
    extra = len(lines) - n
    if extra > 0 or (truncated and len(lines) > 0):
        if truncated:
            preview.append(f"… {max(extra, 0)}+ more {line_label}s")
        else:
            plural = "s" if extra != 1 else ""
            preview.append(f"… {extra} more {line_label}{plural}")
    return preview


def _render_thinking_html(text: str, key: str, ctx: Optional[_RenderCtx]) -> str:
    """A thinking block, collapsed to one muted line by default; the
    Show / Hide link (holler://expand/<key>) toggles the full text."""
    style = (f"color:{_FG_DIM}; font-size: {theme.FONT_XS}pt; margin: {theme.SPACE_SM}px 0;")
    small = f"font-size: {theme.FONT_XS}pt;"
    if ctx is None:
        return f"<p style='{style} font-style: italic;'>✻ {_esc(text)}</p>"
    if key in ctx.expanded:
        return (f"<p style='{style}'>✻ Thinking · "
                f"{_link(f'holler://expand/{key}', 'Hide', size=theme.FONT_XS)}</p>"
                f"<p style='color:{_FG_DIM}; font-size: {theme.FONT_XS}pt; font-style: italic; "
                f"margin: 0 0 {theme.SPACE_SM}px 0;'>{_esc(text)}</p>")
    first = " ".join((text or "").split())
    if len(first) > _THINKING_PREVIEW_CHARS:
        first = first[: _THINKING_PREVIEW_CHARS - 1].rstrip() + "…"
    return (f"<p style='{style}'>✻ Thinking · <span style='{small} font-style: italic;'>{_esc(first)}</span> · "
            f"{_link(f'holler://expand/{key}', 'Show', size=theme.FONT_XS)}</p>")


def _api_error_text(text: str) -> Optional[str]:
    """The CLI reports an API failure ("API Error: 400 …") as an ordinary
    assistant text block, so it would render as a reply above an empty
    banner. Returns the error line when ``text`` is one, else None."""
    t = (text or "").strip()
    if t.startswith("API Error") and "\n" not in t and len(t) <= 600:
        return t
    return None


def _error_hint(detail: str) -> str:
    """One line on what to do next, for failures the operator can fix."""
    too_old = claude_cli.parse_too_old(detail)
    if too_old:
        return (f"The Claude Code CLI that ran this turn ({too_old[0]}) is too old for "
                f"this model, which needs {too_old[1]}+. Holler uses the newest installed "
                f"Claude Code — run `claude update` in a terminal, then send again.")
    return ""


def _render_error_banner_html(message: str) -> str:
    """Turn-level failure banner: what happened, in words — an errored turn
    never collapses to an empty "No reply"."""
    detail = (message or "").strip() or "No error details were saved for this turn."
    hint = _error_hint(detail)
    hint_html = (
        f"<p style='margin: {theme.SPACE_XS}px 0 0 0; color:{_FG_DIM}; "
        f"font-size: {theme.FONT_XS}pt;'>{_esc(hint)}</p>"
    ) if hint else ""
    return _card(
        f"<p style='margin: 0; color:{_RED}; font-weight: {theme.WEIGHT_SEMIBOLD}; "
        f"font-size: {theme.FONT_SM}pt;'>{_icon_img('alert-triangle', _RED)}&nbsp; Turn failed</p>"
        f"<p style='margin: {theme.SPACE_XS}px 0 0 0; color:{_FG}; font-size: {theme.FONT_SM}pt; "
        f"white-space: pre-wrap;'>{_esc(detail)}</p>{hint_html}",
        bg=theme.ERROR_BANNER_BG, border=theme.ERROR_BANNER_BORDER,
        margin=f"{theme.SPACE_SM}px 0 0 0",
    ) if message is not None else ""


# The error text sdk_session reports when the operator stops a turn (Stop /
# Esc). A stop isn't a failure: such turns get a neutral "Stopped" note, not
# the red banner. (The job/turn stay status error / is_error — the sessions
# list and other consumers treat a stopped turn as before.)
_STOPPED_ERROR = "cancelled"


def _is_stopped(error_text: str) -> bool:
    return (error_text or "").strip() == _STOPPED_ERROR


def _render_stopped_note_html() -> str:
    """Neutral marker for a turn the operator stopped."""
    return (f"<p style='color:{_FG_DIM}; font-style: italic; font-size: {theme.FONT_SM}pt; "
            f"margin: {theme.SPACE_SM}px 0 0 0;'>Stopped</p>")


def _no_reply_html() -> str:
    return f"<p style='color:{_FG_DIM}; font-style: italic;'>No reply</p>"


def _footer_html(bits: list[str], copy_key: Optional[str]) -> str:
    """`✻ Baked for 14s · 3 tools` on the left, the reply's Copy link right."""
    if not bits and not copy_key:
        return ""
    left = (f"<span style='color:{_ACCENT};'>✻</span> {_esc(' · '.join(bits))}" if bits else "")
    right = (_link(f"holler://copy/msg/{copy_key}", f"{_icon_img('copy', _ACCENT)}&nbsp;Copy reply",
                   size=theme.FONT_XS)
             if copy_key else "")
    cell = f"style='padding: 0; border: none; color:{_FG_DIM}; font-size: {theme.FONT_XS}pt;'"
    return (
        f"<table width='100%' cellspacing='0' cellpadding='0' border='0' "
        f"style='margin: {theme.SPACE_MD}px 0 0 0; border: none;'><tr>"
        f"<td {cell}>{left}</td><td align='right' {cell}>{right}</td></tr></table>"
    )


def _job_tool_key(job, idx: int, b) -> str:
    tid = re.sub(r"[^A-Za-z0-9_]", "", b.tool_id or "") or str(idx)
    return f"j{job.id}-{tid}"


def _render_job_blocks_html(job, *, highlight_code: bool = True,
                            ctx: Optional[_RenderCtx] = None) -> str:
    """Build the HTML body for a Job's ordered content blocks (live in-flight view).

    Pass ``highlight_code=False`` while the job is still streaming to skip
    Pygments on the growing text buffer; pass True for the final paint.
    """
    out: list[str] = []
    texts: list[str] = []
    job_blocks = list(job.blocks)
    api_error = None
    if job.status == "error" and not job.error and job_blocks \
            and job_blocks[-1].kind == "text":
        api_error = _api_error_text(job_blocks[-1].text)
        if api_error:
            job_blocks.pop()
    for i, b in enumerate(job_blocks):
        if b.kind == "text":
            if b.text.strip():
                texts.append(b.text)
            out.append(_render_assistant_text_html(b.text, highlight_code=highlight_code, ctx=ctx))
        elif b.kind == "tool":
            running = not b.ended_ts and not job.status in ("done", "error")
            out.append(_render_tool_block_html(
                b.tool_name, b.tool_input, b.tool_result, b.tool_is_error, running,
                ctx=ctx, key=_job_tool_key(job, i, b),
            ))
        elif b.kind == "thinking":
            out.append(_render_thinking_html(b.text, f"j{job.id}-k{i}", ctx))
    failed = job.status == "error"
    if failed and _is_stopped(str(job.error or "")):
        out.append(_render_stopped_note_html())
    elif failed:
        # The failure reason: the error event's text, else a result message
        # the stream didn't already show (an SDK error result).
        msg = str(job.error or "") or api_error or ""
        final = _norm_ws(job.final_text or "")
        if not msg and final and final not in _norm_ws(" ".join(texts)) \
                and final not in _norm_ws("".join(texts)):
            msg = job.final_text
        out.append(_render_error_banner_html(msg))
    elif not out and job.status in ("queued", "running"):
        out.append(f"<p style='color:{_FG_DIM}; font-style: italic;'>Starting…</p>")
    elif not out:
        out.append(_no_reply_html())
    # In-flight heartbeat is rendered by the sticky bottom strip (DetailView's
    # _live_bottom_bar), not inline in the conversation HTML — the inline
    # version froze between events and made the UI look stalled. Completed
    # jobs still get a one-shot summary footer below.
    if job.status in ("done", "error"):
        copy_key = ctx.copy_key("\n\n".join(t.strip() for t in texts), "m") if (ctx and texts) else None
        out.append(_render_job_done_footer(job, copy_key=copy_key))
    return "".join(out)


def _render_job_done_footer(job, *, copy_key: Optional[str] = None) -> str:
    """Final `✻ Baked for Xs · ↓ Y tokens · Z tools` after a completed in-flight job."""
    bits: list[str] = []
    if job.duration_ms:
        bits.append(f"Baked for {_fmt_duration(job.duration_ms / 1000.0)}")
    elif job.started_at and job.ended_at and job.ended_at > job.started_at:
        bits.append(f"Baked for {_fmt_duration(job.ended_at - job.started_at)}")
    job_total = job.input_tokens + job.output_tokens
    if job_total:
        bits.append(f"{_fmt_tokens(job_total)} tokens")
    if job.tool_log:
        n = len(job.tool_log)
        bits.append(f"{n} tool{'s' if n != 1 else ''}")
    if job.total_cost_usd:
        bits.append(f"${job.total_cost_usd:.3f}")
    return _footer_html(bits, copy_key)


def _parse_turn_blocks(turn, n_tool_calls: int) -> Optional[list[dict]]:
    """Decode ``turn.blocks_json`` (see jobs._blocks_json) for rendering.

    Returns None — caller falls back to the legacy layout — when absent or
    not trustworthy: malformed JSON, unknown block shapes, tool indices out of
    range / repeated, or not every tool_call referenced (never drop a tool).
    """
    raw = getattr(turn, "blocks_json", "") or ""
    if not raw:
        return None
    try:
        blocks = json.loads(raw)
    except Exception:
        return None
    if not isinstance(blocks, list) or not blocks:
        return None
    seen: set[int] = set()
    for b in blocks:
        if not isinstance(b, dict):
            return None
        kind = b.get("t")
        if kind in ("text", "error"):
            if not isinstance(b.get("text"), str):
                return None
        elif kind == "tool":
            i = b.get("i")
            if type(i) is not int or not (0 <= i < n_tool_calls) or i in seen:
                return None
            seen.add(i)
        else:
            return None
    if len(seen) != n_tool_calls:
        return None
    return blocks


def _committed_error(turn, tool_calls: list) -> tuple[Optional[str], bool]:
    """Legacy-layout failure handling → (banner message or None, whether
    final_text IS that message). A one-line final_text on an errored turn
    without tool calls is the SDK's error string, not a reply."""
    if not getattr(turn, "is_error", False):
        return None, False
    final = (turn.final_text or "").strip()
    if final and not tool_calls and "\n" not in final and len(final) <= 400:
        return final, True
    return "", False


def _render_committed_turn_html(
    turn,
    tool_calls: list,
    *,
    anchor_before_reply: bool = False,
    ctx: Optional[_RenderCtx] = None,
) -> str:
    """Render a DB-committed turn: tool calls interleaved with assistant text.

    When the turn carries a valid ``blocks_json`` (v14+), text segments and
    tool calls render in the order Claude emitted them — the same layout as
    the live view. Older rows (and merged turns, which have no block record)
    use the legacy approximation: all tool calls first (in start order), then
    the final_text below.

    When ``anchor_before_reply`` is True, emit ``<a name='latest-reply-top'>``
    immediately before the assistant's final_text (or before the tool calls
    when there is no text) so the session-re-entry scroll lands at the start
    of the most recent reply, not at the operator's prior user-turn header.
    """
    blocks = _parse_turn_blocks(turn, len(tool_calls))
    if blocks is not None:
        return _render_committed_blocks_html(
            turn, tool_calls, blocks, anchor_before_reply=anchor_before_reply, ctx=ctx,
        )
    err_msg, final_is_error = _committed_error(turn, tool_calls)
    final_text = "" if final_is_error else turn.final_text
    parts: list[str] = []
    anchor_html = "<a name='latest-reply-top'></a>" if anchor_before_reply else ""
    anchor_placed = False
    if anchor_html and not final_text:
        # No assistant text → anchor on the reply's first surface (tool calls,
        # or the "No reply" placeholder). Always lands past the user header.
        parts.append(anchor_html)
        anchor_placed = True
    for tc in tool_calls:
        parts.append(_render_tool_call_record_html(tc, ctx=ctx))
    if final_text:
        parts.append(_REPLY_GAP_HTML)
        if anchor_html and not anchor_placed:
            parts.append(anchor_html)
            anchor_placed = True
        parts.append(_render_assistant_text_html(final_text, ctx=ctx))
    elif not tool_calls and err_msg is None:
        parts.append(_no_reply_html())
    if err_msg is not None:
        parts.append(_render_error_banner_html(err_msg))
    # Claude-Code-style "Baked for Xs" footer per completed turn.
    copy_key = ctx.copy_key(final_text.strip(), "m") if (ctx and final_text.strip()) else None
    parts.append(_render_turn_stats_footer(turn, len(tool_calls), copy_key=copy_key))
    return "".join(parts)


def _render_tool_call_record_html(tc, *, ctx: Optional[_RenderCtx] = None) -> str:
    try:
        inp = json.loads(tc.input_json) if tc.input_json else {}
    except Exception:
        inp = {}
    return _render_tool_block_html(
        tc.name, inp, tc.result_json or "", bool(tc.is_error), running=False,
        ctx=ctx, key=f"tc{tc.id}" if getattr(tc, "id", None) is not None else "",
    )


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def _render_committed_blocks_html(
    turn,
    tool_calls: list,
    blocks: list[dict],
    *,
    anchor_before_reply: bool,
    ctx: Optional[_RenderCtx] = None,
) -> str:
    """Interleaved committed render from a validated block list.

    final_text (the SDK result) normally equals the last text block — or, for
    a turn that errored before a result, all streamed text concatenated — so
    it's covered by the blocks and not repeated. Only a final_text the blocks
    don't contain is appended at the end. The reply anchor goes before the
    final answer (last text block / appended final_text), else before the
    first block. ``error`` blocks (a failed job's reason) render as the
    turn's failure banner after everything else.
    """
    errors = [b["text"] for b in blocks if b["t"] == "error"]
    blocks = [b for b in blocks if b["t"] != "error"]
    texts = [b["text"] for b in blocks if b["t"] == "text"]
    final = _norm_ws(turn.final_text or "")
    # The SDK result is the text of the last streamed message, so when it was
    # streamed it appears (whitespace aside) within the stored text — even
    # when consecutive deltas were glued into one block or whitespace-only
    # segments between tools were dropped from blocks_json.
    # (An errored turn's final_text is the deltas concatenated with no
    # separator, hence both joins.)
    covered = (
        not final
        or final in _norm_ws(" ".join(texts))
        or final in _norm_ws("".join(texts))
    )
    stopped = bool(errors) and all(_is_stopped(e) for e in errors)
    if stopped:
        errors = []
    err_msg: Optional[str] = "\n".join(errors) if errors else (
        "" if getattr(turn, "is_error", False) and not stopped else None)
    if err_msg == "" and blocks and blocks[-1]["t"] == "text":
        api_error = _api_error_text(blocks[-1]["text"])
        if api_error:
            blocks = blocks[:-1]
            texts = [b["text"] for b in blocks if b["t"] == "text"]
            err_msg, covered = api_error, True
    if not covered and err_msg == "" and "\n" not in turn.final_text.strip() \
            and len(turn.final_text.strip()) <= 400:
        # An errored turn whose result is a one-line error string the stream
        # never showed: that string is the failure reason, not a reply.
        err_msg, covered = turn.final_text.strip(), True
    last_text_idx: Optional[int] = None
    for idx, b in enumerate(blocks):
        if b["t"] == "text" and b["text"].strip():
            last_text_idx = idx
    anchor_html = "<a name='latest-reply-top'></a>" if anchor_before_reply else ""
    # Where the anchor lands: "start" (no assistant text at all), a block
    # index (final answer is that text block), or "append" (uncovered
    # final_text rendered after the blocks).
    if not covered:
        anchor_at: object = "append"
    elif last_text_idx is not None:
        anchor_at = last_text_idx
    else:
        anchor_at = "start"

    parts: list[str] = []
    copy_texts: list[str] = []
    if anchor_html and anchor_at == "start":
        parts.append(anchor_html)
    for idx, b in enumerate(blocks):
        if b["t"] == "tool":
            parts.append(_render_tool_call_record_html(tool_calls[b["i"]], ctx=ctx))
            continue
        if anchor_html and anchor_at == idx:
            parts.append(anchor_html)
        if b["text"].strip():
            copy_texts.append(b["text"].strip())
        parts.append(_render_assistant_text_html(b["text"], ctx=ctx))
    if not covered:
        parts.append(_REPLY_GAP_HTML)
        if anchor_html:
            parts.append(anchor_html)
        copy_texts.append(turn.final_text.strip())
        parts.append(_render_assistant_text_html(turn.final_text, ctx=ctx))
    elif last_text_idx is None and not tool_calls and err_msg is None and not stopped:
        parts.append(_no_reply_html())
    if err_msg is not None:
        parts.append(_render_error_banner_html(err_msg))
    elif stopped:
        parts.append(_render_stopped_note_html())
    copy_key = ctx.copy_key("\n\n".join(copy_texts), "m") if (ctx and copy_texts) else None
    parts.append(_render_turn_stats_footer(turn, len(tool_calls), copy_key=copy_key))
    return "".join(parts)


def _render_turn_stats_footer(turn, tool_count: int, *, copy_key: Optional[str] = None) -> str:
    """Compact `✻ Baked for 14s · 3 tools` line shown after a completed turn."""
    bits: list[str] = []
    if turn.duration_ms:
        bits.append(f"Baked for {_fmt_duration(turn.duration_ms / 1000.0)}")
    elif turn.started_at and turn.ended_at and turn.ended_at > turn.started_at:
        bits.append(f"Baked for {_fmt_duration(turn.ended_at - turn.started_at)}")
    if tool_count:
        bits.append(f"{tool_count} tool{'s' if tool_count != 1 else ''}")
    return _footer_html(bits, copy_key)

import html as html_lib
import re
import time as time_mod

from holler.domain import Session, ToolCallRecord, Turn
from holler.jobs import ContentBlock, Job, JobStore, ToolEntry
from holler.markdown_render import (
    mono_font_family,
    mono_font_stack,
    render as render_markdown,
    render_body,
    wrap_document,
)
from holler.tool_format import (
    diff_stats,
    edit_diff,
    summarize as summarize_tool,
    summarize_args as summarize_tool_args,
    summarize_result as summarize_tool_result,
)


_BG = theme.CANVAS
_BG_ROW = theme.SURFACE
_FG = theme.FG
_FG_DIM = theme.FG_MUTED
_ACCENT = theme.ACCENT
_RED = theme.DANGER
_GREEN = theme.SUCCESS


_DETAIL_QSS_TEMPLATE = f"""
QMainWindow {{ background: {_BG}; }}
QWidget {{ background: {_BG}; color: {_FG}; }}
QLabel.h {{ color: {_FG}; font-weight: {theme.WEIGHT_SEMIBOLD}; font-size: {theme.FONT_MD}pt; }}
QLabel.dim {{ color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; }}
QTextBrowser {{ background: {_BG}; color: {_FG}; border: none; }}
QTreeWidget {{ background: {_BG_ROW}; color: {_FG}; border: none; alternate-background-color: {theme.SUNKEN}; }}
QTreeWidget::item {{ padding: {theme.SPACE_XS}px; }}
QTreeWidget::item:selected {{ background: {theme.SURFACE_HOVER}; }}
QHeaderView::section {{ background: {_BG_ROW}; color: {_FG_DIM}; border: none; padding: {theme.SPACE_XS}px; font-size: {theme.FONT_XS}pt; }}
QPushButton {{ background: {theme.CONTROL}; color: {_FG}; border: 1px solid {theme.BORDER}; padding: {theme.SPACE_XS}px {theme.SPACE_MD}px; border-radius: {theme.RADIUS_MD}px; font-size: {theme.FONT_SM}pt; }}
QPushButton:pressed {{ background: {_BG_ROW}; }}
QPushButton:hover {{ background: {theme.BORDER}; border-color: {theme.BORDER_STRONG}; }}
QPushButton:focus {{ border-color: {_ACCENT}; }}
QSplitter::handle {{ background: {theme.BORDER_SUBTLE}; }}
QFrame#detailHeader {{ border: none; border-bottom: 1px solid {theme.BORDER_SUBTLE}; }}
QFrame#liveBar {{ background: {theme.LIVE_BG}; border-top: 1px solid {_ACCENT}; border-bottom: 1px solid {theme.LIVE_BORDER}; }}
QLabel#liveSpinner {{ color: {_ACCENT}; font-size: {theme.FONT_XL}pt; font-family: __MONO__; font-weight: {theme.WEIGHT_BOLD}; }}
QLabel#liveStatus {{ color: {_FG}; font-size: {theme.FONT_MD}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; }}
QLabel#liveMeta {{ color: {_FG_DIM}; font-size: {theme.FONT_SM}pt; }}
QPushButton#liveCancel {{ background: transparent; color: {_FG}; border: 1px solid {theme.BORDER_STRONG}; padding: {theme.SPACE_XS}px {theme.SPACE_MD}px; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; border-radius: {theme.RADIUS_MD}px; }}
QPushButton#liveCancel:hover {{ color: {_RED}; border-color: {_RED}; background: {theme.ERROR_BANNER_BG}; }}
QPushButton#liveCancel:pressed {{ background: {theme.SURFACE}; }}
QPushButton#liveCancel:focus {{ border-color: {_ACCENT}; }}
QFrame#liveBottomBar {{ background: {theme.LIVE_BG}; border-top: 1px solid {_ACCENT}; border-bottom: none; }}
QLabel#liveBottomSpinner {{ color: {_ACCENT}; font-size: {theme.FONT_XL}pt; font-family: __MONO__; font-weight: {theme.WEIGHT_BOLD}; }}
QLabel#liveBottomVerb {{ color: {_FG}; font-size: {theme.FONT_MD}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; font-style: italic; }}
QLabel#liveBottomMeta {{ color: {_FG_DIM}; font-size: {theme.FONT_SM}pt; }}
QFrame#liveBar QLabel, QFrame#liveBottomBar QLabel {{ background: transparent; }}
""" + theme.menu_qss()


def _detail_qss() -> str:
    """Window QSS with the monospace stack resolved at use time (the family
    can only be derived once a QGuiApplication exists)."""
    return _DETAIL_QSS_TEMPLATE.replace("__MONO__", mono_font_stack())


# Frames cycled for the live-status spinner — heavier braille than Claude Code's
# TUI so it reads at a glance even at 1000px+ down the conversation pane.
_SPINNER_FRAMES = "⣾⣽⣻⢿⡿⣟⣯⣷"

# Synonym verbs cycled when the same verb ("Thinking…", "Responding…") has
# persisted for long enough that the operator might think the UI froze. Kept
# whimsical on purpose — the point is to prove the UI is alive, not to convey
# any specific state.
_QUIET_VERBS_THINKING = (
    "Thinking…", "Pondering…", "Mulling…", "Considering…", "Brewing…",
    "Marinating…", "Cogitating…", "Stewing…", "Ruminating…",
)
_QUIET_VERBS_RESPONDING = (
    "Responding…", "Composing…", "Penning…", "Phrasing…", "Wording…",
    "Drafting…",
)
# After this many seconds with the SAME verb, start cycling synonyms.
_QUIET_VERB_CYCLE_AFTER_S = 4.0
# How long each synonym shows before swapping to the next.
_QUIET_VERB_CYCLE_INTERVAL_S = 1.6


def _live_group_ids(job) -> tuple:
    """The turn ids the in-flight ``job`` answers: its own turn, then any
    turns smart-batched into it (``merged_in``) — one logical turn, rendered
    together as the live segment (their messages, then the one reply)."""
    tid = getattr(job, "local_turn_id", None) if job is not None else None
    if tid is None:
        return ()
    ids = [tid]
    for mt in getattr(job, "merged_turns", None) or ():
        mid = mt.get("local_turn_id") if isinstance(mt, dict) else None
        if mid is not None and mid not in ids:
            ids.append(mid)
    return tuple(ids)


def _insert_html_in_place(cursor: QtGui.QTextCursor, html_doc: str,
                          scratch: QtGui.QTextDocument) -> None:
    """``cursor.insertHtml(html_doc)`` without leaking a QTextFrame per image.

    Qt's HTML importer inserts every ``<img>`` through
    ``QTextCursor::insertImage(fmt, position)``, which creates a positioning
    QTextFrame object for it and stores that object's index in the image's
    char format. Copying the image into a document creates a fresh frame
    object there — and removing the image later never deletes it (only a
    setHtml/clear does). The live segment is removed and re-inserted on every
    streaming tick, so each icon in it (Copy links, thumbnails …) leaked one
    frame per tick: tens of thousands of objects and hundreds of MB over a
    long turn. So: import into ``scratch`` (a throwaway document — its frame
    objects die with its next clear), swap each image for a plain inline
    image char (same format, no object index), then copy that over."""
    if "<img" not in html_doc.lower():
        cursor.insertHtml(html_doc)
        return
    dst = cursor.document()
    scratch.clear()
    scratch.setDefaultFont(dst.defaultFont())
    scratch.setDefaultStyleSheet(dst.defaultStyleSheet())
    src = QtGui.QTextCursor(scratch)
    src.insertHtml(html_doc)
    images: list[tuple[int, int, QtGui.QTextCharFormat]] = []
    block = scratch.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if fmt.isImageFormat() and fmt.objectIndex() != -1:
                images.append((frag.position(), frag.length(), fmt))
            it += 1
        block = block.next()
    # setCharFormat preserves object indices, so replace the characters.
    # Back to front: equal-length swaps, but keep positions trivially valid.
    for pos, length, fmt in reversed(images):
        fmt.setObjectIndex(-1)
        swap = QtGui.QTextCursor(scratch)
        swap.setPosition(pos)
        swap.setPosition(pos + length, QtGui.QTextCursor.KeepAnchor)
        swap.removeSelectedText()
        swap.insertText("￼" * length, fmt)
    src.select(QtGui.QTextCursor.Document)
    cursor.insertFragment(QtGui.QTextDocumentFragment(src))
    scratch.clear()


class _TranscriptBrowser(QtWidgets.QTextBrowser):
    """QTextBrowser that serves the transcript's image resources: inline
    screenshot thumbnails (``holler-shot:<turn id>``) and tinted icons
    (``holler-icon:<name>/<rrggbb>/<px>``). Everything else falls through to
    QTextBrowser's own loader. Images are produced at the screen's device
    pixel ratio so they stay crisp on HiDPI displays."""

    def __init__(self, shot_path_for: Callable[[str], Optional[str]]) -> None:
        super().__init__()
        self._shot_path_for = shot_path_for

    def _dpr(self) -> float:
        return max(2.0, float(self.devicePixelRatioF() or 1.0))

    def loadResource(self, type_: int, url: QtCore.QUrl):  # noqa: N802 (Qt override)
        scheme = url.scheme()
        if scheme == _SHOT_SCHEME:
            img = self._shot_image(url.path())
            if img is not None:
                return img
            return QtGui.QImage()
        if scheme == _ICON_SCHEME:
            try:
                name, color, size = url.path().split("/")
                px = int(size)
                # Only holler's own icons at chrome sizes: anything else
                # (e.g. a model-written ![](holler-icon:copy/fff/30000)) would
                # render — and cache — an arbitrarily large image.
                if (not 0 < px <= _ICON_MAX_PX or len(color) != 6 or not _HEX_DIGITS.issuperset(color)
                        or name not in _icon_names()):
                    return QtGui.QImage()
                pm = iconset.pixmap(name, f"#{color}", px, self._dpr())
                return pm.toImage()
            except Exception:
                return QtGui.QImage()
        return super().loadResource(type_, url)

    _SHOT_CACHE: dict[tuple, QtGui.QImage] = {}
    _SHOT_CACHE_CAP = 64

    def _shot_image(self, turn_key: str) -> Optional[QtGui.QImage]:
        path = self._shot_path_for(turn_key)
        if not path:
            return None
        box = _thumb_box(path)
        if box is None:
            return None
        dpr = self._dpr()
        key = (path, box, dpr, Path(path).stat().st_mtime if Path(path).exists() else 0.0)
        img = self._SHOT_CACHE.get(key)
        if img is None:
            src = QtGui.QImage(path)
            if src.isNull():
                return None
            img = src.scaled(round(box[0] * dpr), round(box[1] * dpr),
                             QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            if len(self._SHOT_CACHE) >= self._SHOT_CACHE_CAP:
                self._SHOT_CACHE.pop(next(iter(self._SHOT_CACHE)), None)
            self._SHOT_CACHE[key] = img
        return img


class DetailView(QtWidgets.QWidget):
    """Embeddable detail content (transcript + reply + tool log).

    Signals
    - ``cancel_requested(job_id)`` — the operator asked to stop the live turn
      (Stop button, or Esc anywhere in the hosting window while a turn runs).
    - ``copy_notice(text)`` — a transcript Copy link put text on the
      clipboard; ``text`` is a one-line confirmation ("Copied code"). Optional
      for hosts: when nothing is connected the view shows the confirmation as
      a tooltip at the pointer itself.
    """

    cancel_requested = QtCore.Signal(int)  # job_id of the live in-flight turn
    copy_notice = QtCore.Signal(str)

    # Upper bound on cached per-turn HTML entries (see _build_committed_html).
    _TURN_CACHE_CAP = 800
    # Table/image elements the in-place live update may insert before it
    # takes a full render instead (see _replace_live_segment). ~1 KB of
    # never-freed Qt format data each → bounds that growth at tens of MB.
    _INPLACE_ELEMENT_BUDGET = 30000

    # True while a push-to-talk capture is in progress. The capture overlay's
    # Esc-to-cancel comes from the global hotkey listener, which doesn't
    # swallow the key — so the same Esc must not also stop a running turn.
    # Process-wide; the runner installs it (set_capture_active_predicate).
    _capture_active: Callable[[], bool] = staticmethod(lambda: False)

    @classmethod
    def set_capture_active_predicate(cls, fn: Optional[Callable[[], bool]]) -> None:
        """Install ``fn() -> bool`` (True while a capture is in progress);
        Esc is then ignored for stopping a turn. ``None`` restores the
        default (never capturing)."""
        cls._capture_active = staticmethod(fn) if fn is not None else staticmethod(lambda: False)

    def _capture_in_progress(self) -> bool:
        try:
            return bool(type(self)._capture_active())
        except Exception:
            return False

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(_detail_qss())

        self._job: Optional[Job] = None
        self._captures_dir: Optional[Path] = None
        # Repo cwd used to resolve relative file links in the conversation.
        # None → relative links are ignored (see _on_anchor_clicked).
        self._repo_cwd: Optional[Path] = None
        self._prior_turns: list[Turn] = []
        self._session_title: str = ""
        self._session_for_header = None
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(220)
        self._render_timer.timeout.connect(self._render_now)
        self._pending_render = False
        # Set to True before a render that should land at the bottom regardless
        # of the user's previous scroll position (e.g. just entered a session).
        self._force_bottom_next = False
        # One-shot: when set, the next render lands at this HTML anchor name
        # (top of the most recent turn) instead of the bottom. Used when the
        # operator re-enters an idle/finished session — a long final reply is
        # more readable from its top than its tail. Wins over _force_bottom_next.
        self._force_anchor_next: Optional[str] = None
        # One-shot: the next render keeps the viewport exactly where it is
        # (no near-bottom auto-tail). Set when the operator expands/collapses
        # a block so the block they clicked stays under the pointer.
        self._hold_scroll_next = False
        # One-shot settle window armed on session entry. `setHtml`'s
        # `maximum()` reads stale until Qt finishes laying out the (possibly
        # huge) transcript, so a single post-render snap lands short — at the
        # TOP — for large sessions. While this is set, every documentSizeChanged
        # (fired as layout/images settle) re-snaps to the true bottom, even for
        # DONE sessions where sticky `_tailing` is off. Cleared after a short
        # window or as soon as the operator scrolls away themselves.
        self._force_bottom_pending = False
        self._force_bottom_timer = QtCore.QTimer(self)
        self._force_bottom_timer.setSingleShot(True)
        self._force_bottom_timer.setInterval(600)
        self._force_bottom_timer.timeout.connect(self._end_force_bottom)
        # Sticky auto-tail intent. When True, every render follows new content
        # to the bottom — independent of the lying `sb.maximum()` heuristic.
        # Flipped to False only when the user actively scrolls (wheel,
        # scrollbar drag, PageUp/Down, arrows). Reset to True on submit + on
        # session show. Fixes the streaming race where one stale at-bottom
        # check would pin the scroll to a mid-conversation position and never
        # recover.
        self._tailing = True
        # Operator's last user-driven scroll position. Captured after each
        # user wheel/drag/key-nav so we have a known-good target to restore
        # to after a full re-render — Qt's scrollbar value briefly reads as 0
        # right after setHtml (layout not done yet), so probing
        # ``sb.value()`` mid-tick lies and clamping prev_val against a stale
        # ``sb.maximum()`` flings the viewport around.
        self._intended_scroll: Optional[int] = None
        # Cached HTML for the non-live (committed) turns, built per
        # show_session (from a per-turn cache, so unchanged turns cost a
        # dict lookup). Streaming never touches it: the document holds the
        # committed HTML once and the live turn sits in its own QTextFrame
        # at the end, replaced in place each tick (see _replace_live_segment).
        self._committed_html: str = ""
        # The live group (see _live_group_ids) _committed_html excludes.
        self._committed_html_for_live_turn: tuple = ()
        self._turn_html_cache: dict[tuple, tuple[str, dict, dict]] = {}
        # What the document currently holds: ("s", session id) / ("j", job
        # id) plus the committed HTML it was built from, and the frame that
        # contains the live segment (None → next render is a full setHtml).
        self._doc_key: Optional[tuple] = None
        self._doc_committed: Optional[str] = None
        self._live_frame: Optional[QtGui.QTextFrame] = None
        # Throwaway import target for the live segment (_insert_html_in_place)
        # and a running count of the table/image elements in-place live
        # updates have inserted since the last full render (see
        # _replace_live_segment's _INPLACE_ELEMENT_BUDGET), plus a selection
        # (anchor, position) in the committed turns to carry across the full
        # render that budget forces.
        self._live_scratch = QtGui.QTextDocument(self)
        self._live_scratch.setUndoRedoEnabled(False)
        self._inplace_elements = 0
        self._carry_selection: Optional[tuple[int, int]] = None
        # Interactive transcript state: Copy-link payloads, screenshot paths
        # behind thumbnails (committed + live), and expanded block keys.
        self._committed_payloads: dict[str, str] = {}
        self._committed_shots: dict[str, str] = {}
        self._live_payloads: dict[str, str] = {}
        self._live_shots: dict[str, str] = {}
        self._expanded: set[str] = set()
        # Window whose key events we watch for Esc-to-stop (see eventFilter).
        self._esc_window: Optional[QtWidgets.QWidget] = None

        # Spinner animation for the live status bar.
        self._spinner_idx = 0
        self._spinner_timer = QtCore.QTimer(self)
        # 180ms ≈ 5.5Hz — still smooth visually, ~half the GUI-thread cost
        # of the previous 80ms (12.5Hz) tick.
        self._spinner_timer.setInterval(180)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        # Track integer-second of last header refresh so we only rebuild the
        # title bar when the displayed elapsed actually changes.
        self._last_header_elapsed_int: int = -1

        # Verb-cycling state: which verb is currently displayed, when its
        # "real" source last changed (so we know when to start cycling), and
        # when the current synonym was swapped in.
        self._last_real_verb: str = ""
        self._last_real_verb_at: float = 0.0
        self._last_cycle_at: float = 0.0
        self._cycle_idx: int = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- Header strip: transcript + thumbnail ----
        # A QFrame (not a bare QWidget) so the QSS hairline under it paints:
        # it separates the title from the transcript scrolling beneath.
        top = QtWidgets.QFrame()
        top.setObjectName("detailHeader")
        top_layout = QtWidgets.QHBoxLayout(top)
        top_layout.setContentsMargins(theme.SPACE_MD, theme.SPACE_MD, theme.SPACE_MD, theme.SPACE_SM)
        top_layout.setSpacing(theme.SPACE_MD)
        self._transcript = QtWidgets.QLabel("")
        self._transcript.setTextFormat(QtCore.Qt.PlainText)
        self._transcript.setProperty("class", "h")
        self._transcript.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_MD}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        self._transcript.setWordWrap(True)
        self._transcript.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        top_layout.addWidget(self._transcript, 1)
        self._thumb = _ClickableThumbnail()
        top_layout.addWidget(self._thumb, 0, QtCore.Qt.AlignTop)
        top.setMaximumHeight(104)
        outer.addWidget(top)

        # ---- Conversation browser (the whole turn timeline, inline tools) ----
        self._reply_browser = _TranscriptBrowser(self._shot_path_for)
        self._reply_browser.setAccessibleName("Conversation")
        # Read-only transcript: no undo history (every streaming tick edits
        # the document in place — an undo stack would grow without bound).
        self._reply_browser.document().setUndoRedoEnabled(False)
        # Never let QTextBrowser navigate itself: setSource() on a relative /
        # file link would replace the whole conversation with that file's
        # contents. Route every click through _on_anchor_clicked instead.
        self._reply_browser.setOpenLinks(False)
        self._reply_browser.setOpenExternalLinks(False)
        self._reply_browser.anchorClicked.connect(self._on_anchor_clicked)
        # User-initiated scroll → exit sticky tailing. We install an event
        # filter on the viewport (wheel/keys) and listen for scrollbar drag
        # actions; programmatic setValue calls (our auto-tail) do NOT fire
        # ``actionTriggered`` so they don't accidentally turn tailing off.
        self._reply_browser.verticalScrollBar().actionTriggered.connect(
            self._on_user_scroll_action
        )
        self._reply_browser.installEventFilter(self)
        self._reply_browser.viewport().installEventFilter(self)
        # Layout-driven tailing: documentSizeChanged fires when QTextBrowser
        # finishes (re)laying out and the document grows — exactly when
        # scrollbar.maximum() becomes correct. setHtml's own maximum() reads
        # stale, so a one-shot snap after setHtml lands short of the true bottom
        # and streaming text drifts off-screen. Snapping here instead makes
        # tailing reliable regardless of layout timing or content size.
        self._reply_browser.document().documentLayout().documentSizeChanged.connect(
            self._on_doc_size_changed
        )
        outer.addWidget(self._reply_browser, 1)

        # ---- The live bar — one strip pinned below the conversation, where
        # the operator's eye (reading the tail) catches the heartbeat: spinner,
        # current verb (cycling synonyms during quiet gaps so even an idle
        # 10s "Thinking…" visibly animates), elapsed / tools / tokens / cost,
        # and the Stop button (Esc). Hidden when no turn is in flight. ----
        self._live_bottom_bar = QtWidgets.QFrame()
        self._live_bottom_bar.setObjectName("liveBottomBar")
        self._live_bottom_bar.setFixedHeight(theme.LIVE_BAR_H + theme.SPACE_XS)
        bb = QtWidgets.QHBoxLayout(self._live_bottom_bar)
        bb.setContentsMargins(theme.SPACE_LG, theme.SPACE_XS, theme.SPACE_MD, theme.SPACE_XS)
        bb.setSpacing(theme.SPACE_MD)
        self._bottom_spinner = QtWidgets.QLabel(_SPINNER_FRAMES[0])
        self._bottom_spinner.setObjectName("liveBottomSpinner")
        self._bottom_spinner.setAccessibleName("Working")
        self._bottom_verb = QtWidgets.QLabel("Working…")
        self._bottom_verb.setTextFormat(QtCore.Qt.PlainText)
        self._bottom_verb.setObjectName("liveBottomVerb")
        # One stable name — the cycling synonyms must not flood a screen reader.
        self._bottom_verb.setAccessibleName("Claude is working")
        self._bottom_meta = QtWidgets.QLabel("")
        self._bottom_meta.setTextFormat(QtCore.Qt.PlainText)
        self._bottom_meta.setObjectName("liveBottomMeta")
        self._cancel_btn = QtWidgets.QPushButton("Stop")
        self._cancel_btn.setObjectName("liveCancel")
        self._cancel_btn.setIcon(iconset.icon("stop", theme.DANGER, theme.TRANSCRIPT_ICON))
        self._cancel_btn.setIconSize(QtCore.QSize(theme.TRANSCRIPT_ICON, theme.TRANSCRIPT_ICON))
        self._cancel_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._cancel_btn.setToolTip("Stop this turn (Esc)")
        self._cancel_btn.setAccessibleName("Stop turn")
        self._cancel_btn.clicked.connect(self._emit_cancel)
        bb.addWidget(self._bottom_spinner, 0)
        bb.addWidget(self._bottom_verb, 1)
        bb.addWidget(self._bottom_meta, 0)
        bb.addWidget(self._cancel_btn, 0, QtCore.Qt.AlignVCenter)
        self._live_bottom_bar.hide()
        outer.addWidget(self._live_bottom_bar)
        # Back-compat names: there used to be a second live bar above the
        # conversation with its own labels. One bar now; the old names point
        # at it.
        self._live_bar = self._live_bottom_bar
        self._spinner_label = self._bottom_spinner
        self._status_label = self._bottom_verb
        self._meta_label = self._bottom_meta

        # Kept for backward compat with `_render_tool_log` callers (DetailWindow).
        # Not added to the layout in this view — DetailWindow re-creates its own.
        self._tool_tree = QtWidgets.QTreeWidget()
        self._tool_tree.setColumnCount(4)
        self._tool_tree.setHeaderLabels(["Tool", "Input", "Duration", "Status"])
        self._tool_tree.hide()

    def set_captures_dir(self, path: Path) -> None:
        self._captures_dir = path

    # ----- document bookkeeping -----

    def _shot_path_for(self, turn_key: str) -> Optional[str]:
        return self._live_shots.get(turn_key) or self._committed_shots.get(turn_key)

    def _forget_document(self) -> None:
        """The document is about to be replaced wholesale: drop everything
        that points into it."""
        self._live_frame = None
        self._doc_key = None
        self._doc_committed = None

    def _valid_live_frame(self) -> Optional[QtGui.QTextFrame]:
        frame = self._live_frame
        if frame is None:
            return None
        try:
            if frame.document() is not self._reply_browser.document():
                raise RuntimeError("stale frame")
            frame.firstPosition()
        except RuntimeError:
            # The browser's content was replaced behind our back (direct
            # setHtml / clear): the frame is gone.
            self._live_frame = None
            return None
        return frame

    def clear(self) -> None:
        """Reset the view to the empty state. Used when nothing is selected."""
        self._job = None
        self._prior_turns = []
        self._session_title = ""
        self._session_for_header = None
        self._committed_turn_seconds = 0.0
        self._committed_turn_count = 0
        self._pending_render = False
        if self._render_timer.isActive():
            self._render_timer.stop()
        self._hide_live_bar()
        self._transcript.setText("New session")
        self._thumb.clear()
        # Default to the minimal "ready" state. The SessionsWindow can layer
        # a richer dashboard via `show_dashboard()` immediately after this.
        mic = _icon_img("mic", theme.FG_MUTED, theme.GLYPH_HERO)
        ready_html = f"""
            <html><body style='background: {theme.CANVAS}; color: {theme.FG};'>
            <div align='center' style='margin: {theme.SPACE_XL * 2}px {theme.SPACE_XL}px;'>
                <p align='center' style='margin: 0 0 {theme.SPACE_LG}px 0;'>{mic}</p>
                <p align='center' style='color: {theme.FG}; font-size: {theme.FONT_XL}pt; font-weight: {theme.WEIGHT_SEMIBOLD};
                          margin: 0 0 {theme.SPACE_SM}px 0;'>Ready for a new session</p>
                <p align='center' style='color: {theme.FG_MUTED}; font-size: {theme.FONT_MD}pt; margin: 0;'>Hold your push-to-talk key to start.<br>Or pick a session on the left to continue it.</p>
            </div>
            </body></html>
        """
        self._forget_document()
        self._reply_browser.setHtml(ready_html)
        self._tool_tree.clear()

    def show_dashboard(self, html_doc: str) -> None:
        """Replace the empty-state splash with a richer dashboard. SessionsWindow
        uses this to surface recents + week cost + errored-sessions triage when
        nothing is selected — the rail is no longer the only useful place."""
        self._job = None
        self._prior_turns = []
        self._transcript.setText("Holler")
        self._thumb.clear()
        self._hide_live_bar()
        # A dashboard reads top-down: drop any sticky tail / session-entry
        # settle left over from a session, or the layout pass after setHtml
        # snaps a taller-than-viewport dashboard to its bottom.
        self._tailing = False
        self._force_bottom_pending = False
        self._force_bottom_timer.stop()
        self._forget_document()
        self._reply_browser.setHtml(html_doc)
        self._reply_browser.verticalScrollBar().setValue(0)

    def show_session(
        self,
        session: Session,
        turns: list[Turn],
        tool_calls_by_turn: dict[int, list[ToolCallRecord]],
        live_job: Optional[Job] = None,
        captures_dir: Optional[Path] = None,
        auto_scroll: bool = True,
    ) -> None:
        """Render the entire conversation: every turn stacked, latest at bottom.

        If `live_job` is set, its streaming text replaces the final turn's
        committed `final_text` (so the live view stays current while the DB
        catches up).

        ``auto_scroll=True`` (the default — preserves caller-side back-compat)
        forces the view to the bottom regardless of the user's current scroll
        position. Pass ``False`` when re-rendering an already-visible session
        (e.g. metadata refresh, sibling-session update side effects) so the
        operator's scroll position is preserved. The at-bottom heuristic
        still auto-tails when the user *was* near the bottom, so
        live-streaming new content remains visible.

        When the session's committed turns are unchanged since the last
        render, only the live segment is replaced — the operator's text
        selection and scroll position survive the refresh.
        """
        if captures_dir is not None:
            self._captures_dir = captures_dir
        if self._doc_key is not None and self._doc_key != ("s", session.id):
            # Different session: expansion state doesn't carry over.
            self._expanded.clear()
        self._session_title = session.title or "Untitled session"
        # Cache the static parts of the header so the spinner tick can refresh
        # the live "total time" without rebuilding everything.
        self._session_for_header = session
        self._committed_turn_seconds = sum(
            (t.duration_ms / 1000.0) for t in turns if t.duration_ms
        )
        self._committed_turn_count = len(turns)
        self._last_turn_model = next(
            (t.model for t in reversed(turns) if getattr(t, "model", "")), ""
        )
        self._refresh_header(live_job)

        # Screenshots render inline, per turn, in the conversation. The header
        # thumbnail only remains for a session screenshot no turn carries.
        inline_shot = any(t.screenshot_path and Path(t.screenshot_path).exists() for t in turns)
        first_thumb = None if inline_shot else session.screenshot_path
        if first_thumb and Path(first_thumb).exists():
            self._thumb.set_image(Path(first_thumb))
        else:
            self._thumb.clear()

        self._prior_turns = list(turns)
        self._tool_calls_by_turn = tool_calls_by_turn
        self._job = live_job

        # Build / refresh the committed-HTML string (per-turn cached).
        live_ids = _live_group_ids(live_job)
        self._committed_html = self._build_committed_html(
            turns, tool_calls_by_turn, live_ids,
        )
        self._committed_html_for_live_turn = live_ids

        if live_job is not None and live_job.status in ("queued", "running"):
            self._show_live_bar(live_job)
        else:
            self._hide_live_bar()

        # Entering a session — land at the BOTTOM (most recent content)
        # regardless of whether the session is live or done. Mirrors Claude
        # Code's web UI: clicking into a conversation always drops you at
        # the latest message. Re-renders triggered by background updates
        # (sibling session activity, this session's own metadata churn)
        # pass auto_scroll=False so they don't yank the operator out of
        # whatever they were reading.
        #
        # Sticky-tailing differs by liveness:
        #   Live (queued/running) → enter sticky tailing so streaming
        #     content keeps landing at the bottom.
        #   Done → no sticky tailing; nothing is streaming and a stray
        #     re-render shouldn't yank the operator if they scroll up
        #     to read prior turns.
        if auto_scroll:
            is_live = (
                live_job is not None
                and live_job.status in ("queued", "running")
            )
            self._force_bottom_next = True
            self._tailing = is_live or not turns
            # Clear stale operator-scroll anchor — they're being yanked to
            # the tail on purpose, prior mid-document position is irrelevant.
            self._intended_scroll = None
        self._render_session_markdown()

    def set_repo_cwd(self, cwd: Optional[Path | str]) -> None:
        """Set the directory relative file links resolve against."""
        self._repo_cwd = Path(cwd) if cwd else None

    def _resolve_link(self, url: QtCore.QUrl) -> Optional[QtCore.QUrl]:
        """Map a clicked link to a URL to open externally, or None to ignore."""
        scheme = url.scheme().lower()
        if scheme in ("http", "https", "mailto"):
            return url
        if scheme == "file":
            local = url.toLocalFile()
            return QtCore.QUrl.fromLocalFile(local) if local else None
        if scheme:
            # Windows drive letters parse as a one-letter scheme ("C:/x").
            if len(scheme) == 1 and url.toString()[1:2] == ":":
                return QtCore.QUrl.fromLocalFile(url.toString())
            return None
        raw = url.path()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            if self._repo_cwd is None:
                return None
            path = self._repo_cwd / path
        return QtCore.QUrl.fromLocalFile(str(path))

    def _on_anchor_clicked(self, url: QtCore.QUrl) -> None:
        # Content of the view must never change on a link click — except our
        # own holler:// actions (copy / expand / open screenshot).
        if url.scheme().lower() == "holler":
            self._on_holler_action(url)
            return
        if not url.scheme() and not url.path() and url.hasFragment():
            frag = url.fragment()
            if frag:
                self._reply_browser.scrollToAnchor(frag)
            return
        target = self._resolve_link(url)
        if target is not None:
            QtGui.QDesktopServices.openUrl(target)

    def _on_holler_action(self, url: QtCore.QUrl) -> None:
        action = url.host().lower()
        parts = [p for p in url.path().split("/") if p]
        if action == "copy" and len(parts) == 2:
            text = self._live_payloads.get(parts[1])
            if text is None:
                text = self._committed_payloads.get(parts[1])
            if text is None:
                return
            QtWidgets.QApplication.clipboard().setText(text)
            self._announce_copy("Copied code" if parts[0] == "code" else "Copied reply")
        elif action == "expand" and len(parts) == 1:
            self._toggle_expanded(parts[0])
        elif action == "open" and len(parts) == 2 and parts[0] == "shot":
            path = self._shot_path_for(parts[1])
            if path:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _announce_copy(self, text: str) -> None:
        """Confirm a copy: through the host (copy_notice) when it listens,
        else as a tooltip at the pointer so the click never goes unanswered."""
        sig = QtCore.QMetaMethod.fromSignal(self.copy_notice)
        if self.isSignalConnected(sig):
            self.copy_notice.emit(text)
        else:
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), text, self._reply_browser)

    def _toggle_expanded(self, key: str) -> None:
        """Expand / collapse one block and re-render it in place, holding the
        viewport still (the clicked block stays under the pointer)."""
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        sb = self._reply_browser.verticalScrollBar()
        live_key = key.startswith("j")
        # Pin the viewport for the re-render — committed blocks (full
        # re-render) and live ones (frame replaced in place) alike. Tailing
        # pauses too, or the size change would snap to the bottom; scrolling
        # back down to the tail resumes it.
        self._tailing = False
        self._force_bottom_pending = False
        self._intended_scroll = sb.value()
        self._hold_scroll_next = True
        if self._prior_turns:
            if not live_key:
                self._committed_html = self._build_committed_html(
                    self._prior_turns, getattr(self, "_tool_calls_by_turn", {}) or {},
                    self._committed_html_for_live_turn,
                )
            self._render_session_markdown()
        elif self._job is not None:
            self._render_now()

    def show_job(self, job: Job, captures_dir: Optional[Path] = None, auto_scroll: bool = True) -> None:
        """Single-job mode (used by the popup DetailWindow).

        See ``show_session`` for the meaning of ``auto_scroll``.
        """
        self._job = job
        self._prior_turns = []
        self._tool_calls_by_turn = {}
        # Drop a *different* session's header cache — otherwise the spinner
        # tick's _refresh_header overwrites this job's transcript line with
        # that stale title. A job of the session already shown (the Sessions
        # window's first-turn path) keeps its session header.
        hdr = getattr(self, "_session_for_header", None)
        if hdr is None or getattr(hdr, "id", None) != getattr(job, "local_session_id", None):
            self._session_for_header = None
        if captures_dir is not None:
            self._captures_dir = captures_dir

        self._transcript.setText(_split_screen_suffix(job.transcript)[0] or "(no speech recognized)")
        if job.thumbnail_path and Path(job.thumbnail_path).exists():
            self._thumb.set_image(Path(job.thumbnail_path))
        else:
            self._thumb.clear()
        if job.status in ("queued", "running"):
            self._show_live_bar(job)
        else:
            self._hide_live_bar()

        # Render the live blocks interleaved (tools + text). Same scroll
        # contract as show_session: only force the bottom on a user-initiated
        # show; background updates preserve the operator's read position.
        if auto_scroll:
            self._force_bottom_next = True
            self._tailing = True
            self._intended_scroll = None
        self._render_job_only()

    def update_streaming(self, job: Job) -> None:
        """Throttled live update — coalesce text_deltas to ~4 renders/sec."""
        if self._job is None or self._job.id != job.id:
            return
        self._job = job
        # Status changes (started/done) update the live bar immediately.
        if job.status in ("queued", "running"):
            self._show_live_bar(job)
        else:
            self._hide_live_bar()
        if job.status in ("done", "error"):
            self._render_now()
            return
        # Update spinner/status meta (cheap) every event; defer the big HTML
        # re-render to the 220ms throttle.
        self._update_live_bar(job)
        if not self._render_timer.isActive():
            self._render_timer.start()
        self._pending_render = True

    def _render_now(self) -> None:
        if self._job is None:
            return
        self._pending_render = False
        if self._prior_turns:
            self._render_session_markdown()
            self._update_live_bar(self._job)
        else:
            self._render_job_only()

    def _render_job_only(self) -> None:
        """Single-job document: the whole job is the live segment."""
        job = self._job
        if job is None:
            return
        ctx = _RenderCtx(f"j{job.id}", frozenset(self._expanded))
        body = _render_job_blocks_html(
            job, highlight_code=job.status not in ("queued", "running"), ctx=ctx)
        self._live_payloads, self._live_shots = ctx.payloads, ctx.shots
        key = ("j", job.id)
        if self._doc_key == key and self._replace_live_segment(body):
            return
        self._set_browser_html(wrap_document(""), live_html=body)
        self._doc_key, self._doc_committed = key, ""

    def _turn_cache_key(self, i: int, turn, calls: list, anchor_here: bool) -> tuple:
        shot = turn.screenshot_path or ""
        try:
            shot_m = Path(shot).stat().st_mtime if shot else 0.0
        except OSError:
            shot_m = -1.0
        return (
            turn.id, i, anchor_here, turn.transcript, turn.final_text, turn.blocks_json,
            bool(turn.is_error), turn.duration_ms, turn.started_at, turn.ended_at, shot, shot_m,
            tuple((tc.id, tc.name, tc.input_json, tc.result_json, bool(tc.is_error)) for tc in calls),
            # Only this turn's own expanded cards — expanding one card must
            # not invalidate every other turn's cached HTML.
            frozenset(k for k in (f"tc{tc.id}" for tc in calls) if k in self._expanded),
        )

    def _build_committed_html(
        self,
        turns: list,
        tool_calls_by_turn: dict,
        live_ids: tuple,
    ) -> str:
        """Render every NON-live turn to a single HTML string.

        The live turn — with any turns merged into it (``live_ids``, see
        _live_group_ids) — is excluded so its segment can be re-rendered
        cheaply on every streaming tick. If there's no live turn, all turns
        get committed-rendered. Each turn's HTML is cached by its content, so a
        refresh of an unchanged session costs dict lookups, not Pygments.
        """
        # Pre-pass: find the index of the LAST non-live turn so we can tag
        # only that one with the reply anchor. The committed turn renderer
        # places the anchor immediately before the assistant text, which is
        # the position session re-entry scrolls to.
        last_committed_idx: Optional[int] = None
        for i, turn in enumerate(turns):
            if turn.id in live_ids:
                continue
            last_committed_idx = i

        parts: list[str] = []
        payloads: dict[str, str] = {}
        shots: dict[str, str] = {}
        cache = self._turn_html_cache
        for i, turn in enumerate(turns, start=1):
            if turn.id in live_ids:
                continue
            calls = tool_calls_by_turn.get(turn.id, []) if tool_calls_by_turn else []
            anchor_here = (
                not live_ids
                and last_committed_idx is not None
                and (i - 1) == last_committed_idx
            )
            key = self._turn_cache_key(i, turn, calls, anchor_here)
            hit = cache.get(key)
            if hit is None:
                ctx = _RenderCtx(f"t{turn.id}", frozenset(self._expanded))
                html_turn = (
                    _render_user_turn_html(i, turn, ctx=ctx)
                    + _render_committed_turn_html(turn, calls, anchor_before_reply=anchor_here, ctx=ctx)
                    + _TURN_DIVIDER_HTML
                )
                hit = (html_turn, ctx.payloads, ctx.shots)
                if len(cache) >= self._TURN_CACHE_CAP:
                    cache.clear()
                cache[key] = hit
            parts.append(hit[0])
            payloads.update(hit[1])
            shots.update(hit[2])
        self._committed_payloads = payloads
        self._committed_shots = shots
        return "".join(parts)

    def _build_live_segment_html(self) -> str:
        """Render the live turn's section. Cheap; called per streaming tick.

        Skips Pygments highlighting while still in flight (queued/running),
        then re-renders WITH Pygments once the job hits a terminal state so
        the final paint matches the committed view.
        """
        if not self._job or not self._prior_turns:
            return ""
        live_ids = _live_group_ids(self._job)
        if not live_ids:
            return ""
        # The live turn, then the turns merged into it, in transcript order.
        group = [(i, t) for i, t in enumerate(self._prior_turns, start=1) if t.id in live_ids]
        if not any(t.id == live_ids[0] for _i, t in group):
            return ""
        in_flight = self._job.status in ("queued", "running")
        ctx = _RenderCtx(f"j{self._job.id}", frozenset(self._expanded))
        parts = [
            *(_render_user_turn_html(i, t, ctx=ctx) for i, t in group),
            # Anchor for re-entry into a session whose latest turn is live.
            # Lands the operator at the top of the reply (first content block),
            # not at their user-turn header. Mirrors the committed-side anchor
            # in _build_committed_html — exactly one anchor with this name
            # exists in the document at any time.
            "<a name='latest-reply-top'></a>",
            _render_job_blocks_html(self._job, highlight_code=not in_flight, ctx=ctx),
            _TURN_DIVIDER_HTML,
        ]
        self._live_payloads, self._live_shots = ctx.payloads, ctx.shots
        return "".join(parts)

    def _ensure_committed_cache(self) -> None:
        """Rebuild the committed-HTML cache if the live-turn id changed.

        Happens when a new live turn lands while the operator is still in the
        same session (e.g. they hit PTT again before the previous turn finished
        committing). Cheap if cache is still valid.
        """
        current_live = _live_group_ids(self._job)
        if current_live == self._committed_html_for_live_turn:
            return
        self._committed_html = self._build_committed_html(
            self._prior_turns,
            getattr(self, "_tool_calls_by_turn", {}) or {},
            current_live,
        )
        self._committed_html_for_live_turn = current_live

    def _render_session_markdown(self) -> None:
        """Committed HTML (set once) + the live segment (replaced in place).

        When the document already holds this session's committed HTML, only
        the live segment's frame is rewritten — cost is independent of the
        session's length and a selection in committed content survives.
        Otherwise the whole document is rebuilt.
        """
        self._ensure_committed_cache()
        live_segment = self._build_live_segment_html()
        if not self._committed_html and not live_segment:
            live_segment = (f"<p style='color:{_FG_DIM}'>No turns yet — hold your "
                            f"push-to-talk key to talk.</p>")
        sess = self._session_for_header
        key = ("s", getattr(sess, "id", None))
        if (self._doc_key == key and self._doc_committed == self._committed_html
                and self._replace_live_segment(live_segment)):
            return
        self._set_browser_html(wrap_document(self._committed_html), live_html=live_segment)
        self._doc_key, self._doc_committed = key, self._committed_html

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:
        if (ev.key() == QtCore.Qt.Key_Escape and self._live_turn_running()
                and not self._capture_in_progress()):
            self._emit_cancel()
            return
        super().keyPressEvent(ev)

    def _render_combined_tool_log(
        self,
        turns: list[Turn],
        tool_calls_by_turn: dict[int, list[ToolCallRecord]],
        live_job: Optional[Job],
    ) -> None:
        """Bottom tool tree across all turns, grouped by turn number."""
        self._tool_tree.clear()
        for i, turn in enumerate(turns, start=1):
            calls = tool_calls_by_turn.get(turn.id, [])
            # If this is the live turn, prefer the in-flight job's tool_log
            # (more accurate during streaming).
            if live_job is not None and live_job.local_turn_id == turn.id and live_job.tool_log:
                calls_live = live_job.tool_log
                entries_iter = [
                    (e.name, e.input, e.duration_ms, e.result_preview, e.is_error)
                    for e in calls_live
                ]
            else:
                entries_iter = [
                    (
                        tc.name,
                        _safe_loads(tc.input_json),
                        int((tc.ended_at - tc.started_at) * 1000) if tc.ended_at else 0,
                        tc.result_json,
                        tc.is_error,
                    )
                    for tc in calls
                ]
            if not entries_iter:
                continue
            group = QtWidgets.QTreeWidgetItem([f"Turn {i}", f"{len(entries_iter)} tool{'s' if len(entries_iter) != 1 else ''}", "", ""])
            for name, inp, dur_ms, result_text, is_err in entries_iter:
                top = QtWidgets.QTreeWidgetItem([
                    name,
                    summarize_tool(name, inp, max_len=100),
                    f"{dur_ms}ms" if dur_ms else "—",
                    "Failed" if is_err else ("Done" if dur_ms else "Running"),
                ])
                if is_err:
                    for col in range(4):
                        top.setForeground(col, QtGui.QBrush(QtGui.QColor(_RED)))
                # Children: pretty input + result preview
                try:
                    pretty = json.dumps(inp, indent=2, default=str) if isinstance(inp, (dict, list)) else str(inp)
                except Exception:
                    pretty = repr(inp)
                input_child = QtWidgets.QTreeWidgetItem(["", "Input:", "", ""])
                input_child.addChild(QtWidgets.QTreeWidgetItem(["", pretty, "", ""]))
                top.addChild(input_child)
                if result_text:
                    res_child = QtWidgets.QTreeWidgetItem(["", "Result:", "", ""])
                    res_text_item = QtWidgets.QTreeWidgetItem(["", str(result_text), "", ""])
                    if is_err:
                        res_text_item.setForeground(1, QtGui.QBrush(QtGui.QColor(_RED)))
                    res_child.addChild(res_text_item)
                    top.addChild(res_child)
                group.addChild(top)
            self._tool_tree.addTopLevelItem(group)
            group.setExpanded(True)

    def _merge_live_tool_log_in_place(self, live_job: Job) -> None:
        """Cheap append of live tool_log entries to the existing tree."""
        # Easiest: rebuild only the LAST turn group. Find or create it.
        if not self._prior_turns:
            return
        last_turn = self._prior_turns[-1]
        # Walk top-level items; find the "Turn N" group for last turn.
        # For simplicity we rebuild the full tree — it's small and fast.
        # (We could cache row items by id later if profiling demands.)
        committed: dict[int, list[ToolCallRecord]] = {}
        self._render_combined_tool_log(self._prior_turns, committed, live_job)

    # --- rendering helpers ---

    def request_scroll_to_bottom(self) -> None:
        """Re-enter sticky auto-tail mode and snap to the current bottom now.
        Called from SessionsWindow when the operator submits a text turn —
        they want to see the reply land, even if they were scrolled up
        reading prior content.

        Sticky: subsequent streaming renders keep tailing until the user
        actively scrolls (mouse wheel, scrollbar drag, PageUp/Down, arrows).
        The previous one-shot ``_force_bottom_next`` flag survived only the
        FIRST render after submit, then handed off to a position-based
        heuristic that could spuriously decide "not at bottom" on Qt's
        post-setHtml stale ``sb.maximum()`` and re-pin scroll to a stale
        position. Sticky tailing closes that race.
        """
        self._tailing = True
        self._force_bottom_next = True
        # Operator explicitly wants the tail — any prior scroll anchor is
        # stale and would compete with the next streaming render's restore.
        self._intended_scroll = None
        # Snap to bottom now AND again after Qt layout settles. The new
        # user turn is persisted on a background thread, so the first
        # render that includes it may fire after this method returns. The
        # 50ms retry catches that case so the operator's just-submitted
        # message is visible without them having to scroll. Two singleShots
        # only — not a streaming-tick concern (this fires once per submit).
        QtCore.QTimer.singleShot(0, self, self._scroll_to_bottom)
        QtCore.QTimer.singleShot(50, self, self._scroll_to_bottom)
        QtCore.QTimer.singleShot(150, self, self._scroll_to_bottom)

    # How close to the bottom (px) counts as "at the bottom" for re-sticking
    # auto-tail after the user scrolls back down.
    _TAIL_RESTICK_PX = 40

    def _on_doc_size_changed(self, _new_size) -> None:
        """Fires when the document finishes (re)layout and its size changes.
        That's the moment scrollbar.maximum() is finally correct, so if we're
        tailing — or inside a forced-bottom entry settle window — snap to the
        true bottom here. Robust against setHtml's stale-maximum timing for any
        content size, including large done sessions where tailing is off."""
        if self._tailing or self._force_bottom_pending:
            sb = self._reply_browser.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _end_force_bottom(self) -> None:
        """Close the session-entry settle window; later layout changes no
        longer force the viewport to the bottom (so the operator's own scroll
        position is respected once the transcript has settled)."""
        self._force_bottom_pending = False

    def _on_user_scroll_action(self, _action: int) -> None:
        """Fired only on user-initiated scrollbar actions (drag, click,
        keyboard step). Programmatic ``setValue`` calls (our auto-tail) do
        NOT trigger this, so auto-scrolling never exits tailing mode."""
        # The action fires BEFORE Qt updates sb.value(); decide after the
        # current event finishes processing (re-stick if they land at bottom).
        QtCore.QTimer.singleShot(0, self, self._update_tailing_from_scroll)

    def _update_tailing_from_scroll(self) -> None:
        """Re-evaluate tailing from the operator's current scroll position:
        at/near the bottom re-enables tailing (so returning to the tail tracks
        the latest message again); higher up pauses it and records the anchor."""
        sb = self._reply_browser.verticalScrollBar()
        at_bottom = (sb.maximum() - sb.value()) <= self._TAIL_RESTICK_PX
        self._tailing = at_bottom
        # An intentional scroll away from the tail cancels the entry settle
        # window so we don't yank the operator back down mid-read.
        if not at_bottom:
            self._force_bottom_pending = False
        self._intended_scroll = None if at_bottom else sb.value()

    def showEvent(self, ev: QtGui.QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(ev)
        self._watch_window_for_esc()

    def _watch_window_for_esc(self) -> None:
        """Esc stops the running turn from anywhere in the hosting window.

        Hosts keep keyboard focus elsewhere (the Sessions window holds the
        transcript NoFocus and focus in its composer), so Esc rarely reaches
        this widget's keyPressEvent. Key events the focused widget doesn't
        consume propagate up to the window; a filter there catches Esc. A
        widget that uses Esc itself (search box shortcut, an open popup or
        dialog — separate windows) still gets it first."""
        win = self.window()
        if win is self or win is self._esc_window:
            return
        if self._esc_window is not None:
            try:
                self._esc_window.removeEventFilter(self)
            except RuntimeError:
                pass
        self._esc_window = win
        win.installEventFilter(self)

    def _live_turn_running(self) -> bool:
        return self._job is not None and self._job.status in ("queued", "running")

    def eventFilter(self, obj, ev) -> bool:
        """Catch wheel + key events on the browser that imply the user is
        actively scrolling away from the tail (returns False so the event
        still reaches the widget for actual scrolling), and Esc reaching the
        hosting window while a turn runs (→ stop it)."""
        et = ev.type()
        if obj is self._esc_window and obj is not None:
            if (et == QtCore.QEvent.KeyPress and ev.key() == QtCore.Qt.Key_Escape
                    and not (ev.modifiers() & ~QtCore.Qt.KeypadModifier)
                    and self.isVisible() and self._live_turn_running()
                    and not self._capture_in_progress()):
                self._emit_cancel()
                return True
            return False
        if et == QtCore.QEvent.Wheel:
            # Re-evaluate tailing from the resulting scroll position after the
            # event applies: scrolling up pauses tailing, scrolling (or staying)
            # at the bottom keeps it tracking the latest message.
            QtCore.QTimer.singleShot(0, self, self._update_tailing_from_scroll)
        elif et == QtCore.QEvent.KeyPress:
            k = ev.key()
            if k in (
                QtCore.Qt.Key_PageUp, QtCore.Qt.Key_PageDown,
                QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
                QtCore.Qt.Key_Home, QtCore.Qt.Key_End,
            ):
                QtCore.QTimer.singleShot(0, self, self._update_tailing_from_scroll)
        return super().eventFilter(obj, ev)

    def _render_reply(self, md_text: str) -> None:
        self._set_browser_html(render_markdown(md_text) or "")

    def _take_scroll_intent(self) -> tuple[bool, bool, Optional[str], bool]:
        """Consume the one-shot scroll flags → (force, at_bottom, anchor, hold)."""
        sb = self._reply_browser.verticalScrollBar()
        force = self._force_bottom_next
        self._force_bottom_next = False
        anchor = self._force_anchor_next
        self._force_anchor_next = None
        hold = self._hold_scroll_next
        self._hold_scroll_next = False
        # Sticky tail wins; otherwise fall back to the position heuristic for
        # cases like opening a session with auto_scroll=False where the user
        # was already at the bottom.
        at_bottom = force or (not hold and (
            self._tailing or (sb.maximum() - sb.value()) < 80))
        return force, at_bottom, anchor, hold

    def _set_browser_html(self, html_doc: str, live_html: Optional[str] = None) -> None:
        """Replace the browser content. Auto-tail when sticky tailing is on
        OR when the user was within 80px of the bottom (back-compat for
        callers that didn't explicitly enter tailing). Programmatic scroll
        uses cursor-based positioning because ``sb.maximum()`` lies
        immediately after ``setHtml`` (Qt layout isn't done yet).

        ``live_html`` (optional) is appended inside its own QTextFrame — the
        live segment that later ticks replace in place."""
        sb = self._reply_browser.verticalScrollBar()
        prev_val = sb.value()
        force, at_bottom, anchor, _hold = self._take_scroll_intent()

        # Capture the restore target BEFORE setHtml. Prefer the operator's
        # last user-driven scroll position over the live sb.value(), which
        # may already have been reset to 0 by an in-flight prior setHtml
        # whose deferred-restore hasn't fired yet. That race is what makes
        # streaming re-renders fling the viewport between random positions.
        restore_target = (
            self._intended_scroll if self._intended_scroll is not None else prev_val
        )

        self._forget_document()
        self._reply_browser.setHtml(html_doc or "")
        if live_html is not None:
            doc = self._reply_browser.document()
            cur = QtGui.QTextCursor(doc)
            cur.movePosition(QtGui.QTextCursor.End)
            self._live_frame = cur.insertFrame(QtGui.QTextFrameFormat())
            if live_html:
                inner = QtGui.QTextCursor(doc)
                inner.setPosition(self._live_frame.firstPosition())
                _insert_html_in_place(inner, wrap_document(live_html), self._live_scratch)
        self._inplace_elements = 0
        budget_rerender, self._budget_rerender = getattr(self, "_budget_rerender", False), False
        carry, self._carry_selection = self._carry_selection, None
        if carry is not None:
            # Forced full render of an unchanged committed part (the in-place
            # budget ran out): the same positions hold the same text.
            frame = self._live_frame
            limit = frame.firstPosition() if frame is not None else 0
            if max(carry) < limit:
                cur = QtGui.QTextCursor(self._reply_browser.document())
                cur.setPosition(carry[0])
                cur.setPosition(carry[1], QtGui.QTextCursor.KeepAnchor)
                self._reply_browser.setTextCursor(cur)

        if anchor:
            # One-shot land at the named anchor (top of latest turn). Fired
            # after the next event-loop tick so Qt's layout has settled —
            # scrollToAnchor against a freshly-setHtml doc returns false
            # otherwise.
            QtCore.QTimer.singleShot(
                0, self, lambda a=anchor: self._reply_browser.scrollToAnchor(a)
            )
        elif at_bottom:
            # One scroll to bottom after layout settles. We previously fired
            # THREE retries (immediate + singleShot(0) + singleShot(50)) at
            # every render tick — 4-12 layout passes/sec while streaming
            # contributed materially to GUI thread saturation.
            #
            # On a forced-bottom entry, also arm the settle window so the
            # documentSizeChanged handler keeps re-snapping to the true bottom
            # as the large transcript finishes laying out. Without this, a done
            # session (sticky tailing off) lands at the top because this single
            # snap fires against setHtml's stale `maximum()`. Streaming ticks
            # (tailing on) don't arm it — documentSizeChanged already tracks
            # them via `_tailing`.
            if force:
                self._force_bottom_pending = True
                self._force_bottom_timer.start()
            QtCore.QTimer.singleShot(0, self, self._scroll_to_bottom)
        else:
            if budget_rerender:
                self._reply_browser.document().size()  # force layout now
                sb.setValue(min(restore_target, sb.maximum()))
            # Defer the restore — sb.maximum() reads as the OLD value (or 0)
            # immediately after setHtml. Clamping against it would pin the
            # viewport to the top or to whatever stale max Qt reports while
            # mid-layout.
            QtCore.QTimer.singleShot(
                0, self, lambda t=restore_target: sb.setValue(min(t, sb.maximum()))
            )

    def _replace_live_segment(self, live_html: str) -> bool:
        """Swap the live segment's frame contents for ``live_html``, leaving
        everything before it (the committed turns, any selection in them, and
        the viewport when it isn't tailing) untouched. One edit block → one
        incremental layout of the tail only. Returns False when there is no
        live frame (the caller then does a full render)."""
        frame = self._valid_live_frame()
        if frame is None:
            return False
        doc = self._reply_browser.document()
        # Qt never frees the formats of removed tables (each table/cell
        # carries its own object index → its own format entry) until the
        # document is reset, so every in-place tick grows the document by
        # ~1 KB per table in the live segment. Past the budget, take the full
        # render instead (it starts clean), keeping a selection in the
        # committed turns — that HTML is unchanged, so its positions hold.
        added = live_html.count("<table") + live_html.count("<td") + live_html.count("<img")
        if self._inplace_elements + added > self._INPLACE_ELEMENT_BUDGET:
            sel = self._reply_browser.textCursor()
            self._carry_selection = (
                (sel.anchor(), sel.position())
                if sel.hasSelection() and max(sel.anchor(), sel.position()) < frame.firstPosition()
                else None)
            # The reader may be scrolled up mid-stream: restore the position
            # synchronously after this rebuild so the view doesn't flash to
            # the top for a frame (rare path — the layout cost is fine here).
            self._budget_rerender = True
            return False
        self._inplace_elements += added
        sb = self._reply_browser.verticalScrollBar()
        prev_val = sb.value()
        force, at_bottom, anchor, hold = self._take_scroll_intent()
        cur = QtGui.QTextCursor(doc)
        cur.beginEditBlock()
        cur.setPosition(frame.firstPosition())
        cur.setPosition(frame.lastPosition(), QtGui.QTextCursor.KeepAnchor)
        cur.removeSelectedText()
        # The block left behind keeps the removed segment's LAST block format
        # (the turn divider's <hr> margins / ruler); reset it so the new
        # segment lays out exactly like a fresh frame — otherwise everything
        # in it shifts down on the first in-place replace.
        cur.setBlockFormat(QtGui.QTextBlockFormat())
        cur.setCharFormat(QtGui.QTextCharFormat())
        if live_html:
            _insert_html_in_place(cur, wrap_document(live_html), self._live_scratch)
        cur.endEditBlock()
        if anchor:
            QtCore.QTimer.singleShot(
                0, self, lambda a=anchor: self._reply_browser.scrollToAnchor(a)
            )
        elif at_bottom:
            if force:
                self._force_bottom_pending = True
                self._force_bottom_timer.start()
            QtCore.QTimer.singleShot(0, self, self._scroll_to_bottom)
        elif hold:
            # Expand / collapse inside the live turn: keep the clicked line
            # put even if the edit briefly shrank the document (clamping the
            # scrollbar) before it re-grew.
            sb.setValue(min(prev_val, sb.maximum()))
            QtCore.QTimer.singleShot(0, self, lambda t=prev_val: sb.setValue(min(t, sb.maximum())))
        # Not tailing: nothing above the live segment moved, so the viewport
        # stays exactly where the operator left it — no restore needed.
        return True

    def _scroll_to_bottom(self) -> None:
        cursor = self._reply_browser.textCursor()
        # Moving the text cursor would drop the operator's selection; with a
        # selection, the scrollbar alone does it (documentSizeChanged keeps
        # correcting it as layout settles).
        if not cursor.hasSelection():
            cursor.movePosition(QtGui.QTextCursor.End)
            self._reply_browser.setTextCursor(cursor)
            self._reply_browser.ensureCursorVisible()
        sb = self._reply_browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ----- live status bar (spinner + tool/elapsed + stop) -----

    def _show_live_bar(self, job: Job) -> None:
        self._live_bottom_bar.show()
        if not self._spinner_timer.isActive():
            self._spinner_timer.start()
        self._update_live_bar(job)

    def _hide_live_bar(self) -> None:
        self._live_bottom_bar.hide()
        if self._spinner_timer.isActive():
            self._spinner_timer.stop()
        # Reset verb-cycle state so the next session starts fresh.
        self._last_real_verb = ""
        self._last_real_verb_at = 0.0
        self._last_cycle_at = 0.0
        self._cycle_idx = 0

    def _real_verb_for(self, job: Job) -> str:
        """The 'true' verb for the current job state — pre-synonym-cycling."""
        if job.current_tool is not None:
            return summarize_tool(job.current_tool.name, job.current_tool.input, max_len=80)
        if job.streaming_text:
            return "Responding…"
        return "Thinking…"

    def _display_verb_for(self, real_verb: str, now: float) -> str:
        """Apply verb-cycling: when the real verb has been static for a while,
        rotate through synonyms so the UI looks alive even during silent gaps.
        Tool-name verbs (anything that isn't 'Thinking…' / 'Responding…') are
        never cycled — those already carry real signal.
        """
        if real_verb != self._last_real_verb:
            self._last_real_verb = real_verb
            self._last_real_verb_at = now
            self._last_cycle_at = now
            self._cycle_idx = 0
            return real_verb
        # Only cycle the two generic states. Tool-call verbs stay literal.
        if real_verb == "Thinking…":
            pool = _QUIET_VERBS_THINKING
        elif real_verb == "Responding…":
            pool = _QUIET_VERBS_RESPONDING
        else:
            return real_verb
        if (now - self._last_real_verb_at) < _QUIET_VERB_CYCLE_AFTER_S:
            return real_verb
        if (now - self._last_cycle_at) >= _QUIET_VERB_CYCLE_INTERVAL_S:
            self._cycle_idx = (self._cycle_idx + 1) % len(pool)
            self._last_cycle_at = now
        return pool[self._cycle_idx]

    def _update_live_bar(self, job: Job) -> None:
        # Status verb = current tool summary, or "Thinking…" if no tool yet.
        real_verb = self._real_verb_for(job)
        display_verb = self._display_verb_for(real_verb, time_mod.time())
        self._bottom_verb.setText(display_verb)
        # Meta: elapsed · tools · tokens · $cost
        if job.started_at:
            elapsed = max(0.0, time_mod.time() - job.started_at)
            elapsed_s = f"{elapsed:0.1f}s" if elapsed < 60 else f"{int(elapsed//60)}m {int(elapsed%60):02d}s"
        else:
            elapsed_s = ""
        tools = len(job.tool_log)
        parts = []
        if elapsed_s:
            parts.append(elapsed_s)
        if tools:
            parts.append(f"{tools} tool{'s' if tools != 1 else ''}")
        # Tokens — only new content (fresh input + output). Cache reads/creates
        # are the same conversation being re-billed each turn, not new tokens.
        new_tokens = job.input_tokens + job.output_tokens
        if new_tokens:
            parts.append(f"{_fmt_tokens(new_tokens)} tokens")
        if job.total_cost_usd:
            parts.append(f"${job.total_cost_usd:.3f}")
        self._bottom_meta.setText(" · ".join(parts))

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
        self._bottom_spinner.setText(_SPINNER_FRAMES[self._spinner_idx])
        # Refresh meta + session header. The header is the costlier of the
        # two (string concat + model lookup); only rebuild it when the
        # displayed integer second actually changed, since that's the only
        # part of the title that mutates between ticks.
        if self._job is not None and self._job.status in ("queued", "running"):
            self._update_live_bar(self._job)
            if getattr(self, "_session_for_header", None) is not None:
                elapsed_int = int(max(0.0, time_mod.time() - (self._job.started_at or time_mod.time())))
                if elapsed_int != self._last_header_elapsed_int:
                    self._last_header_elapsed_int = elapsed_int
                    self._refresh_header(self._job)

    def _refresh_header(self, live_job: Optional[Job]) -> None:
        """Rebuild the title bar string: `Title · N turns · 1m 14s total · Model`."""
        sess = getattr(self, "_session_for_header", None)
        if sess is None:
            return
        bits: list[str] = [
            sess.title or "Untitled session",
            f"{self._committed_turn_count} turn{'s' if self._committed_turn_count != 1 else ''}",
        ]
        total = self._committed_turn_seconds
        if live_job is not None and live_job.status in ("queued", "running") and live_job.started_at:
            total += max(0.0, time_mod.time() - live_job.started_at)
        if total > 0:
            bits.append(_fmt_duration(total) + " total")
        # The model the latest turn ran, not the one the session started on —
        # the badge can switch models mid-session.
        model = (getattr(live_job, "model", "") if live_job is not None else "") \
            or getattr(self, "_last_turn_model", "") or sess.model
        if model:
            from holler.model_prefix import format_model_label
            bits.append(format_model_label(model))
        self._transcript.setText(" · ".join(bits))

    def _emit_cancel(self) -> None:
        if self._job is not None:
            self.cancel_requested.emit(self._job.id)

    def _render_tool_log(self, entries: list[ToolEntry]) -> None:
        self._tool_tree.clear()
        for entry in entries:
            top = QtWidgets.QTreeWidgetItem([
                entry.name,
                summarize_tool(entry.name, entry.input, max_len=100),
                f"{entry.duration_ms}ms" if entry.duration_ms else "—",
                "Failed" if entry.is_error else ("Done" if entry.ended_ts else "Running"),
            ])
            if entry.is_error:
                for col in range(4):
                    top.setForeground(col, QtGui.QBrush(QtGui.QColor(_RED)))
            # Child rows: full input + result preview
            try:
                input_pretty = json.dumps(entry.input, indent=2, default=str)
            except Exception:
                input_pretty = repr(entry.input)
            input_child = QtWidgets.QTreeWidgetItem(["", "Input:", "", ""])
            input_child.setData(0, QtCore.Qt.UserRole, input_pretty)
            input_child.setToolTip(1, input_pretty[:4000])
            input_text = QtWidgets.QTreeWidgetItem(["", input_pretty, "", ""])
            input_text.setFont(1, QtGui.QFont(mono_font_family()))
            input_child.addChild(input_text)
            top.addChild(input_child)

            if entry.result_preview:
                result_child = QtWidgets.QTreeWidgetItem(["", "Result:", "", ""])
                result_text = QtWidgets.QTreeWidgetItem(["", entry.result_preview, "", ""])
                result_text.setFont(1, QtGui.QFont(mono_font_family()))
                if entry.is_error:
                    result_text.setForeground(1, QtGui.QBrush(QtGui.QColor(_RED)))
                result_child.addChild(result_text)
                top.addChild(result_child)

            self._tool_tree.addTopLevelItem(top)


class _ClickableThumbnail(QtWidgets.QLabel):
    """A small 144x84 thumbnail label that opens the source PNG in the OS viewer on click.

    Deliberately compact so the transcript below gets the vertical space — the
    screenshot is a glance-reference, click to open full-size.
    """

    # Class-level LRU of pre-scaled QPixmaps keyed by (path, mtime). Caps at
    # 64 entries so we don't grow forever for long-running sessions. Loading
    # a fullsize PNG is the dominant cost of every show_session; cache means
    # switching between sessions paints instantly after the first visit.
    _THUMB_CACHE: dict[tuple, QtGui.QPixmap] = {}
    _THUMB_CACHE_CAP = 64

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(144, 84)
        # One rule scoped to the label itself (not concatenated declarations
        # that would cascade to anything the label hosts).
        self.setStyleSheet(
            f"QLabel {{ background: {_BG_ROW}; border: 1px solid {theme.BORDER}; "
            f"border-radius: {theme.RADIUS_SM}px; color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; }}"
        )
        self.setAlignment(QtCore.Qt.AlignCenter)
        self._source: Optional[Path] = None
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setToolTip("Open screenshot")
        self.setAccessibleName("Screenshot — open full size")
        # Focusable so it opens from the keyboard too (Space / Return).
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        # No empty "(no screenshot)" box: hidden until there's an image.
        self.hide()

    def set_image(self, path: Path) -> None:
        self._source = path
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        key = (str(path), mtime)
        scaled = self._THUMB_CACHE.get(key)
        if scaled is None:
            pix = QtGui.QPixmap(str(path))
            if pix.isNull():
                self.setText("Can't show screenshot")
                self.show()
                return
            scaled = pix.scaled(
                self.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.SmoothTransformation,
            )
            # Evict the oldest entry once the cap is reached. dict in Python
            # 3.7+ preserves insertion order, so popping the first key is FIFO.
            if len(self._THUMB_CACHE) >= self._THUMB_CACHE_CAP:
                first_key = next(iter(self._THUMB_CACHE))
                self._THUMB_CACHE.pop(first_key, None)
            self._THUMB_CACHE[key] = scaled
        self.setPixmap(scaled)
        self.show()

    def clear(self) -> None:  # type: ignore[override]
        self._source = None
        super().clear()
        self.hide()

    def _open(self) -> None:
        if self._source:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self._source)))

    def mousePressEvent(self, ev: QtGui.QMouseEvent) -> None:
        if ev.button() == QtCore.Qt.LeftButton:
            self._open()

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:
        if ev.key() in (QtCore.Qt.Key_Space, QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self._open()
            return
        super().keyPressEvent(ev)


class DetailWindow(QtWidgets.QMainWindow):
    """Standalone detail window — opens on row-click in the panel."""

    closed = QtCore.Signal(int)  # job_id

    def __init__(
        self,
        job: Job,
        captures_dir: Path,
        store: JobStore,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._job_id = job.id
        self._store = store
        self._captures_dir = captures_dir
        self._screen_signals_connected: bool = False

        self.setStyleSheet(_detail_qss())
        title = (_split_screen_suffix(job.transcript)[0] or "(no speech recognized)").strip().split("\n", 1)[0]
        if len(title) > 60:
            title = title[:59] + "…"
        self.setWindowTitle(f"{title} — Holler")
        # Size against the screen the cursor is on rather than a fixed 900x900
        # that overflows 1366x768 laptops. Falls back to 900x900 on a bad
        # screen handle so we don't shrink unnecessarily on big displays.
        self._apply_adaptive_default_size()

        # Central: button row above the DetailView.
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        button_row = QtWidgets.QHBoxLayout()
        button_row.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_MD, theme.SPACE_SM)
        button_row.setSpacing(theme.SPACE_SM)
        copy_reply_btn = QtWidgets.QPushButton("Copy reply")
        copy_reply_btn.clicked.connect(self._copy_reply)
        copy_log_btn = QtWidgets.QPushButton("Copy tool log")
        copy_log_btn.clicked.connect(self._copy_tool_log)
        open_caps_btn = QtWidgets.QPushButton("Show screenshots folder")
        open_caps_btn.clicked.connect(self._open_captures)
        button_row.addWidget(copy_reply_btn)
        button_row.addWidget(copy_log_btn)
        button_row.addWidget(open_caps_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._view = DetailView(self)
        self._view.set_captures_dir(captures_dir)
        layout.addWidget(self._view, 1)

        self.setCentralWidget(central)
        self._view.show_job(job, captures_dir)

        store.job_updated.connect(self._on_job_updated)

    def set_repo_cwd(self, cwd: Optional[Path | str]) -> None:
        """Directory relative file links resolve against (see DetailView)."""
        self._view.set_repo_cwd(cwd)

    def job_id(self) -> int:
        return self._job_id

    @QtCore.Slot(int)
    def _on_job_updated(self, job_id: int) -> None:
        if job_id != self._job_id:
            return
        job = self._store.get(job_id)
        if job is None:
            return
        self._view.update_streaming(job)

    def showEvent(self, ev: QtGui.QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(ev)
        # Subscribe once. DetailWindows are pooled in runners.full's
        # detail_windows dict, so a re-show of a cached instance must not
        # double-connect signal handlers.
        if self._screen_signals_connected:
            return
        app = QtGui.QGuiApplication.instance()
        if app is not None:
            try:
                app.screenRemoved.connect(self._clamp_self_to_screen)
            except Exception:
                pass
        wh = self.windowHandle()
        if wh is not None:
            try:
                wh.screenChanged.connect(self._clamp_self_to_screen)
            except Exception:
                pass
        self._screen_signals_connected = True

    def _clamp_self_to_screen(self, *_args) -> None:
        """Re-validate position against the screen the window sits on now.

        Position-only — does not resize. If the user shrank a monitor's
        resolution while the window was off-screen, it may still overflow,
        but resizing on screen change is out of scope.

        Drag-aware: if the mouse button is still held (active drag), defer
        until release. Calling move() during a WM_MOVING grab on Windows
        aborts the drag, so the operator can't move the window across
        monitor boundaries.
        """
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            self._clamp_retry_count = getattr(self, "_clamp_retry_count", 0) + 1
            if self._clamp_retry_count > 20:  # ~4s ceiling at 200ms ticks
                self._clamp_retry_count = 0
                return
            QtCore.QTimer.singleShot(200, self, self._clamp_self_to_screen)
            return
        self._clamp_retry_count = 0
        try:
            geom = self.frameGeometry()
            scr = QtGui.QGuiApplication.screenAt(geom.center())
            if scr is None:
                scr = QtGui.QGuiApplication.primaryScreen()
            if scr is None:
                return
            avail = scr.availableGeometry()
            cur = self.geometry()
            new_x = max(avail.left(), min(cur.x(), avail.right() - cur.width()))
            new_y = max(avail.top(), min(cur.y(), avail.bottom() - cur.height()))
            if (new_x, new_y) != (cur.x(), cur.y()):
                self.move(new_x, new_y)
        except Exception:
            pass

    def _apply_adaptive_default_size(self) -> None:
        """Pick a default size that fits the cursor's screen.

        Caps at 900x900 (the historical default) on big displays so the
        window doesn't stretch uselessly, but shrinks below that on small
        laptops where the fixed value would overflow the screen.
        """
        pos = QtGui.QCursor.pos()
        scr = QtGui.QGuiApplication.screenAt(pos) or QtGui.QGuiApplication.primaryScreen()
        if scr is None:
            self.resize(900, 900)
            return
        avail = scr.availableGeometry()
        w = min(900, max(560, int(avail.width() * 0.55)))
        h = min(900, max(480, int(avail.height() * 0.85)))
        w = min(w, avail.width())
        h = min(h, avail.height())
        self.resize(w, h)

    def _copy_reply(self) -> None:
        job = self._store.get(self._job_id)
        if job is None:
            return
        QtWidgets.QApplication.clipboard().setText(job.final_text or job.streaming_text or "")

    def _copy_tool_log(self) -> None:
        job = self._store.get(self._job_id)
        if job is None:
            return
        log_data = [
            {
                "id": e.id, "name": e.name, "input": e.input,
                "duration_ms": e.duration_ms, "result_preview": e.result_preview,
                "is_error": e.is_error,
            }
            for e in job.tool_log
        ]
        QtWidgets.QApplication.clipboard().setText(json.dumps(log_data, indent=2, default=str))

    def _open_captures(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self._captures_dir)))

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        # Disconnect screen-change signals so the cached DetailWindow doesn't
        # keep firing handlers after it's removed from the pool. Wrapped in
        # try/except RuntimeError because Qt raises on already-disconnected
        # signals.
        if self._screen_signals_connected:
            app = QtGui.QGuiApplication.instance()
            if app is not None:
                try:
                    app.screenRemoved.disconnect(self._clamp_self_to_screen)
                except (RuntimeError, TypeError):
                    pass
            wh = self.windowHandle()
            if wh is not None:
                try:
                    wh.screenChanged.disconnect(self._clamp_self_to_screen)
                except (RuntimeError, TypeError):
                    pass
            self._screen_signals_connected = False
        self.closed.emit(self._job_id)
        super().closeEvent(ev)
