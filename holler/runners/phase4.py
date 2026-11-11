"""Phase 4 runner: full pipeline wired end-to-end, console output only.

PTT press  -> capture active window snapshot, show overlay
PTT release -> hide overlay, get strokes, composite, transcribe audio,
               base64 the image, submit to SDK session
SDK events -> printed to stdout
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Optional

from PIL import Image
from PySide6 import QtCore, QtWidgets

from holler.audio import Recorder
from holler.composite import compose, to_png_b64
from holler.config import load as load_config
from holler.hotkey import HotkeyListener
from holler.overlay import OverlayWindow
from holler.screen import capture_active_window, downscale, set_dpi_awareness
from holler.sdk_session import Session, SessionEvent
from holler.sysprompt import build_append
from holler.transcriber import Transcriber


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    set_dpi_awareness()
    cfg = load_config()

    app = QtWidgets.QApplication(sys.argv)
    overlay = OverlayWindow(stroke_color=cfg.stroke_color, stroke_width=cfg.stroke_width)

    recorder = Recorder()
    transcriber = Transcriber(
        model_size=cfg.whisper_model,
        device=cfg.whisper_device,
        compute_type=cfg.whisper_compute,
        language=cfg.whisper_language,
    )
    print("[phase4] preloading whisper in background...")
    transcriber.preload_async()

    def on_event(ev: SessionEvent) -> None:
        if ev.kind == "submitted":
            t = ev.data.get("transcript", "")
            print(f"\n[job {ev.job_id}] submitted: {t[:80]!r}")
        elif ev.kind == "started":
            print(f"[job {ev.job_id}] started")
        elif ev.kind == "text_delta":
            print(ev.data["text"], end="", flush=True)
        elif ev.kind == "tool_use":
            print(f"\n[job {ev.job_id}] tool: {ev.data['name']}({_shorten(ev.data['input'])})")
        elif ev.kind == "result":
            print(
                f"\n[job {ev.job_id}] DONE {ev.data['duration_ms']}ms "
                f"err={ev.data['is_error']}"
            )
        elif ev.kind == "error":
            print(f"\n[job {ev.job_id}] ERROR {ev.data['error']}", file=sys.stderr)
        elif ev.kind == "session_rotated":
            print(f"\n[session] rotated to a fresh client")
        elif ev.kind == "session_init":
            print(f"\n[session] init {ev.data['session_id']}")

    session = Session(
        repo_cwd=str(cfg.repo_cwd),
        model=cfg.claude_model,
        system_prompt_append=build_append(),
        on_event=on_event,
    )
    session.start()

    # State carried from press to release.
    state: dict[str, Optional[object]] = {"snapshot": None}

    def on_press_qt() -> None:
        snap = capture_active_window()
        if snap is None:
            print("[phase4] no foreground window")
            return
        state["snapshot"] = snap
        recorder.start()
        overlay.show_for(snap.image, snap.rect)

    def on_release_qt() -> None:
        snap = state.pop("snapshot", None)
        overlay.hide()
        audio = recorder.stop()
        if snap is None:
            return
        strokes = overlay.get_strokes()
        # Run transcription + composite + submit on a worker thread so the GUI
        # stays responsive and the user can immediately PTT again.
        threading.Thread(
            target=_dispatch_job,
            args=(snap, strokes, audio, transcriber, session, cfg),
            daemon=True,
        ).start()

    invoker = _InvokeOnUiThread()
    invoker.requested_press.connect(on_press_qt)
    invoker.requested_release.connect(on_release_qt)

    listener = HotkeyListener(
        cfg.hotkey,
        on_press=lambda: invoker.requested_press.emit(),
        on_release=lambda: invoker.requested_release.emit(),
    )
    listener.start()
    print(f"[phase4] hold {cfg.hotkey} on any window to capture+talk. Ctrl+C to quit.")

    try:
        sys.exit(app.exec())
    finally:
        listener.stop()
        session.stop()


def _dispatch_job(snap, strokes, audio, transcriber: Transcriber, session: Session, cfg) -> None:
    t0 = time.time()
    try:
        transcript = transcriber.transcribe(audio)
    except Exception as e:
        print(f"[dispatch] transcribe error: {e!r}", file=sys.stderr)
        transcript = ""
    transcribe_ms = int((time.time() - t0) * 1000)

    composed = compose(snap.image, strokes, cfg.stroke_color, cfg.stroke_width)
    composed = downscale(composed, cfg.max_screenshot_long_edge)
    image_b64 = to_png_b64(composed)
    if cfg.debug_save_captures:
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out_path = cfg.captures_dir / f"{stamp}.png"
            composed.save(out_path)
        except Exception:
            pass

    print(f"\n[dispatch] transcript ({transcribe_ms}ms): {transcript!r}")
    if not transcript and not strokes:
        print("[dispatch] no transcript and no strokes — skipping submit")
        return
    session.submit(image_b64, transcript)


class _InvokeOnUiThread(QtCore.QObject):
    requested_press = QtCore.Signal()
    requested_release = QtCore.Signal()


def _shorten(obj, n: int = 80) -> str:
    s = repr(obj)
    return s if len(s) <= n else s[: n - 3] + "..."


if __name__ == "__main__":
    main()
