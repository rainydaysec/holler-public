"""GPU re-promotion must not rebuild a CPU model on every failed retry (E13)."""

from __future__ import annotations

import time
import types

import numpy as np

from holler import transcriber
from holler.transcriber import Transcriber


class _Model:
    def transcribe(self, audio, **kw):
        # Confidence fields mirror a real faster-whisper Segment, which always
        # carries them; the fabrication gate reads both.
        seg = types.SimpleNamespace(text="hi", no_speech_prob=0.01, avg_logprob=-0.3)
        return [seg], types.SimpleNamespace(duration_after_vad=1.0)


def _cpu_fallen_transcriber():
    t = Transcriber(device="cuda")
    t._model = _Model()
    t._actual_device = "cpu"
    t._gpu_lost_at = time.time() - 120
    t._ready_event.set()
    return t


def test_failed_gpu_retry_builds_no_cpu_model(monkeypatch):
    builds = []

    def _build(size, device, compute):
        builds.append(device)
        if device != "cpu":
            raise RuntimeError("no CUDA")
        return _Model()

    monkeypatch.setattr(transcriber, "_build_model", _build)
    t = _cpu_fallen_transcriber()
    before = t._model
    out = t.transcribe_detailed(np.ones(16_000, np.float32))
    assert out.text == "hi"
    assert builds == ["cuda"], f"retry built extra models: {builds}"
    assert t._model is before and t._actual_device == "cpu"
    assert time.time() - t._gpu_lost_at < 5  # retry timer reset


def test_successful_gpu_retry_promotes(monkeypatch):
    gpu = _Model()
    monkeypatch.setattr(transcriber, "_build_model", lambda s, d, c: gpu)
    t = _cpu_fallen_transcriber()
    t.transcribe_detailed(np.ones(16_000, np.float32))
    assert t._model is gpu and t._actual_device == "cuda" and t._gpu_lost_at == 0.0
