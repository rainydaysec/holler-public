"""In-session capture chains.

The Superintendent evidence inbox lets an operator hold several push-to-talk
captures (voice + screenshot) and *file* them as ONE evidence item that spawns
a worker. This module gives the same "hold, then file" affordance to ordinary
push-to-talk: filing submits a single multi-image TURN (no worker, no evidence
ledger) into the selected session, or into a session created on the spot when
nothing is selected.

State lives in the runner keyed by local session id (see
``runners.full`` ``state["session_chains"]``). These helpers are deliberately
pure so the accumulate/collapse logic can be unit-tested without Qt, audio, or
the SDK.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple


NEW_SESSION = "new"


def chain_key(target_sid: Optional[int]) -> Any:
    """Key into the chains map for a routing target.

    With nothing selected the chain belongs to a session that doesn't exist yet
    — filing it creates one. That's the default PTT target, so it needs a key of
    its own rather than being refused.
    """
    return target_sid if isinstance(target_sid, int) else NEW_SESSION


def new_chain() -> Dict[str, Any]:
    """An armed, empty chain. Armed-but-empty is a real state: the operator
    clicked "Start chain" and the bar shows "waiting for first capture"."""
    return {
        "armed": True,
        "shots": [],          # on-disk PNG paths (for the filed turn's thumbnail)
        "b64": [],            # PNG base64 (seeded as image blocks on the turn)
        "transcripts": [],    # per-capture voice/text, joined at file time
        "opened_at": time.time(),
    }


def is_armed(chain: Optional[Dict[str, Any]]) -> bool:
    return bool(chain and chain.get("armed"))


def count(chain: Optional[Dict[str, Any]]) -> int:
    """Running number of held captures (max across shots/images/transcripts so a
    text-only or image-only chain still reports a truthful count)."""
    if not chain:
        return 0
    return max(
        len(chain.get("b64") or []),
        len(chain.get("transcripts") or []),
        len(chain.get("shots") or []),
    )


def append(
    chain: Dict[str, Any],
    *,
    shot: Optional[str],
    image_b64: Optional[str],
    transcript: str,
) -> int:
    """Add one held capture. Returns the running count."""
    if shot:
        chain["shots"].append(shot)
    if image_b64:
        chain["b64"].append(image_b64)
    if (transcript or "").strip():
        chain["transcripts"].append(transcript.strip())
    chain["opened_at"] = time.time()
    return count(chain)


def build_turn(
    chain: Dict[str, Any],
) -> Tuple[str, Optional[str], List[str], Optional[str]]:
    """Collapse a held chain into one turn's inputs.

    Returns ``(joined_transcript, primary_image_b64, extra_image_b64s,
    primary_shot_path)`` — the first image rides as the turn's main image and
    the rest as ``extra_images`` (additional image blocks on the same user
    message); the first on-disk path is used for the turn-row thumbnail.
    """
    imgs: List[str] = [b for b in (chain.get("b64") or []) if b]
    shots: List[str] = [s for s in (chain.get("shots") or []) if s]
    transcripts: List[str] = [
        t for t in (chain.get("transcripts") or []) if (t or "").strip()
    ]
    joined = "\n\n".join(transcripts)
    primary = imgs[0] if imgs else None
    extras = imgs[1:] if len(imgs) > 1 else []
    primary_shot = shots[0] if shots else None
    return joined, primary, extras, primary_shot
