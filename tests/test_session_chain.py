"""Tests for in-session capture chains (session_chain.py).

The operator can arm a "chain" on a normal, pre-existing session, hold several
push-to-talk captures (voice + screenshot) without each firing a model turn,
then File it — collapsing the whole batch into ONE multi-image turn back into
that same session. This module holds the pure accumulate/collapse logic; these
tests exercise it directly (no Qt, no SDK, no audio) so the signal/noise
behavior is pinned independently of the runner wiring.
"""

from __future__ import annotations

from holler import session_chain as sc


def test_new_chain_is_armed_and_empty() -> None:
    chain = sc.new_chain()
    assert sc.is_armed(chain) is True
    assert sc.count(chain) == 0


def test_is_armed_handles_none_and_disarmed() -> None:
    assert sc.is_armed(None) is False
    assert sc.is_armed({"armed": False}) is False


def test_append_accumulates_and_counts_truthfully() -> None:
    chain = sc.new_chain()
    assert sc.append(chain, shot="a.png", image_b64="AAA", transcript="first") == 1
    assert sc.append(chain, shot="b.png", image_b64="BBB", transcript="second") == 2
    assert sc.count(chain) == 2


def test_count_is_exact_for_normal_voice_plus_screenshot_captures() -> None:
    # The common case: every PTT carries both a screenshot and voice, so all
    # three dimensions grow together and count() is exact.
    chain = sc.new_chain()
    sc.append(chain, shot="a.png", image_b64="AAA", transcript="one")
    sc.append(chain, shot="b.png", image_b64="BBB", transcript="two")
    sc.append(chain, shot="c.png", image_b64="CCC", transcript="three")
    assert sc.count(chain) == 3


def test_count_tracks_dominant_dimension_for_homogeneous_chain() -> None:
    # count() is max() across dimensions, so a text-only chain still reports a
    # truthful count. (A mixed image-only-then-text-only chain would under-count
    # by design — same heuristic as the superintendent draft counter.)
    chain = sc.new_chain()
    sc.append(chain, shot=None, image_b64=None, transcript="first")
    sc.append(chain, shot=None, image_b64=None, transcript="second")
    assert sc.count(chain) == 2


def test_build_turn_collapses_three_images_into_one_multiimage_turn() -> None:
    # The core assertion: 3 held captures -> primary = first image, the other
    # two ride as extras, and every transcript is joined into one message.
    chain = sc.new_chain()
    sc.append(chain, shot="a.png", image_b64="AAA", transcript="alpha")
    sc.append(chain, shot="b.png", image_b64="BBB", transcript="bravo")
    sc.append(chain, shot="c.png", image_b64="CCC", transcript="charlie")

    joined, primary, extras, primary_shot = sc.build_turn(chain)

    assert primary == "AAA"
    assert extras == ["BBB", "CCC"]
    assert primary_shot == "a.png"
    assert joined == "alpha\n\nbravo\n\ncharlie"


def test_build_turn_text_only_chain_has_no_images() -> None:
    chain = sc.new_chain()
    sc.append(chain, shot=None, image_b64=None, transcript="no screenshot here")
    sc.append(chain, shot=None, image_b64=None, transcript="second thought")

    joined, primary, extras, primary_shot = sc.build_turn(chain)

    assert primary is None
    assert extras == []
    assert primary_shot is None
    assert joined == "no screenshot here\n\nsecond thought"


def test_build_turn_skips_blank_transcripts_and_falsy_images() -> None:
    chain = sc.new_chain()
    sc.append(chain, shot="a.png", image_b64="AAA", transcript="   ")  # blank text
    sc.append(chain, shot=None, image_b64=None, transcript="real note")

    joined, primary, extras, primary_shot = sc.build_turn(chain)

    assert primary == "AAA"
    assert extras == []
    assert joined == "real note"


def test_append_ignores_blank_transcript_but_keeps_image() -> None:
    chain = sc.new_chain()
    n = sc.append(chain, shot="a.png", image_b64="AAA", transcript="   ")
    assert n == 1
    assert chain["transcripts"] == []
    assert chain["b64"] == ["AAA"]
