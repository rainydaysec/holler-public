"""faster-whisper wrapper.

Lazy-loads the model in a background thread on first init() so the GUI doesn't
block. Subsequent transcribe() calls are synchronous (called from a worker
thread by main.py).

If the configured GPU device fails to initialize (missing cuBLAS/cuDNN, OOM,
etc.), we fall back to CPU automatically — the user gets slower transcription
but the app keeps working.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


# Whisper reports, per segment, how likely it is the segment is really silence.
# Measured on this hardware with `small`: real speech — including quiet speech
# and a bare "yes" — stays at or below 0.11, while decodes of faint, reversed or
# noise-masked speech run 0.27–0.76. 0.5 therefore sits ~5x above anything a
# real utterance produced, so it rejects the near-silence regime that invents
# words without risking a turn the operator actually spoke. Fabrications in the
# 0.27–0.46 band are deliberately left alone: separating those needs
# real-microphone data we don't have, and dropping a real turn is the worse
# failure. Every capture logs its score so that data accumulates.
_NO_SPEECH_REJECT = 0.5
# Whisper emits these verbatim from its caption training data when handed
# near-silence. They carry no instruction, but the operator could say "thank
# you" for real mid-chain, so they only count as fabrication when the decode is
# also unconfident — a spoken "thank you" scores ~0.01 here.
_CANNED_NO_SPEECH = 0.2
_CANNED_HALLUCINATIONS = frozenset({
    "thanks for watching", "thank you for watching", "thanks for watching everyone",
    "please subscribe", "subscribe to my channel", "like and subscribe",
    "thank you", "thanks", "you", "bye",
})


def _is_canned(text: str) -> bool:
    return text.strip().strip(".!?,;:…-— ").lower() in _CANNED_HALLUCINATIONS


@dataclasses.dataclass(frozen=True)
class Transcription:
    """One transcribe pass, carrying the VAD's verdict alongside the text.

    `speech_seconds` is how much of the clip faster-whisper's VAD judged to be
    speech. When it is zero, the capture provably held no speech — a different
    fault from "whisper misheard you", and one the UI must not blame on the
    operator's diction. Returned by value (not stashed on the Transcriber) so
    two rapid push-to-talks can't race over each other's diagnostics.
    """

    text: str
    audio_seconds: float
    speech_seconds: float
    # Whisper's own confidence, worst case across segments: the highest
    # no_speech_prob and the lowest avg_logprob.
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0

    @property
    def vad_found_speech(self) -> bool:
        return self.speech_seconds > 0.0

    @property
    def looks_fabricated(self) -> bool:
        """True when the text is whisper inventing words over near-silence.

        A fabricated turn is worse than a dropped one: it spends a real Claude
        turn on words nobody said. avg_logprob is deliberately not part of this
        test — measured, it overlaps between real speech (-0.32…-0.55) and
        fabrication (-0.38…-0.91), so it cannot separate the two.
        """
        if not self.text.strip():
            return False
        if self.no_speech_prob > _NO_SPEECH_REJECT:
            return True
        return _is_canned(self.text) and self.no_speech_prob > _CANNED_NO_SPEECH


def short_load_reason(err: Optional[BaseException], limit: int = 90) -> str:
    """One-line, UI-sized summary of a model-load failure."""
    if err is None:
        return "unknown error"
    detail = (str(err).strip().splitlines() or [""])[0].strip()
    text = f"{type(err).__name__}: {detail}" if detail else type(err).__name__
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


class WhisperUnavailable(RuntimeError):
    """The speech model failed to load, so nothing can be transcribed. Distinct
    from an empty Transcription (genuine silence): the UI must say the model is
    broken, not that the operator wasn't heard. `reason` is short and UI-safe."""

    def __init__(self, error: Optional[BaseException]):
        self.error = error
        self.reason = short_load_reason(error)
        super().__init__(f"whisper unavailable: {error!r}")


def _register_nvidia_dll_dirs() -> None:
    """pip installs cuBLAS/cuDNN into site-packages/nvidia/<lib>/bin/, but
    CTranslate2 calls Win32 LoadLibrary internally for cublas64_12.dll etc.
    To make sure those resolve, do three things:
      1. add each bin dir via os.add_dll_directory (covers ctypes / Python LoadLibrary)
      2. prepend each bin dir to PATH (covers native LoadLibrary by name)
      3. preload the .dll files into the process via ctypes — once a DLL is
         mapped, any subsequent LoadLibrary by name returns the loaded handle.
    """
    if sys.platform != "win32":
        return
    try:
        import sysconfig
    except ImportError:
        return
    candidates: list[Path] = []
    for key in ("purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p:
            candidates.append(Path(p) / "nvidia")

    bin_dirs: list[Path] = []
    for nv_root in candidates:
        if not nv_root.is_dir():
            continue
        for sub in nv_root.iterdir():
            bin_dir = sub / "bin"
            if bin_dir.is_dir():
                bin_dirs.append(bin_dir)

    if not bin_dirs:
        log.info("no nvidia/*/bin dirs found in site-packages — running on CPU")
        return

    # 1) os.add_dll_directory
    if hasattr(os, "add_dll_directory"):
        for d in bin_dirs:
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass

    # 2) prepend to PATH
    new_path = os.pathsep.join(str(d) for d in bin_dirs)
    os.environ["PATH"] = new_path + os.pathsep + os.environ.get("PATH", "")

    # 3) preload critical DLLs (cublas, cudnn) so LoadLibrary by name finds them
    import ctypes
    preloaded = 0
    for d in bin_dirs:
        for dll_name in ("cublas64_12.dll", "cudnn64_9.dll", "cublasLt64_12.dll"):
            dll_path = d / dll_name
            if dll_path.is_file():
                try:
                    ctypes.WinDLL(str(dll_path))
                    preloaded += 1
                except OSError as e:
                    log.warning("preload %s failed: %r", dll_path, e)
    log.info("registered %d nvidia DLL bin dirs, preloaded %d DLLs", len(bin_dirs), preloaded)


_register_nvidia_dll_dirs()


class Transcriber:
    def __init__(
        self,
        model_size: str = "medium",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "en",
    ):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model = None
        self._actual_device = device  # may be downgraded to "cpu" if GPU init fails
        self._load_lock = threading.Lock()
        self._transcribe_lock = threading.Lock()  # CT2 model is not thread-safe
        self._ready_event = threading.Event()
        self._load_error: Optional[BaseException] = None
        # When CUDA fails mid-session we drop to CPU and remember the time.
        # After this many seconds we'll attempt to re-promote to CUDA.
        self._gpu_lost_at: float = 0.0
        self._gpu_retry_seconds: float = 60.0
        # Optional callback for surfacing UI notices from the worker thread.
        self.on_notice: Optional[Callable[[str], None]] = None

    def preload_async(self) -> None:
        """Kick off model load on a daemon thread so the GUI can paint."""
        threading.Thread(target=self._ensure_loaded, name="whisper-loader", daemon=True).start()

    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        """True once the load attempt has FINISHED — check `load_error` to
        tell success from failure."""
        return self._ready_event.wait(timeout=timeout)

    @property
    def load_error(self) -> Optional[BaseException]:
        """Why the model failed to load (None while loading or once loaded)."""
        return self._load_error

    def _ensure_loaded(self) -> None:
        with self._load_lock:
            if self._model is not None or self._load_error is not None:
                return
            self._model, self._actual_device, err = _try_load(
                self._model_size, self._device, self._compute_type
            )
            if self._model is None:
                self._load_error = err
            self._ready_event.set()

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribed text only — for callers that don't diagnose failures."""
        return self.transcribe_detailed(audio).text

    def transcribe_detailed(self, audio: np.ndarray) -> Transcription:
        if not self._ready_event.is_set():
            self._ensure_loaded()
        self._ready_event.wait()
        if self._load_error is not None:
            raise WhisperUnavailable(self._load_error)
        if audio.size == 0:
            return Transcription("", 0.0, 0.0)

        assert self._model is not None
        # faster-whisper expects 16 kHz float32 mono. audio.py already produces that.
        # The CTranslate2 model is not thread-safe; serialize concurrent calls.
        vad_params = {
            "threshold": 0.35,
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 400,
        }
        with self._transcribe_lock:
            audio_seconds = len(audio) / SAMPLE_RATE
            # Periodically re-attempt CUDA after a CPU fallback.
            if (
                self._actual_device == "cpu"
                and self._device != "cpu"
                and self._gpu_lost_at > 0
                and time.time() - self._gpu_lost_at > self._gpu_retry_seconds
            ):
                # GPU only: _try_load() would fall back to building a whole new
                # CPU model on failure — discarded immediately, yet rebuilt
                # under the transcribe lock on every retry.
                try:
                    cuda_model = _build_model(self._model_size, self._device, self._compute_type)
                except Exception as e:
                    log.info("Whisper GPU re-promotion to %s failed (%r); staying on CPU",
                             self._device, e)
                    cuda_model = None
                if cuda_model is not None:
                    log.info("Whisper recovered to %s (%s)", self._device, self._compute_type)
                    self._model = cuda_model
                    self._actual_device = self._device
                    self._gpu_lost_at = 0.0
                    if self.on_notice:
                        try:
                            self.on_notice("Speech recognition is back on the GPU.")
                        except Exception:
                            pass
                else:
                    self._gpu_lost_at = time.time()
            def _decode() -> tuple[str, float, float, float]:
                """Returns (text, speech_seconds, no_speech_prob, avg_logprob).
                Reads self._model at call time so a CPU re-load between attempts
                takes effect."""
                assert self._model is not None
                segments, info = self._model.transcribe(
                    audio,
                    language=self._language,
                    vad_filter=True,
                    vad_parameters=vad_params,
                    # Beam search (faster-whisper's default) over greedy decode:
                    # short PTT clips are exactly where greedy mishears ambiguous
                    # speech ("just checking" -> "The second"). Costs ~100-200ms
                    # on CPU for a ~1s clip — a fair trade for fewer wrong turns.
                    beam_size=5,
                    condition_on_previous_text=False,
                )
                segs = list(segments)
                text = "".join(seg.text for seg in segs).strip()
                return (
                    text,
                    float(getattr(info, "duration_after_vad", 0.0) or 0.0),
                    max((float(s.no_speech_prob) for s in segs), default=0.0),
                    min((float(s.avg_logprob) for s in segs), default=0.0),
                )

            try:
                text, speech_seconds, no_speech_prob, avg_logprob = _decode()
            except RuntimeError as e:
                if self._actual_device != "cpu":
                    log.warning("transcribe failed on %s (%r); falling back to CPU", self._actual_device, e)
                    cpu_model, dev, _ = _try_load(self._model_size, "cpu", "int8")
                    if cpu_model is None:
                        raise
                    self._model = cpu_model
                    self._actual_device = "cpu"
                    self._gpu_lost_at = time.time()
                    if self.on_notice:
                        try:
                            self.on_notice("Speech recognition switched to the CPU (slower). Holler will retry the GPU in a minute.")
                        except Exception:
                            pass
                    text, speech_seconds, no_speech_prob, avg_logprob = _decode()
                else:
                    raise
        if not text and audio_seconds >= 1.0:
            log.warning(
                "no transcript from %.1fs of audio (%.1fs of it speech per VAD) — mic "
                "level may be too low or the wrong input device is selected",
                audio_seconds, speech_seconds,
            )
        heard = Transcription(
            text=text, audio_seconds=audio_seconds, speech_seconds=speech_seconds,
            no_speech_prob=no_speech_prob, avg_logprob=avg_logprob,
        )
        # Logged on every capture, not just rejections: it is the only way to
        # retune _NO_SPEECH_REJECT against real microphone audio.
        log.info(
            "transcribed %.1fs (%.1fs speech): no_speech=%.3f avg_logprob=%.3f fabricated=%s",
            audio_seconds, speech_seconds, no_speech_prob, avg_logprob, heard.looks_fabricated,
        )
        if heard.looks_fabricated:
            log.warning("discarding likely-fabricated transcript %r (no_speech=%.3f)",
                        text, no_speech_prob)
        return heard


def _build_model(model_size: str, device: str, compute_type: str):
    """Construct a WhisperModel, preferring the local cache.

    We try `local_files_only=True` first: if the model is already cached (the
    common case after first run), this avoids every network call — which matters
    behind a corporate VPN that does TLS interception and breaks Hugging Face
    downloads with `CERTIFICATE_VERIFY_FAILED: self-signed certificate in chain`.
    Only on a genuine cache miss do we fall back to a networked download.
    """
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(model_size, device=device, compute_type=compute_type, local_files_only=True)
    except Exception:
        # Not cached (or a local path that isn't a model) — allow a download.
        log.info("whisper model %r not in local cache; attempting download", model_size)
        return WhisperModel(model_size, device=device, compute_type=compute_type)


def _try_load(model_size: str, device: str, compute_type: str):
    """Try to load WhisperModel on `device`; on GPU failure, fall back to CPU.

    Returns (model, actual_device, error). On success, error is None.
    """
    t0 = time.time()
    try:
        model = _build_model(model_size, device, compute_type)
        log.info("whisper model loaded: %s (%s, %s) in %.2fs", model_size, device, compute_type, time.time() - t0)
        return model, device, None
    except BaseException as e:
        if device != "cpu":
            log.warning("whisper load on %s failed (%r); falling back to CPU", device, e)
            t0 = time.time()
            try:
                model = _build_model(model_size, "cpu", "int8")
                log.info("whisper model loaded: %s (cpu, int8) in %.2fs", model_size, time.time() - t0)
                return model, "cpu", None
            except BaseException as e2:
                log.exception("whisper load failed on CPU too")
                return None, device, e2
        log.exception("whisper load failed")
        return None, device, e
