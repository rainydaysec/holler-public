"""Background auto-titler — generates a 5-7 word title for a session.

Two modes:

- **First-turn titling** (`request(...)`): kicked off by the runner when a
  session is created and again when its first turn ends. Generates a title
  from the transcript + screenshot.
- **Rolling re-title** (`request_retitle(...)`): kicked off by the runner at
  later turn-count thresholds (e.g. 3, 6, 12, 24, 48, 96). Generates a fresh
  title from recent transcripts so the rail reflects the conversation's
  current topic, not its starting prompt.

Both modes only overwrite when `sess.auto_titled == True`; the right-click
Rename action clears that flag so a user-chosen title is sticky.

Robustness:

- Each request retries up to `_MAX_API_ATTEMPTS` times with exponential
  backoff (2/8/30 s). Per-session attempt counters in the DB cap consecutive
  failures at `_MAX_LIFETIME_ATTEMPTS` (a successful title resets the count)
  so a permanently failing API never hammers the network.
- If every retry fails OR if `claude_agent_sdk` isn't importable, the titler
  falls back to a deterministic local heuristic — strips filler words from
  the transcript head and title-cases the result. Worst case is a clunky
  title that's still readable in the rail; the raw transcript-head
  placeholder never persists.
- `stats()` exposes ok / retrying / failed / fallback counters so the UI can
  surface silent failures.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Callable, Optional, Sequence

from holler import claude_cli

log = logging.getLogger(__name__)


_TITLE_SYSTEM_PROMPT_FIRST = (
    "You generate a 5-to-7 word title for a Claude conversation. Output only the "
    "title, no quotes, no punctuation, no explanation. Capture the action and topic."
)

_TITLE_SYSTEM_PROMPT_RETITLE = (
    "You re-title an in-progress Claude conversation based on its most recent "
    "turns. Output only a 5-to-7 word title, no quotes, no punctuation, no "
    "explanation. Capture what the conversation is ACTUALLY about now — if it "
    "drifted from the original prompt, reflect the current topic."
)


# Per-call retry policy. Backoffs in seconds — short enough that a transient
# network blip resolves within a couple seconds, long enough that a sustained
# Anthropic outage doesn't melt the loop.
_MAX_API_ATTEMPTS = 3
_BACKOFFS_S = (2.0, 8.0, 30.0)

# Lifetime cap on FAILED auto-title attempts per session. Each attempt is
# counted up front (so a crash mid-call still counts) and the counter is reset
# when the API produces a title, so only consecutive failures accrue. After
# this many failed tries (each of which may itself have done up to
# _MAX_API_ATTEMPTS sub-tries), the titler stops trying. The local heuristic fallback always runs at least once before
# this cap is hit, so every session ends up with *some* title.
_MAX_LIFETIME_ATTEMPTS = 5

# Per-message JSON buffer for the titler's SDK subprocess (SDK default: 1 MB).
_MAX_BUFFER_SIZE = 32 * 1024 * 1024

# Long edge, in pixels, of the screenshot attached to a title request. The
# titler needs to tell a terminal from a browser from a design tool; it does
# not need to read the text on screen, and it answers in five to seven words.
# Anthropic bills an image at roughly width*height/750 tokens, so the captured
# composite (up to `max_screenshot_long_edge`, 1280 by default) costs ~1300
# tokens per title request while this thumbnail costs ~120.
TITLE_IMAGE_LONG_EDGE = 384


def title_image_b64(screenshot_path: Optional[str]) -> Optional[str]:
    """Base64 PNG thumbnail of a session's screenshot, for a title request.

    Returns None when there is no usable image. That is not an error: the
    titler works from the transcript alone, and a session may have no
    screenshot at all (voice-only turn, or screen recording denied).
    """
    if not screenshot_path:
        return None
    try:
        from PIL import Image

        from holler.composite import to_png_b64
        from holler.screen import downscale

        with Image.open(screenshot_path) as img:
            thumb = downscale(img.convert("RGB"), TITLE_IMAGE_LONG_EDGE)
            return to_png_b64(thumb) or None
    except Exception:
        log.warning("auto-title: unusable screenshot %s", screenshot_path, exc_info=True)
        return None


class AutoTitler:
    def __init__(
        self,
        store,                                # holler.store.Store
        repo_cwd: str,
        model: str = "claude-haiku-4-5-20251001",
        loop: Optional[asyncio.AbstractEventLoop] = None,
        on_titled: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self._store = store
        self._repo_cwd = repo_cwd
        self._model = model
        self._loop = loop
        self._on_titled = on_titled
        # Lightweight in-memory stats. The Sessions window polls `stats()` for
        # the status footer; nothing here is persisted across runs.
        self._stats_lock = threading.Lock()
        self._stats = {
            "ok": 0,           # API call succeeded
            "fallback": 0,     # API gave up; local heuristic wrote a title
            "retrying": 0,     # currently in-flight (sum of all live requests)
            "failed": 0,       # gave up entirely (lifetime cap hit, no fallback)
        }
        # Track which sessions have a request currently in flight so we don't
        # double-fire from overlapping triggers (creation + first-turn-complete
        # racing each other, for instance).
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    def _bump(self, key: str, delta: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + delta

    def request(self, local_session_id: int, transcript: str, image_b64: Optional[str]) -> None:
        """First-turn titling. Fire-and-forget."""
        if self._loop is None:
            log.debug("AutoTitler has no loop attached — skipping")
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._run(local_session_id, [transcript], image_b64, retitle=False),
                self._loop,
            )
        except RuntimeError:
            pass

    def request_retitle(self, local_session_id: int, transcripts: Sequence[str]) -> None:
        """Rolling re-title from the recent N turn transcripts. Fire-and-forget.

        Image is omitted on re-title — the conversation's recent text is more
        signal than the original screenshot at this point.
        """
        if self._loop is None:
            return
        if not transcripts:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._run(local_session_id, list(transcripts), None, retitle=True),
                self._loop,
            )
        except RuntimeError:
            pass

    async def _run(
        self,
        local_session_id: int,
        transcripts: Sequence[str],
        image_b64: Optional[str],
        *,
        retitle: bool,
    ) -> None:
        # Coalesce overlapping triggers: if a request is already in flight for
        # this session, drop this one. The current request will pick up any
        # extra transcript content when it re-reads `sess` at write time.
        with self._inflight_lock:
            if local_session_id in self._inflight:
                return
            self._inflight.add(local_session_id)
        self._bump("retrying", +1)

        try:
            await self._run_inner(local_session_id, transcripts, image_b64, retitle=retitle)
        finally:
            self._bump("retrying", -1)
            with self._inflight_lock:
                self._inflight.discard(local_session_id)

    async def _run_inner(
        self,
        local_session_id: int,
        transcripts: Sequence[str],
        image_b64: Optional[str],
        *,
        retitle: bool,
    ) -> None:
        # Pre-checks: respect user renames + lifetime retry cap.
        try:
            sess = self._store.get_session(local_session_id)
        except Exception:
            log.exception("auto-title: failed to read session %d", local_session_id)
            return
        if sess is None or not sess.auto_titled:
            log.info(
                "auto-title skipped: session %d %s",
                local_session_id,
                "missing" if sess is None else "user-renamed",
            )
            return
        if sess.title_attempts >= _MAX_LIFETIME_ATTEMPTS:
            log.info(
                "auto-title skipped: session %d hit lifetime cap (%d attempts)",
                local_session_id,
                sess.title_attempts,
            )
            return

        # Record the attempt up front so even a crash mid-call counts against
        # the budget — prevents loops if some sessions are pathological.
        try:
            self._store.bump_title_attempt(local_session_id)
        except Exception:
            log.exception("auto-title: failed to bump attempt counter for %d", local_session_id)

        primary_transcript = transcripts[0] if transcripts else ""

        # Try the API up to _MAX_API_ATTEMPTS times.
        title = ""
        last_error: Optional[BaseException] = None
        for attempt in range(_MAX_API_ATTEMPTS):
            try:
                title = await self._call_api(transcripts, image_b64, retitle=retitle)
            except Exception as e:
                last_error = e
                log.warning(
                    "auto-title attempt %d/%d failed for session %d: %s",
                    attempt + 1, _MAX_API_ATTEMPTS, local_session_id, e,
                )
                title = ""
            if title:
                break
            # Backoff before next try (unless this was the last try).
            if attempt + 1 < _MAX_API_ATTEMPTS:
                await asyncio.sleep(_BACKOFFS_S[attempt])

        title = _clean_title(title)
        used_fallback = False
        if not title:
            # Local fallback — guarantees the rail entry is never the raw
            # transcript head.
            title = _local_heuristic_title(primary_transcript)
            used_fallback = bool(title)
            if not title:
                # No transcript at all — bail without writing anything.
                self._bump("failed", +1)
                log.warning(
                    "auto-title: no title produced for session %d (last error: %s)",
                    local_session_id, last_error,
                )
                return

        # Re-check before write: the user may have renamed mid-flight, or the
        # session may have been deleted.
        try:
            fresh = self._store.get_session(local_session_id)
            if fresh is None or not fresh.auto_titled:
                log.info(
                    "auto-title skipped at write: session %d retitled mid-flight",
                    local_session_id,
                )
                return
            self._store.set_title(local_session_id, title, auto=True)
        except Exception:
            log.exception("auto-title store update failed")
            self._bump("failed", +1)
            return

        if used_fallback:
            self._bump("fallback", +1)
        else:
            self._bump("ok", +1)
            # The lifetime cap budgets FAILURES, not titles: a successful API
            # title clears the counter so later rolling re-titles (turn 6, 12,
            # 24, 48, 96) still fire on long sessions.
            try:
                self._store.reset_title_attempts(local_session_id)
            except Exception:
                log.exception("auto-title: failed to reset attempt counter for %d", local_session_id)

        if self._on_titled:
            try:
                self._on_titled(local_session_id, title)
            except Exception:
                pass

    async def _call_api(
        self,
        transcripts: Sequence[str],
        image_b64: Optional[str],
        *,
        retitle: bool,
    ) -> str:
        """One API attempt. Returns the raw title text (may be empty). Raises
        on any failure — caller decides whether to retry, give up, or fall back."""
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
        )

        sysprompt = _TITLE_SYSTEM_PROMPT_RETITLE if retitle else _TITLE_SYSTEM_PROMPT_FIRST
        opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=self._model,
            cwd=self._repo_cwd,
            system_prompt=sysprompt,
            # Titling is a one-shot text task: load NO filesystem settings, so
            # the operator's user/project hooks, CLAUDE.md and plugins never
            # run (or bill tokens) for a 5-word title.
            setting_sources=[],
            # SDK default is 1 MB per JSON message; a screenshot-bearing first
            # turn can exceed it. Match the main session's generous limit.
            max_buffer_size=_MAX_BUFFER_SIZE,
            cli_path=claude_cli.cli_path(),
        )

        if retitle:
            joined = "\n\n".join(
                f"Turn {i + 1}: \"{t[:300]}\""
                for i, t in enumerate(transcripts)
            )
            user_text = f"{joined}\n\nUpdated title (5-7 words):"
        else:
            primary = (transcripts[0] if transcripts else "")[:500]
            user_text = f'First voice transcript: "{primary}"\n\nTitle (5-7 words):'

        title = ""
        client = ClaudeSDKClient(options=opts)
        try:
            await client.connect()

            async def _gen():
                content: list[dict[str, Any]] = []
                if image_b64:
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                    })
                content.append({"type": "text", "text": user_text})
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "parent_tool_use_id": None,
                }

            await client.query(_gen())

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            title += block.text
                if isinstance(msg, ResultMessage):
                    break
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        return title


def _clean_title(s: str) -> str:
    if not s:
        return ""
    t = s.strip().split("\n", 1)[0]
    t = t.strip().strip('"').strip("'")
    if t.endswith("."):
        t = t[:-1]
    return t[:70]


# Words to skip at the start of a transcript when building a local fallback
# title. Conversational filler ("Can you…", "I want to…") swamps the actual
# topic if we don't strip it.
_FILLER_PREFIX = frozenset({
    "a", "an", "the",
    "i", "me", "my", "we", "our", "us",
    "you", "your",
    "it", "its",
    "this", "that", "these", "those",
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did",
    "can", "could", "will", "would", "should", "shall", "may", "might", "must",
    "have", "has", "had",
    "please", "kindly",
    "let", "lets",
    "ok", "okay", "alright", "yeah", "yes", "no", "so", "and", "but", "or",
    "to", "at", "in", "on", "for", "of", "with", "by", "from",
    "what", "where", "when", "why", "how", "who", "which",
    "as", "if", "than", "then", "now", "just",
    "want", "wanted", "need", "needed",
    "look", "looking", "see", "seeing", "show", "tell",
    "hi", "hey", "hello",
})

# Words to capitalize differently when title-cased (we generally Title Case,
# but acronyms should stay uppercase).
_ALWAYS_UPPER = frozenset({"ui", "ux", "api", "ci", "cd", "sdk", "url", "css", "html", "json", "sql", "aws", "gpu", "cpu", "ai", "ml", "llm", "mvp", "smb", "ioc", "ip", "dns", "tls", "ssl", "ssh", "rds"})


def _local_heuristic_title(transcript: str) -> str:
    """Deterministic local fallback when the AI titler is unavailable.

    Strips leading conversational filler, then takes the first 5-7 meaningful
    words and title-cases them. The result is never as good as a Haiku-
    generated title, but always better than a raw transcript head ending
    mid-word."""
    text = (transcript or "").strip().split("\n", 1)[0]
    if not text:
        return ""
    # Tokenize on whitespace + drop most punctuation; keep apostrophes and
    # hyphens so "don't" / "well-known" survive intact.
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", text)
    if not tokens:
        return ""
    # Skip leading filler. Stop once we hit a non-filler word — anything past
    # that point is meaningful, even if it's later in the same sentence.
    start = 0
    for i, tok in enumerate(tokens):
        if tok.lower() not in _FILLER_PREFIX:
            start = i
            break
    meaningful = tokens[start:start + 7]
    # If the strip left us with too little, fall back to the original first 6
    # tokens (better something than nothing).
    if len(meaningful) < 3:
        meaningful = tokens[:6]
    out = []
    for tok in meaningful:
        low = tok.lower()
        if low in _ALWAYS_UPPER:
            out.append(low.upper())
        else:
            out.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(out)[:70]


def is_placeholder_title(session) -> bool:
    """True iff the session's title is still the raw transcript-head default.

    Matches `Store.create_session`'s default rule exactly: first non-blank
    line of the transcript, first 60 chars. Used by both the runner (to
    decide whether the first-turn-complete hook should fire) and the
    Sessions window (to fade placeholder rows visually)."""
    if not session.auto_titled:
        return False
    head = (session.transcript or "").strip().split("\n", 1)[0][:60]
    if not head:
        return False
    return bool(session.title) and session.title.startswith(head)


# Turn counts at which rolling re-title fires. Logarithmic backoff: early
# conversation pivots fast; late conversation pivots slow. Exposed so the
# runner and tests can share the same source of truth.
#
# v7: extended past 24 → also fires at 48 and 96 so multi-hour sessions whose
# topic drifts past turn 24 still get refreshed.
RETITLE_TURN_THRESHOLDS = frozenset({3, 6, 12, 24, 48, 96})


def should_retitle(turn_count: int, last_retitle_at: float, now: float, debounce_s: float = 60.0) -> bool:
    """Decide whether to fire a rolling re-title for a session.

    True iff:
      - turn_count is one of the configured thresholds (3, 6, 12, 24, 48, 96)
      - at least `debounce_s` seconds have passed since the last retitle
        (prevents jitter when multiple turns land in quick succession)
    """
    if turn_count not in RETITLE_TURN_THRESHOLDS:
        return False
    if now - last_retitle_at < debounce_s:
        return False
    return True
