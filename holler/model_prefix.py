"""Voice prefix → per-job model override resolver.

Matches a transcript that starts with `with sonnet,` / `using haiku,` / `on opus,`
(case-insensitive, optional comma/colon/period after the model name) and resolves
the bare alias to a full Claude model id.

Also: a lightweight `format_model_label` to turn full ids into short
"opus 4.8" / "sonnet 4.6" / "haiku 4.5" badges for the panel header.

Aliases resolve to the NEWEST discovered id of the family (parsed version
numbers, not list order), so they follow new model releases automatically.
"""

from __future__ import annotations

import re
from typing import Optional

from holler.config import KNOWN_MODELS


# Static fallback only — used when neither discovery nor the baseline list
# carries an id of the spoken family. Normal resolution picks the NEWEST id of
# the family from the live model pool (see `resolve_alias`), so "with opus"
# follows new releases without a code edit.
_ALIAS_TO_FULL: dict[str, str] = {
    "fable": "claude-fable-5",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5-20251001",
}

_FAMILIES = tuple(_ALIAS_TO_FULL)

# `\b` after the alias: "with sonnets of love" must not become a sonnet job.
_PREFIX_PATTERN = re.compile(
    r"^\s*(?:with|using|on)\s+(" + "|".join(_FAMILIES) + r")\b\s*[,:.]?\s*",
    re.IGNORECASE,
)

# claude-<family>-<major>[-<minor>][-<YYYYMMDD>]. The minor is 1-3 digits so an
# 8-digit date suffix is never mistaken for a minor version
# (`claude-opus-4-20250514` is opus 4, not opus 4.20250514).
_ID_PATTERN = re.compile(
    r"^claude-([a-z]+)-(\d+)(?:-(\d{1,3}))?(?:-(\d{8}))?$"
)


def parse_model_id(full_id: str) -> Optional[tuple[str, int, Optional[int], Optional[str]]]:
    """`claude-haiku-4-5-20251001` → ("haiku", 4, 5, "20251001"); None on miss."""
    m = _ID_PATTERN.match(full_id or "")
    if not m:
        return None
    minor = int(m.group(3)) if m.group(3) is not None else None
    return m.group(1), int(m.group(2)), minor, m.group(4)


def _version_key(parsed) -> tuple[int, int, int]:
    """Sort key: higher = newer. Version first (5-5 > 5 > 4-8); the date only
    breaks ties between snapshots of the SAME version (newer date wins, a
    dated snapshot outranks the bare alias of the same version)."""
    _family, major, minor, date = parsed
    return (major, minor if minor is not None else 0, int(date) if date else 0)


def newest_of_family(family: str, models) -> Optional[str]:
    """Newest id of `family` in `models` by parsed version, or None."""
    best: Optional[str] = None
    best_key = None
    for mid in models or ():
        if not isinstance(mid, str):
            continue
        parsed = parse_model_id(mid)
        if parsed is None or parsed[0] != family:
            continue
        key = _version_key(parsed)
        if best_key is None or key > best_key:
            best, best_key = mid, key
    return best


def _default_pool() -> list[str]:
    """Discovered ids (cache) ∪ KNOWN_MODELS — `effective_models()` minus the
    operator's explicit list, which needs a Config the caller may not have."""
    pool: list[str] = []
    try:
        from holler.model_discovery import cached_model_ids

        pool.extend(cached_model_ids())
    except Exception:
        pass
    pool.extend(KNOWN_MODELS)
    return pool


def resolve_alias(alias: str, models: Optional[list[str]] = None) -> Optional[str]:
    """Bare voice alias → newest full id of that family.

    `models` is the live pool (pass `config.effective_models(cfg)` when a Config
    is at hand); default is discovered ∪ KNOWN_MODELS. Falls back to the static
    map when the pool has nothing of that family.
    """
    alias = (alias or "").lower()
    if alias not in _ALIAS_TO_FULL:
        return None
    pool = models if models is not None else _default_pool()
    return newest_of_family(alias, pool) or _ALIAS_TO_FULL[alias]


def extract(transcript: str, models: Optional[list[str]] = None) -> tuple[str, Optional[str]]:
    """Strip a leading model-prefix from `transcript`. Returns (stripped, full_id_or_None).

    Only scans the first 60 characters to avoid mid-sentence false positives.
    `models` optionally supplies the live model pool (see `resolve_alias`).
    """
    if not transcript:
        return transcript, None
    head = transcript[:60]
    m = _PREFIX_PATTERN.match(head)
    if not m:
        return transcript, None
    full_id = resolve_alias(m.group(1), models)
    if full_id is None:
        return transcript, None
    stripped = transcript[m.end():].lstrip()
    return stripped, full_id


def format_model_label(full_id: str) -> str:
    """`claude-opus-4-8` → `opus 4.8`, etc. Returns the input unchanged on miss.

    A trailing date snapshot is never shown as a minor version:
    `claude-opus-4-20250514` → `opus 4`.
    """
    if not full_id:
        return ""
    parsed = parse_model_id(full_id)
    if parsed is None:
        # short fallback: strip leading "claude-"
        return full_id.removeprefix("claude-")
    family, major, minor, _date = parsed
    if minor is not None:
        return f"{family} {major}.{minor}"
    return f"{family} {major}"


def cycle_next(current: str, models: list[str] | None = None) -> str:
    """Return the next model after `current`, wrapping. Falls back to the first.

    `models` is the live picker list from `config.effective_models()`, so the
    badge cycles through whatever is actually available. Defaults to the
    KNOWN_MODELS baseline for callers without a Config in hand.
    """
    pool = models or KNOWN_MODELS
    if current in pool:
        return pool[(pool.index(current) + 1) % len(pool)]
    return pool[0]
