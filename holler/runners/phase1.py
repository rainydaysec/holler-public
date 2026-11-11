"""Phase 1 runner: PTT + audio + Whisper. Prints transcripts to stdout."""

from __future__ import annotations

import logging
import sys
import threading
import time

from holler.audio import Recorder
from holler.config import load as load_config
from holler.hotkey import HotkeyListener
from holler.transcriber import Transcriber


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()

    transcriber = Transcriber(
        model_size=cfg.whisper_model,
        device=cfg.whisper_device,
        compute_type=cfg.whisper_compute,
        language=cfg.whisper_language,
    )
    print("[phase1] preloading whisper in background...")
    transcriber.preload_async()

    recorder = Recorder()
    stop_event = threading.Event()

    def on_press():
        print("[phase1] PTT pressed")
        recorder.start()

    def on_release():
        audio = recorder.stop()
        print(f"[phase1] PTT released ({audio.size / 16_000:.2f}s captured)")
        if not transcriber.is_ready():
            print("[phase1] whisper still loading, waiting...")
        t0 = time.time()
        try:
            text = transcriber.transcribe(audio)
        except Exception as e:
            print(f"[phase1] transcribe error: {e!r}")
            return
        dt = time.time() - t0
        print(f"[phase1] transcript ({dt*1000:.0f}ms): {text!r}")

    listener = HotkeyListener(cfg.hotkey, on_press, on_release)
    listener.start()
    print(f"[phase1] hold {cfg.hotkey} to record. Ctrl+C to quit.")

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        print("[phase1] bye")


if __name__ == "__main__":
    main()
