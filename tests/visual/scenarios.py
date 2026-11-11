"""Deterministic render scenarios for holler's golden-image tests.

Shared by `test_visual_goldens.py` (pytest) and `render_review.py` (CLI), so
what a human reviews is byte-for-byte what the tests compare.

Determinism levers (all applied by `VisualEnv`):

* app data dir redirected to a temp dir; holler.state save functions no-op'd
  (never touches ~/Library/Application Support/holler);
* `time.time` frozen at NOW and TZ forced to UTC, so "3h ago", elapsed
  counters, buckets and strftime tooltips are stable;
* every *repeating* QTimer created during a scenario is inert (pulse dots,
  spinner, 1 Hz ticker, quota/titler polls, overlay hold clock) — the
  initial phase is what gets painted. Single-shot timers (debounces, deferred
  renders, scroll settles) still run normally;
* monospace requests ("Cascadia Code"/"Consolas", missing on macOS) pinned
  to Menlo and Qt's font cache flushed, so fallback resolution can't depend
  on what earlier tests rendered;
* the app-level style holler applies at startup (holler.theme
  .apply_app_style: dark scheme, token palette, app stylesheet);
* fixed app font, cursor flash disabled, mouse cursor parked off every window
  (no hover state), fixed window sizes and positions;
* external probes stubbed: the `claude` CLI lookup in FirstRunDialog and the
  sounddevice input-device list in SettingsDialog.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOLLER_ACCOUNTS_DISABLED"] = "1"  # keep the real Claude login out of renders

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

# Fixed "now" — 2026-09-21 13:46:40 UTC.
NOW = 1_790_000_000.0
APP_FONT = ("Helvetica Neue", 13)
WIN_POS = QtCore.QPoint(60, 60)
CURSOR_PARK = QtCore.QPoint(0, 0)


# ── environment ──────────────────────────────────────────────────────────────

_RealQTimer = QtCore.QTimer


class _InertRepeatingTimer(_RealQTimer):
    """QTimer whose repeating instances never start (animation / poll ticks).

    Single-shot instances behave normally. Static `singleShot` is inherited
    unchanged."""

    def start(self, *args):  # noqa: D401 - Qt override
        if not self.isSingleShot():
            return
        super().start(*args)


_EMOJI = "Apple Color Emoji"
_FONT_SUBSTITUTIONS = {
    "Cascadia Code": ("Menlo", _EMOJI),
    "Consolas": ("Menlo", _EMOJI),
    "Menlo": (_EMOJI,),
    "Helvetica Neue": (_EMOJI,),
}
_FONT_CACHE_PROBE = "/System/Library/Fonts/Menlo.ttc"


def _flush_font_cache() -> None:
    """Invalidate Qt's font-engine cache (adding/removing an application font
    clears it), so family resolution doesn't depend on earlier renders."""
    if not os.path.exists(_FONT_CACHE_PROBE):
        return
    fid = QtGui.QFontDatabase.addApplicationFont(_FONT_CACHE_PROBE)
    if fid >= 0:
        QtGui.QFontDatabase.removeApplicationFont(fid)


def _drain_events() -> None:
    app = qapp()
    for _ in range(3):
        QtCore.QCoreApplication.sendPostedEvents()
        app.processEvents()


def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def pump(ms: int = 120) -> None:
    app = qapp()
    end = time.monotonic() + ms / 1000.0
    while True:
        app.processEvents(QtCore.QEventLoop.AllEvents, 20)
        if time.monotonic() >= end:
            break
        QtCore.QThread.msleep(5)
    app.processEvents()


_SAVED_APP_STYLE: Optional[tuple] = None


def _ensure_app_style(app) -> None:
    """Apply holler's startup app style + the pinned scenario font, once."""
    global _SAVED_APP_STYLE
    if _SAVED_APP_STYLE is not None:
        return
    from holler import theme

    _SAVED_APP_STYLE = (app.styleSheet(), QtGui.QPalette(app.palette()), QtGui.QFont(app.font()))
    theme.apply_app_style(app)
    app.setFont(QtGui.QFont(*APP_FONT))


def restore_app_style() -> None:
    """Undo `_ensure_app_style` (called at the end of the visual session)."""
    global _SAVED_APP_STYLE
    app = QtWidgets.QApplication.instance()
    if app is None or _SAVED_APP_STYLE is None:
        return
    qss, palette, font = _SAVED_APP_STYLE
    _SAVED_APP_STYLE = None
    app.setStyleSheet(qss)
    app.setPalette(palette)
    app.setFont(font)
    hints = app.styleHints()
    # Back to "follow the system" (apply_app_style requested Dark).
    if hasattr(hints, "unsetColorScheme"):
        hints.unsetColorScheme()
    elif hasattr(hints, "setColorScheme"):
        hints.setColorScheme(QtCore.Qt.ColorScheme.Unknown)


class VisualEnv(contextlib.AbstractContextManager):
    """Hermetic, frozen environment for rendering one scenario."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.appdata = self.root / "appdata"
        self.captures = self.appdata / "captures"
        self._stack = contextlib.ExitStack()
        self._before: set[int] = set()

    # -- setup / teardown --
    def __enter__(self) -> "VisualEnv":
        app = qapp()
        self.appdata.mkdir(parents=True, exist_ok=True)
        self.captures.mkdir(parents=True, exist_ok=True)
        st = self._stack
        try:
            self._before = {id(w) for w in app.topLevelWidgets()}

            # TZ → UTC (strftime / localtime in tooltips).
            old_tz = os.environ.get("TZ")
            os.environ["TZ"] = "UTC"
            time.tzset()

            def _restore_tz():
                if old_tz is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = old_tz
                time.tzset()

            st.callback(_restore_tz)

            # Frozen wall clock.
            st.enter_context(mock.patch.object(time, "time", lambda: NOW))

            # App data + persisted state.
            import holler.appdir as appdir
            from holler import state as hstate

            st.enter_context(mock.patch.object(appdir, "app_data_dir", lambda: self.appdata))
            # Settings shows app-data paths as text; render the per-run tmp
            # root as the real macOS location so goldens don't depend on it.
            import holler.settings_dialog as settings_dialog

            _real_display_path = settings_dialog.display_path
            _fake_root = Path.home() / "Library" / "Application Support" / "holler"

            def _stable_display_path(path, *a, **k):
                try:
                    path = _fake_root / Path(path).relative_to(self.appdata)
                except ValueError:
                    pass
                return _real_display_path(path, *a, **k)

            st.enter_context(mock.patch.object(settings_dialog, "display_path", _stable_display_path))
            for fn in ("save", "merged_save", "merged_save_debounced", "flush_pending"):
                if hasattr(hstate, fn):
                    st.enter_context(mock.patch.object(hstate, fn, lambda *a, **k: None))

            # Repeating timers inert.
            st.enter_context(mock.patch.object(QtCore, "QTimer", _InertRepeatingTimer))

            # External probes.
            from holler import first_run

            fake_shutil = mock.Mock()
            fake_shutil.which = lambda *_a, **_k: None
            st.enter_context(mock.patch.object(first_run, "shutil", fake_shutil))
            try:
                import holler.audio as haudio

                st.enter_context(mock.patch.object(haudio, "refresh_devices", lambda *a, **k: None))
            except Exception:
                pass
            try:
                import sounddevice as sd

                st.enter_context(mock.patch.object(sd, "query_devices", lambda *a, **k: []))
            except Exception:
                pass

            # Never pop real dialogs / open Finder.
            st.enter_context(mock.patch.object(
                QtGui.QDesktopServices, "openUrl", staticmethod(lambda *a, **k: True)))

            # Monospace: holler asks for "Cascadia Code"/"Consolas", absent on
            # macOS. Qt's fallback for a missing family depends on whatever
            # font-engine cache earlier code in the process populated (it
            # differed between an isolated run and the full suite), so pin
            # it explicitly and flush the cache on entry and exit.
            # Likewise pin glyph fallback for emoji (🔗 📥 🎙 …): left to the
            # system fallback lookup, the chosen face (and so the text's
            # height) varied between runs in the full suite.
            for fam, subs in _FONT_SUBSTITUTIONS.items():
                if not QtGui.QFont.substitutes(fam):
                    QtGui.QFont.insertSubstitutions(fam, list(subs))
                    st.callback(QtGui.QFont.removeSubstitutions, fam)
            _flush_font_cache()
            st.callback(_flush_font_cache)

            # The app-level style holler.runners.full applies at startup
            # (dark colour scheme, token palette, app stylesheet).
            from holler import theme

            # Applied once per process and restored by the visual conftest at
            # session end: every app-wide stylesheet/palette change re-polishes
            # every live top-level window (~190 leak from earlier tests), so
            # per-scenario apply+restore made the full suite ~8× slower.
            _ensure_app_style(app)

            # Stable typography, no cursor blink, no hover.
            old_flash = app.cursorFlashTime()
            app.setCursorFlashTime(0)
            st.callback(lambda: app.setCursorFlashTime(old_flash))
            QtGui.QCursor.setPos(CURSOR_PARK)

            # app.setFont() and the font-cache flush post ApplicationFontChange
            # / FontChange events. Left queued they get delivered mid-scenario
            # and re-polish widgets at a timing-dependent moment (seen as a
            # 25px vs 30px chain-bar button). Drain them now — and again on
            # exit, after the restores, so later tests start clean too.
            _drain_events()
            st.callback(_drain_events)

            # Close every window the scenario opened (runs first on exit).
            st.callback(self._close_new_windows)
        except BaseException:
            st.close()
            raise
        return self

    def __exit__(self, *exc) -> Optional[bool]:
        self._stack.close()
        return None

    def _close_new_windows(self) -> None:
        app = qapp()
        for w in list(app.topLevelWidgets()):
            if id(w) in self._before:
                continue
            try:
                w.hide()
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        for store in getattr(self, "_stores", []):
            try:
                store.close()
            except Exception:
                pass
        pump(30)

    # -- helpers --
    def place(self, w: QtWidgets.QWidget, size: tuple[int, int]) -> None:
        w.resize(*size)
        w.move(WIN_POS)

    def write_state(self, data: dict) -> None:
        (self.appdata / "state.json").write_text(json.dumps(data), encoding="utf-8")

    def store(self):
        from holler.store import Store

        s = Store(self.appdata / "holler.db")
        self._stores = getattr(self, "_stores", []) + [s]
        return s


def grab(w: QtWidgets.QWidget) -> QtGui.QImage:
    pump(60)
    img = w.grab().toImage().convertToFormat(QtGui.QImage.Format_ARGB32)
    return img


# ── deterministic fixture data ───────────────────────────────────────────────

RICH_FINAL_TEXT = """\
## Token refresh now happens *before* expiry

The middleware only called `refresh_token()` **after** a 401, so every request
that raced the expiry window failed once. Changes:

1. Check `token.expires_in` before dispatching the request
2. Refresh when fewer than **60s** remain
3. Retry exactly once on a stale-token 401

- Added `tests/test_auth.py::test_refresh_before_expiry`
- Kept the old 401 path as a fallback

```python
def ensure_fresh(token: Token, *, skew: int = 60) -> Token:
    if token.expires_in < skew:
        token = refresh_token(token)
    return token
```

| Case | Before | After |
|------|--------|-------|
| Fresh token | 200 | 200 |
| Expires in 30s | 401 → retry | 200 |
| Revoked | 401 | 401 |

A deliberately long line so wrapping is exercised: the refresh path now logs `auth.refresh.preemptive` with the remaining TTL, the account id, the upstream region and the request id so the on-call can correlate a burst of refreshes with a specific deploy without grepping three services.

### Follow-ups
- [ ] Move `skew` into config
- Consider jitter so a fleet doesn't refresh in lockstep
"""

TURN1_FINAL_TEXT = """\
Found it — the refresh lives in `app/auth.py` and is only reached from the
401 handler. I'll move the expiry check ahead of the request.
"""

BASH_OUTPUT = """\
============================= test session starts ==============================
collected 14 items

tests/test_auth.py ..............                                        [100%]

============================== 14 passed in 0.42s ==============================
"""

GREP_OUTPUT = """\
app/auth.py:41:def refresh_token(token):
app/auth.py:88:    token = refresh_token(token)
app/middleware.py:17:from app.auth import refresh_token
"""


def _png(path: Path, w: int, h: int, accent: tuple[int, int, int]) -> str:
    """Small deterministic 'screenshot': a fake editor window, pure shapes."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (24, 28, 34))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, 22], fill=(40, 44, 52))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([8 + i * 16, 6, 18 + i * 16, 16], fill=c)
    d.rectangle([0, 23, 120, h - 1], fill=(30, 34, 41))
    y = 34
    k = 0
    while y < h - 10:
        width = 60 + (k * 37) % (w - 220)
        d.rectangle([134, y, 134 + width, y + 6], fill=accent if k % 5 == 0 else (110, 118, 129))
        y += 14
        k += 1
    d.rectangle([w // 2, h // 3, w // 2 + 120, h // 3 + 60], outline=(255, 46, 46), width=4)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), format="PNG")
    return str(path)


def _set_times(store, sid: int, *, created: float, last: float) -> None:
    with store._tx() as c:
        c.execute("UPDATE sessions SET created_at=?, last_activity_at=? WHERE id=?", (created, last, sid))


def _turn(store, sid: int, transcript: str, *, final_text: str = "", started: float,
          duration_s: float = 0.0, shot: Optional[str] = None, tokens=(0, 0, 0, 0), cost=0.0,
          is_error: bool = False, tools: tuple = (), committed: bool = True) -> int:
    tid = store.add_turn(session_id=sid, transcript=transcript, screenshot_path=shot, job_id=None)
    t = started
    for name, inp, result, err in tools:
        tc = store.add_tool_call(turn_id=tid, name=name, input_json=json.dumps(inp), started_at=t)
        store.update_tool_call(tc, result_json=result, is_error=1 if err else 0, ended_at=t + 1.5)
        t += 2.0
    if committed:
        fresh, out, cc, cr = tokens
        store.update_turn(
            tid, final_text=final_text, duration_ms=int(duration_s * 1000),
            ended_at=started + duration_s, is_error=1 if is_error else 0,
            input_tokens=fresh, output_tokens=out, cache_creation_input_tokens=cc,
            cache_read_input_tokens=cr, total_cost_usd=cost,
        )
    with store._tx() as c:
        c.execute("UPDATE turns SET started_at=? WHERE id=?", (started, tid))
    return tid


RICH_TOOLS = (
    ("Read", {"file_path": "/repo/app/auth.py"}, "\n".join(f"{i:>4}  line {i}" for i in range(1, 121)), False),
    ("Grep", {"pattern": "refresh_token", "path": "/repo/app"}, GREP_OUTPUT, False),
    ("Bash", {"command": "cd /repo && pytest -q tests/test_auth.py\necho done"}, BASH_OUTPUT, False),
    ("Edit", {"file_path": "/repo/app/auth.py",
              "old_string": "if resp.status == 401:\n    token = refresh_token(token)",
              "new_string": "token = ensure_fresh(token)\nif resp.status == 401:\n    token = refresh_token(token)"},
     "The file /repo/app/auth.py has been updated.", False),
    ("Write", {"file_path": "/repo/tests/test_auth_refresh.py",
               "content": "def test_refresh_before_expiry():\n    assert True\n"},
     "File created successfully at: /repo/tests/test_auth_refresh.py", False),
    ("Bash", {"command": "git push origin HEAD"},
     "ERROR: fatal: could not read Username for 'https://github.com': terminal prompts disabled", True),
)


class Seed:
    """Ids of the seeded sessions (stable: insertion order into a fresh DB)."""

    rich: int
    long_title: int
    pinned: int
    done_recent: int
    done_yesterday: int
    archived: int
    worktree: int
    errored: int
    placeholder: int
    live_turn: int


def seed_store(store, shots_dir: Path, *, live: bool = False) -> Seed:
    s = Seed()
    shot_a = _png(shots_dir / "shot_a.png", 640, 400, (88, 166, 255))
    shot_b = _png(shots_dir / "shot_b.png", 480, 300, (63, 185, 80))

    def mk(title, transcript, *, profile=None, model="claude-opus-5", status="idle", shot=None):
        return store.create_session(model=model, transcript=transcript, screenshot_path=shot,
                                    status=status, title=title, profile=profile)

    # 1. Rich conversation — two committed turns, all tool kinds, markdown.
    s.rich = mk("Refactor auth middleware so tokens refresh before expiry",
                "refactor the auth middleware so tokens refresh before expiry",
                profile="engineering", shot=shot_a)
    _turn(store, s.rich, "refactor the auth middleware so tokens refresh before expiry",
          final_text=TURN1_FINAL_TEXT, started=NOW - 1500, duration_s=42, shot=shot_a,
          tokens=(12_400, 1_900, 3_000, 40_000), cost=0.41, tools=RICH_TOOLS[:2])
    _turn(store, s.rich, "ok do it, and add a test for the refresh path",
          final_text=RICH_FINAL_TEXT, started=NOW - 600, duration_s=191,
          tokens=(8_100, 3_300, 2_000, 120_000), cost=0.97, tools=RICH_TOOLS[2:])
    _set_times(store, s.rich, created=NOW - 1500, last=NOW - 120)

    # 2. Long title, investigation profile.
    s.long_title = mk(
        "Investigate why the canvas summary panel stops updating after the websocket "
        "reconnects on flaky networks and the retry backoff kicks in",
        "why does the canvas summary stop updating after a reconnect",
        profile="investigation", model="claude-sonnet-4-6")
    _turn(store, s.long_title, "why does the canvas summary stop updating after a reconnect",
          final_text="The summary widget subscribes to the **old** channel id. After a "
                     "reconnect it never re-subscribes.\n\n- `canvas/summary.ts:88`\n- `ws/client.ts:140`",
          started=NOW - 3 * 3600, duration_s=64, tokens=(5_200, 900, 0, 18_000), cost=0.12,
          tools=(("Read", {"file_path": "/repo/canvas/summary.ts"}, "export function Summary() {}\n", False),))
    _set_times(store, s.long_title, created=NOW - 4 * 3600, last=NOW - 3 * 3600)

    # 3. Pinned.
    s.pinned = mk("Weekly planning notes", "start the weekly planning notes", profile="planning")
    store.pin(s.pinned, True)
    _turn(store, s.pinned, "start the weekly planning notes",
          final_text="### This week\n1. Ship token refresh\n2. Canvas reconnect fix",
          started=NOW - 3 * 86400, duration_s=12, tokens=(900, 400, 0, 0), cost=0.02)
    _set_times(store, s.pinned, created=NOW - 3 * 86400, last=NOW - 3 * 86400)

    # 4. Done (reviewed) today.
    s.done_recent = mk("Fix flaky test in test_retention", "the retention test is flaky, fix it")
    _turn(store, s.done_recent, "the retention test is flaky, fix it",
          final_text="Pinned the clock with `monkeypatch` — no more flake.", started=NOW - 2 * 3600,
          duration_s=30, tokens=(2_000, 600, 0, 9_000), cost=0.05)
    _set_times(store, s.done_recent, created=NOW - 2 * 3600, last=NOW - 2 * 3600)
    with store._tx() as c:
        c.execute("UPDATE sessions SET reviewed_at=? WHERE id=?", (NOW - 3600, s.done_recent))

    # 5. Done yesterday.
    s.done_yesterday = mk("Bump pygments and regenerate the lockfile", "bump pygments",
                          model="claude-haiku-4-5-20251001")
    _turn(store, s.done_yesterday, "bump pygments", final_text="Done — `pygments==2.19.2`.",
          started=NOW - 26 * 3600, duration_s=8, tokens=(300, 120, 0, 0), cost=0.003)
    _set_times(store, s.done_yesterday, created=NOW - 26 * 3600, last=NOW - 26 * 3600)
    with store._tx() as c:
        c.execute("UPDATE sessions SET reviewed_at=? WHERE id=?", (NOW - 25 * 3600, s.done_yesterday))

    # 6. Archived, old.
    s.archived = mk("Old experiment: whisper large-v3 on CPU", "try whisper large v3 on cpu")
    _turn(store, s.archived, "try whisper large v3 on cpu", final_text="Too slow (~9s/clip).",
          started=NOW - 40 * 86400, duration_s=20)
    _set_times(store, s.archived, created=NOW - 40 * 86400, last=NOW - 40 * 86400)
    store.archive(s.archived, True)

    # 7. Worktree session — also hosts the live (running) turn.
    s.worktree = mk("Add a dark mode toggle to settings", "add a dark mode toggle to settings",
                    profile="ui-ux", shot=shot_b)
    _turn(store, s.worktree, "add a dark mode toggle to settings",
          final_text="Added the toggle behind `settings.theme`. Screenshot attached.",
          started=NOW - 1800, duration_s=75, shot=shot_b, tokens=(4_000, 1_200, 500, 30_000), cost=0.22,
          tools=(("Read", {"file_path": "/repo/ui/settings.tsx"}, "export const Settings = () => null\n", False),))
    store.set_worktree(s.worktree, path="/repo/.claude/worktrees/holler-7-dark-mode",
                       branch="holler/7-dark-mode", state="active")
    s.live_turn = 0
    if live:
        s.live_turn = _turn(store, s.worktree, "now wire it to the system appearance",
                            started=NOW - 38, committed=False)
        _set_times(store, s.worktree, created=NOW - 1800, last=NOW - 38)
        with store._tx() as c:
            c.execute("UPDATE sessions SET status='running' WHERE id=?", (s.worktree,))
    else:
        _set_times(store, s.worktree, created=NOW - 1800, last=NOW - 1800 + 75)

    # 8. Errored in the last 24h.
    s.errored = mk("Deploy the staging build", "deploy the staging build")
    _turn(store, s.errored, "deploy the staging build",
          final_text="CLIConnectionError: Cannot write to terminated process (exit code: 143)",
          started=NOW - 5 * 3600, duration_s=3, is_error=True)
    _set_times(store, s.errored, created=NOW - 5 * 3600, last=NOW - 5 * 3600)

    # 9. Placeholder (auto) title — still the transcript head.
    s.placeholder = store.create_session(
        model="claude-opus-5",
        transcript="hey can you look at the logs from last night and tell me what blew up",
        screenshot_path=None, status="idle")
    _turn(store, s.placeholder, "hey can you look at the logs from last night and tell me what blew up",
          final_text="Two OOM kills on `worker-3` at 02:14 and 02:31 UTC.", started=NOW - 9 * 86400,
          duration_s=50, tokens=(1_000, 300, 0, 0), cost=0.01)
    _set_times(store, s.placeholder, created=NOW - 9 * 86400, last=NOW - 9 * 86400)
    return s


def rate_limit_state() -> dict:
    return {
        "five_hour": {"utilization": 0.64, "status": "allowed_warning",
                      "resets_at": NOW + 2 * 3600 + 14 * 60, "seen_at": NOW - 300},
        "seven_day": {"utilization": 0.31, "status": "allowed",
                      "resets_at": NOW + 3 * 86400 + 5 * 3600, "seen_at": NOW - 300},
    }


def running_job(seed: Optional[Seed] = None):
    from holler.jobs import ContentBlock, Job, ToolEntry

    j = Job(id=901, transcript="now wire it to the system appearance", submitted_at=NOW - 40,
            status="running", started_at=NOW - 38, model="claude-opus-5")
    j.blocks = [
        ContentBlock(kind="thinking", text="Settings already exposes a theme key; hook QStyleHints."),
        ContentBlock(kind="tool", tool_name="Read", tool_input={"file_path": "/repo/ui/theme.ts"},
                     tool_id="t1", tool_result="\n".join(f"line {i}" for i in range(60)),
                     started_ts=NOW - 30, ended_ts=NOW - 29),
        ContentBlock(kind="text", text="The theme is read once at startup. I'll subscribe to the "
                     "**system appearance** change instead:\n\n```python\n"
                     "hints.colorSchemeChanged.connect(apply_theme)\n```"),
        ContentBlock(kind="tool", tool_name="Bash", tool_input={"command": "npm test -- theme"},
                     tool_id="t2", started_ts=NOW - 5),
    ]
    j.tool_log = [
        ToolEntry(id="t1", name="Read", input={"file_path": "/repo/ui/theme.ts"}, started_ts=NOW - 30,
                  ended_ts=NOW - 29, duration_ms=840, result_preview="line 0\nline 1\nline 2"),
        ToolEntry(id="t2", name="Bash", input={"command": "npm test -- theme"}, started_ts=NOW - 5),
    ]
    j.current_tool = j.tool_log[-1]
    j.streaming_text = "The theme is read once at startup."
    j.input_tokens, j.output_tokens = 6_200, 1_100
    j.cache_read_input_tokens = 22_000
    j.total_cost_usd = 0.184
    if seed is not None:
        j.local_session_id = seed.worktree
        j.local_turn_id = seed.live_turn
    return j


def panel_jobs():
    from holler.jobs import Job

    j_run = running_job()
    j_q = Job(id=902, transcript="add a dark mode toggle to settings", submitted_at=NOW - 3, status="queued")
    j_done = Job(id=903, transcript="why is the canvas summary not updating", submitted_at=NOW - 600,
                 status="done", started_at=NOW - 590, ended_at=NOW - 400, duration_ms=190_000,
                 final_text="The summary widget subscribes to the wrong channel. Fixed in "
                            "`canvas/summary.ts` and added a test.",
                 model="claude-opus-5", input_tokens=48_000, output_tokens=3_100, total_cost_usd=0.84)
    j_err = Job(id=904, transcript="deploy the staging build", submitted_at=NOW - 900, status="error",
                error="CLIConnectionError: Cannot write to terminated process (exit code: 143)",
                started_at=NOW - 899, ended_at=NOW - 880, duration_ms=19_000)
    return [j_err, j_done, j_q, j_run]


def fixed_config():
    """A Config with fixed, machine-independent values (repo_cwd=/tmp exists everywhere on macOS)."""
    from holler.config import Config

    return Config(
        repo_cwd=Path("/tmp"), claude_model="claude-opus-5", hotkey="right alt", mic_device="",
        ui_scale=0.0, whisper_model="small", whisper_compute="int8", whisper_device="cpu",
        whisper_language="en", panel_corner="top-right", panel_width=380,
        max_screenshot_long_edge=1280, stroke_color="#FF2E2E", stroke_width=4,
        debug_save_captures=True, sessions_window_hotkey="ctrl+shift+h", auto_archive_days=30,
        purge_reviewed_days=3, mini_panel_max_sessions=12, open_sessions_on_launch=True,
        concurrency_cap=8, per_session_worktrees=True,
    )


# ── surfaces ─────────────────────────────────────────────────────────────────

def make_sessions_window(env: VisualEnv, *, seeded: bool = True, live: bool = False,
                         size=(1220, 760)):
    from holler.jobs import JobStore
    from holler.model_prefix import format_model_label
    from holler.sessions_window import SessionsWindow

    store = env.store()
    seed = None
    if seeded:
        seed = seed_store(store, env.root / "shots", live=live)
        env.write_state({"sessions_recents": [seed.rich, seed.worktree, seed.long_title],
                         "rate_limits": rate_limit_state()})
    jobs = JobStore(store)
    if live and seed is not None:
        jobs.add(running_job(seed))
    w = SessionsWindow(store, jobs, env.captures, Path("/tmp"), hotkey="right alt")
    w.set_model_label(format_model_label("claude-opus-5").upper())
    w._refresh_warmth()
    env.place(w, size)
    w.show()
    pump(150)
    env.place(w, size)
    pump(200)
    return w, store, jobs, seed


def _browser(w):
    return w._detail._reply_browser


def _settle_selection(w, sid: int) -> None:
    w.select_session(sid)
    # Selection triggers deferred scroll/settle single-shots (≤600 ms).
    pump(800)


def _scroll(w, where: str) -> None:
    sb = _browser(w).verticalScrollBar()
    sb.setValue(0 if where == "top" else sb.maximum())
    pump(80)


Scenario = Callable[[VisualEnv], QtWidgets.QWidget]
SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str):
    def deco(fn: Scenario) -> Scenario:
        SCENARIOS[name] = fn
        return fn
    return deco


@scenario("sessions_empty_dashboard")
def _s_empty(env):
    w, *_ = make_sessions_window(env, seeded=False)
    return w


@scenario("sessions_dashboard")
def _s_dashboard(env):
    w, *_ = make_sessions_window(env)
    return w


@scenario("sessions_rich_top")
def _s_rich_top(env):
    w, _, _, seed = make_sessions_window(env)
    _settle_selection(w, seed.rich)
    _scroll(w, "top")
    return w


@scenario("sessions_rich_bottom")
def _s_rich_bottom(env):
    w, _, _, seed = make_sessions_window(env)
    _settle_selection(w, seed.rich)
    _scroll(w, "bottom")
    return w


@scenario("sessions_live_streaming")
def _s_live(env):
    w, _, _, seed = make_sessions_window(env, live=True)
    _settle_selection(w, seed.worktree)
    _scroll(w, "bottom")
    return w


@scenario("sessions_filter_done")
def _s_done(env):
    w, *_ = make_sessions_window(env)
    w._set_filter("done")
    pump(150)
    return w


@scenario("sessions_multiselect")
def _s_multi(env):
    w, _, _, seed = make_sessions_window(env)
    items = {}
    for i in range(w._list.count()):
        it = w._list.item(i)
        sess = it.data(QtCore.Qt.UserRole)
        if sess is not None and not it.data(QtCore.Qt.UserRole + 1):
            items[sess.id] = it
    w._list.setCurrentItem(items[seed.rich])
    pump(800)
    items[seed.long_title].setSelected(True)
    pump(200)
    _scroll(w, "top")
    return w


@scenario("sessions_narrow")
def _s_narrow(env):
    w, _, _, seed = make_sessions_window(env, size=(860, 640))
    _settle_selection(w, seed.rich)
    _scroll(w, "top")
    return w


@scenario("quick_switcher")
def _s_qs(env):
    from holler.sessions_window import QuickSwitcherDialog

    store = env.store()
    seed = seed_store(store, env.root / "shots")
    d = QuickSwitcherDialog(store, [seed.rich, seed.worktree, seed.done_recent])
    env.place(d, (640, 480))
    d.show()
    pump(150)
    return d


@scenario("profile_picker")
def _s_pp(env):
    from holler.sessions_window import ProfilePickerDialog

    d = ProfilePickerDialog()
    env.place(d, (520, 560))
    d.show()
    pump(150)
    return d


def _settings(env):
    from holler.settings_dialog import SettingsDialog

    return SettingsDialog(fixed_config(), on_save=lambda c, ch: None)


@scenario("settings_default")
def _s_settings_default(env):
    d = _settings(env)
    d.move(WIN_POS)
    d.show()  # default open size = Qt's sizeHint-driven initial size
    pump(150)
    return d


@scenario("settings_tall")
def _s_settings_tall(env):
    d = _settings(env)
    env.place(d, (620, 1300))
    d.show()
    pump(150)
    return d


@scenario("first_run")
def _s_first_run(env):
    from holler.first_run import FirstRunDialog

    d = FirstRunDialog(fixed_config())
    d.move(WIN_POS)
    d.show()
    pump(150)  # lets the deferred _check_auth (stubbed: CLI missing) run
    return d


@scenario("panel_jobs")
def _s_panel(env):
    from holler.jobs import JobStore
    from holler.panel import Panel

    jobs = JobStore(None)
    p = Panel(jobs, corner="top-right", width=380, model_label="OPUS 5")
    for j in panel_jobs():
        jobs.add(j)
    p.set_routing_target(7, "Add a dark mode toggle to settings")
    env.place(p, (380, 640))
    p.show()
    pump(150)
    return p


@scenario("toast")
def _s_toast(env):
    from holler.panel import Toast

    t = Toast()
    t.show_message('Sent to new session · "refactor the auth middleware so tokens refresh…"',
                   (900, 500), undo_callback=lambda: None)
    pump(100)
    return t


@scenario("overlay_hud")
def _s_overlay(env):
    from PIL import Image, ImageDraw

    from holler.overlay import OverlayWindow

    img = Image.new("RGB", (1600, 1000), (30, 33, 39))
    d = ImageDraw.Draw(img)
    for i, y in enumerate(range(60, 1000, 34)):
        d.rectangle([40, y, 40 + 200 + (i * 97) % 900, y + 10], fill=(150, 156, 166))
    ov = OverlayWindow(stroke_color="#FF2E2E", stroke_width=4)
    ov.show_for(img, (WIN_POS.x(), WIN_POS.y(), WIN_POS.x() + 800, WIN_POS.y() + 500))
    ov._strokes = [
        [(300, 200), (420, 180), (560, 230), (600, 330)],
        [(900, 600), (1100, 600), (1100, 760), (900, 760), (900, 600)],
        [(200, 820), (260, 860), (340, 830)],
    ]
    ov._hold_seconds = 2.4
    ov._mic_level = 0.45
    ov.update()
    pump(100)
    return ov


def _detail_window(env, job):
    from holler.detail import DetailWindow
    from holler.jobs import JobStore

    jobs = JobStore(None)
    jobs.add(job)
    dw = DetailWindow(job, env.captures, jobs)
    env.place(dw, (900, 760))
    dw.show()
    pump(800)
    return dw


@scenario("detail_running")
def _s_detail_running(env):
    return _detail_window(env, running_job())


@scenario("detail_error")
def _s_detail_error(env):
    return _detail_window(env, panel_jobs()[0])


def render(name: str, env: VisualEnv) -> QtGui.QImage:
    w = SCENARIOS[name](env)
    QtGui.QCursor.setPos(CURSOR_PARK)
    return grab(w)


# ── Phase 3 scenarios (only the Phase 3 agent edits between these markers) ──

@scenario("sessions_header_compact")
def _s_header_compact(env):
    """Below theme.HEADER_COMPACT_W the usage + quota chips fold into one."""
    w, _, _, seed = make_sessions_window(env, live=True, size=(960, 640))
    _settle_selection(w, seed.worktree)
    _scroll(w, "bottom")
    return w


@scenario("sessions_inline_rename")
def _s_inline_rename(env):
    w, _, _, seed = make_sessions_window(env)
    _settle_selection(w, seed.rich)
    w._rename_item(w._item_for_session(seed.rich))
    w._rename_editor.setText("Refactor auth middleware (token refresh)")
    pump(80)
    return w


@scenario("quick_switcher_palette")
def _s_palette(env):
    """⌘K palette over the window: a query with highlighted matches and the
    matching actions."""
    from holler.sessions_window import QuickSwitcherDialog

    w, _, _, seed = make_sessions_window(env)
    _settle_selection(w, seed.rich)
    d = QuickSwitcherDialog(w._store, w._recents, parent=w, actions=w._palette_actions())
    d.resize(640, 480)
    d.show()
    d._input.setText("re")
    pump(150)
    return d

# ── end Phase 3 scenarios ──


# ── Phase 4 scenarios (only the Phase 4 agent edits between these markers) ──
# The transcript (DetailView) on its own, at the Sessions window's right-pane
# size, so these don't depend on the rail / header.

def _transcript_view(env, sid_attr: str, *, live: bool = False, size=(820, 900),
                     where: str = "bottom", expand=()):
    from holler.detail import DetailView
    from holler.jobs import JobStore

    store = env.store()
    seed = seed_store(store, env.root / "shots", live=live)
    jobs = JobStore(store)
    job = running_job(seed) if live else None
    if job is not None:
        jobs.add(job)
    sid = getattr(seed, sid_attr)
    turns = store.get_turns(sid)
    v = DetailView()
    v.set_captures_dir(env.captures)
    env.place(v, size)
    v.show()
    pump(60)
    v.show_session(store.get_session(sid), turns,
                   store.get_tool_calls_for_turns([t.id for t in turns]), job, env.captures)
    pump(800)
    for key_fn in expand:
        v._toggle_expanded(key_fn(store, turns))
        pump(200)
    sb = v._reply_browser.verticalScrollBar()
    sb.setValue(0 if where == "top" else sb.maximum())
    pump(80)
    return v


def _tc_key(name: str, nth: int = 0):
    def pick(store, turns):
        calls = [tc for t in turns for tc in store.get_tool_calls(t.id) if tc.name == name]
        return f"tc{calls[nth].id}"
    return pick


@scenario("transcript_rich_top")
def _s_tr_rich_top(env):
    return _transcript_view(env, "rich", where="top")


@scenario("transcript_rich_bottom")
def _s_tr_rich_bottom(env):
    return _transcript_view(env, "rich", where="bottom")


@scenario("transcript_expanded")
def _s_tr_expanded(env):
    # Grep output (turn 1) and Bash output (turn 2) expanded in place.
    return _transcript_view(env, "rich", where="top", size=(820, 1100),
                            expand=(_tc_key("Grep"), _tc_key("Bash")))


@scenario("transcript_live")
def _s_tr_live(env):
    return _transcript_view(env, "worktree", live=True, where="bottom")


@scenario("transcript_error")
def _s_tr_error(env):
    return _transcript_view(env, "errored", where="top", size=(820, 420))


@scenario("transcript_stopped")
def _s_tr_stopped(env):
    # A turn the operator stopped (Stop / Esc): neutral "Stopped" note, not
    # the red failure banner.
    from holler.detail import DetailView
    from holler.jobs import JobStore
    from holler.sdk_session import SessionEvent

    store = env.store()
    jobs = JobStore(store)
    sid = store.create_session(model="claude-opus-4-1", transcript="Refactor the parser",
                               screenshot_path=None)
    tid = store.add_turn(session_id=sid, transcript="Refactor the parser into smaller functions",
                         screenshot_path=None, job_id=1)
    for kind, data in (
        ("submitted", {"transcript": "Refactor the parser into smaller functions",
                       "local_session_id": sid, "local_turn_id": tid}),
        ("started", {}),
        ("text_delta", {"text": "I'll start by reading the parser module."}),
        ("tool_use", {"id": "u1", "name": "Read", "input": {"file_path": "/repo/parser.py"}}),
        ("tool_result", {"tool_use_id": "u1", "content": "def parse(src):\n    ...", "is_error": False}),
        ("error", {"error": "cancelled"}),
    ):
        jobs.apply_event(SessionEvent(1, kind, data))
    turns = store.get_turns(sid)
    v = DetailView()
    env.place(v, (820, 420))
    v.show()
    pump(60)
    v.show_session(store.get_session(sid), turns,
                   store.get_tool_calls_for_turns([t.id for t in turns]), None, env.captures)
    pump(800)
    v._reply_browser.verticalScrollBar().setValue(0)
    pump(80)
    return v
# ── end Phase 4 scenarios ──


# ── Phase 5 scenarios (only the Phase 5 agent edits between these markers) ──
def _p5_overlay(env, size, *, level, seconds, **hud):
    from PIL import Image, ImageDraw

    from holler.overlay import OverlayWindow

    w, h = size
    img = Image.new("RGB", (w * 2, h * 2), (30, 33, 39))
    d = ImageDraw.Draw(img)
    for i, y in enumerate(range(60, h * 2, 34)):
        d.rectangle([40, y, 40 + 200 + (i * 97) % max(1, w * 2 - 300), y + 10], fill=(150, 156, 166))
    ov = OverlayWindow(stroke_color="#FF2E2E", stroke_width=4)
    ov.show_for(img, (WIN_POS.x(), WIN_POS.y(), WIN_POS.x() + w, WIN_POS.y() + h), **hud)
    ov._strokes = [
        [(300.0, 420.0), (420.0, 380.0), (560.0, 430.0), (600.0, 530.0), (560.0, 600.0)],
    ]
    ov._hold_seconds = seconds
    ov._mic_level = level
    ov.update()
    pump(100)
    return ov


@scenario("overlay_hud_capture")
def _s_p5_overlay_capture(env):
    return _p5_overlay(
        env, (800, 500), level=0.45, seconds=12.4,
        destination="→ Continuing “Add a dark mode toggle to settings”",
        source="Safari — holler/README.md", hints=True,
    )


@scenario("overlay_hud_narrow")
def _s_p5_overlay_narrow(env):
    return _p5_overlay(
        env, (300, 220), level=0.82, seconds=65.0,
        destination="→ New session",
        source="Terminal — very long window title that has to elide",
        warning="Screen Recording is off — sending voice only", hints=True,
    )


@scenario("overlay_hud_short")
def _s_p5_overlay_short(env):
    # Too short for the full card at both dodge spots: status line + level bar.
    return _p5_overlay(
        env, (800, 200), level=0.3, seconds=4.0,
        destination="→ New session", source="Terminal — zsh", hints=True,
    )


@scenario("toast_error_action")
def _s_p5_toast_error(env):
    from holler.panel import Toast

    t = Toast()
    card = t.show_message(
        "Holler can't see other windows. Allow Holler in System Settings → Privacy & "
        "Security → Screen Recording, then reopen Holler.",
        (900, 500), action=("Open Settings", lambda: None),
    )
    pump(100)
    return card or t
# ── end Phase 5 scenarios ──


# ── Phase 6 scenarios (only the Phase 6 agent edits between these markers) ──
def _p6_first_run(env, step: int, *, relaunch: bool = False):
    from holler import first_run, mac_permissions as mp

    # Fixed permission states (the real TCC answers vary by machine).
    st = env._stack
    st.enter_context(mock.patch.object(first_run, "mac_permissions_available", lambda: True))
    st.enter_context(mock.patch.object(mp, "mic_authorization_status", lambda: mp.AUTHORIZED))
    st.enter_context(mock.patch.object(mp, "accessibility_trusted", lambda: False))
    st.enter_context(mock.patch.object(mp, "screen_recording_status", lambda: "unknown"))
    d = first_run.FirstRunDialog(fixed_config(), on_relaunch=(lambda: None) if relaunch else None)
    d.move(WIN_POS)
    d.show()
    pump(150)
    d._go(step)
    pump(100)
    return d


@scenario("first_run_permissions")
def _s_p6_first_run_permissions(env):
    return _p6_first_run(env, 1, relaunch=True)


@scenario("first_run_hotkey")
def _s_p6_first_run_hotkey(env):
    d = _p6_first_run(env, 2)
    d._hotkey_record.click()  # recording state
    pump(60)
    return d


@scenario("settings_voice")
def _s_p6_settings_voice(env):
    d = _settings(env)
    env.place(d, (720, 640))
    d.show()
    pump(150)
    d._nav.setCurrentRow(1)
    pump(100)
    return d


@scenario("settings_validation")
def _s_p6_settings_validation(env):
    from holler.settings_dialog import SettingsDialog

    d = SettingsDialog(fixed_config(), on_save=lambda c, ch: None, on_restart=lambda: None)
    env.place(d, (720, 640))
    d.show()
    pump(150)
    d._fields["hotkey"].setText("ctrl+space")
    d._fields["whisper_model"].setCurrentIndex(d._fields["whisper_model"].findData("medium"))
    d._fields["repo_cwd"].setText("/no/such/folder")
    d._on_save_clicked()  # inline errors; nothing is saved
    pump(100)
    return d
# ── end Phase 6 scenarios ──
