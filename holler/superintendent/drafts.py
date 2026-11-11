"""Voice-delimited draft chaining for the Superintendent evidence inbox.

Every PTT into the inbox looks identical, so the operator signals intent with a
spoken cue at the *end* of the utterance:

  - hold cue    -> keep accumulating; this shot joins an open draft (or opens one)
  - commit cue  -> flush the whole draft as ONE evidence row
  - discard cue -> throw the open draft away
  - none        -> default: fire immediately as a single item (unless a draft is
                   already open, in which case it's treated as a continued chain)

`classify_inbox_utterance` returns the detected action plus the transcript with
the trailing cue phrase stripped, so the cue words never pollute the evidence
text handed to the worker.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

# Seconds a draft may sit idle before the dispatcher auto-commits it, so a
# missed commit cue never strands evidence.
DRAFT_AUTOCOMMIT_S = 90.0

# Cue phrases, matched case-insensitively against the tail of the utterance.
# Longer/multi-word phrases are listed so the matcher can strip them cleanly.
HOLD_CUES: List[str] = [
    "more coming",
    "more to come",
    "hold this",
    "hold on this",
    "chain it",
    "chain this",
    "another shot",
    "another one",
    "keep open",
    "keep this open",
    "keep it open",
    "standby",
    "stand by",
    "standby for another screenshot",
    "stand by for another screenshot",
    "standby for another",
    "stand by for another",
    "another screenshot coming",
    "another screenshot",
    "more screenshots coming",
    "more screenshots",
    "let me show you",
    "let me show you more",
    "i'll show you",
    "ill show you",
    "one more coming",
    "there's more",
    "theres more",
    "not done yet",
    "hold on",
]

# Hold cues that also match at the START of the utterance ("standby for another
# screenshot, the error is..."). Kept to distinctive multi-word phrases so
# ordinary sentences never false-positive.
HOLD_HEAD_CUES: List[str] = [
    "standby for another screenshot",
    "stand by for another screenshot",
    "standby for another",
    "stand by for another",
    "hold this",
    "chain this",
    "more coming",
]

COMMIT_CUES: List[str] = [
    "file it",
    "file this",
    "queue it",
    "queue this",
    "that's the issue",
    "thats the issue",
    "that's it",
    "thats it",
    "send it",
    "ship this one",
    "submit it",
]

DISCARD_CUES: List[str] = [
    "scratch that",
    "discard this",
    "discard that",
    "cancel that",
    "cancel this draft",
    "throw this out",
    "forget this",
]

_TRAIL_PUNCT = " \t\r\n.,!?;:—-\"'"


def _normalize(text: str) -> str:
    # Collapse whitespace, lowercase, and drop trailing punctuation so a cue at
    # the very end ("...file it.") still matches.
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(_TRAIL_PUNCT)


def _match_tail(norm: str, cues: List[str]) -> str | None:
    """Return the cue that the normalized utterance ends with, else None."""
    for cue in cues:
        # Anchor at end; allow trailing punctuation already stripped by _normalize
        # on the source, but the cue list itself has none.
        if norm == cue or norm.endswith(" " + cue):
            return cue
    return None


def _strip_tail(original: str, cue: str) -> str:
    """Remove the trailing cue phrase from the ORIGINAL (case-preserving) text."""
    stripped = original.rstrip(_TRAIL_PUNCT)
    # Case-insensitive tail removal of the cue.
    low = stripped.lower()
    if low.endswith(cue):
        stripped = stripped[: len(stripped) - len(cue)]
    return stripped.rstrip(_TRAIL_PUNCT).strip()


def _match_head(norm: str, cues: List[str]) -> str | None:
    """Return the cue the normalized utterance STARTS with, else None."""
    for cue in cues:
        if norm == cue or norm.startswith(cue + " ") or norm.startswith(cue + ","):
            return cue
    return None


def _strip_head(original: str, cue: str) -> str:
    """Remove the leading cue phrase from the ORIGINAL (case-preserving) text."""
    stripped = original.strip()
    low = stripped.lower()
    idx = low.find(cue)
    if idx != -1 and idx < 4:  # allow leading punctuation/space slop
        stripped = stripped[idx + len(cue):]
    return stripped.lstrip(_TRAIL_PUNCT).strip()


def classify_inbox_utterance(transcript: str) -> Tuple[str, str]:
    """Classify an inbox PTT.

    Returns (action, cleaned_transcript) where action is one of
    'hold' | 'commit' | 'discard' | 'none'. The cleaned transcript has the
    trailing cue phrase removed (for 'discard' the text is irrelevant and
    returned as-is).
    """
    norm = _normalize(transcript)
    if not norm:
        return "none", transcript or ""

    cue = _match_tail(norm, DISCARD_CUES)
    if cue is not None:
        return "discard", transcript or ""

    cue = _match_tail(norm, COMMIT_CUES)
    if cue is not None:
        return "commit", _strip_tail(transcript, cue)

    cue = _match_tail(norm, HOLD_CUES)
    if cue is not None:
        return "hold", _strip_hold_cues(_strip_tail(transcript, cue))

    cue = _match_head(norm, HOLD_HEAD_CUES)
    if cue is not None:
        return "hold", _strip_hold_cues(_strip_head(transcript, cue))

    return "none", transcript or ""


def _strip_hold_cues(text: str) -> str:
    """Strip any residual hold cues from either end ("standby for another
    screenshot, let me show you" carries two back-to-back cues)."""
    changed = True
    while changed and text:
        changed = False
        norm = _normalize(text)
        cue = _match_tail(norm, HOLD_CUES)
        if cue is not None:
            text = _strip_tail(text, cue)
            changed = True
            continue
        cue = _match_head(norm, HOLD_HEAD_CUES)
        if cue is not None:
            text = _strip_head(text, cue)
            changed = True
    return text


def new_draft(mode: str = "deploy") -> Dict[str, Any]:
    # `mode` is locked at draft-open time so a chain always files into the lane
    # it was started in, even if the operator flips the toggle mid-chain.
    return {
        "shots": [], "transcripts": [], "turn_ids": [],
        "opened_at": time.time(), "mode": mode or "deploy",
    }


def draft_count(draft: Optional[Dict[str, Any]]) -> int:
    """Running count of chained inputs (shots and/or text) in a draft."""
    if not draft:
        return 0
    return max(
        len(draft.get("turn_ids") or []),
        len(draft.get("shots") or []),
        len(draft.get("transcripts") or []),
    )


def append_to_draft(
    draft: Dict[str, Any],
    *,
    shot: Optional[str],
    transcript: str,
    turn_id: Optional[int],
) -> int:
    """Add one input to the draft. Returns the running count of chained inputs
    (screenshots and/or text), so a text-only chain still reports a real count."""
    if shot:
        draft["shots"].append(shot)
    if (transcript or "").strip():
        draft["transcripts"].append(transcript.strip())
    if turn_id is not None:
        draft["turn_ids"].append(turn_id)
    draft["opened_at"] = time.time()
    return max(len(draft["turn_ids"]), len(draft["shots"]), len(draft["transcripts"]))


def flush_draft(store: Any, inbox_sid: Optional[int], draft: Dict[str, Any]) -> Optional[int]:
    """Persist an accumulated draft as ONE evidence row. Returns its id (or None
    if the draft is empty)."""
    shots: List[str] = list(draft.get("shots") or [])
    transcripts: List[str] = list(draft.get("transcripts") or [])
    turn_ids: List[int] = list(draft.get("turn_ids") or [])
    if not shots and not transcripts:
        return None
    joined = "\n".join(t for t in transcripts if t.strip())
    return store.add_evidence_multi(
        transcript=joined,
        screenshot_paths=shots,
        inbox_session_id=inbox_sid,
        inbox_turn_ids=turn_ids,
        mode=draft.get("mode", "deploy"),
    )


def draft_is_stale(draft: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    if not draft:
        return False
    now = now if now is not None else time.time()
    return (now - float(draft.get("opened_at") or 0)) > DRAFT_AUTOCOMMIT_S
