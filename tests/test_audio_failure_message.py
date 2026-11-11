"""Tests for the empty-capture diagnosis toast.

The fault these guard against: an idle room on Mac hardware measures rms ~0.014,
which is above `_LOW_RMS`, so a capture containing no speech at all classifies
as quality "ok". The toast then read "Audio came through but nothing was
transcribed — try speaking a bit longer or more clearly", blaming the operator's
diction for a press that was either too short or pointed at the wrong mic.
"""

from __future__ import annotations

from holler.audio import SignalStats, _classify
from holler.runners.full import _MIN_HOLD_SECONDS, _audio_failure_message
from holler.transcriber import Transcription

import numpy as np


def _stats(seconds: float, quality: str = "ok", rms: float = 0.014) -> SignalStats:
    n = int(seconds * 16_000)
    return SignalStats(n, seconds, rms, rms * 10, 1.0, quality)


def _heard(audio_seconds: float, speech_seconds: float, text: str = "") -> Transcription:
    return Transcription(text, audio_seconds, speech_seconds)


def test_permission_denial_wins_over_everything():
    msg, is_perm = _audio_failure_message("permission", _stats(3.0), "Mic", _heard(3.0, 2.0))
    assert is_perm is True
    assert "Privacy & Security" in msg


def test_open_failure_reported_before_signal_analysis():
    msg, is_perm = _audio_failure_message("unavailable", None, None, None)
    assert is_perm is False
    assert "Couldn't open a microphone" in msg


def test_short_press_is_named_as_such_not_blamed_on_diction():
    """The operator's actual failures: 0.4-0.9s captures classed "ok"."""
    msg, _ = _audio_failure_message(None, _stats(0.415), "MacBook Pro Microphone",
                                    _heard(0.415, 0.0))
    assert "0.4s recorded" in msg
    assert "too short" in msg
    assert "clearly" not in msg


def test_every_logged_failure_duration_gets_the_short_press_message():
    """The exact capture lengths from the operator's log, all previously told
    to "speak a bit longer or more clearly"."""
    for seconds in (0.415, 0.494, 0.511, 0.554, 0.731, 0.786, 0.803, 0.896, 0.934):
        msg, _ = _audio_failure_message(None, _stats(seconds), "Mic", _heard(seconds, 0.0))
        assert "too short" in msg, f"{seconds}s fell through to: {msg}"


def test_no_speech_in_a_long_enough_capture_points_at_the_device():
    """Energy said "ok" but the VAD found no speech — don't blame diction."""
    msg, _ = _audio_failure_message(None, _stats(4.0), "MacBook Pro Microphone",
                                    _heard(4.0, 0.0))
    assert "Didn't hear speech" in msg
    assert "MacBook Pro Microphone" in msg
    assert "clearly" not in msg


def test_speech_present_but_undecodable_still_blames_diction():
    """The one case where "speak more clearly" is honest advice."""
    msg, _ = _audio_failure_message(None, _stats(4.0), "Mic", _heard(4.0, 3.2))
    assert "make out any words" in msg
    assert "more clearly" in msg


def test_device_faults_outrank_the_short_press_diagnosis():
    """A muted/wrong device is the real fault even on a quick tap."""
    all_zero, _ = _audio_failure_message(None, _stats(0.3, "all_zero", 0.0), "Mic",
                                         _heard(0.3, 0.0))
    assert "only silence" in all_zero
    silent, _ = _audio_failure_message(None, _stats(0.3, "silent", 1e-4), "Mic",
                                       _heard(0.3, 0.0))
    assert "Almost no sound" in silent


def test_quiet_but_speechlike_audio_asks_for_more_gain():
    msg, _ = _audio_failure_message(None, _stats(3.0, "low", 0.003), "Mic", _heard(3.0, 2.0))
    assert "very quiet" in msg


def test_no_capture_at_all_is_distinct():
    msg, _ = _audio_failure_message(None, _stats(0.0, "empty", 0.0), "Mic", _heard(0.0, 0.0))
    assert "No audio was recorded" in msg


def test_message_survives_a_missing_transcription():
    """transcribe() raising must not turn the toast into a TypeError."""
    msg, _ = _audio_failure_message(None, _stats(3.0), "Mic", None)
    assert msg
    long_enough, _ = _audio_failure_message(None, _stats(3.0), "Mic")
    assert long_enough


def test_idle_room_audio_classifies_as_low_not_ok():
    """Ambient room noise (rms ~0.011-0.014 on MacBook mics) must not read as
    speech-range "ok" energy now that LOW_RMS is calibrated (E4). The VAD
    verdict still outranks the RMS band in the toast."""
    rng = np.random.default_rng(0)
    ambient = (rng.standard_normal(16_000 * 3) * 0.014).astype(np.float32)
    stats = _classify(ambient)
    assert stats.quality == "low"
    msg, _ = _audio_failure_message(None, stats, "Mic", _heard(3.0, 0.0))
    assert "Didn't hear speech" in msg


def _fabricated(audio_seconds: float = 3.0, speech_seconds: float = 1.0) -> Transcription:
    heard = Transcription("Thanks for watching!", audio_seconds, speech_seconds,
                          no_speech_prob=0.6)
    assert heard.looks_fabricated
    return heard


def test_a_discarded_fabrication_says_nothing_was_sent():
    """Whisper produced words from room noise and dispatch dropped them. The
    toast must say so, or the operator waits for a turn that never happens."""
    msg, is_perm = _audio_failure_message(None, _stats(3.0), "Mic", _fabricated())
    assert is_perm is False
    assert "nothing was sent" in msg
    assert "background noise" in msg
    assert "make out any words" not in msg


def test_fabrication_outranks_the_quiet_input_advice():
    """Faint audio is exactly the regime that fabricates, so "raise your input
    level" would be the wrong lede."""
    msg, _ = _audio_failure_message(None, _stats(3.0, "low", 0.003), "Mic", _fabricated())
    assert "nothing was sent" in msg
    assert "very quiet" not in msg


def test_device_and_short_press_faults_still_outrank_fabrication():
    """A fabricated decode is a weaker signal than a muted mic or a 0.4s tap —
    naming the real fault stays more useful."""
    short, _ = _audio_failure_message(None, _stats(0.4), "Mic", _fabricated(0.4, 0.3))
    assert "too short" in short
    no_speech, _ = _audio_failure_message(
        None, _stats(4.0), "Mic",
        Transcription("Thank you.", 4.0, 0.0, no_speech_prob=0.6))
    assert "Didn't hear speech" in no_speech


def test_min_hold_threshold_covers_the_vad_pad_and_silence_window():
    """400ms speech pad + 500ms min-silence — anything under that can't segment."""
    assert _MIN_HOLD_SECONDS >= 0.9
