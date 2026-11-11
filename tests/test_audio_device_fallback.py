"""A pinned mic that no longer exists must not cost the operator a push-to-talk.

macOS input-device names are not stable identities: "External Microphone"
disappears the moment a wired headset is unplugged, and Bluetooth sets show up
under their own names. Holler used to treat an unresolvable configured name as a
hard failure and drop the turn with zero audio captured. These tests pin the
replacement behaviour — substitute, record, and report which mic was used.
"""

from __future__ import annotations

import numpy as np
import pytest

from holler.audio import Recorder


class _FakeStream:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        pass


@pytest.fixture
def no_device_refresh(monkeypatch):
    """refresh_devices() re-inits PortAudio for real; stub it out."""
    monkeypatch.setattr("holler.audio.refresh_devices", lambda: False)


def _opener(available):
    """Build an _open(device) that only succeeds for devices in `available`."""

    def _open(device):
        if device in available:
            return _FakeStream()
        raise ValueError(f"No input device matching {device!r}")

    return _open


def test_missing_pinned_mic_falls_back_to_system_default(no_device_refresh):
    rec = Recorder(device="External Microphone")
    stream, name, substituted, error = rec._open_with_fallback(_opener({None}))

    assert stream is not None, "turn was dropped instead of recorded"
    assert error is None
    assert substituted == "External Microphone"
    assert name is not None


def test_available_pinned_mic_is_used_without_substitution(no_device_refresh):
    rec = Recorder(device="External Microphone")
    stream, _name, substituted, error = rec._open_with_fallback(
        _opener({"External Microphone"})
    )

    assert stream is not None
    assert error is None
    assert substituted is None, "substituted a mic that was actually available"


def test_no_input_device_at_all_still_reports_unavailable(no_device_refresh, monkeypatch):
    monkeypatch.setattr("sounddevice.query_devices", lambda *a, **k: [])
    rec = Recorder(device="External Microphone")
    stream, _name, _substituted, error = rec._open_with_fallback(_opener(set()))

    assert stream is None
    assert error == "unavailable"


def test_permission_denial_is_not_masked_by_fallback(no_device_refresh):
    def _open(_device):
        raise RuntimeError("Internal PortAudio error", -9986)

    rec = Recorder(device="External Microphone")
    stream, _name, _substituted, error = rec._open_with_fallback(_open)

    assert stream is None
    assert error == "permission", "a denied mic permission must not look like a missing device"


def test_start_notifies_on_fallback(no_device_refresh, monkeypatch):
    monkeypatch.setattr("sounddevice.InputStream", lambda **kw: _FakeStream())
    monkeypatch.setattr(
        "sounddevice.query_devices",
        lambda *a, **k: {"name": "MacBook Pro Microphone"},
    )

    rec = Recorder(device="External Microphone")
    seen: list[tuple[str, str]] = []
    rec.on_fallback = lambda configured, actual: seen.append((configured, actual))

    # InputStream is stubbed to succeed for every device, so force the pinned
    # name to fail the way PortAudio does when it cannot resolve a name.
    real_open = rec._open_with_fallback

    def _patched(_open):
        return real_open(_opener({None}))

    monkeypatch.setattr(rec, "_open_with_fallback", _patched)
    rec.start()

    assert seen == [("External Microphone", "MacBook Pro Microphone")]
    assert rec.last_open_fallback == "External Microphone"
    assert isinstance(rec.stop(), np.ndarray)
