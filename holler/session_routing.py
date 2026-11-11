"""Voice routing: parse "follow up on …" prefixes and fuzzy-match recent sessions.

Three states for a PTT routing decision:
  - "new": create a fresh session (default for any unprefixed PTT, or when
    fuzzy-match falls below threshold).
  - "resume": resume a specific local_session_id (its sdk_session_id is loaded
    from the Store).
  - The runner is responsible for emitting toast confirmation.

Algorithm: tokenize topic + each candidate title (stripping stopwords, light
suffix stemming); score = 1.0 for an exact substring hit, else the fraction of
topic tokens found in the title. Threshold default 0.6 — so "login bug fix"
matches "Fix login bug in auth flow" (all tokens present, different order).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_STOPWORDS = {
    "the", "a", "an", "on", "with", "in", "of", "to", "for", "and", "or",
    "this", "that", "is", "are", "was", "were",
}

_RESUME_PREFIX = re.compile(
    r"^\s*(?:follow[- ]?up\s+on|follow\s+up\s+with|continue|resume|"
    r"go\s+back\s+to|keep\s+working\s+on)\s+(?:the\s+|with\s+)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoutingDecision:
    kind: str  # "new" | "resume"
    local_session_id: Optional[int]
    sdk_session_id: Optional[str]
    matched_title: Optional[str]
    confidence: float
    cleaned_transcript: str
    note: str = ""  # short user-facing note for toast


def parse_topic(transcript: str) -> tuple[Optional[str], str]:
    """If transcript starts with a resume prefix, return (topic, transcript_minus_prefix).

    Otherwise (None, transcript).
    """
    if not transcript:
        return None, transcript
    m = _RESUME_PREFIX.match(transcript)
    if not m:
        return None, transcript
    rest = transcript[m.end():].lstrip()
    if not rest:
        return None, transcript
    # The "topic" is the first ~60 chars of the rest, used for matching.
    # We KEEP the original transcript (minus prefix) — Claude needs it to act on.
    topic = rest.split(",", 1)[0].split(".", 1)[0].split("?", 1)[0].strip()
    if not topic:
        topic = rest[:60]
    return topic, rest


def _stem(t: str) -> str:
    """Very light suffix stripping so "fixes"/"fixing"/"fixed" ~ "fix"."""
    for suf in ("ing", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def _tokenize(s: str) -> set[str]:
    return {
        _stem(t) for t in re.split(r"\W+", (s or "").lower())
        if t and t not in _STOPWORDS
    }


def score(topic: str, title: str) -> float:
    """1.0 if the topic appears verbatim in the title; otherwise the fraction
    of (stemmed, stopword-free) topic tokens present in the title.

    The old 0.5*substring + 0.5*overlap blend capped any non-verbatim match at
    0.5 — below the 0.6 threshold — so word-order differences ("login bug fix"
    vs "Fix login bug") never resumed."""
    if not topic or not title:
        return 0.0
    tt = _tokenize(topic)
    if not tt:
        return 0.0
    if topic.lower().strip() in title.lower():
        return 1.0
    tn = _tokenize(title)
    return len(tt & tn) / len(tt)


def fuzzy_match(
    topic: str,
    candidates: list[tuple[int, str, Optional[str]]],
    threshold: float = 0.6,
) -> Optional[tuple[int, str, Optional[str], float]]:
    """`candidates` is `[(local_id, title, sdk_id_or_None), ...]`.

    Returns (id, title, sdk_id, score) of best match above threshold, else None.
    """
    best = None
    best_score = 0.0
    for cid, title, sdk_id in candidates:
        s = score(topic, title)
        if s > best_score:
            best_score = s
            best = (cid, title, sdk_id, s)
    if best is None or best_score < threshold:
        return None
    return best


def decide(
    transcript: str,
    *,
    active_local_session_id: Optional[int],
    candidates: list[tuple[int, str, Optional[str]]],
    threshold: float = 0.6,
) -> RoutingDecision:
    """Produce a routing decision. Precedence:
       1. Active selection wins regardless of voice content.
       2. Voice prefix + fuzzy match above threshold → resume.
       3. Voice prefix but no match → new (with warning note for toast).
       4. No prefix → new.
    """
    if active_local_session_id is not None:
        # Find sdk_id for that session.
        sdk_id = None
        title = ""
        for cid, t, sid in candidates:
            if cid == active_local_session_id:
                sdk_id = sid
                title = t
                break
        return RoutingDecision(
            kind="resume",
            local_session_id=active_local_session_id,
            sdk_session_id=sdk_id,
            matched_title=title,
            confidence=1.0,
            cleaned_transcript=transcript,
            # No notice: continuing the selected session is the expected
            # path, and a toast on every push-to-talk was noise.
            note="",
        )

    topic, cleaned = parse_topic(transcript)
    if topic is None:
        return RoutingDecision(
            kind="new", local_session_id=None, sdk_session_id=None,
            matched_title=None, confidence=0.0, cleaned_transcript=transcript,
        )

    match = fuzzy_match(topic, candidates, threshold=threshold)
    if match is None:
        return RoutingDecision(
            kind="new", local_session_id=None, sdk_session_id=None,
            matched_title=None, confidence=0.0, cleaned_transcript=cleaned,
            note=f"No session matches “{topic[:40]}”, so Holler started a new one.",
        )

    cid, matched_title, sdk_id, conf = match
    return RoutingDecision(
        kind="resume",
        local_session_id=cid,
        sdk_session_id=sdk_id,
        matched_title=matched_title,
        confidence=conf,
        cleaned_transcript=cleaned,
        note=f"Continuing “{matched_title}”",
    )
