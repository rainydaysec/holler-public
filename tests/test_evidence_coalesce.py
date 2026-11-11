"""Tests for Superintendent evidence coalescing.

Regression guard for the "one chain -> two sessions" bug: after the operator
files a chain, a trailing PTT that finishes transcribing a beat later lands as
its own queued evidence row and — before this fix — spawned a second, redundant
worker session. `Store.coalesce_siblings` folds such a straggler (same inbox,
same lane, inside the burst window) into the already-claimed primary so the
whole chain becomes ONE worker.

Nothing is mocked: a real Store on a tmp SQLite DB, real evidence rows with
hand-set created_at timestamps to place them inside/outside the burst window.
"""

from __future__ import annotations

import json
from pathlib import Path

from holler.store import Store


WINDOW = 20.0


def _mk_store(tmp_path: Path) -> tuple[Store, int]:
    store = Store(tmp_path / "holler.db")
    inbox = store.ensure_superintendent_inbox()
    return store, inbox


def _set_created_at(store: Store, eid: int, ts: float) -> None:
    c = store._conn()
    with c:
        c.execute(
            "UPDATE superintendent_evidence SET created_at = ? WHERE id = ?",
            (ts, eid),
        )


def _shots(store: Store, eid: int) -> list[str]:
    row = store.get_evidence(eid)
    raw = row["screenshot_paths"]
    return json.loads(raw) if raw else []


def test_trailing_straggler_folds_into_chain(tmp_path: Path) -> None:
    store, inbox = _mk_store(tmp_path)
    # A filed chain (4 shots) + a straggler dump 1s later — the exact shape of
    # the reported bug (ev#16 4-item chain, ev#17 single, ~1s apart).
    primary = store.add_evidence_multi(
        transcript="investigating this IP",
        screenshot_paths=["a.png", "b.png", "c.png", "d.png"],
        inbox_session_id=inbox,
        inbox_turn_ids=[100],
        mode="investigation",
    )
    straggler_turn = store.add_turn(
        session_id=inbox, transcript="but also this detection",
        screenshot_path="e.png", job_id=None,
    )
    straggler = store.add_evidence_multi(
        transcript="but also this detection",
        screenshot_paths=["e.png"],
        inbox_session_id=inbox,
        inbox_turn_ids=[straggler_turn],
        mode="investigation",
    )
    _set_created_at(store, primary, 1000.0)
    _set_created_at(store, straggler, 1001.0)

    claimed = store.claim_next_queued_evidence()
    assert claimed is not None and int(claimed["id"]) == primary

    merged = store.coalesce_siblings(primary, window_s=WINDOW)
    assert merged == 1

    # Straggler is terminal 'merged' and will never be claimed/dispatched.
    assert store.get_evidence(straggler)["status"] == "merged"
    assert store.claim_next_queued_evidence() is None

    # Primary now carries every shot + both transcripts.
    assert _shots(store, primary) == ["a.png", "b.png", "c.png", "d.png", "e.png"]
    prow = store.get_evidence(primary)
    assert "investigating this IP" in prow["transcript"]
    assert "but also this detection" in prow["transcript"]

    # The straggler's inbox turn is restamped so no phantom session is implied.
    turn = [t for t in store.get_turns(inbox) if t.id == straggler_turn][0]
    assert f"Merged into evidence #{primary}" in (turn.final_text or "")


def test_dump_outside_window_stays_separate(tmp_path: Path) -> None:
    store, inbox = _mk_store(tmp_path)
    primary = store.add_evidence_multi(
        transcript="first", screenshot_paths=["a.png"],
        inbox_session_id=inbox, inbox_turn_ids=[1], mode="investigation",
    )
    later = store.add_evidence_multi(
        transcript="a genuinely new report", screenshot_paths=["z.png"],
        inbox_session_id=inbox, inbox_turn_ids=[2], mode="investigation",
    )
    _set_created_at(store, primary, 1000.0)
    _set_created_at(store, later, 1000.0 + WINDOW + 5.0)

    store.claim_next_queued_evidence()
    assert store.coalesce_siblings(primary, window_s=WINDOW) == 0
    assert store.get_evidence(later)["status"] == "queued"


def test_other_lane_never_merges(tmp_path: Path) -> None:
    store, inbox = _mk_store(tmp_path)
    primary = store.add_evidence_multi(
        transcript="ir report", screenshot_paths=["a.png"],
        inbox_session_id=inbox, inbox_turn_ids=[1], mode="investigation",
    )
    deploy = store.add_evidence_multi(
        transcript="deploy fix", screenshot_paths=["z.png"],
        inbox_session_id=inbox, inbox_turn_ids=[2], mode="deploy",
    )
    _set_created_at(store, primary, 1000.0)
    _set_created_at(store, deploy, 1001.0)  # inside window, but wrong lane

    store.claim_next_queued_evidence()
    assert store.coalesce_siblings(primary, window_s=WINDOW) == 0
    assert store.get_evidence(deploy)["status"] == "queued"


def test_streamed_burst_all_coalesce(tmp_path: Path) -> None:
    # Dumps ~5s apart each extend the frontier, so the whole steady stream folds
    # even though the last is >WINDOW from the first.
    store, inbox = _mk_store(tmp_path)
    ids = []
    for i in range(5):
        eid = store.add_evidence_multi(
            transcript=f"shot {i}", screenshot_paths=[f"{i}.png"],
            inbox_session_id=inbox, inbox_turn_ids=[i], mode="investigation",
        )
        _set_created_at(store, eid, 1000.0 + i * 5.0)
        ids.append(eid)

    primary = ids[0]
    store.claim_next_queued_evidence()
    assert store.coalesce_siblings(primary, window_s=WINDOW) == 4
    assert _shots(store, primary) == [f"{i}.png" for i in range(5)]
    assert store.claim_next_queued_evidence() is None
