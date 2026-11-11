"""Whisper invents words over near-silence, and a fabricated turn is worse than
a dropped one: it spends a real Claude turn on something nobody said. The
operator's 2026-10-01 log has a 0.5s noise capture decoded as the canned
"Thanks for watching!" and submitted.

The numbers below are measured, not invented — whisper `small` / int8 / cpu on
the operator's Mac, through the same vad_parameters holler passes.
"""

from __future__ import annotations

import pytest

from holler.transcriber import Transcription

# (label, no_speech_prob) for real utterances, including the hard cases: quiet
# speech and a single word. These must always survive.
REAL = [
    ("full sentence", 0.001),
    ("full sentence, quiet", 0.005),
    ("second sentence", 0.012),
    ("second sentence, quiet", 0.025),
    ("bare 'yes'", 0.026),
    ("bare 'yes', quiet", 0.103),
]

# Decodes of audio with no real utterance in it.
FABRICATED = [
    ("faint 0.6s speech-shaped", 0.541),
    ("faint 1.0s speech-shaped", 0.542),
    ("faint 1.0s, quietest", 0.758),
]

# Real fabrications that sit below the threshold. Caught only by luck today;
# separating these needs real-microphone data we don't have, and dropping a real
# turn is the worse failure. Pinned so the gap is a decision, not a surprise.
BELOW_THRESHOLD = [
    ("0.5s mid-word fragment", 0.265),
    ("speech + noise at -5dB", 0.303),
    ("background talkers", 0.439),
    ("reversed speech", 0.367),
]


def _heard(text: str, no_speech: float) -> Transcription:
    return Transcription(text, 3.0, 2.0, no_speech_prob=no_speech)


@pytest.mark.parametrize("label,no_speech", REAL)
def test_real_speech_is_never_discarded(label, no_speech):
    assert _heard("fix the spacing on this card", no_speech).looks_fabricated is False


@pytest.mark.parametrize("label,no_speech", FABRICATED)
def test_near_silence_decodes_are_discarded(label, no_speech):
    assert _heard("taking the detour.", no_speech).looks_fabricated is True


@pytest.mark.parametrize("label,no_speech", BELOW_THRESHOLD)
def test_mid_band_fabrications_are_deliberately_kept(label, no_speech):
    assert _heard("the depot.", no_speech).looks_fabricated is False


def test_real_speech_and_fabrication_do_not_overlap():
    # The gate only exists because these two bands are disjoint; if a future
    # model change closes the gap, this fails before the threshold silently
    # starts eating real turns.
    assert max(p for _, p in REAL) < min(p for _, p in FABRICATED + BELOW_THRESHOLD)


def test_silence_is_not_fabrication():
    # No text at all is the existing "didn't hear you" path, which has its own
    # message — it must not be relabelled as background noise.
    assert _heard("", 0.9).looks_fabricated is False
    assert _heard("   ", 0.9).looks_fabricated is False


@pytest.mark.parametrize("canned", [
    "Thanks for watching!", "thanks for watching", "Thank you.", "Thank you",
    "Please subscribe", "Bye.", "You",
])
def test_canned_caption_artefacts_go_when_the_decode_is_unconfident(canned):
    assert _heard(canned, 0.45).looks_fabricated is True


@pytest.mark.parametrize("canned", ["Thank you.", "Thanks", "Bye."])
def test_the_operator_really_saying_thank_you_is_kept(canned):
    # Mid-chain the operator does say these. Spoken for real they score ~0.01,
    # nowhere near the 0.2 the canned list needs.
    assert _heard(canned, 0.01).looks_fabricated is False


def test_a_real_instruction_is_kept_even_when_it_contains_a_canned_phrase():
    assert _heard("thank you for the fix, now ship it", 0.45).looks_fabricated is False
