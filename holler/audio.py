"""Microphone recorder for PTT capture.

Samples mono float32 at 16 kHz, the rate faster-whisper expects.
start() opens a stream and accumulates frames into an in-memory buffer.
stop() closes the stream and returns a single 1-D numpy array of all samples.

Idempotent: calling start() while already recording is a no-op; calling stop()
when not recording returns an empty array.

`on_level(level: float)` callback fires from the audio thread with a 0..1 RMS
reading every block. Used for the overlay mic-level meter. For a polled meter
(e.g. a HUD repainting on a QTimer), `current_level()` returns the latest
reading from any thread without a callback; it reads 0.0 when not recording.

`on_dropout()` fires once from a watchdog thread if the stream opens but no
audio frames arrive shortly after start() — the signature of a muted device or
a macOS Microphone permission that lets the stream open but delivers silence.

`on_fallback(configured, actual)` fires from start() when the configured mic
could not be opened and another device was substituted, so the UI can say which
mic actually recorded instead of leaving the operator guessing.

Diagnosibility: after stop(), `last_stats` holds a SignalStats classifying the
captured buffer (empty / all_zero / silent / low / ok), and after a failed
start() `last_open_error` is set to a coarse cause ("permission" | "unavailable")
so callers can show a specific, actionable message instead of a generic one.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"

# RMS thresholds for classifying a captured float32 [-1, 1] buffer (and the
# Settings mic-test verdict, which imports these — define them only here).
# Measured on a MacBook mic: an idle room sits at rms ~0.011-0.014, real speech
# ~0.05-0.2. LOW_RMS sits between the two so ambient noise reads "low", not
# "ok"; SILENT_RMS catches a dead/muted/wrong device.
SILENT_RMS = 5e-4
LOW_RMS = 0.02

# How long after start() to wait for the first audio callback before deciding
# the stream is dead. PortAudio normally delivers the first block in tens of ms.
_WATCHDOG_SECONDS = 0.6

# PortAudio paInternalError. On macOS this is what a denied Microphone (TCC)
# permission surfaces as when opening the input stream.
_PA_INTERNAL_ERROR = -9986


@dataclasses.dataclass
class SignalStats:
    """Summary of a captured buffer, used to explain an empty transcript."""

    samples: int
    seconds: float
    rms: float
    peak: float
    nonzero_ratio: float
    # "empty"    — no samples captured at all (stream never delivered frames)
    # "all_zero" — samples present but every one is 0 (silenced/permission stream)
    # "silent"   — energy below SILENT_RMS (effectively silence / wrong device)
    # "low"      — quiet but present; whisper may still miss it
    # "ok"       — speech-range energy present
    quality: str


def _classify(audio: np.ndarray) -> SignalStats:
    n = int(audio.size)
    if n == 0:
        return SignalStats(0, 0.0, 0.0, 0.0, 0.0, "empty")
    a = audio.astype(np.float32, copy=False)
    rms = float(np.sqrt(np.mean(a ** 2)))
    peak = float(np.max(np.abs(a)))
    nonzero_ratio = float(np.count_nonzero(a)) / n
    if nonzero_ratio == 0.0:
        quality = "all_zero"
    elif rms < SILENT_RMS:
        quality = "silent"
    elif rms < LOW_RMS:
        quality = "low"
    else:
        quality = "ok"
    return SignalStats(n, n / SAMPLE_RATE, rms, peak, nonzero_ratio, quality)


def _block_level(block: np.ndarray) -> float:
    """0..1 meter level for one audio block (RMS with soft compression —
    speech RMS is typically ~0.05-0.2, so ×6 puts it mid-scale)."""
    rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
    return min(1.0, rms * 6.0)


def _pa_error_code(e: BaseException) -> Optional[int]:
    """sounddevice raises PortAudioError(message, code); pull out the code."""
    args = getattr(e, "args", ())
    if len(args) >= 2 and isinstance(args[1], int):
        return args[1]
    return None


def refresh_devices() -> bool:
    """Rebuild PortAudio's device list so hot-plugged mics become visible.

    PortAudio enumerates devices once at Pa_Initialize() and never refreshes —
    a mic plugged in after the process started is invisible until we re-init.
    Safe only when no stream is open (re-init invalidates all streams/indices),
    so callers must hold no active InputStream. Returns True on success.
    """
    try:
        import sounddevice as sd
        sd._terminate()
        sd._initialize()
        return True
    except Exception:
        log.exception("PortAudio device refresh failed")
        return False


class Recorder:
    def __init__(self, device: "str | int | None" = None) -> None:
        self._stream = None
        # Input device for sounddevice: a device-name substring, an index, or
        # None to follow the OS default input. Empty string is normalized to
        # None so an unset config value means "system default".
        self._device = device or None
        self._frames: "queue.Queue[np.ndarray]" = queue.Queue()
        self._lock = threading.Lock()
        self._recording = False
        self._got_frame = False
        self._last_frame_at: float = 0.0
        self._started_at: float = 0.0
        # Bumped on every start(); a watchdog only acts for its own recording,
        # so a stale watchdog from a quick stop()/start() can't misfire.
        self._generation = 0
        self.on_level: Optional[Callable[[float], None]] = None
        # Latest 0..1 level (see current_level()); a float store is atomic.
        self._level = 0.0
        self.on_dropout: Optional[Callable[[], None]] = None
        self.on_fallback: Optional[Callable[[str, str], None]] = None
        # Diagnostics for the dispatcher / UI.
        self.last_stats: Optional[SignalStats] = None
        # Set after a failed start(): "permission" | "unavailable" | None.
        self.last_open_error: Optional[str] = None
        # Name of the device the last successful start() actually opened.
        self.last_open_device: Optional[str] = None
        # Configured mic that had to be substituted on the last start(), if any.
        self.last_open_fallback: Optional[str] = None

    def current_level(self) -> float:
        """Latest 0..1 mic level (same scale as `on_level`), 0.0 when not
        recording. Lock-free and cheap — safe to poll from the GUI thread."""
        return self._level if self._recording else 0.0

    def set_device(self, device: "str | int | None") -> None:
        """Change the input device. Takes effect on the next start()."""
        self._device = device or None
        log.info("recorder input device set to %r", self._device)

    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            # drain any leftover frames from a previous run
            while not self._frames.empty():
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    break
            self._recording = True
            self._level = 0.0
            self._got_frame = False
            self._generation += 1
            generation = self._generation
            self.last_open_error = None
            self.last_open_device = None
            self.last_open_fallback = None

        import sounddevice as sd

        self._started_at = time.time()
        self._last_frame_at = self._started_at

        def _callback(indata: np.ndarray, _frames: int, _time, status) -> None:
            if status:
                log.warning("audio stream status: %s", status)
            self._got_frame = True
            self._last_frame_at = time.time()
            self._frames.put(indata.copy())
            try:
                level = _block_level(indata)
            except Exception:
                return
            self._level = level
            if self.on_level is not None:
                try:
                    self.on_level(level)
                except Exception:
                    pass

        def _open(device):
            return sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=_callback,
                blocksize=0,
                device=device,
            )

        stream, opened_device, substituted, error = self._open_with_fallback(_open)
        if stream is None:
            self._stream = None
            with self._lock:
                self._recording = False
            self.last_open_error = error
            return
        self._stream = stream
        self.last_open_device = opened_device
        self.last_open_fallback = substituted
        log.debug("recording started (device=%r)", opened_device)
        if substituted is not None:
            cb = self.on_fallback
            if cb is not None:
                try:
                    cb(substituted, opened_device)
                except Exception:
                    log.exception("on_fallback handler raised")
        self._start_watchdog(generation)

    def _open_with_fallback(self, _open):
        """Open the input stream, substituting another mic rather than losing the turn.

        Returns (live_stream, opened_device_name, substituted_for, error). On
        success error is None; substituted_for is the configured mic name when we
        had to open a different device, else None.

        macOS input-device names are not stable identities — they change as the
        operator's earbuds come and go ("External Microphone" simply ceases to
        exist when a wired headset is unplugged, and Bluetooth sets arrive under
        their own names). Holler used to treat a configured name that no longer
        resolved as a hard failure and drop the push-to-talk with zero audio
        captured. For a push-to-talk tool that is the worst possible outcome: the
        operator's words are gone. So prefer the OS default input (which macOS
        already repoints at whatever headset is live), then any other input, and
        report the substitution so it is never silent.

        A macOS permission denial (PaError -9986) is reported as "permission"
        immediately — every device would fail the same way.
        """
        import sounddevice as sd

        def _name(device) -> str:
            try:
                info = sd.query_devices(device, kind="input")
                return str(info.get("name", device))
            except Exception:
                return "system default" if device is None else str(device)

        def _attempt(device):
            """Returns (stream, error_code, exc). stream is None on failure."""
            s = None
            try:
                s = _open(device)
                s.start()
                return s, None, None
            except Exception as e:  # noqa: BLE001 — surface code, keep trying
                if s is not None:
                    # Opened but failed to start — close it, or the PortAudio
                    # stream (and possibly the device) leaks.
                    try:
                        s.close()
                    except Exception:
                        log.debug("closing failed-to-start stream raised", exc_info=True)
                return None, _pa_error_code(e), e

        # 1) configured (or default) device
        stream, code, exc = _attempt(self._device)
        if stream is None and code is None and self._device is not None:
            # The name didn't resolve (not a PortAudio error code) — likely a
            # mic hot-plugged after launch, invisible in PortAudio's cached list.
            # Refresh the device list once and retry before giving up.
            log.info("mic %r not found; refreshing device list and retrying", self._device)
            if refresh_devices():
                stream, code, exc = _attempt(self._device)
        if stream is not None:
            return stream, _name(self._device), None, None
        if sys.platform == "darwin" and code == _PA_INTERNAL_ERROR:
            log.error(
                "mic open failed with PaError %d — likely macOS Microphone "
                "permission denied for the launching app", _PA_INTERNAL_ERROR,
            )
            return None, None, None, "permission"

        configured = str(self._device) if self._device is not None else None
        log.warning("mic %r unavailable (%r); substituting another input", self._device, exc)

        # 2) the OS default input — macOS already points this at the live headset
        if self._device is not None:
            stream, code, exc = _attempt(None)
            if stream is not None:
                actual = _name(None)
                log.warning("configured mic %r unavailable; recorded from default input %r instead", configured, actual)
                return stream, actual, configured, None

        # 3) any other input-capable device
        try:
            devices = list(sd.query_devices())
        except Exception:
            devices = []
        for idx, d in enumerate(devices):
            if d.get("max_input_channels", 0) <= 0:
                continue
            stream, _code, _exc = _attempt(idx)
            if stream is not None:
                actual = str(d.get("name", idx))
                log.warning("fell back to input device [%d] %s", idx, actual)
                return stream, actual, configured, None

        log.error("no input device could be opened")
        return None, None, None, "unavailable"

    def _start_watchdog(self, generation: int) -> None:
        """Fire on_dropout once if no frames arrive shortly after start().

        `generation` ties the watchdog to the start() that spawned it: after a
        quick stop()/start(), the old watchdog wakes during the NEW recording
        (before its first frame) and must not report "No mic input".
        """

        def _watch() -> None:
            time.sleep(_WATCHDOG_SECONDS)
            with self._lock:
                if not self._recording or self._generation != generation:
                    return
                got = self._got_frame
            if got:
                return
            log.warning(
                "no audio frames %.1fs after start — stream opened but is "
                "delivering nothing (muted device or stale mic permission)",
                _WATCHDOG_SECONDS,
            )
            cb = self.on_dropout
            if cb is not None:
                try:
                    cb()
                except Exception:
                    log.exception("on_dropout handler raised")

        threading.Thread(target=_watch, name="mic-watchdog", daemon=True).start()

    def stop(self) -> np.ndarray:
        with self._lock:
            if not self._recording:
                self.last_stats = _classify(np.zeros(0, dtype=np.float32))
                return np.zeros(0, dtype=np.float32)
            self._recording = False
        self._level = 0.0

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.exception("error stopping audio stream")
            self._stream = None

        chunks = []
        while True:
            try:
                chunks.append(self._frames.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            audio = np.zeros(0, dtype=np.float32)
        else:
            # concatenate mono frames -> 1-D
            audio = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32, copy=False)

        self.last_stats = _classify(audio)
        log.debug(
            "recording stopped, %d samples (%.2fs) quality=%s rms=%.5f",
            self.last_stats.samples, self.last_stats.seconds,
            self.last_stats.quality, self.last_stats.rms,
        )
        return audio
