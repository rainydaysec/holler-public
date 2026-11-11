"""Build a synthetic 'prior conversation' block for SDK resume.

The Claude Agent SDK only supports resume via its internal JSONL store at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. When Holler restarts
that JSONL may be missing, empty, or stale — the model spawns with zero
memory of the conversation. Holler's own SQLite store has the full turn
history, though, so we rebuild context ourselves: each time a fresh SDK
subprocess is started for a session that has prior turns on disk, we
prepend a single user-message block summarizing the whole conversation
so far. The model reads it the same way it would read a pasted transcript.

This module is pure: it takes Turn + ToolCallRecord rows and returns text.
Token budgeting uses a rough chars/4 heuristic (close enough for caps).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Optional

from holler.domain import ToolCallRecord, Turn


# Rough chars-per-token heuristic. The Anthropic tokenizer is closer to 3.5
# for English prose with code mixed in; 4 is a safe-enough cap for budgeting.
_CHARS_PER_TOKEN = 4

# Default budget in tokens for the entire replay block. Sized so a typical
# 20-30 turn session fits comfortably while leaving plenty of room for the
# operator's new turn, the system prompt, and the model's response.
DEFAULT_BUDGET_TOKENS = 50_000

# How many turns to always keep at the head/tail when the budget forces a
# middle-drop. First N grounds the model on session origin; last M keeps
# recent context (which is what the operator is most likely referencing).
_KEEP_HEAD_TURNS = 5
_KEEP_TAIL_TURNS = 10

# Truncation caps for tool call detail. Tool input/result JSON can be huge
# (full file diffs, multi-page reports) — we keep only the gist.
_TOOL_INPUT_PREVIEW_CHARS = 120
_TOOL_RESULT_PREVIEW_CHARS = 200


def build_replay_text(
    turns: list[Turn],
    tool_calls_by_turn: dict[int, list[ToolCallRecord]],
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> str:
    """Render prior turns as a single text block the model can read as context.

    Format:
        <session_replay>
        ...preamble explaining what this is...

        == Turn 1 (<timestamp>) ==
        Operator: ...
        Tools used:
          - Bash(git status) -> 4 lines, success
          - Edit(threats.py) -> ok
        Assistant: ...

        == Turn 2 ... ==
        ...
        </session_replay>

    If the rendered block exceeds `budget_tokens`, degrade in order:
      1. Strip tool call detail (keep only the name).
      2. Drop middle turns, keep first _KEEP_HEAD_TURNS + last _KEEP_TAIL_TURNS.
    """
    if not turns:
        return ""

    # Tier 1: full detail.
    text = _render(turns, tool_calls_by_turn, tool_detail="full", dropped_count=0)
    if _estimate_tokens(text) <= budget_tokens:
        return text

    # Tier 2: names-only tool calls.
    text = _render(turns, tool_calls_by_turn, tool_detail="names", dropped_count=0)
    if _estimate_tokens(text) <= budget_tokens:
        return text

    # Tier 3: drop middle turns. Always-keep-tail beats truncate-to-fit
    # because the operator's most recent context matters most.
    if len(turns) > _KEEP_HEAD_TURNS + _KEEP_TAIL_TURNS:
        head = turns[:_KEEP_HEAD_TURNS]
        tail = turns[-_KEEP_TAIL_TURNS:]
        dropped = len(turns) - _KEEP_HEAD_TURNS - _KEEP_TAIL_TURNS
        kept = head + tail
        text = _render(
            kept,
            tool_calls_by_turn,
            tool_detail="names",
            dropped_count=dropped,
            dropped_after_index=_KEEP_HEAD_TURNS,
        )
        if _estimate_tokens(text) <= budget_tokens:
            return text
    else:
        kept = turns
        dropped = 0

    # Tier 4: pathological — kept turns themselves are too big (extremely
    # long assistant responses in audit/review sessions). Truncate each
    # turn's final_text to fit the per-turn share of the remaining budget.
    # Worst case: model sees "Assistant: …" with the tail cut off, which is
    # acceptable since the operator's recent turns are still intact.
    per_turn_char_budget = max(
        500, (budget_tokens * _CHARS_PER_TOKEN) // max(1, len(kept) * 2)
    )
    truncated = [_truncate_turn(t, per_turn_char_budget) for t in kept]
    text = _render(
        truncated,
        tool_calls_by_turn,
        tool_detail="names",
        dropped_count=dropped,
        dropped_after_index=_KEEP_HEAD_TURNS if dropped > 0 else None,
    )
    return text


def _render(
    turns: list[Turn],
    tool_calls_by_turn: dict[int, list[ToolCallRecord]],
    *,
    tool_detail: str,  # "full" | "names" | "none"
    dropped_count: int,
    dropped_after_index: Optional[int] = None,
) -> str:
    """Render a (possibly-pruned) turn list into a single replay block."""
    parts: list[str] = []
    parts.append("<session_replay>")
    parts.append(_preamble(dropped_count))

    for idx, turn in enumerate(turns):
        if (
            dropped_after_index is not None
            and idx == dropped_after_index
            and dropped_count > 0
        ):
            parts.append(
                f"\n[... {dropped_count} middle turn(s) omitted to fit token budget ...]\n"
            )

        parts.append(_render_turn(turn, tool_calls_by_turn.get(turn.id, []), tool_detail))

    parts.append("</session_replay>")
    return "\n".join(parts)


def _preamble(dropped_count: int) -> str:
    note = (
        f"\nNote: {dropped_count} middle turn(s) omitted to fit token budget; "
        "the head and tail of the conversation are preserved."
        if dropped_count > 0
        else ""
    )
    return (
        "This block is the full prior history of an ongoing Holler session that "
        "you are resuming. Holler restarted (or the SDK subprocess respawned) "
        "since your last reply, so your in-memory context was lost — this replay "
        "is how you recover it. Treat everything inside this block as past events: "
        "the operator's prior push-to-talks, your prior responses, and a "
        "summary of tools you ran. The operator's NEW request follows after "
        "the closing </session_replay> tag." + note
    )


def _render_turn(
    turn: Turn,
    tool_calls: list[ToolCallRecord],
    tool_detail: str,
) -> str:
    ts = _format_ts(turn.started_at)
    header = f"\n== Turn {turn.seq} ({ts}) =="
    op_line = f"Operator: {_collapse_ws(turn.transcript) or '(no transcript)'}"

    lines = [header, op_line]

    if tool_calls and tool_detail != "none":
        lines.append("Tools used:")
        for tc in tool_calls:
            lines.append("  - " + _format_tool_call(tc, tool_detail))

    final = turn.final_text or ""
    if turn.is_error and not final:
        lines.append("Assistant: (turn was interrupted by an error)")
    elif not final:
        lines.append("Assistant: (no reply produced)")
    else:
        lines.append("Assistant: " + _collapse_ws(final))

    return "\n".join(lines)


def _format_tool_call(tc: ToolCallRecord, tool_detail: str) -> str:
    name = tc.name or "Tool"
    if tool_detail == "names":
        status = "error" if tc.is_error else "ok"
        return f"{name} -> {status}"

    # Full detail: name + brief input preview + brief result indicator.
    input_preview = _short_json(tc.input_json, _TOOL_INPUT_PREVIEW_CHARS)
    result_preview = _short_json(tc.result_json, _TOOL_RESULT_PREVIEW_CHARS)
    status = "error" if tc.is_error else "ok"
    if input_preview:
        head = f"{name}({input_preview})"
    else:
        head = name
    if result_preview:
        return f"{head} -> {status}: {result_preview}"
    return f"{head} -> {status}"


def _short_json(raw: str, max_chars: int) -> str:
    """Compress a JSON string into a single-line preview.

    Strips base64 image data and other huge values; keeps just enough to
    show 'what was this tool call about'.
    """
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _collapse_ws(raw)[:max_chars]

    cleaned = _strip_heavy_values(parsed)
    try:
        compact = json.dumps(cleaned, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        compact = str(cleaned)
    if len(compact) > max_chars:
        return compact[: max_chars - 1] + "…"
    return compact


def _strip_heavy_values(obj):
    """Recursively replace huge string values (base64, big blobs) with markers."""
    if isinstance(obj, dict):
        return {k: _strip_heavy_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_heavy_values(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 200:
        return f"<{len(obj)}-char string>"
    return obj


def _truncate_turn(turn: Turn, char_budget: int) -> Turn:
    """Clip a turn's transcript and final_text to fit a per-turn char budget.

    Splits the budget 1/3 to the operator transcript, 2/3 to the assistant
    response (which is usually the much larger field). Appends a marker so
    the model knows truncation happened on its own prior reply.
    """
    transcript_budget = max(120, char_budget // 3)
    final_budget = max(240, char_budget - transcript_budget)
    new_transcript = _clip(turn.transcript, transcript_budget)
    new_final = _clip(turn.final_text, final_budget)
    if new_transcript == turn.transcript and new_final == turn.final_text:
        return turn
    return Turn(
        id=turn.id,
        session_id=turn.session_id,
        seq=turn.seq,
        transcript=new_transcript,
        screenshot_path=turn.screenshot_path,
        final_text=new_final,
        duration_ms=turn.duration_ms,
        started_at=turn.started_at,
        ended_at=turn.ended_at,
        is_error=turn.is_error,
        job_id=turn.job_id,
    )


def _clip(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + " …(truncated)"


def _collapse_ws(text: str) -> str:
    """Single-line-ify with internal whitespace collapsed. Preserves readability."""
    if not text:
        return ""
    return " ".join(text.split())


def _format_ts(epoch: float) -> str:
    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return "?"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def load_replay_from_store(
    store, local_session_id: int, before_turn_id: Optional[int] = None,
) -> str:
    """Convenience wrapper: Store -> replay text.

    Wired by the runner so Session.__init__ can take a simple
    `replay_loader: Callable[[int, Optional[int]], str]` without learning
    about Store. Returns empty string for sessions with no prior turns (the
    very first PTT of a new session needs no replay).

    `before_turn_id`: the turn currently being sent. It is already persisted
    (with no reply yet) by the time the SDK subprocess spawns, so it — and
    any later turns, e.g. smart-batch merges riding the same request — must
    be excluded; otherwise the request is replayed as a "(no reply produced)"
    prior turn AND sent again as the new message.
    """
    if local_session_id is None:
        return ""
    turns = store.get_turns(local_session_id)
    turns = _turns_before(turns, before_turn_id)
    if not turns:
        return ""
    turn_ids = [t.id for t in turns]
    tool_calls = store.get_tool_calls_for_turns(turn_ids)
    return build_replay_text(turns, tool_calls)


def _turns_before(turns: list[Turn], before_turn_id: Optional[int]) -> list[Turn]:
    """Drop the in-flight turn `before_turn_id` and every turn after it."""
    if before_turn_id is None:
        return turns
    cur = next((t for t in turns if t.id == before_turn_id), None)
    if cur is not None:
        return [t for t in turns if t.seq < cur.seq]
    # Turn row not found (shouldn't happen) — ids are monotonic, fall back.
    return [t for t in turns if t.id < before_turn_id]
