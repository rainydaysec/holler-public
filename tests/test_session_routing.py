"""Regression tests for voice routing fuzzy match (catalogue A8)."""

from __future__ import annotations

from holler.session_routing import decide, fuzzy_match, score


CANDS = [
    (1, "Refactor sidebar layout", "sdk-1"),
    (2, "Fix login bug in auth flow", "sdk-2"),
    (3, "Write release notes", None),
]


def test_follow_up_matches_reordered_title() -> None:
    d = decide("follow up on the login bug fix, it still fails",
               active_local_session_id=None, candidates=CANDS)
    assert d.kind == "resume"
    assert d.local_session_id == 2
    assert d.sdk_session_id == "sdk-2"


def test_stemmed_tokens_match() -> None:
    assert score("fixing the login bugs", "Fix login bug in auth flow") >= 0.6


def test_exact_substring_still_scores_full() -> None:
    assert score("release notes", "Write release notes") == 1.0


def test_unrelated_topic_does_not_match() -> None:
    assert fuzzy_match("database migration", CANDS) is None
    d = decide("follow up on the database migration",
               active_local_session_id=None, candidates=CANDS)
    assert d.kind == "new"


def test_partial_overlap_below_threshold() -> None:
    # 1 of 3 topic tokens present -> 0.33 < 0.6
    assert fuzzy_match("login page redesign", CANDS) is None
