"""Phase 3 runner: hardcoded image+text turn against the SDK session.

Reads %LOCALAPPDATA%\\holler\\captures\\last.png (produced by phase 2) and a
hardcoded transcript, posts both to the persistent SDK session, and prints
streamed events to the console.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from PIL import Image

from holler.composite import to_png_b64
from holler.config import load as load_config
from holler.sdk_session import Session, SessionEvent
from holler.sysprompt import build_append


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()

    last_png = cfg.captures_dir / "last.png"
    if not last_png.exists():
        print(f"[phase3] no capture at {last_png} — run --phase 2 first", file=sys.stderr)
        sys.exit(2)

    img = Image.open(last_png)
    image_b64 = to_png_b64(img)
    transcript = "What's on the screen? Answer in one or two sentences."

    def on_event(ev: SessionEvent) -> None:
        if ev.kind == "submitted":
            print(f"[job {ev.job_id}] submitted: {ev.data['transcript']!r}")
        elif ev.kind == "started":
            print(f"[job {ev.job_id}] started")
        elif ev.kind == "session_init":
            print(f"[job {ev.job_id}] session_id={ev.data['session_id']}")
        elif ev.kind == "text_delta":
            print(ev.data["text"], end="", flush=True)
        elif ev.kind == "tool_use":
            print(f"\n[job {ev.job_id}] tool_use {ev.data['name']}({_shorten(ev.data['input'])})")
        elif ev.kind == "thinking":
            pass  # uncomment for debug: print(f"\n[thinking] {ev.data['text'][:120]}...")
        elif ev.kind == "result":
            print(
                f"\n[job {ev.job_id}] DONE in {ev.data['duration_ms']}ms "
                f"(turns={ev.data['num_turns']}, err={ev.data['is_error']})"
            )
        elif ev.kind == "error":
            print(f"\n[job {ev.job_id}] ERROR {ev.data['error']}", file=sys.stderr)
        elif ev.kind == "session_rotated":
            print(f"\n[session] rotated")

    session = Session(
        repo_cwd=str(cfg.repo_cwd),
        model=cfg.claude_model,
        system_prompt_append=build_append(),
        on_event=on_event,
    )
    session.start()
    print("[phase3] session started")

    job1 = session.submit(image_b64, transcript)
    job2 = session.submit(image_b64, "Anything else worth flagging in that screenshot?")

    # Wait for both jobs to finish (poll a flag set in on_event).
    done = {1: False, 2: False}

    def patched(ev: SessionEvent) -> None:
        on_event(ev)
        if ev.kind == "result" and ev.job_id in done:
            done[ev.job_id] = True

    session._on_event = patched

    try:
        deadline = time.time() + 300
        while not all(done.values()) and time.time() < deadline:
            time.sleep(0.1)
        if not all(done.values()):
            print("\n[phase3] timeout waiting for jobs", file=sys.stderr)
    finally:
        session.stop()


def _shorten(obj, n: int = 80) -> str:
    s = repr(obj)
    return s if len(s) <= n else s[: n - 3] + "..."


if __name__ == "__main__":
    main()
