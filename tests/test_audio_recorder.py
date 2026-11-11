"""Recorder classification thresholds, stream cleanup and watchdog (E4, E11, E12).

sounddevice is always stubbed — no test opens the real microphone.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from holler import audio
from holler.audio import LOW_RMS, SILENT_RMS, Recorder, _classify


def _noise(rms: float, seconds: float = 2.0) -> np.ndarray:
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(16_000 * seconds)) * rms).astype(np.float32)


def test_thresholds_sit_between_measured_ambient_and_speech():
    assert SILENT_RMS < 0.011 < LOW_RMS < 0.05


@pytest.mark.parametrize("rms,quality", [
    (0.011, "low"),   # MacBook mic, idle room
    (0.014, "low"),
    (0.05, "ok"),     # quiet speech
    (0.2, "ok"),
    (1e-4, "silent"),
])
def test_classification_bands(rms, quality):
    assert _classify(_noise(rms)).quality == quality


def test_empty_and_all_zero():
    assert _classify(np.zeros(0, np.float32)).quality == "empty"
    assert _classify(np.zeros(1600, np.float32)).quality == "all_zero"


class _StartFails:
    def __init__(self):
        self.closed = False

    def start(self):
        raise RuntimeError("boom")

    def close(self):
        self.closed = True


def test_stream_closed_when_start_raises(monkeypatch):
    monkeypatch.setattr(audio, "refresh_devices", lambda: False)
    monkeypatch.setattr("sounddevice.query_devices", lambda *a, **k: [])
    made = []

    def _open(_device):
        s = _StartFails()
        made.append(s)
        return s

    stream, _n, _s, err = Recorder()._open_with_fallback(_open)
    assert stream is None and err == "unavailable"
    assert made and all(s.closed for s in made), "failed-to-start stream leaked"


class _OkStream:
    def start(self): pass
    def stop(self): pass
    def close(self): pass


def test_stale_watchdog_does_not_fire_for_next_recording(monkeypatch):
    monkeypatch.setattr(audio, "_WATCHDOG_SECONDS", 0.4)
    monkeypatch.setattr("sounddevice.InputStream", lambda **kw: _OkStream())
    monkeypatch.setattr("sounddevice.query_devices", lambda *a, **k: {"name": "Fake"})
    rec = Recorder()
    fired = []
    rec.on_dropout = lambda: fired.append(1)

    rec.start()
    rec._got_frame = True       # first recording got audio
    rec.stop()
    time.sleep(0.25)
    rec.start()                 # second recording, first watchdog still sleeping
    rec._got_frame = False
    time.sleep(0.3)             # first watchdog wakes (t=.4); second not yet (t=.65)
    assert fired == [], "stale watchdog reported 'no mic input' for a new recording"
    rec._got_frame = True
    time.sleep(0.2)
    rec.stop()
    assert fired == []


def test_watchdog_still_fires_for_dead_stream(monkeypatch):
    monkeypatch.setattr(audio, "_WATCHDOG_SECONDS", 0.05)
    monkeypatch.setattr("sounddevice.InputStream", lambda **kw: _OkStream())
    monkeypatch.setattr("sounddevice.query_devices", lambda *a, **k: {"name": "Fake"})
    rec = Recorder()
    fired = []
    rec.on_dropout = lambda: fired.append(1)
    rec.start()
    time.sleep(0.2)
    rec.stop()
    assert fired == [1]
