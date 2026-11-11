"""Full-app runner: PTT + audio + overlay + composite + SDK + panel UI.

Replaces phase4's console output with the PySide6 panel + toasts. This is what
`python -m holler` (no --phase) launches.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from holler import state as app_state
from holler.audio import Recorder
from holler.auto_title import AutoTitler, title_image_b64
from holler.composite import compose, image_fingerprint, to_png_b64
from holler.config import (
    Config,
    effective_models,
    load as load_config,
    save as save_config,
    worker_model_setting,
)
from holler.hotkey import HotkeyListener
from holler.jobs import JobStore
from holler.model_prefix import extract as extract_model_prefix, format_model_label
from holler.overlay import OverlayWindow
from holler.panel import Panel, set_toast_click_blocker
from holler.quitting import consume_internal_quit, quit_app
from holler.screen import (
    ForegroundTracker,
    ScreenRecordingPermissionError,
    capture_active_window,
    capture_hwnd,
    capture_self_window,
    downscale,
    is_self_frontmost,
    set_dpi_awareness,
)
from holler.replay import load_replay_from_store
from holler.sdk_session import Session, SessionEvent
from holler.session_routing import RoutingDecision, decide as route_decision
from holler.store import Store
from holler.sysprompt import build_append
from holler.superintendent.override import authority_override_for_profile
from holler.transcriber import Transcriber, Transcription, WhisperUnavailable, short_load_reason


def _apply_ui_scale(cfg) -> None:
    """Set QT_SCALE_FACTOR before QApplication so the UI is readable on 1x
    (non-Retina) displays. `cfg.ui_scale > 0` pins an explicit factor; 0.0 means
    auto — 1.3 when the primary display is non-Retina, else unchanged.

    A pinned scale (set via the A+/A− buttons) is authoritative: it OVERWRITES
    any QT_SCALE_FACTOR inherited from the launch env. Without this, a stale env
    value leaked into holler's process permanently shadows the in-app buttons —
    they'd save config.toml but the change could never take effect. Only auto
    mode (ui_scale == 0) defers to a deliberate external override."""
    import os
    try:
        scale = float(getattr(cfg, "ui_scale", 0.0) or 0.0)
    except (TypeError, ValueError):
        scale = 0.0

    if scale > 0:
        scale = max(1.0, scale)
        os.environ["QT_SCALE_FACTOR"] = f"{scale:.3f}"
        logging.info("UI scale pinned to %.2f from config (QT_SCALE_FACTOR)", scale)
        return

    # Auto mode: respect a deliberate external override, else bump to 1.3 on a
    # non-Retina primary display.
    if os.environ.get("QT_SCALE_FACTOR"):
        return
    scale = 1.0
    if sys.platform == "darwin":
        try:
            from AppKit import NSScreen
            if float(NSScreen.mainScreen().backingScaleFactor()) < 1.5:
                scale = 1.3
        except Exception:
            logging.debug("could not read main display backing scale", exc_info=True)
    scale = max(1.0, scale)
    if abs(scale - 1.0) > 0.01:
        os.environ["QT_SCALE_FACTOR"] = f"{scale:.3f}"
        logging.info("UI scale set to %.2f (QT_SCALE_FACTOR)", scale)


def _first_run_relaunch() -> None:
    """First-run "Save and relaunch" (after granting permissions macOS only
    applies them to a fresh process). The dialog has already saved the
    config; nothing else has started yet, so a hard exit after spawning the
    replacement skips no cleanup that matters."""
    cmd, env = build_relaunch_command()
    logging.info("relaunching holler (first-run permissions): %s", cmd)
    subprocess.Popen(cmd, cwd=os.getcwd(), env=env, start_new_session=True)
    logging.shutdown()
    os._exit(0)


def build_relaunch_command() -> tuple[list[str], dict[str, str]]:
    """Command + env to relaunch this holler process in place.

    Qt reads QT_SCALE_FACTOR once at launch, so the A+/A- text-size buttons can
    only take effect on a fresh process. This reconstructs the original
    `python -m holler ...` invocation. QT_SCALE_FACTOR is stripped from the
    child env so `_apply_ui_scale` re-derives it from the (already-saved)
    config instead of inheriting a stale launch value that would shadow the
    operator's new choice."""
    cmd = [sys.executable, "-m", "holler", *sys.argv[1:]]
    env = dict(os.environ)
    env.pop("QT_SCALE_FACTOR", None)
    return cmd, env


def _busy_session_count(job_store) -> int:
    """How many sessions have a turn queued or running — the work a relaunch
    (A-/A+, ⟳) would kill. Jobs are grouped by their local session; a job not
    yet tied to one counts on its own."""
    keys: set[tuple[str, int]] = set()
    for job in job_store.all():
        if job.status not in ("queued", "running"):
            continue
        sid = getattr(job, "local_session_id", None)
        keys.add(("s", sid) if sid is not None else ("j", job.id))
    # Background subagents die with the process too, even between turns.
    running_agents = getattr(job_store, "running_agents", None)
    for agent in (running_agents() if callable(running_agents) else []):
        sid = agent.local_session_id
        keys.add(("s", sid) if sid is not None else ("a", agent.tool_use_id))
    return len(keys)


class _Relauncher:
    """Serializes self-relaunches. ⟳ Restart, Settings → Restart now and the
    debounced A+/A− text-size restart each ask "sessions still running?"
    first; without coordination the debounce timer could fire during (or
    after) another restart's prompt → a second prompt and a second
    replacement process. So: a restart cancels a pending text-size restart
    (it applies the new size too), and once a relaunch is under way nothing
    else starts one."""

    TEXT_SCALE_DELAY_MS = 1400
    SETTINGS_DEFERRED = "Saved — changes apply the next time Holler restarts."

    def __init__(self, *, confirm, relaunch, notice, text_scale_notice) -> None:
        # confirm() -> bool; relaunch(reason, fail_notice) -> bool (spawned);
        # notice(text, kind); text_scale_notice() -> str.
        self._confirm = confirm
        self._relaunch = relaunch
        self._notice = notice
        self._text_scale_notice = text_scale_notice
        self.relaunching = False
        self._timer = QtCore.QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._text_scale_due)

    def schedule_text_scale(self) -> None:
        if not self.relaunching:
            self._timer.start(self.TEXT_SCALE_DELAY_MS)

    def text_scale_pending(self) -> bool:
        return self._timer.isActive()

    def _text_scale_due(self) -> None:
        if self.relaunching:
            return
        if not self._confirm():
            # The new size is already saved; it applies on the next launch.
            self._notice(self._text_scale_notice(), "info")
            return
        self.relaunch(
            "text scale",
            "Couldn't restart automatically. Quit and reopen Holler to apply the new size.",
        )

    def restart(self, from_settings: bool = False) -> None:
        if self.relaunching:
            return
        scale_pending = self._timer.isActive()
        self._timer.stop()
        if not self._confirm():
            if from_settings:
                self._notice(self.SETTINGS_DEFERRED, "info")
            elif scale_pending:
                self._notice(self._text_scale_notice(), "info")
            return
        self._notice("Restarting Holler…", "info")
        self.relaunch("restart button", "Couldn't restart Holler. Quit and reopen it.")

    def relaunch(self, reason: str, fail_notice: str) -> None:
        if self.relaunching:
            return
        self.relaunching = True
        self._timer.stop()
        try:
            spawned = bool(self._relaunch(reason, fail_notice))
        except Exception:
            logging.exception("relaunch (%s) failed", reason)
            spawned = False
        if not spawned:
            self.relaunching = False  # still running: the operator may retry


def _build_restart_confirm(parent, running: int):
    """The "sessions still running" confirmation. Returns (box, restart_button).
    Cancel is the default (and Escape) button so a stray Return can't kill
    running work."""
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle("Restart Holler?")
    noun = "session is" if running == 1 else "sessions are"
    box.setText(f"{running} {noun} still working. Restarting stops {'them' if running != 1 else 'it'}.")
    restart_btn = box.addButton("Restart now", QtWidgets.QMessageBox.DestructiveRole)
    cancel_btn = box.addButton(QtWidgets.QMessageBox.Cancel)
    box.setDefaultButton(cancel_btn)
    box.setEscapeButton(cancel_btn)
    return box, restart_btn


def _confirm_restart_if_busy(job_store, parent) -> bool:
    """True when it's OK to relaunch now: nothing is running, or the operator
    chose "Restart now"."""
    running = _busy_session_count(job_store)
    if running <= 0:
        return True
    box, restart_btn = _build_restart_confirm(parent, running)
    box.exec()
    return box.clickedButton() is restart_btn


def _apply_app_style(app) -> None:
    """Dark colour scheme, token palette and the app stylesheet
    (holler.theme), applied once before the first window is built. Cosmetic:
    a failure here must never block launch. Idempotent."""
    try:
        from holler import theme
        theme.apply_app_style(app)
    except Exception:
        logging.exception("failed to apply app style")


def main() -> None:
    from holler.log_setup import configure as configure_logging
    configure_logging()
    # Skip the SDK's `claude -v` subprocess on every connect — it spawns a
    # second Node cold-start per turn (300-800ms on Windows) just to log a
    # warning we don't care about. Set BEFORE any SDK import path runs.
    import os
    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    set_dpi_awareness()
    cfg = load_config()

    # Must run before ANY QApplication is constructed — Qt reads QT_SCALE_FACTOR
    # once at startup. On a non-Retina (1x) primary display, holler's fixed
    # point-size fonts render physically tiny on high-res external monitors;
    # this bumps the global UI scale so they stay readable.
    _apply_ui_scale(cfg)

    # Wipe stale session-aliveness markers from any previous Holler run. Any
    # coordinating slash commands consult these markers to tell live
    # locks from abandoned ones; markers from a dead Holler process are by
    # definition stale.
    from holler import session_locks
    session_locks.wipe_all()

    # Migrate any legacy pre-V3 sysprompt into the user-override file
    # (one-shot, idempotent), then check if first-run setup is needed before any UI initializes.
    from holler.first_run import (
        FirstRunDialog,
        maybe_migrate_legacy_sysprompt,
        needs_first_run,
    )
    maybe_migrate_legacy_sysprompt()
    if needs_first_run(cfg):
        _bootstrap_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        _apply_app_style(_bootstrap_app)
        dlg = FirstRunDialog(cfg, on_relaunch=_first_run_relaunch)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            sys.exit(0)
        cfg = load_config()

    # The first-run dialog above may already have created the QApplication;
    # constructing a second one aborts ("QApplication singleton already
    # exists") on a fresh install.
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    _apply_app_style(app)
    # Sessions window is the primary UI; its close → quit. Default behavior
    # works (last visible window closed → app quits).
    app.setQuitOnLastWindowClosed(True)

    overlay = OverlayWindow(stroke_color=cfg.stroke_color, stroke_width=cfg.stroke_width)

    # Persistence layer (Phase 2). The auto-archive sweep used to run before
    # the first paint, doing a full-table UPDATE on the sessions table — tens
    # of ms for a multi-thousand-session DB on the critical launch path.
    # Defer it 2s past first paint so the UI shows up immediately.
    from holler.appdir import app_data_dir
    db_path = app_data_dir() / "holler.db"
    persistent_store = Store(db_path)

    def _deferred_auto_archive() -> None:
        cutoff = time.time() - cfg.auto_archive_days * 86400
        try:
            archived_n = persistent_store.auto_archive_older_than(cutoff)
            if archived_n:
                logging.info(
                    "auto-archived %d sessions older than %d days",
                    archived_n, cfg.auto_archive_days,
                )
        except Exception:
            logging.exception("deferred auto-archive sweep failed")

    QtCore.QTimer.singleShot(2000, _deferred_auto_archive)

    worktree_holder: dict[str, object] = {"mgr": None}

    def _deferred_purge_reviewed() -> None:
        # Rolling retention: permanently remove sessions marked Done more than
        # `purge_reviewed_days` ago, including their on-disk captures + SDK
        # transcript. 0 = disabled. Deferred off the launch path like the
        # auto-archive sweep so first paint isn't blocked.
        if cfg.purge_reviewed_days <= 0:
            return
        from holler.retention import purge_reviewed_sessions
        cutoff = time.time() - cfg.purge_reviewed_days * 86400
        try:
            result = _call_purge_reviewed(
                purge_reviewed_sessions, persistent_store, cfg.captures_dir, cutoff,
                worktree_holder.get("mgr"),
            )
            if result["sessions"]:
                logging.info(
                    "retention purge: removed %d reviewed sessions (>%d days) "
                    "and %d on-disk files",
                    result["sessions"], cfg.purge_reviewed_days, result["files"],
                )
        except Exception:
            logging.exception("deferred retention purge sweep failed")

    QtCore.QTimer.singleShot(3500, _deferred_purge_reviewed)

    store = JobStore(store=persistent_store)

    # Stage 1: per-session worktree mode. When enabled, construct the manager
    # once + reconcile DB state vs `git worktree list` before any PTT can
    # spawn a session. When disabled, worktree_manager stays None and
    # Session._execute falls through to the shared cwd (legacy behavior).
    worktree_manager = None
    if cfg.per_session_worktrees:
        # One manager per repo: the default repo (cfg.repo_cwd) plus any
        # configured project a session is pinned to (cfg.projects). With no
        # project sessions it behaves exactly like a single WorktreeManager.
        from holler.worktree_manager import WorktreeRegistry, WorktreeError
        try:
            worktree_manager = WorktreeRegistry(
                main_repo_cwd=cfg.repo_cwd,
                store=persistent_store,
            )
            recovery = worktree_manager.recover_on_startup()
            logging.info(
                "worktree recovery: checked=%d healthy=%d orphaned=%d",
                recovery["checked"], recovery["healthy"], len(recovery["orphaned"]),
            )
        except WorktreeError:
            logging.exception(
                "WorktreeManager init failed — disabling per-session worktrees "
                "for this run. Sessions will share the main checkout."
            )
            worktree_manager = None
    # Live manager, read by the deferred purge and swapped by apply_settings
    # when repo_cwd changes.
    worktree_holder["mgr"] = worktree_manager

    # Restore last panel position/size if we have one.
    persisted = app_state.load()
    panel_geom_dict = persisted.get("panel") or {}
    saved_panel_geom = None
    if all(k in panel_geom_dict for k in ("x", "y", "w", "h")):
        saved_panel_geom = (
            int(panel_geom_dict["x"]), int(panel_geom_dict["y"]),
            int(panel_geom_dict["w"]), int(panel_geom_dict["h"]),
        )

    # Panel is constructed for its method shims (show_inline_notice, set_routing_target,
    # set_model_label, flash_ptt_pulse, current_geometry) but is NOT shown.
    # The SessionsWindow is the primary UI; the mini panel was redundant.
    panel = Panel(
        store,
        corner=cfg.panel_corner,
        width=cfg.panel_width,
        model_label=format_model_label(cfg.claude_model),
        saved_geometry=saved_panel_geom,
        sessions_hotkey=cfg.sessions_window_hotkey,
    )
    # panel.show() intentionally skipped — Sessions window owns the UI.

    # Persist panel geometry on quit.
    def _persist_state() -> None:
        try:
            x, y, w, h = panel.current_geometry()
            app_state.merged_save({"panel": {"x": x, "y": y, "w": w, "h": h}})
        except Exception:
            logging.exception("failed to persist panel state")
    app.aboutToQuit.connect(_persist_state)

    # Force native handles so we can exclude our own windows from the tracker.
    # Calling winId() materializes the underlying HWND without showing the window.
    own_hwnds: set[int] = set()
    try:
        own_hwnds.add(int(panel.winId()))
        own_hwnds.add(int(overlay.winId()))
    except Exception:
        logging.exception("failed to read own hwnds; tracker will allow self-capture")

    tracker = ForegroundTracker()
    tracker.set_exclude(own_hwnds)
    tracker.start()

    recorder = Recorder(device=cfg.mic_device)
    transcriber = Transcriber(
        model_size=cfg.whisper_model,
        device=cfg.whisper_device,
        compute_type=cfg.whisper_compute,
        language=cfg.whisper_language,
    )

    # Mic level → overlay HUD via a Qt signal (audio callback runs off-thread).
    mic_signal = _MicLevelSignal()
    mic_signal.level.connect(overlay.set_mic_level)
    recorder.on_level = lambda lvl: mic_signal.level.emit(float(lvl))

    # Thread-safe toast helper — marshals a notice to whichever surface is up
    # (Sessions window if open, else the mini panel).
    def _ui_notice(msg: str) -> None:
        target = state.get("sessions_window") or panel
        try:
            QtCore.QMetaObject.invokeMethod(
                target, "show_inline_notice", QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, msg),
            )
        except Exception:
            pass

    # Dead-stream watchdog: the mic opened but delivered no frames — a muted
    # device or a macOS Microphone permission that's stale enough to let the
    # stream open but feed it silence.
    def _on_mic_dropout() -> None:
        dev = recorder.last_open_device
        dev = f"“{dev}”" if dev else "your mic"
        _ui_notice(f"No sound from {dev}. Check it isn't muted, then try again.")
    recorder.on_dropout = _on_mic_dropout

    # The pinned mic was gone (earbuds in/out rename macOS input devices), so we
    # recorded from another one rather than losing the turn. Say which.
    def _on_mic_fallback(configured: str, actual: str) -> None:
        _ui_notice(
            f"“{configured}” isn't connected, so Holler used “{actual}”. To "
            "follow your headset automatically, choose System default in "
            "Settings → Microphone."
        )
    recorder.on_fallback = _on_mic_fallback

    # Whisper notices → Sessions window toast (thread-safe via QMetaObject).
    transcriber.on_notice = _ui_notice

    # `state["cfg"]` is the live, mutable config reference. Worker threads read
    # from here so settings dialog updates are picked up on the next dispatch.
    state: dict[str, object] = {
        "snapshot": None,
        "thumb_path": None,
        "ready_audio": False,
        "ready_session": False,
        "cfg": cfg,
        "listener": None,  # populated below
        "persistent_store": persistent_store,
        "active_session_id": None,
        # Held between "+ New session" click (with profile selected) and the
        # first PTT that creates the session row. Cleared after consumption.
        "pending_profile": None,
        "panel": panel,
        "sessions_window": None,  # set lazily when SessionsWindow is opened
    }

    def _speech_state() -> str:
        if state.get("audio_error"):
            return "failed"
        return "ready" if state.get("ready_audio") else "loading"

    def update_warmth() -> None:
        speech = _speech_state()
        if speech == "failed":
            panel.set_warmth("Speech unavailable")
        elif speech == "loading":
            panel.set_warmth("Loading speech model…")
        elif not state.get("ready_session"):
            panel.set_warmth("Starting…")
        else:
            panel.set_warmth("Ready")
        sw = state.get("sessions_window")
        if sw is not None:
            try:
                sw.set_speech_state(speech)
            except Exception:
                logging.exception("could not push speech state to the Sessions window")

    # Marshal "warmth" updates to the GUI thread.
    warmth_signal = _WarmthSignal()
    warmth_signal.warmth.connect(update_warmth)

    def _mic_permission_preflight() -> None:
        """On macOS, check the Microphone (TCC) grant up front: prompt if it's
        never been asked, warn clearly if it's been denied. Without this a denied
        permission only shows up as a silent failed capture mid-conversation."""
        try:
            from holler import mac_permissions as macperm
        except Exception:
            return
        if not macperm.is_macos():
            return
        status = macperm.mic_authorization_status()
        logging.info("microphone permission status: %s", macperm.status_name(status))
        if status == macperm.NOT_DETERMINED:
            macperm.request_mic_access()  # surfaces the OS prompt
        elif macperm.is_denied():
            _ui_notice(
                "macOS is blocking Holler's mic. Allow it in System Settings → "
                "Privacy & Security → Microphone, then reopen Holler."
            )

        # Accessibility (TCC): the PTT hotkey listener (pynput) silently does
        # nothing without it. Running as its own app bundle, holler has a
        # different TCC identity than the launching terminal, so it must be
        # granted separately. Prompt + register the app if not yet trusted.
        if not macperm.accessibility_trusted():
            logging.warning("Accessibility not granted — PTT hotkey will not work until enabled")
            macperm.request_accessibility()  # registers the app + shows the OS dialog
            _ui_notice(
                "Holler needs Accessibility access for the push-to-talk key. Turn on "
                "Holler in System Settings → Privacy & Security → Accessibility, "
                "then reopen Holler."
            )

    def warmup() -> None:
        _mic_permission_preflight()
        # Whisper model load
        try:
            _warm_whisper(transcriber, state, _ui_notice)
        except Exception:
            logging.exception("whisper warmup failed")
        warmth_signal.warmth.emit()

    def on_event(ev: SessionEvent) -> None:
        store.apply_event(ev, thumbnail_path=str(state.get("thumb_path") or ""))

    session = Session(
        repo_cwd=str(cfg.repo_cwd),
        model=cfg.claude_model,
        system_prompt_append=build_append(),
        worktree_manager=worktree_manager,
        # Shared-checkout mode: a session pinned to a project repo still runs
        # in that repo (worktree mode routes via the registry instead).
        session_repo_resolver=lambda sid: _session_repo(persistent_store, sid),
        on_event=on_event,
        # Per-lane SDK turn cap from config (was silently the hardcoded 8).
        concurrency=cfg.concurrency_cap,
        is_terminal=store.is_terminal,
        # Holler-owned conversation continuity: when the SDK subprocess is
        # fresh (post-restart, post-reaper, options-change), rebuild context
        # from our SQLite store instead of relying on the SDK's hidden
        # JSONL (which goes missing/stale). See holler/replay.py.
        # before_turn_id: the turn being sent is excluded from its own replay.
        replay_loader=lambda sid, before_turn_id=None: load_replay_from_store(
            persistent_store, sid, before_turn_id,
        ),
    )
    session.start()
    # Mark session "ready" as soon as the loop is up — no shared client to warm.
    state["ready_session"] = True
    warmth_signal.warmth.emit()

    # Superintendent: ensure the pinned evidence-inbox session exists and stash
    # its id so _dispatch_job can intercept PTTs routed there. The dispatcher
    # (started below) needs the live Session object, so expose it on state.
    state["session"] = session
    # Interactive session chains — the same "hold several captures, then file
    # them as one" affordance as the Superintendent inbox, but for a normal
    # (non-inbox) session: filing submits ONE multi-image turn INTO that session
    # (no new session, no worker). Maps local_session_id -> chain dict (see
    # holler.session_chain). Guarded by its own lock: appended from the dispatch
    # thread, flushed from the chain-bar thread. Independent of the
    # superintendent feature flag so plain sessions can chain even when the
    # inbox is disabled.
    state["session_chains"] = {}
    state["session_chain_lock"] = threading.Lock()
    try:
        if getattr(cfg, "superintendent_enabled", True):
            state["superintendent_inbox_sid"] = persistent_store.ensure_superintendent_inbox()
            # Voice-chained evidence: PTTs accumulate here until a commit cue (or
            # the auto-commit timeout in the dispatcher tick) flushes them as one
            # row. Guarded by a lock — mutated from the dispatch thread (append)
            # and the dispatcher thread (auto-commit).
            state["superintendent_draft"] = None
            state["superintendent_draft_lock"] = threading.Lock()
            # Deterministic chain mode — armed via the inbox Chain bar button;
            # while armed EVERY dump is held (no cue needed) until File/Discard.
            state["superintendent_chain_armed"] = False
            # Evidence lane selected in the inbox toolbar: 'deploy' (autonomous
            # remediation → prod) or 'investigation' (IR/forensics → report, no
            # deploy). Every dump/chain is stamped with the lane it was filed in.
            state["superintendent_mode"] = "deploy"
    except Exception:
        logging.exception("failed to ensure superintendent inbox session")

    # Start the Superintendent dispatcher a few seconds past first paint (after
    # the other lane dispatchers) so their boot git calls don't collide. Gated
    # by cfg.superintendent_enabled.
    def _start_superintendent() -> None:
        try:
            live_cfg = state.get("cfg") or cfg
            if not getattr(live_cfg, "superintendent_enabled", True):
                return
            from holler.superintendent.dispatcher import SuperintendentDispatcher
            sd = SuperintendentDispatcher(persistent_store, session, state)
            sd.start()
            app._superintendent = sd  # type: ignore[attr-defined]
            app.aboutToQuit.connect(lambda: sd.stop(join_timeout=1.0))
        except Exception:
            logging.exception("superintendent dispatcher failed to start")
    QtCore.QTimer.singleShot(8000, _start_superintendent)

    threading.Thread(target=warmup, name="holler-warmup", daemon=True).start()

    # ----- capture-time notices -----
    # Notices that need more than show_inline_notice offers (an action button,
    # a sticky status) go through the panel's Toast, anchored where the
    # Sessions window's own toasts appear when it's open; the shared toast
    # stack keeps the two from overlapping.
    def _sw_anchor():
        win = state.get("sessions_window")
        try:
            if win is not None and win.isVisible() and not win.isMinimized():
                p = win.mapToGlobal(QtCore.QPoint(win.width(), win.height()))
                return (p.x(), p.y())
        except Exception:
            pass
        return None

    def _gui_notice(text: str, **opts):
        """GUI thread only. Returns the toast card (or None if queued)."""
        try:
            return panel.show_notice(text, anchor=_sw_anchor(), **opts)
        except Exception:
            logging.exception("capture notice failed")
            return None

    def _screen_recording_notice(blocked: bool) -> None:
        from holler import mac_permissions as macperm
        _gui_notice(
            _screen_recording_message(blocked),
            kind="error",
            action=("Open Settings", macperm.open_screen_recording_settings),
        )

    # "Transcribing…" from PTT release until the turn is submitted (or the
    # capture fails). Shown only if transcription takes noticeably long, so a
    # quick turn doesn't flash a toast.
    transcribing = _TranscribingStatus(
        lambda: _gui_notice(_TRANSCRIBING_TEXT, kind="info", sticky=True))

    capture_ui = _CaptureUiSignals()
    capture_ui.transcribe_finished.connect(transcribing.finished)

    def on_press_qt() -> None:
        if _ptt_press_ignored(state):
            return
        # A new hold: forget any cancel (or ignored press) from a previous one.
        state["capture_cancelled"] = False
        state.pop("ptt_press_ignored", None)
        # Open the mic before grabbing the window. The input stream needs
        # ~40-120ms to deliver its first frame and the window grab blocks this
        # thread for another ~70-110ms; doing the grab first put all of that
        # latency in front of the first frame, so the onset of the operator's
        # first word landed on the floor. On a sub-second press that is the
        # difference between a transcript and a VAD that finds no speech.
        recorder.start()
        # When the operator deliberately focuses holler, capture holler's own
        # window (via Qt, permission-free) so they can talk about its UI.
        # Otherwise capture what they were last looking at (the tracker path).
        self_focused = is_self_frontmost()
        # Routing is fixed now, at press: the selected session, else a new one.
        ptt_target = _ptt_target_sid(state)
        if self_focused and _chain_capture_active(state):
            # A chain is building: the operator only transiently focused holler
            # to watch it grow. Keep targeting the external window they're
            # documenting instead of screenshotting holler's own UI.
            self_focused = False
        permission_blocked = False
        if self_focused:
            snap = capture_self_window()
        else:
            target = tracker.target_hwnd()
            try:
                snap = (capture_hwnd(target, raise_on_permission=True) if target
                        else capture_active_window())
            except ScreenRecordingPermissionError:
                snap, permission_blocked = None, True
        if snap is None:
            # This press is dead — close the stream we just opened, or it stays
            # live and the next start() is a no-op against a stale buffer.
            recorder.stop()
            if self_focused:
                logging.info("holler frontmost but no grabbable window; skipping")
                _gui_notice("Couldn't capture the Holler window. Try again.", kind="error")
            elif permission_blocked or _screen_recording_denied():
                logging.info("no capturable window — Screen Recording not granted")
                _screen_recording_notice(blocked=True)
            else:
                logging.info("no capturable window; skipping")
                _gui_notice(
                    "No window to capture. Click the window you want, then hold "
                    "the push-to-talk key again.",
                    kind="info",
                )
            return
        limited = bool(getattr(snap, "permission_limited", False))
        if limited:
            # The image is wallpaper — the turn goes out voice-only (see
            # _dispatch_job) and the operator gets the fix.
            _screen_recording_notice(blocked=False)
        label = (getattr(snap, "title", "") or "").strip()
        if self_focused:
            source = f"Holler — {label}" if label else "Holler"
        else:
            source = label
        state["snapshot"] = snap
        state["ptt_target"] = ptt_target
        panel.flash_ptt_pulse("●")
        overlay.show_for(
            snap.image, snap.rect,
            destination=_capture_destination(state, ptt_target),
            source=source,
            warning=_LIMITED_HUD_WARNING if limited else "",
            hints=True,
        )
        # Overlay is now in front; the tracker will skip it via exclusion.
        try:
            own_hwnds.add(int(overlay.winId()))
            tracker.set_exclude(own_hwnds)
        except Exception:
            pass

    def on_release_qt() -> None:
        if _ptt_release_ignored(state):
            return
        if _release_was_cancelled(state):
            # Esc already discarded this hold (overlay down, audio dropped).
            overlay.hide()
            return
        snap = state.pop("snapshot", None)
        ptt_target = state.pop("ptt_target", None)
        overlay.hide()
        audio = recorder.stop()
        # Snapshot the recorder's diagnostics now — they're overwritten on the
        # next start(), and the dispatch worker runs asynchronously.
        signal_stats = recorder.last_stats
        open_error = recorder.last_open_error
        device_name = recorder.last_open_device
        if snap is None:
            return
        strokes = overlay.get_strokes()
        # Each PTT carries its own thumb_path so two rapid PTTs don't race
        # over a shared `state["thumb_path"]`.
        thumb_target = cfg.captures_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time()*1000)%1000:03d}.png"
        def _launch(token: int) -> None:
            threading.Thread(
                target=_closing_thread_db(
                    persistent_store,
                    _signal_when_done(_dispatch_job, capture_ui.transcribe_finished, token),
                ),
                args=(snap, strokes, audio, transcriber, session, cfg, state, panel, thumb_target,
                      signal_stats, open_error, device_name, ptt_target),
                daemon=True,
            ).start()
        if transcribing.launch(_launch) is None:
            _gui_notice("Couldn't process that capture — nothing was sent. Try again.",
                        kind="error")

    def on_cancel_qt() -> None:
        """Esc during the hold: discard the screenshot, strokes and audio.
        The PTT release still arrives afterwards and dispatches nothing."""
        if _cancel_capture(state, overlay, recorder):
            _gui_notice("Cancelled — nothing was sent.", kind="info")

    def on_undo_qt() -> None:
        """⌘Z / Ctrl+Z during the hold: remove the last stroke."""
        if overlay.isVisible():
            overlay.undo_last_stroke()

    # Toasts sit above the capture overlay: while it's up, a click on one
    # (meant for the snapshot) must not open a session mid-capture.
    def _capture_in_progress() -> bool:
        return _capture_guard_active(state, overlay)
    set_toast_click_blocker(_capture_in_progress)
    # Same guard for Esc-stops-turn in the transcript: an Esc pressed during a
    # hold cancels the capture and must not also stop a running turn — even
    # once the cancel has hidden the overlay (the key is still held).
    from holler.detail import DetailView
    DetailView.set_capture_active_predicate(_capture_in_progress)

    invoker = _InvokeOnUiThread()
    invoker.requested_press.connect(on_press_qt)
    invoker.requested_release.connect(on_release_qt)
    invoker.requested_cancel.connect(on_cancel_qt)
    invoker.requested_undo.connect(on_undo_qt)

    listener = HotkeyListener(
        cfg.hotkey,
        on_press=lambda: invoker.requested_press.emit(),
        on_release=lambda: invoker.requested_release.emit(),
        # Fire on the listener thread — only emit signals there.
        on_cancel=lambda: invoker.requested_cancel.emit(),
        on_undo=lambda: invoker.requested_undo.emit(),
    )
    listener.start()
    state["listener"] = listener

    # ----- model badge: cycle on click -----
    from holler.model_prefix import cycle_next as cycle_next_model

    def on_model_cycle() -> None:
        cur_cfg: Config = state["cfg"]  # type: ignore[assignment]
        nxt = cycle_next_model(cur_cfg.claude_model, effective_models(cur_cfg))
        # One model for everything: the badge also un-pins Superintendent
        # workers (superintendent_model="" = inherit), so they follow it.
        new_cfg = dataclasses.replace(cur_cfg, claude_model=nxt, superintendent_model="")
        state["cfg"] = new_cfg
        session.set_default_model(nxt)
        panel.set_model_label(format_model_label(nxt))
        # Mirror to the SessionsWindow header badge.
        sw = state.get("sessions_window")
        if sw is not None:
            try:
                sw.set_model_label(format_model_label(nxt))  # type: ignore[attr-defined]
                sw.set_superintendent_model_label(format_model_label(nxt))  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            save_config(new_cfg)
        except Exception:
            logging.exception("failed to persist model change")

    panel.model_cycle_requested.connect(on_model_cycle)

    # Refresh the available-model list off the UI thread. `effective_models()`
    # reads the cache on every click, so whatever this finds is live on the
    # badge's next cycle without a restart.
    def _refresh_models() -> None:
        from holler import claude_cli
        from holler.model_discovery import refresh as refresh_models

        # Probe installed CLIs here, off the UI thread: the picker's
        # CLI-requirement filter and the first turn both need the answer.
        claude_cli.cli_path()
        ids, err = refresh_models()
        if err:
            logging.warning("model discovery: %s (keeping %d cached)", err, len(ids))

    threading.Thread(target=_refresh_models, name="model-discovery", daemon=True).start()

    # ----- settings dialog -----
    def apply_settings(new_cfg: Config, changed: set[str]) -> None:
        state["cfg"] = new_cfg
        if "claude_model" in changed:
            session.set_default_model(new_cfg.claude_model)
            panel.set_model_label(format_model_label(new_cfg.claude_model))
        if "repo_cwd" in changed:
            session.set_repo_cwd(str(new_cfg.repo_cwd))
            # Worktree mode: the manager is bound to the repo it was built
            # for, so without a swap every session keeps editing worktrees of
            # the OLD repo. Rebuild against the new repo; on failure keep the
            # old manager and tell the operator.
            if session.worktree_manager is not None:
                msg = _swap_worktree_manager(
                    session, worktree_holder, new_cfg.repo_cwd, persistent_store,
                )
                if msg:
                    try:
                        panel.show_inline_notice(
                            msg, kind="error" if msg.startswith("Couldn't") else "info")
                    except Exception:
                        pass
        if "panel_width" in changed:
            x, y, _w, h = panel.current_geometry()
            panel.setGeometry(x, y, new_cfg.panel_width, h)
        if ("stroke_color" in changed or "stroke_width" in changed) and hasattr(overlay, "set_stroke"):
            overlay.set_stroke(new_cfg.stroke_color, new_cfg.stroke_width)
        if "mic_device" in changed:
            recorder.set_device(new_cfg.mic_device)
        if "hotkey" in changed:
            cur = state.get("listener")
            if cur is not None:
                try:
                    cur.stop()  # type: ignore[union-attr]
                except Exception:
                    pass
            new_l = HotkeyListener(
                new_cfg.hotkey,
                on_press=lambda: invoker.requested_press.emit(),
                on_release=lambda: invoker.requested_release.emit(),
                on_cancel=lambda: invoker.requested_cancel.emit(),
                on_undo=lambda: invoker.requested_undo.emit(),
            )
            new_l.start()
            state["listener"] = new_l
            hk_win = sessions_window_holder.get("win")
            if hk_win is not None:
                hk_win.set_hotkey(new_cfg.hotkey)  # key name in hints/dashboard

    def _restart_from_settings() -> None:
        _ensure_sessions_window()  # builds the relauncher
        state["relauncher"].restart(from_settings=True)

    def on_settings() -> None:
        from holler.settings_dialog import SettingsDialog
        cur_cfg: Config = state["cfg"]  # type: ignore[assignment]
        win = sessions_window_holder.get("win")
        dlg = SettingsDialog(
            cur_cfg, on_save=apply_settings,
            # Parent on the visible Sessions window (the panel is hidden, which
            # placed the dialog oddly); "Restart now" reuses the window's
            # confirm-if-busy relaunch path.
            parent=win if win is not None else panel,
            on_restart=_restart_from_settings,
        )
        dlg.exec()

    panel.settings_requested.connect(on_settings)

    # ----- Sessions window (Phase 2) -----
    sessions_window_holder: dict[str, object] = {"win": None}
    # ⌘Q / Dock → Quit arrive as a spontaneous Quit event, not a window close:
    # confirm there too when turns are in flight.
    _quit_guard = _SystemQuitGuard(lambda: sessions_window_holder.get("win"), app)
    app.installEventFilter(_quit_guard)

    def _ensure_sessions_window():
        win = sessions_window_holder.get("win")
        if win is not None:
            return win
        from holler.sessions_window import SessionsWindow
        cur_cfg: Config = state["cfg"]  # type: ignore[assignment]
        win = SessionsWindow(
            persistent_store, store, cur_cfg.captures_dir, cur_cfg.repo_cwd,
            hotkey=cur_cfg.hotkey,
            auto_titler=state.get("auto_titler"),
        )
        state["sessions_window"] = win
        try:
            win.set_speech_state(_speech_state())
        except Exception:
            logging.exception("could not set the speech state on the Sessions window")
        # Superintendent Chain bar: tell the window which session is the inbox
        # (so the bar shows only there) and wire its buttons to the draft state.
        inbox_sid = state.get("superintendent_inbox_sid")
        if isinstance(inbox_sid, int):
            try:
                win.set_superintendent_inbox_sid(inbox_sid)
            except Exception:
                logging.exception("failed to set superintendent inbox sid on window")

        def _on_sup_chain_action(action: str) -> None:
            # The one Chain bar drives two contexts:
            # - Inbox on screen (or nothing selected): superintendent draft →
            #   filing creates a new worker session.
            # - Any other session on screen: that session's interactive chain →
            #   filing adds a turn to the visible session.
            active = state.get("active_session_id")
            inbox = state.get("superintendent_inbox_sid")
            if isinstance(inbox, int) and (active is None or active == inbox):
                target, args = (
                    _superintendent_chain_control,
                    (action, state, persistent_store, panel),
                )
            else:
                target, args = (
                    _session_chain_control,
                    (action, session, state, persistent_store, panel),
                )
            threading.Thread(
                target=_closing_thread_db(persistent_store, target),
                args=args, daemon=True,
            ).start()
        win.superintendent_chain_action.connect(_on_sup_chain_action)

        def _on_sup_mode_action(mode: str) -> None:
            state["superintendent_mode"] = (
                "investigation" if mode == "investigation" else "deploy"
            )
        win.superintendent_mode_action.connect(_on_sup_mode_action)
        # Restore geometry if persisted. fit_saved_sw_geometry shrinks/re-
        # anchors the saved rect if it no longer fits the current monitor
        # (laptop undocked into a smaller display, external unplugged, etc.).
        # If no persisted rect is found, SessionsWindow.__init__ already
        # applied a fresh monitor-adaptive default — don't overwrite it.
        sw_geom = persisted.get("sessions_window") or {}
        if all(k in sw_geom for k in ("x", "y", "w", "h")):
            try:
                from holler.sessions_window import fit_saved_sw_geometry
                fx, fy, fw, fh = fit_saved_sw_geometry(
                    int(sw_geom["x"]), int(sw_geom["y"]),
                    int(sw_geom["w"]), int(sw_geom["h"]),
                )
                win.setGeometry(fx, fy, fw, fh)
            except Exception:
                logging.exception("could not restore sessions window geometry")
        # Selection IS the routing decision: clicking a session in the left rail
        # makes subsequent PTTs continue it. "+ New Session" clears selection.
        def _on_sw_select(local_id: int) -> None:
            if local_id < 0:
                state["active_session_id"] = None
                state["active_session_title"] = ""
                panel.set_routing_target(None)
                _push_session_chain_status(state)
                return
            sess = persistent_store.get_session(local_id)
            state["active_session_id"] = local_id
            state["active_session_title"] = sess.title if sess else ""
            # Selecting an existing session abandons a profile picked for a
            # *new* one — the window clears its copy too, so the pill never
            # shows a profile the next new session wouldn't get.
            state["pending_profile"] = None
            panel.set_routing_target(local_id, sess.title if sess else "")
            # Refresh the Chain bar for the newly-selected session so its held
            # count / armed state is accurate the instant it becomes active.
            _push_session_chain_status(state)
        win.session_selected.connect(_on_sw_select)

        def _on_sw_new() -> None:
            state["active_session_id"] = None
            state["active_session_title"] = ""
            panel.set_routing_target(None)
            _push_session_chain_status(state)
        win.new_session_requested.connect(_on_sw_new)

        # Profile chosen in the picker → stored as pending for the next PTT.
        # SessionsWindow emits the profile name (or "" for "No profile").
        def _on_sw_profile(profile_name: str) -> None:
            state["pending_profile"] = profile_name or None
        win.profile_selected.connect(_on_sw_profile)

        def _on_sw_cancel(job_id: int) -> None:
            ok = session.cancel_job(job_id)
            if not ok:
                logging.info("cancel of job %d failed (already done?)", job_id)
        win.cancel_job_requested.connect(_on_sw_cancel)

        # Agents pane Stop → ask the CLI to stop that subagent. Off the GUI
        # thread: stop_agent waits on the SDK loop.
        def _on_sw_stop_agent(local_session_id: int, tool_use_id: str) -> None:
            def _run() -> None:
                if not session.stop_agent(local_session_id, tool_use_id):
                    logging.info("stop of agent %s in session %s failed (already done?)",
                                 tool_use_id, local_session_id)
            threading.Thread(target=_run, name="holler-stop-agent", daemon=True).start()
        win.stop_agent_requested.connect(_on_sw_stop_agent)

        # Text input at bottom of right pane → run a transcript-only turn through
        # the same routing as PTT (no screenshot, no audio).
        def _on_sw_text(text: str) -> None:
            text = (text or "").strip()
            if not text:
                return
            threading.Thread(
                target=_closing_thread_db(persistent_store, _dispatch_text_job),
                args=(text, session, state.get("cfg") or cfg, state, panel),
                daemon=True,
            ).start()
        win.text_submitted.connect(_on_sw_text)

        # Text-size buttons (A− / A+) → step the global UI scale + persist.
        def _on_text_scale(direction: int) -> None:
            # Step from the persisted scale (the operator's last saved intent) so
            # repeated presses accumulate even when the live QT_SCALE_FACTOR was
            # inherited from the launch env and lags config. Fall back to the live
            # factor (then 1.0) only in auto mode where nothing is pinned yet.
            cur_cfg: Config = state["cfg"]  # type: ignore[assignment]
            try:
                cur_scale = float(getattr(cur_cfg, "ui_scale", 0.0) or 0.0)
            except (TypeError, ValueError):
                cur_scale = 0.0
            if cur_scale <= 0:
                try:
                    cur_scale = float(os.environ.get("QT_SCALE_FACTOR") or 1.0)
                except (TypeError, ValueError):
                    cur_scale = 1.0
            new_scale = round(min(2.0, max(1.0, cur_scale + 0.1 * direction)), 3)
            if abs(new_scale - cur_scale) < 0.001:
                panel.show_inline_notice(
                    "Text is already at the largest size" if direction > 0
                    else "Text is already at the smallest size",
                    kind="info",
                )
                return
            if relauncher.relaunching:
                return  # already on the way out; the next launch re-reads config
            new_cfg = dataclasses.replace(cur_cfg, ui_scale=new_scale)
            state["cfg"] = new_cfg
            try:
                save_config(new_cfg)
            except Exception:
                logging.exception("failed to persist ui_scale change")
                panel.show_inline_notice("Couldn't save the text size. Try again.", kind="error")
                return
            panel.show_inline_notice(
                f"Text size {int(round(new_scale * 100))}% — restarting Holler…", kind="info"
            )
            # Qt can't restyle a running app's scale, so apply by relaunching.
            # Debounce so a burst of A+/A- clicks results in ONE restart rather
            # than one per click — the timer only fires once the operator stops.
            relauncher.schedule_text_scale()

        def _relaunch_in_place(reason: str, fail_notice: str) -> bool:
            # Persist the window's live geometry BEFORE spawning the replacement.
            # The fail-safe below spawns the new process first, and the fresh
            # holler reads state.json on startup to restore its position. If we
            # relied on aboutToQuit (which runs after the spawn) the new window
            # could race ahead and restore a stale rect — so the operator's last
            # position must be flushed to disk synchronously here, first.
            try:
                g = win.geometry()
                app_state.merged_save({
                    "sessions_window": {
                        "x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()
                    }
                })
                app_state.flush_pending()
            except Exception:
                logging.exception("could not persist window geometry before relaunch")
            # Fail-safe: spawn the replacement FIRST; only quit if it launched.
            # A spawn failure leaves the current app running so the operator is
            # never stranded without holler — they can restart manually.
            cmd, env = build_relaunch_command()
            try:
                subprocess.Popen(cmd, cwd=os.getcwd(), env=env, start_new_session=True)
            except Exception:
                logging.exception("self-relaunch (%s) failed", reason)
                panel.show_inline_notice(fail_notice, kind="error")
                return False
            logging.info("relaunching holler (%s): %s", reason, cmd)
            quit_app()
            return True

        def _restart_confirmed() -> bool:
            # A relaunch kills every queued/running turn — ask first, parented
            # to the Sessions window when it's showing.
            parent = win if win.isVisible() else None
            return _confirm_restart_if_busy(store, parent)

        def _text_scale_notice() -> str:
            scale = getattr(state["cfg"], "ui_scale", 0.0) or 0.0
            return f"Text size {int(round(scale * 100))}% applies the next time Holler starts"

        # One relaunch at a time: ⟳ Restart, Settings → Restart now and the
        # A+/A− debounce timer all go through it (see _Relauncher).
        relauncher = _Relauncher(
            confirm=_restart_confirmed,
            relaunch=_relaunch_in_place,
            notice=lambda text, kind: panel.show_inline_notice(text, kind=kind),
            text_scale_notice=_text_scale_notice,
        )
        state["relauncher"] = relauncher

        win.text_scale_bumped.connect(_on_text_scale)
        win.restart_requested.connect(relauncher.restart)

        # Model badge cycle (was on the panel; now on SessionsWindow header).
        win.model_cycle_requested.connect(panel.model_cycle_requested.emit)
        # Settings menu (was on panel; now on SessionsWindow header).
        win.settings_requested.connect(panel.settings_requested.emit)
        # Mirror panel.set_model_label calls into the SessionsWindow header.
        win.set_model_label(format_model_label(state["cfg"].claude_model))  # type: ignore[union-attr]

        # ----- Superintendent worker-model badge (inbox toolbar) -----
        def _effective_sup_model(c: Config) -> str:
            # Empty override inherits the interactive model.
            return getattr(c, "superintendent_model", "") or c.claude_model

        def _on_sup_model_cycle() -> None:
            cur_cfg: Config = state["cfg"]  # type: ignore[assignment]
            nxt = cycle_next_model(
                _effective_sup_model(cur_cfg), effective_models(cur_cfg)
            )
            new_cfg = dataclasses.replace(
                cur_cfg,
                superintendent_model=worker_model_setting(nxt, cur_cfg.claude_model),
            )
            state["cfg"] = new_cfg
            try:
                win.set_superintendent_model_label(format_model_label(nxt))  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                save_config(new_cfg)
            except Exception:
                logging.exception("failed to persist superintendent model change")

        win.superintendent_model_cycle_requested.connect(_on_sup_model_cycle)
        win.set_superintendent_model_label(  # type: ignore[union-attr]
            format_model_label(_effective_sup_model(state["cfg"]))  # type: ignore[arg-type]
        )
        sessions_window_holder["win"] = win
        return win

    def open_sessions_window() -> None:
        win = _ensure_sessions_window()
        win.show_and_focus()

    panel.sessions_window_requested.connect(open_sessions_window)

    # Clicking a completion toast opens that turn's session.
    def _on_toast_open(job_id: int) -> None:
        job = store.get(job_id)
        sid = getattr(job, "local_session_id", None) if job is not None else None
        open_sessions_window()
        if isinstance(sid, int):
            win = sessions_window_holder.get("win")
            try:
                win.select_session(sid)  # type: ignore[union-attr]
            except Exception:
                logging.exception("could not select session %s from toast", sid)
    panel.toast_open_requested.connect(_on_toast_open)

    # Completion toasts are for when holler is in the background; while the
    # Sessions window is the active window the result is already in view.
    def _sessions_window_active() -> bool:
        sw = sessions_window_holder.get("win")
        return sw is not None and sw.isActiveWindow()
    panel.set_completion_toast_suppressor(_sessions_window_active)

    def _session_title(local_session_id: int) -> str:
        sess = persistent_store.get_session(local_session_id)
        return (sess.title or "") if sess is not None else ""
    panel.set_session_title_resolver(_session_title)

    # Persist sessions window geometry on quit too.
    def _persist_sw_state() -> None:
        win = sessions_window_holder.get("win")
        if win is None:
            return
        try:
            g = win.geometry()
            app_state.merged_save({
                "sessions_window": {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}
            })
        except Exception:
            pass
    app.aboutToQuit.connect(_persist_sw_state)
    # Force any pending debounced state writes (selection, sort, rate-limits)
    # to flush before exit so the operator's last state survives a quick quit.
    app.aboutToQuit.connect(app_state.flush_pending)

    # Global hotkey for the Sessions window.
    sw_invoker = _SessionsInvoker()
    sw_invoker.requested.connect(open_sessions_window)
    global_hotkey_handle = None
    try:
        from holler.hotkey import add_global_hotkey
        global_hotkey_handle = add_global_hotkey(
            cfg.sessions_window_hotkey, lambda: sw_invoker.requested.emit()
        )
    except Exception:
        logging.exception("could not register sessions window hotkey %s", cfg.sessions_window_hotkey)

    # ----- auto-titler -----
    auto_titler = AutoTitler(
        store=persistent_store,
        repo_cwd=str(cfg.repo_cwd),
        model="claude-haiku-4-5-20251001",
    )
    # Attach to the SDK session's loop. Session.start() already created it.
    if session._loop is not None:
        auto_titler.attach_loop(session._loop)

    state["auto_titler"] = auto_titler  # type: ignore[assignment]
    state["active_session_id"] = None

    # ----- titler hook: when a turn completes, decide whether to (re)title.
    # Two paths:
    #   1. First turn finished + title still the transcript-head placeholder
    #      → kick the initial titler with the screenshot for context. This is
    #      a *refinement* pass on top of the title-at-creation kick further
    #      down — that one fires immediately when the session is created so
    #      even errored / never-completed first turns still get a title.
    #   2. Turn count crosses a re-title threshold (3, 6, 12, 24, 48, 96) AND
    #      the session is still auto_titled (user hasn't renamed) AND debounce
    #      window has elapsed → kick a rolling re-title with recent transcripts.
    from holler.auto_title import is_placeholder_title as _is_placeholder
    from holler.auto_title import should_retitle as _should_retitle

    last_retitle_at: dict[int, float] = {}  # session_id → epoch

    def _on_session_changed(local_session_id: int) -> None:
        sess = persistent_store.get_session(local_session_id)
        if sess is None:
            return
        turns = persistent_store.get_turns(local_session_id)
        if not turns:
            return
        latest = turns[-1]
        if not (latest.ended_at > 0):
            return  # still in flight; wait for completion

        # Path 1 — first-turn titling refinement. Fires when the first turn
        # ends AND the title is still the transcript-head placeholder. The
        # is_error gate is intentionally NOT applied: an errored first turn
        # still has a meaningful operator transcript to title from.
        if (
            len(turns) == 1
            and sess.auto_titled
            and _is_placeholder(sess)
        ):
            auto_titler.request(
                local_session_id, sess.transcript, title_image_b64(sess.screenshot_path)
            )
            last_retitle_at[local_session_id] = time.time()
            return

        # Path 2 — rolling re-title at threshold turn counts.
        now = time.time()
        if _should_retitle(len(turns), last_retitle_at.get(local_session_id, 0.0), now):
            recent = turns[-3:]
            transcripts = [t.transcript for t in recent if t.transcript]
            if transcripts:
                auto_titler.request_retitle(local_session_id, transcripts)
                last_retitle_at[local_session_id] = now

    store.session_changed.connect(_on_session_changed)

    # ----- title-at-creation kick.
    # Path 0: fire the AutoTitler the moment a new session row appears in the
    # store, before the first turn even completes. This is the safety net for
    # sessions that crash, time out, or are closed mid-first-turn — the post-
    # first-turn refinement hook (Path 1) would never fire for those. The
    # AutoTitler internally coalesces overlapping requests, so the refinement
    # pass that follows is a no-op if this call already wrote a real title.
    _seen_for_creation: set[int] = set()

    def _on_session_changed_for_creation(local_session_id: int) -> None:
        # Fire once per session per process. The session-changed signal fires
        # on every row update, so we de-dupe with an in-memory set; the
        # placeholder check is the real correctness guard.
        if local_session_id in _seen_for_creation:
            return
        _seen_for_creation.add(local_session_id)
        try:
            sess = persistent_store.get_session(local_session_id)
        except Exception:
            return
        if sess is None or not sess.auto_titled or not _is_placeholder(sess):
            return
        auto_titler.request(
            local_session_id, sess.transcript, title_image_b64(sess.screenshot_path)
        )

    store.session_changed.connect(_on_session_changed_for_creation)

    # ----- resume-failed → toast so the operator sees what happened.
    def _on_resume_failed(local_id: int, reason: str) -> None:
        logging.info("resume failed for session %s: %s", local_id, reason)
        target = state.get("sessions_window") or panel
        try:
            QtCore.QMetaObject.invokeMethod(
                target, "show_inline_notice", QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(
                    str,
                    _resume_failed_message(persistent_store, local_id),
                ),
            )
        except Exception:
            logging.exception("failed to toast resume_failed")
    store.resume_failed.connect(_on_resume_failed)

    # ----- one-time backfill on launch.
    # Sessions stranded by past auto-title failures (or by a holler crash mid-
    # first-turn) still display their raw transcript-head placeholder. Queue
    # them through the AutoTitler at ~1 req/s so we don't burst the API.
    def _backfill_placeholder_titles() -> None:
        try:
            stale = persistent_store.list_placeholder_sessions()
        except Exception:
            logging.exception("title backfill: query failed")
            return
        if not stale:
            return
        logging.info("title backfill: queuing %d placeholder sessions", len(stale))

        def _worker():
            for sess in stale:
                try:
                    auto_titler.request(
                        sess.id, sess.transcript, title_image_b64(sess.screenshot_path)
                    )
                except Exception:
                    logging.exception("title backfill: failed to queue session %d", sess.id)
                time.sleep(1.0)

        t = threading.Thread(
            target=_closing_thread_db(persistent_store, _worker),
            name="title-backfill", daemon=True,
        )
        t.start()

    # Delay a couple seconds so the SDK loop and UI are up before we start
    # hammering Haiku.
    QtCore.QTimer.singleShot(2500, _backfill_placeholder_titles)

    # ----- detail windows -----
    detail_windows: dict[int, "object"] = {}

    def open_detail(job_id: int) -> None:
        from holler.detail import DetailWindow
        existing = detail_windows.get(job_id)
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return
            except Exception:
                detail_windows.pop(job_id, None)
        job_obj = store.get(job_id)
        if job_obj is None:
            return
        win = DetailWindow(
            job_obj,
            state["cfg"].captures_dir,  # type: ignore[union-attr]
            store,
            parent=None,
        )
        win.set_repo_cwd(state["cfg"].repo_cwd)  # type: ignore[union-attr]
        detail_windows[job_id] = win
        win.closed.connect(lambda jid: detail_windows.pop(jid, None))
        # Place to the left of the panel, but clamp inside the panel's screen
        # so the detail window never opens half off-screen on smaller monitors.
        # The detail window has already sized itself adaptively in __init__.
        px, py, _pw, _ph = panel.current_geometry()
        wsz = win.size()
        target_x = px - wsz.width() - 20
        target_y = py
        scr = QtGui.QGuiApplication.screenAt(QtCore.QPoint(px, py))
        if scr is None:
            scr = QtGui.QGuiApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            target_x = max(avail.left(), min(target_x, avail.right() - wsz.width()))
            target_y = max(avail.top(), min(target_y, avail.bottom() - wsz.height()))
        win.move(target_x, target_y)
        win.show()

    panel.detail_open_requested.connect(open_detail)

    # ----- Routing pill clear button: clears active_session_id back to None.
    def on_routing_cleared() -> None:
        state["active_session_id"] = None
        state["active_session_title"] = ""
        panel.set_routing_target(None)
        # Also deselect in the Sessions window so the UI stays in sync.
        win = sessions_window_holder.get("win")
        if win is not None:
            try:
                win._list.clearSelection()  # type: ignore[attr-defined]
                win._list.setCurrentItem(None)  # type: ignore[attr-defined]
            except Exception:
                pass
    panel.routing_cleared.connect(on_routing_cleared)

    # ----- Cancel button on a row → cancel the SDK task.
    def on_cancel(job_id: int) -> None:
        ok = session.cancel_job(job_id)
        if not ok:
            panel.show_inline_notice("That turn already finished, so there was nothing to stop.", kind="info")
    panel.cancel_requested.connect(on_cancel)

    # Sessions window is the primary UI — open it immediately, not on a delay.
    QtCore.QTimer.singleShot(0, open_sessions_window)

    # ----- Keyboard shortcuts on the Panel.
    QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+H"), panel, activated=open_sessions_window)
    QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Comma"), panel, activated=on_settings)
    QtGui.QShortcut(QtGui.QKeySequence("Ctrl+L"), panel, activated=lambda: panel._clear_completed())
    QtGui.QShortcut(QtGui.QKeySequence("Escape"), panel, activated=on_routing_cleared)

    try:
        sys.exit(app.exec())
    finally:
        tracker.stop()
        cur_listener = state.get("listener")
        if cur_listener is not None:
            try:
                cur_listener.stop()  # type: ignore[union-attr]
            except Exception:
                pass
        if global_hotkey_handle is not None:
            try:
                from holler.hotkey import remove_global_hotkey
                remove_global_hotkey(global_hotkey_handle)
            except Exception:
                pass
        # Terminates every SDK subprocess (turns, pooled clients, AutoTitler on
        # the same loop) within ~2s — a quit/relaunch must not orphan a CLI.
        session.stop(reap_all_sdk_children=True)


def _closing_thread_db(persistent_store, target):
    """Wrap a short-lived worker-thread body so the thread's per-thread sqlite
    connection is closed when it exits (otherwise one leaks per thread)."""
    def _run(*args, **kwargs):
        try:
            return target(*args, **kwargs)
        finally:
            closer = getattr(persistent_store, "close_thread_connection", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    logging.exception("close_thread_connection failed")
    return _run


def _block_system_quit(ev, win) -> bool:
    """True when a system-initiated quit (⌘Q / Dock → Quit / logout) must be
    refused because the operator cancelled the "sessions still running"
    prompt. Holler's own quits go through `quitting.quit_app()` and are let
    through untouched — a relaunch has already spawned its replacement and
    must never be cancelled. (Qt 6 delivers both kinds as a spontaneous Quit
    event, so spontaneity can't distinguish them.)"""
    if ev.type() != QtCore.QEvent.Quit or win is None:
        return False
    if consume_internal_quit():
        return False
    try:
        return not win._confirm_quit_if_busy()
    except Exception:
        logging.exception("quit confirmation failed; allowing quit")
        return False


class _SystemQuitGuard(QtCore.QObject):
    def __init__(self, get_window, parent=None) -> None:
        super().__init__(parent)
        self._get_window = get_window

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 — Qt override
        if _block_system_quit(ev, self._get_window()):
            ev.ignore()  # unaccepted Quit → macOS cancels the termination
            return True
        return False


def _call_purge_reviewed(purge_fn, persistent_store, captures_dir, cutoff, worktree_manager):
    """Run the retention purge, handing it the live WorktreeManager so purged
    sessions' worktrees are released instead of leaked."""
    return purge_fn(
        persistent_store, captures_dir, cutoff, worktree_manager=worktree_manager,
    )


def _n_shots(n: int) -> str:
    """`1 screenshot` / `3 screenshots` for chain and evidence notices."""
    return f"{n} screenshot{'s' if n != 1 else ''}"


def _resume_failed_message(store, local_id: int) -> str:
    """The toast for a session whose Claude conversation couldn't be resumed:
    Holler replays the stored turns into a fresh Claude session, so nothing
    was lost — say that, by session title (never an internal id)."""
    title = ""
    try:
        sess = store.get_session(local_id)
        title = (getattr(sess, "title", "") or "").strip() if sess is not None else ""
    except Exception:
        title = ""
    name = f"“{title[:60]}”" if title else "this session"
    return (f"Holler continued {name} in a fresh Claude session and replayed "
            "the conversation into it. Nothing was lost.")


def _session_repo(persistent_store, sid: int) -> Optional[str]:
    """Project repo recorded on a session row, or None for the default repo."""
    sess = persistent_store.get_session(sid) if persistent_store is not None else None
    return (getattr(sess, "repo_cwd", "") or None) if sess is not None else None


def _profile_repo(cfg, profile: Optional[str]) -> Optional[str]:
    """Repo a new interactive session with `profile` should be pinned to —
    the profile's default project, or None (default repo). Never raises: a
    misconfigured mapping falls back to the default repo."""
    from holler.projects import resolve_session_repo
    try:
        return resolve_session_repo(cfg, profile=profile)
    except Exception:
        logging.exception("project lookup for profile %r failed", profile)
        return None


def _swap_worktree_manager(session, worktree_holder, new_repo_cwd, persistent_store,
                           manager_cls=None) -> str:
    """Rebuild the WorktreeManager for a new repo_cwd and swap it into the
    live Session. Returns a user-facing notice ("" on silent success)."""
    if manager_cls is None:
        from holler.worktree_manager import WorktreeRegistry as manager_cls
    try:
        new_mgr = manager_cls(main_repo_cwd=new_repo_cwd, store=persistent_store)
    except Exception as exc:
        logging.exception("WorktreeManager rebuild for %s failed", new_repo_cwd)
        return (
            f"Couldn't use {new_repo_cwd} as the project folder, so sessions still "
            "use the previous one. Check that it's a Git repository, then try again."
        )
    session.set_worktree_manager(new_mgr)
    worktree_holder["mgr"] = new_mgr
    logging.info("worktree manager rebound to %s", new_repo_cwd)
    return f"Project folder changed to {new_repo_cwd}. Sessions use it from their next turn."


def _resolve_routing(persistent_store, state, text: str, *, screenshot_path,
                     effective_model: str, notice, force_new: bool = False,
                     selected_sid: Optional[int] = None) -> tuple:
    """Routing decision shared by the PTT and typed-text paths.

    Returns (local_session_id, local_turn_id, resume_session_id, cleaned_text).

    The operator's selected session is ALWAYS continued — even when it has no
    sdk_session_id yet (cold start, first turn failed before session_init) or
    is older than the recent-50 candidate list. Such a turn goes out with
    resume_session_id=None: Session rebuilds context from the local store via
    replay, and session_init attaches the new sdk id to the same row.
    """
    active_local = selected_sid if isinstance(selected_sid, int) else state.get("active_session_id")
    active_local = active_local if isinstance(active_local, int) else None
    if force_new:
        # Push-to-talk: always a fresh session, whatever is selected and
        # whatever the voice says ("follow up on …" no longer redirects).
        decision = RoutingDecision(
            kind="new", local_session_id=None, sdk_session_id=None,
            matched_title=None, confidence=0.0, cleaned_transcript=text,
        )
        return _apply_routing_decision(persistent_store, state, decision, text,
                                       screenshot_path=screenshot_path,
                                       effective_model=effective_model)
    candidates = []
    try:
        for sess in persistent_store.list_sessions(limit=50):
            if sess.archived:
                continue
            candidates.append((sess.id, sess.title, sess.sdk_session_id))
    except Exception:
        logging.exception("failed to load session candidates")
    active_row = None
    if active_local is not None:
        try:
            active_row = persistent_store.get_session(active_local)
        except Exception:
            logging.exception("failed to load active session %s", active_local)
        if active_row is None:
            active_local = None  # selected row vanished (deleted) → new session
        else:
            candidates = [c for c in candidates if c[0] != active_row.id]
            candidates.insert(0, (active_row.id, active_row.title, active_row.sdk_session_id))

    decision = route_decision(
        text, active_local_session_id=active_local, candidates=candidates,
    )
    if decision.note:
        notice(decision.note)
    return _apply_routing_decision(persistent_store, state, decision, text,
                                   screenshot_path=screenshot_path,
                                   effective_model=effective_model)


def _apply_routing_decision(persistent_store, state, decision, text: str, *,
                            screenshot_path, effective_model: str) -> tuple:
    """Create the turn (and, for a "new" decision, the session) a routing
    decision calls for. Returns (local_session_id, local_turn_id,
    resume_session_id, cleaned_text)."""
    if decision.kind == "resume" and decision.local_session_id is not None:
        local_session_id = decision.local_session_id
        resume_session_id = decision.sdk_session_id or None
        cleaned = decision.cleaned_transcript
        local_turn_id = persistent_store.add_turn(
            session_id=local_session_id,
            transcript=cleaned,
            screenshot_path=screenshot_path,
            job_id=None,
        )
    else:
        # Consume the pending profile (set when "+ New session" was clicked
        # with a profile selected). Cleared so the NEXT new session starts
        # at the picker default again.
        pending_profile = state.get("pending_profile")
        state["pending_profile"] = None
        cleaned = decision.cleaned_transcript or text
        resume_session_id = None
        profile = pending_profile if isinstance(pending_profile, str) else None
        local_session_id = persistent_store.create_session(
            model=effective_model,
            transcript=cleaned,
            screenshot_path=screenshot_path,
            profile=profile,
            repo_cwd=_profile_repo(state.get("cfg"), profile),
        )
        local_turn_id = persistent_store.add_turn(
            session_id=local_session_id,
            transcript=cleaned,
            screenshot_path=screenshot_path,
            job_id=None,
        )
    return local_session_id, local_turn_id, resume_session_id, cleaned


def _screen_unchanged_for_turn(state, session, local_session_id, fingerprint, *,
                               resume_session_id, model_override, append_override) -> bool:
    """True iff the screenshot may be skipped for this turn: identical to the
    previous turn's image for the session AND the turn will reuse the live
    pooled client that saw it. Always records the new fingerprint."""
    if local_session_id is None:
        return False
    fp_cache = state.setdefault("screenshot_fp_by_session", {})
    prev_fp = fp_cache.get(local_session_id)
    fp_cache[local_session_id] = fingerprint
    if prev_fp != fingerprint:
        return False
    try:
        return bool(session.will_reuse_client(
            resume_session_id,
            model_override=model_override,
            system_prompt_append_override=append_override,
        ))
    except Exception:
        return False


# Appended to a deduped turn's transcript for the MODEL. Display code should
# strip it from the operator's own bubble (it isn't something they said).
SCREEN_UNCHANGED_SUFFIX = " (screen unchanged since last turn)"

_TRANSCRIBING_TEXT = "Transcribing…"
_TRANSCRIBING_DELAY_MS = 300
# A dispatch that hasn't finished by then no longer holds the toast up (a hung
# one used to keep "Transcribing…" coming back on every later turn).
_TRANSCRIBING_DEADLINE_S = 90.0


class _TranscribingStatus:
    """The sticky "Transcribing…" toast, shared by overlapping dispatches.

    `launch(start)` allocates a token and calls `start(token)` (which starts
    the dispatch thread); only if that succeeds is the token tracked, with its
    own deadline. The toast appears `delay_ms` later if that token is still
    in flight, and is dismissed once no tracked token is both unfinished and
    within its deadline — `finished(token)` from the worker, or the deadline
    passing for a dispatch that hung. GUI thread only.
    """

    def __init__(self, show, *, delay_ms: int = _TRANSCRIBING_DELAY_MS,
                 deadline_s: float = _TRANSCRIBING_DEADLINE_S,
                 clock=time.monotonic, single_shot=None):
        self._show_notice = show  # () -> the toast card, or None if queued
        self._delay_ms = int(delay_ms)
        self._deadline_s = float(deadline_s)
        self._clock = clock
        self._single_shot = single_shot or QtCore.QTimer.singleShot
        self._seq = 0
        self._deadlines: dict[int, float] = {}
        self._card = None

    def launch(self, start) -> Optional[int]:
        """Start one dispatch via `start(token)`. Returns the token, or None
        (nothing tracked, logged) when `start` raised."""
        self._seq += 1
        token = self._seq
        try:
            start(token)
        except Exception:
            logging.exception("could not start the capture dispatch")
            return None
        self._deadlines[token] = self._clock() + self._deadline_s
        self._single_shot(self._delay_ms, lambda: self._show(token))
        # Re-check just after the deadline so a hung dispatch's toast goes.
        self._single_shot(int(self._deadline_s * 1000) + 50, self._refresh)
        return token

    def finished(self, token: int) -> None:
        self._deadlines.pop(token, None)
        self._refresh()

    def pending(self) -> set[int]:
        """Tokens still in flight and within their deadline."""
        now = self._clock()
        for t in [t for t, d in self._deadlines.items() if d <= now]:
            del self._deadlines[t]
        return set(self._deadlines)

    def card(self):
        card = self._card
        try:
            if card is not None and card.isVisible() and card._text == _TRANSCRIBING_TEXT:
                return card
        except RuntimeError:
            pass
        return None

    def _show(self, token: int) -> None:
        if token in self.pending() and self.card() is None:
            self._card = self._show_notice()

    def _refresh(self) -> None:
        if self.pending():
            return
        card = self.card()
        self._card = None
        if card is not None:
            card.dismiss()
_LIMITED_HUD_WARNING = "Screen Recording is off — sending voice only"


def _screen_recording_message(blocked: bool) -> str:
    """Toast for a missing macOS Screen Recording grant. `blocked`: nothing
    could be captured; otherwise the capture was wallpaper (sent voice-only)."""
    fix = ("Allow Holler in System Settings → Privacy & Security → Screen "
           "Recording, then reopen Holler.")
    if blocked:
        return "Holler can't see other windows. " + fix
    return "Screen Recording is off, so this turn is sent without a screenshot. " + fix


def _screen_recording_denied() -> bool:
    """True on macOS when Screen Recording is known to be missing."""
    if sys.platform != "darwin":
        return False
    try:
        from holler import mac_permissions as macperm
        return macperm.screen_recording_status() == "denied"
    except Exception:
        return False


def _cancel_capture(state, overlay, recorder) -> bool:
    """Esc during a hold (GUI thread): drop the snapshot, hide the overlay
    (strokes go with it), discard the audio and flag the hold so the release
    that follows dispatches nothing. False when no capture is in progress."""
    if state.get("snapshot") is None:
        return False
    state.pop("snapshot", None)
    state["capture_cancelled"] = True
    overlay.hide()
    recorder.stop()
    return True


def _ptt_press_ignored(state) -> bool:
    """A push-to-talk press while a Record button is capturing a new key
    (hotkey.key_recording_active): no capture — and its release is skipped
    too (_ptt_release_ignored), even if recording has ended by then."""
    from holler.hotkey import key_recording_active
    if not key_recording_active():
        return False
    state["ptt_press_ignored"] = True
    return True


def _ptt_release_ignored(state) -> bool:
    """Consume the flag set by _ptt_press_ignored for this hold's release."""
    return bool(state.pop("ptt_press_ignored", False))


def _capture_guard_active(state, overlay) -> bool:
    """A push-to-talk hold is in progress: the capture overlay is up, or Esc
    just cancelled this hold (overlay already hidden) and the push-to-talk key
    hasn't been released yet — the cancel flag lives until that release."""
    if state.get("capture_cancelled"):
        return True
    try:
        return bool(overlay.isVisible())
    except RuntimeError:
        return False


def _release_was_cancelled(state) -> bool:
    """Consume the cancel flag for this hold's release."""
    return bool(state.pop("capture_cancelled", False))


def _capture_destination(state, target_sid: Optional[int] = None) -> str:
    """The HUD's "where does this hold go" line for the press-time target
    (`_ptt_target_sid`)."""
    inbox = state.get("superintendent_inbox_sid")
    inbox = inbox if isinstance(inbox, int) else None
    if inbox is not None and target_sid == inbox:
        from holler.superintendent import drafts as _drafts
        n = _drafts.draft_count(state.get("superintendent_draft"))
        if state.get("superintendent_chain_armed") or n:
            return f"→ Added to chain ({n + 1}) · Start session when ready"
        if state.get("active_session_id") == inbox:
            return "→ Evidence inbox"
        lane = "Investigate" if state.get("superintendent_mode") == "investigation" else "Deploy"
        return f"→ New Superintendent session · {lane}"
    if isinstance(target_sid, int):
        title = ""
        store = state.get("persistent_store")
        if store is not None:
            try:
                sess = store.get_session(target_sid)
                title = (sess.title if sess else "") or ""
            except Exception:
                title = ""
        title = (title or state.get("active_session_title") or "").strip() or "selected session"
        return f"→ Continuing “{title}”"
    try:
        from holler import session_chain as _sc
        chain = (state.get("session_chains") or {}).get(_sc.chain_key(None))
        if _sc.is_armed(chain):
            return f"→ Added to chain ({_sc.count(chain) + 1}) · Start session when ready"
    except Exception:
        pass
    profile = state.get("pending_profile")
    if isinstance(profile, str) and profile:
        return f"→ New session · {profile}"
    return "→ New session"


# Shortest hold worth decoding. The VAD pads speech by 400ms and needs 500ms of
# silence to close a segment, and in this operator's logs every capture at or
# under a second came back empty while 1.4s decoded fine. Only consulted to
# explain an already-failed capture, so it never gates a submit.
_MIN_HOLD_SECONDS = 1.0


def _warm_whisper(transcriber, state, notice) -> None:
    """Load the speech model and record the outcome: `ready_audio` only on a
    real load; on failure `audio_error` + a specific notice (a failed load is
    not "ready" — every PTT would come back empty)."""
    transcriber.preload_async()
    if not transcriber.wait_ready(timeout=120):
        # Still loading (first-run download, slow disk) — keep waiting so the
        # badge flips when it's actually usable.
        logging.warning("whisper still loading after 120s; waiting")
        transcriber.wait_ready()
    err = transcriber.load_error
    if err is None:
        state["ready_audio"] = True
        return
    state["audio_error"] = short_load_reason(err)
    notice(_speech_model_failed_message(state["audio_error"]))


def _speech_model_failed_message(reason: str) -> str:
    """The toast for a speech-model load failure. `reason` (the exception
    summary) is logged, not shown — the operator gets what to do instead."""
    logging.warning("speech model unavailable: %s", reason)
    return (
        "The speech model didn't load, so Holler can't transcribe. Check "
        "Settings → Voice & Mic → Speech model, or see the log for details."
    )


def _audio_failure_message(open_error, signal_stats, device_name, heard=None,
                           model_error=None):
    """Map a failed/empty capture to a specific, actionable toast.

    Returns (message, is_permission). The faults this used to collapse into
    one vague string — permission denied, silent/wrong device, too-quiet input,
    and genuinely-no-speech — now each get their own message.

    `heard` is the Transcription, whose VAD verdict is harder evidence than the
    RMS bands: an idle room on Mac hardware measures rms ~0.014, well above
    _LOW_RMS, so "ok" energy does NOT imply anybody spoke. Prefer what the VAD
    actually found over what the RMS band guesses.

    `model_error` (short reason) means whisper never loaded: nothing could
    have been transcribed, so the RMS/VAD verdicts below don't apply.
    """
    dev_q = f"“{device_name}”" if device_name else "Your mic"
    if open_error == "permission":
        return (
            "macOS is blocking Holler's mic. Allow it in System Settings → "
            "Privacy & Security → Microphone, then reopen Holler.",
            True,
        )
    if open_error == "unavailable":
        return (
            "Couldn't open a microphone. Connect one, or pick it in "
            "Settings → Microphone.",
            False,
        )
    if model_error:
        return (_speech_model_failed_message(model_error), False)
    quality = getattr(signal_stats, "quality", None)
    rms = getattr(signal_stats, "rms", 0.0) or 0.0
    if quality in (None, "empty"):
        return ("No audio was recorded. Check your mic and try again.", False)
    if quality == "all_zero":
        return (
            f"{dev_q} sent only silence. It may be muted, or macOS may be blocking "
            "it. Check both, then reopen Holler.",
            False,
        )
    if quality == "silent":
        return (
            f"Almost no sound from {dev_q}. It may be the wrong mic, or muted. "
            "Pick your mic in Settings → Microphone.",
            False,
        )
    captured = getattr(signal_stats, "seconds", 0.0) or 0.0
    if captured < _MIN_HOLD_SECONDS:
        return (
            f"Only {captured:.1f}s recorded — too short. Hold the push-to-talk "
            "key for the whole sentence.",
            False,
        )
    if heard is not None and not heard.vad_found_speech:
        return (
            f"Didn't hear speech in {captured:.1f}s from {dev_q}. Check that "
            "Settings → Microphone is the mic you're using.",
            False,
        )
    if heard is not None and heard.looks_fabricated:
        # The VAD found "speech" and whisper produced words, but the words are
        # near-silence artefacts. Say nothing was sent, so the operator doesn't
        # go looking for a turn that was deliberately dropped.
        return (
            "That sounded like background noise, not speech — nothing was sent. "
            "Try again, a little closer to the mic.",
            False,
        )
    if quality == "low":
        return (
            "Your voice was very quiet. Move closer to the mic or raise its "
            "input level.",
            False,
        )
    # Speech was present and long enough; whisper just couldn't resolve words.
    return ("Couldn't make out any words. Try again, speaking a little more clearly.", False)


def _ptt_target_sid(state) -> Optional[int]:
    """Where a push-to-talk hold goes — decided at press time:

    1. a chain is armed or holding → the chain (the Superintendent inbox's
       draft, or with Superintendent off the new-session chain → None);
    2. a session is selected → that session, whichever window is in front
       (the inbox selected → an evidence dump);
    3. nothing selected → the Superintendent inbox, i.e. a NEW worker
       session; Superintendent off → None, a new plain session."""
    inbox = state.get("superintendent_inbox_sid")
    inbox = inbox if isinstance(inbox, int) else None
    try:
        if inbox is not None:
            from holler.superintendent import drafts as _drafts
            if state.get("superintendent_chain_armed") or _drafts.draft_count(
                    state.get("superintendent_draft")) > 0:
                return inbox
        else:
            from holler import session_chain as _sc
            if _sc.is_armed((state.get("session_chains") or {}).get(_sc.chain_key(None))):
                return None
    except Exception:
        logging.exception("chain check for push-to-talk target failed")
    active = state.get("active_session_id")
    if isinstance(active, int):
        return active
    return inbox


def _routing_target_sid(state) -> Optional[int]:
    """The session a talk-press/typed text currently routes to — i.e. the
    operator's explicit selection. Superintendent's inbox intercept keys off
    this to decide whether an input is an evidence dump vs a normal turn."""
    active = state.get("active_session_id")
    return active if isinstance(active, int) else None


def _try_capture_superintendent_evidence(
    *, target_sid, evidence_shot, transcript, state, persistent_store, notice,
) -> bool:
    """Draft-aware evidence capture for the Superintendent inbox.

    Shared by both the PTT path (`_dispatch_job`) and the typed/pasted-text path
    (`_dispatch_text_job`). When ``target_sid`` is the pinned inbox session, the
    input is an evidence dump — NOT a Claude turn — so persist it to the ledger
    (respecting the voice-chain draft state) and return True so the caller
    returns early. Returns False when the target isn't the inbox, letting the
    caller dispatch a normal turn.
    """
    inbox_sid = state.get("superintendent_inbox_sid")
    if not (
        persistent_store is not None
        and isinstance(inbox_sid, int)
        and target_sid == inbox_sid
    ):
        return False

    from holler.superintendent import drafts as _drafts

    action, cleaned = _drafts.classify_inbox_utterance(transcript)
    lock = state.get("superintendent_draft_lock") or threading.Lock()
    try:
        with lock:
            draft = state.get("superintendent_draft")
            armed = bool(state.get("superintendent_chain_armed"))
            mode = state.get("superintendent_mode") or "deploy"

            # Discard: drop the open draft, no evidence written.
            if action == "discard":
                state["superintendent_draft"] = None
                state["superintendent_chain_armed"] = False
                notice("Draft discarded" if draft else "Nothing to discard")
                _push_sup_chain_status(state)
                return True

            # Every dump records a visible (closed) turn on the inbox so the
            # operator watches the chain build in real time.
            turn_id = persistent_store.add_turn(
                session_id=inbox_sid,
                transcript=cleaned or transcript,
                screenshot_path=evidence_shot,
                job_id=None,
            )

            # Fast path: no cue, no open draft, chain mode not armed ->
            # fire immediately.
            if action == "none" and draft is None and not armed:
                eid = persistent_store.add_evidence_multi(
                    transcript=cleaned or transcript,
                    screenshot_paths=[evidence_shot] if evidence_shot else [],
                    inbox_session_id=inbox_sid,
                    inbox_turn_ids=[turn_id],
                    mode=mode,
                )
                lane = "investigation" if mode == "investigation" else "deployment"
                persistent_store.update_turn(
                    turn_id, ended_at=time.time(),
                    final_text=f"Evidence #{eid} saved → queued for {lane}.",
                )
                notice(f"Evidence #{eid} queued")
                return True

            # Otherwise accumulate into a draft (open one if needed).
            if draft is None:
                draft = _drafts.new_draft(mode)
                state["superintendent_draft"] = draft
            n = _drafts.append_to_draft(
                draft, shot=evidence_shot, transcript=cleaned, turn_id=turn_id,
            )

            if action == "commit":
                turn_ids = list(draft.get("turn_ids", []))
                eid = _drafts.flush_draft(persistent_store, inbox_sid, draft)
                state["superintendent_draft"] = None
                state["superintendent_chain_armed"] = False
                for tid in turn_ids:
                    try:
                        persistent_store.update_turn(
                            tid, ended_at=time.time(),
                            final_text=f"Evidence #{eid} saved ({_n_shots(n)}) → queued.",
                        )
                    except Exception:
                        pass
                notice(f"Evidence #{eid} queued ({_n_shots(n)})")
                _push_sup_chain_status(state)
                return True

            # hold, or accumulating while chained/armed: keep chaining.
            hint = "click Start session or say “file it”" if armed else "say “file it” to start the session"
            persistent_store.update_turn(
                turn_id, ended_at=time.time(),
                final_text=f"Draft: screenshot {n} held — {hint}.",
            )
            notice(f"Chain: {_n_shots(n)} held — {hint}")
            _push_sup_chain_status(state)
    except Exception:
        logging.exception("failed to capture superintendent evidence")
        notice("Couldn't save the evidence. Try again.")
    return True


def _chain_capture_active(state) -> bool:
    """True when an evidence/interactive chain is armed or already holding captures.

    While a chain is building, the operator is accumulating captures of an
    EXTERNAL window (Canvas, a browser, ...). Between holds they routinely click
    into holler's inbox to watch the chain grow, which makes holler frontmost.
    That transient self-focus must NOT redirect the next PTT capture to holler's
    own UI — the operator wants every capture in the chain to target the same
    real window. So when a chain is active, callers skip the self-capture branch
    and always target the last real window via the ForegroundTracker.

    Best-effort, lock-free reads on the hot PTT path: a torn read at worst
    mis-classifies one capture, which is the same rare edge the tracker already
    tolerates — never corruption.
    """
    try:
        if state.get("superintendent_chain_armed"):
            return True
        from holler.superintendent import drafts as _drafts
        if _drafts.draft_count(state.get("superintendent_draft")) > 0:
            return True
        from holler import session_chain as _sc
        for chain in (state.get("session_chains") or {}).values():
            if _sc.is_armed(chain) or _sc.count(chain) > 0:
                return True
    except Exception:
        logging.exception("chain-active check failed")
    return False


def _push_sup_chain_status(state) -> None:
    """Mirror the armed flag + held-item count into the SessionsWindow chain
    bar. Safe from any thread — marshals via a queued invoke."""
    win = state.get("sessions_window")
    if win is None:
        return
    from holler.superintendent import drafts as _drafts

    armed = bool(state.get("superintendent_chain_armed"))
    count = _drafts.draft_count(state.get("superintendent_draft"))
    try:
        QtCore.QMetaObject.invokeMethod(
            win, "set_superintendent_chain_status", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(bool, armed), QtCore.Q_ARG(int, count),
        )
    except Exception:
        pass


def _superintendent_chain_control(action: str, state, persistent_store, panel) -> None:
    """Deterministic chain control from the inbox Chain bar buttons.

    'arm'     -> every subsequent inbox dump is held into one draft
    'file'    -> flush the held draft as ONE evidence row, disarm
    'discard' -> drop the draft, disarm
    """
    from holler.superintendent import drafts as _drafts

    def _notice(text: str) -> None:
        try:
            QtCore.QMetaObject.invokeMethod(
                panel, "show_inline_notice", QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, text),
            )
        except Exception:
            pass

    inbox_sid = state.get("superintendent_inbox_sid")
    lock = state.get("superintendent_draft_lock") or threading.Lock()
    try:
        with lock:
            draft = state.get("superintendent_draft")
            if action == "arm":
                state["superintendent_chain_armed"] = True
                _notice("Chain started — every hold is added until you click Start session")
            elif action == "file":
                state["superintendent_chain_armed"] = False
                if draft is None:
                    _notice("Nothing to file yet")
                else:
                    n = _drafts.draft_count(draft)
                    turn_ids = list(draft.get("turn_ids", []))
                    eid = _drafts.flush_draft(
                        persistent_store,
                        inbox_sid if isinstance(inbox_sid, int) else None,
                        draft,
                    )
                    state["superintendent_draft"] = None
                    for tid in turn_ids:
                        try:
                            persistent_store.update_turn(
                                tid, ended_at=time.time(),
                                final_text=f"Evidence #{eid} saved ({_n_shots(n)}) → queued.",
                            )
                        except Exception:
                            pass
                    _notice(f"Evidence #{eid} queued ({_n_shots(n)})")
            elif action == "discard":
                state["superintendent_chain_armed"] = False
                state["superintendent_draft"] = None
                _notice("Draft discarded" if draft else "Nothing to discard")
    except Exception:
        logging.exception("superintendent chain control failed (%s)", action)
        _notice("That didn't work. Try again.")
    _push_sup_chain_status(state)


def _push_session_chain_status(state) -> None:
    """Mirror the routing target's interactive-chain state (armed + held count)
    into the SessionsWindow Chain bar. Safe from any thread.

    With nothing selected the target is the not-yet-created session, pushed as
    sid 0 — the window maps that back to "no selection"."""
    win = state.get("sessions_window")
    if win is None:
        return
    from holler import session_chain as _sc

    inbox = state.get("superintendent_inbox_sid")
    active = state.get("active_session_id")
    sid = active if (isinstance(active, int) and active != inbox) else None
    chain = (state.get("session_chains") or {}).get(_sc.chain_key(sid))
    try:
        QtCore.QMetaObject.invokeMethod(
            win, "set_session_chain_status", QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(int, sid if isinstance(sid, int) else 0),
            QtCore.Q_ARG(bool, _sc.is_armed(chain)),
            QtCore.Q_ARG(int, _sc.count(chain)),
        )
    except Exception:
        pass


def _submit_session_chain_turn(
    session: Session, state, persistent_store, sid: Optional[int],
    joined: str, primary_b64, extra_b64, primary_shot,
) -> None:
    """Submit a filed chain as ONE turn — the primary screenshot as the turn
    image, the rest as extra image blocks, the held voice/text joined into the
    message.

    ``sid`` None means the chain was armed with nothing selected: the session is
    created here, from the bundle, exactly as an unchained PTT would have."""
    turn_text = joined or ""
    cfg = state.get("cfg")

    if sid is None:
        pending_profile = state.get("pending_profile")
        state["pending_profile"] = None
        profile = pending_profile if isinstance(pending_profile, str) else None
        sid = persistent_store.create_session(
            model=cfg.claude_model,
            transcript=turn_text,
            screenshot_path=primary_shot,
            profile=profile,
            repo_cwd=_profile_repo(cfg, profile),
        )
        resume_sid = None
    else:
        sess = persistent_store.get_session(sid) if persistent_store is not None else None
        resume_sid = getattr(sess, "sdk_session_id", None) if sess else None
        profile = getattr(sess, "profile", None) if sess else None

    append_override = build_append(profile=profile) + authority_override_for_profile(profile)

    local_turn_id = None
    if persistent_store is not None:
        local_turn_id = persistent_store.add_turn(
            session_id=sid,
            transcript=turn_text,
            screenshot_path=primary_shot,
            job_id=None,
        )
    session.submit(
        primary_b64, turn_text,
        resume_session_id=resume_sid,
        local_session_id=sid,
        local_turn_id=local_turn_id,
        system_prompt_append_override=append_override,
        extra_images=list(extra_b64 or []),
    )


def _session_chain_control(action: str, session: Session, state, persistent_store, panel) -> None:
    """Chain-bar control for a normal (non-inbox) routing target.

    'arm'     -> hold every subsequent capture into this target's chain
    'file'    -> submit the held captures as ONE multi-image turn, disarm
    'discard' -> drop the held captures, disarm

    With no session selected the chain belongs to the session that filing will
    create — chaining is how the operator groups several captures into one new
    session instead of one per hold.
    """
    from holler import session_chain as _sc

    def _notice(text: str) -> None:
        target = state.get("sessions_window") or panel
        try:
            QtCore.QMetaObject.invokeMethod(
                target, "show_inline_notice", QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, text),
            )
        except Exception:
            pass

    # Use the active session as the chain target when one is on screen.
    # Never chain into the superintendent inbox itself (that path has its own
    # evidence-draft route). With nothing selected, sid=None → filing creates
    # a new session as before.
    inbox = state.get("superintendent_inbox_sid")
    active = state.get("active_session_id")
    sid = active if (isinstance(active, int) and active != inbox) else None
    key = _sc.chain_key(sid)
    dest = "" if sid is not None else " to a new session"
    lock = state.get("session_chain_lock") or threading.Lock()
    try:
        with lock:
            chains = state.setdefault("session_chains", {})
            chain = chains.get(key)
            if action == "arm":
                if chain is None:
                    chains[key] = _sc.new_chain()
                else:
                    chain["armed"] = True
                _notice(
                    f"Chain started — screenshots are held{dest} "
                    f"until you click Start session"
                )
            elif action == "discard":
                chains.pop(key, None)
                _notice("Chain discarded" if chain else "Nothing to discard")
            elif action == "file":
                if not chain or _sc.count(chain) == 0:
                    chains.pop(key, None)
                    _notice("Nothing to file yet")
                else:
                    n = _sc.count(chain)
                    joined, primary, extras, primary_shot = _sc.build_turn(chain)
                    chains.pop(key, None)
                    _submit_session_chain_turn(
                        session, state, persistent_store, sid,
                        joined, primary, extras, primary_shot,
                    )
                    _notice(f"Sent {_n_shots(n)} as one message{dest}")
    except Exception:
        logging.exception("session chain control failed (%s)", action)
        _notice("That didn't work. Try again.")
    _push_session_chain_status(state)


def _try_hold_session_chain(
    *, target_sid, composed, cfg, transcript, state, notice,
) -> bool:
    """When the routing target has an armed chain, hold this capture (screenshot
    + voice/text) instead of dispatching a turn. Returns True when held (caller
    returns early), False to fall through to normal dispatch.

    ``target_sid`` None is the no-selection target — its chain files into a
    session created at that point. ``composed`` is the composited PIL image, or
    None for a text-only capture."""
    inbox = state.get("superintendent_inbox_sid")
    if isinstance(inbox, int) and target_sid == inbox:
        return False  # the inbox has its own evidence-draft path
    from holler import session_chain as _sc

    key = _sc.chain_key(target_sid)
    lock = state.get("session_chain_lock") or threading.Lock()
    with lock:
        chain = (state.get("session_chains") or {}).get(key)
        if not _sc.is_armed(chain):
            return False
        shot_path = None
        image_b64 = None
        if composed is not None:
            try:
                p = cfg.captures_dir / f"chain-{key}-{int(time.time() * 1000)}.png"
                composed.save(p)
                shot_path = str(p)
            except Exception:
                logging.exception("failed to save chained screenshot")
            try:
                image_b64 = to_png_b64(composed)
            except Exception:
                logging.exception("failed to encode chained screenshot")
        n = _sc.append(
            chain, shot=shot_path, image_b64=image_b64, transcript=transcript,
        )
    notice(f"Chain: {_n_shots(n)} held — click Start session when you're done")
    _push_session_chain_status(state)
    return True


def _dispatch_job(snap, strokes, audio, transcriber: Transcriber, session: Session, cfg, state, panel,
                  thumb_target=None, signal_stats=None, open_error=None, device_name=None,
                  ptt_target: Optional[int] = None) -> None:
    """`ptt_target` is the press-time routing target (`_ptt_target_sid`):
    the inbox (evidence → new worker session / chain), a session to
    continue, or None for a new session."""
    # Always read the *current* cfg from state — hot settings changes land here.
    cfg = state.get("cfg") or cfg
    persistent_store = state.get("persistent_store")
    model_error = None
    try:
        heard = transcriber.transcribe_detailed(audio)
    except WhisperUnavailable as e:
        # Not silence: the model never loaded. Say so instead of "no speech".
        logging.error("transcribe skipped — speech model unavailable: %s", e.reason)
        model_error = e.reason
        heard = Transcription("", float(getattr(audio, "size", 0)) / 16_000, 0.0)
    except Exception:
        logging.exception("transcribe failed")
        heard = Transcription("", float(getattr(audio, "size", 0)) / 16_000, 0.0)
    transcript = heard.text

    # Screen Recording missing (macOS): the snapshot is wallpaper. Send the
    # turn voice-only instead of a misleading image (the operator was told at
    # press time), and strokes drawn on wallpaper don't count as content.
    limited = bool(getattr(snap, "permission_limited", False))
    if limited:
        composed = None
        strokes = []
    else:
        composed = compose(
            snap.image, strokes, cfg.stroke_color, cfg.stroke_width,
            logical_size=getattr(snap, "logical_size", None),
            max_long_edge=cfg.max_screenshot_long_edge,
        )
        composed = downscale(composed, cfg.max_screenshot_long_edge)
    out_path = None
    if composed is not None and cfg.debug_save_captures and thumb_target is not None:
        try:
            composed.save(thumb_target)
            out_path = thumb_target
        except Exception:
            logging.exception("capture save failed")

    # A fabricated transcript counts as no transcript: whisper invents words over
    # near-silence, and submitting them spends a real Claude turn on something
    # nobody said. Strokes still carry the capture, exactly as for silence.
    if (not transcript.strip() or heard.looks_fabricated) and not strokes:
        # On a failed open, device_name is None — fall back to the configured
        # mic name so the message can say which pinned device is missing.
        label = device_name or (getattr(cfg, "mic_device", "") or None)
        msg, is_permission = _audio_failure_message(
            open_error, signal_stats, label, heard, model_error=model_error,
        )
        logging.info(
            "no usable transcript and no strokes — skipping submit (open_error=%r, quality=%s, "
            "captured=%.2fs, speech=%.2fs, fabricated=%s)",
            open_error, getattr(signal_stats, "quality", None),
            heard.audio_seconds, heard.speech_seconds, heard.looks_fabricated,
        )
        target = state.get("sessions_window") or panel
        try:
            QtCore.QMetaObject.invokeMethod(
                target, "show_inline_notice", QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, msg),
            )
        except Exception:
            pass
        # A denied permission is the one fault the user can't fix from the toast
        # alone — open the right Settings pane for them, once per process.
        if is_permission and not state.get("_mic_settings_opened"):
            state["_mic_settings_opened"] = True
            try:
                from holler import mac_permissions as macperm
                macperm.open_mic_privacy_settings()
            except Exception:
                logging.exception("could not open mic privacy settings")
        return

    # ----- Superintendent inbox intercept -----
    # When the routing target is the Superintendent evidence inbox, this PTT is
    # NOT a Claude turn — it's an evidence dump. Persist the screenshot durably
    # and hand off to the shared draft-aware ledger writer (which also serves
    # the typed/pasted-text path).
    def _notice(msg: str) -> None:
        target = state.get("sessions_window") or panel
        try:
            QtCore.QMetaObject.invokeMethod(
                target, "show_inline_notice", QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, msg),
            )
        except Exception:
            pass

    evidence_shot: Optional[str] = str(out_path) if out_path else None
    # Persist a screenshot only when this capture is an evidence dump for the
    # Superintendent inbox (the ledger references the file). Saving it for
    # every PTT left an untracked PNG per capture with debug_save_captures
    # off, growing the captures dir forever.
    inbox_sid = state.get("superintendent_inbox_sid")
    is_inbox_dump = isinstance(inbox_sid, int) and ptt_target == inbox_sid
    if evidence_shot is None and composed is not None and thumb_target is not None and is_inbox_dump:
        try:
            composed.save(thumb_target)
            evidence_shot = str(thumb_target)
        except Exception:
            logging.exception("failed to save evidence screenshot")

    if _try_capture_superintendent_evidence(
        target_sid=ptt_target,
        evidence_shot=evidence_shot,
        transcript=transcript,
        state=state,
        persistent_store=persistent_store,
        notice=_notice,
    ):
        return

    # ----- Interactive session-chain hold -----
    # If the target session has an armed chain, hold this capture (screenshot +
    # voice) instead of dispatching a turn; it's sent later, bundled, on File it.
    if _try_hold_session_chain(
        target_sid=ptt_target,
        composed=composed,
        cfg=cfg,
        transcript=transcript,
        state=state,
        notice=_notice,
    ):
        return

    # Voice prefix: "with sonnet,..." → strip + tag the job's model_override.
    # Resolve against the same pool every picker uses, so an id the operator
    # added to cfg.claude_models by hand is reachable by voice too; without the
    # Config, extract() only sees discovery ∪ KNOWN_MODELS.
    transcript, model_override = extract_model_prefix(transcript, models=effective_models(cfg))
    effective_model = model_override or cfg.claude_model

    # Fingerprint the composed pixel buffer now (cheap) so we can dedup the
    # PNG-encode + base64 + send if this PTT produced the exact same image as
    # the previous PTT for the same session. The encode happens lazily below
    # after the routing decision tells us which session this belongs to.
    fingerprint = image_fingerprint(composed) if composed is not None else None
    image_b64: Optional[str] = None
    screen_unchanged = False

    # ----- Phase 2: routing decision (new session vs resume) -----
    local_session_id = None
    local_turn_id = None
    resume_session_id = None
    if persistent_store is not None:
        def _route_notice(msg: str) -> None:
            _notice(msg)

        thumb_path_str: Optional[str] = str(out_path) if out_path else None
        local_session_id, local_turn_id, resume_session_id, transcript = _resolve_routing(
            persistent_store, state, transcript,
            screenshot_path=thumb_path_str,
            effective_model=effective_model,
            notice=_route_notice,
            force_new=not isinstance(ptt_target, int),
            selected_sid=ptt_target,
        )

    # Compute the per-job system_prompt_append. If the persisted session has
    # a profile attached (set at creation time), layer it into the append.
    # Re-computed every turn so the profile body is always fresh on disk —
    # editing a profile MD takes effect on the next turn without restart
    # (build_append mtime-caches so this is near-free on repeat).
    profile_for_session: Optional[str] = None
    if persistent_store is not None and local_session_id is not None:
        try:
            sess_row = persistent_store.get_session(local_session_id)
            profile_for_session = sess_row.profile if sess_row else None
        except Exception:
            logging.exception("failed to read profile for session %s", local_session_id)
    append_override = build_append(profile=profile_for_session) + authority_override_for_profile(profile_for_session)

    # Dedup: if the composed image is identical to the prior turn for this
    # session, skip the PNG-encode + base64 (~200-400ms saved) and let the
    # SDK turn fire text-only. Append a one-line marker so Claude understands
    # nothing visually changed since last turn.
    #
    # Only safe when this turn will run on the SAME live pooled client that
    # already received that screenshot: a fresh subprocess (pool reaped,
    # model/profile change, previous turn failed, no sdk id yet) rebuilds
    # context from a text-only replay and would never see the image.
    if composed is None:
        # Voice-only turn: the client won't have seen a screenshot, so the
        # next capture must not be deduped against an older one.
        if local_session_id is not None:
            state.setdefault("screenshot_fp_by_session", {}).pop(local_session_id, None)
        screen_unchanged = False
    else:
        screen_unchanged = _screen_unchanged_for_turn(
            state, session, local_session_id, fingerprint,
            resume_session_id=resume_session_id,
            model_override=model_override,
            append_override=append_override,
        )
    if composed is None:
        pass  # voice-only turn (no screenshot to send or to call unchanged)
    elif not screen_unchanged:
        image_b64 = to_png_b64(composed)
    else:
        suffix = SCREEN_UNCHANGED_SUFFIX
        if suffix not in (transcript or ""):
            transcript = (transcript or "") + suffix

    session.submit(
        image_b64, transcript,
        model_override=model_override,
        resume_session_id=resume_session_id,
        local_session_id=local_session_id,
        local_turn_id=local_turn_id,
        system_prompt_append_override=append_override,
    )


def _dispatch_text_job(text: str, session: Session, cfg, state, panel) -> None:
    """Run a text-only turn through the same routing as PTT (no screenshot).

    Mirrors `_dispatch_job` minus the audio/transcribe/composite steps so we
    preserve smart-batch merging, profile layering, and the routing decision.
    """
    cfg = state.get("cfg") or cfg
    persistent_store = state.get("persistent_store")

    # ----- Superintendent inbox intercept (typed/pasted text) -----
    # Typing or pasting into the inbox composer is an evidence dump, not a
    # Claude turn. Mirror the PTT path via the shared helper BEFORE any routing
    # or model-prefix handling so raw text lands in the ledger verbatim.
    if persistent_store is not None:
        def _text_notice(msg: str) -> None:
            try:
                QtCore.QMetaObject.invokeMethod(
                    panel, "show_inline_notice", QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, msg),
                )
            except Exception:
                pass

        if _try_capture_superintendent_evidence(
            target_sid=_routing_target_sid(state),
            evidence_shot=None,
            transcript=text,
            state=state,
            persistent_store=persistent_store,
            notice=_text_notice,
        ):
            return

        # Interactive session-chain hold (typed/pasted text, no screenshot).
        if _try_hold_session_chain(
            target_sid=_routing_target_sid(state),
            composed=None,
            cfg=cfg,
            transcript=text,
            state=state,
            notice=_text_notice,
        ):
            return

    # Strip voice-prefix model override even from typed text — operator may
    # have learned the muscle memory and we want consistent behavior. Same
    # Config-aware pool as the voice path.
    text, model_override = extract_model_prefix(text, models=effective_models(cfg))
    effective_model = model_override or cfg.claude_model

    local_session_id = None
    local_turn_id = None
    resume_session_id = None

    if persistent_store is not None:
        def _route_notice(msg: str) -> None:
            try:
                QtCore.QMetaObject.invokeMethod(
                    panel, "show_inline_notice", QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, msg),
                )
            except Exception:
                pass

        local_session_id, local_turn_id, resume_session_id, text = _resolve_routing(
            persistent_store, state, text,
            screenshot_path=None,
            effective_model=effective_model,
            notice=_route_notice,
        )

    profile_for_session: Optional[str] = None
    if persistent_store is not None and local_session_id is not None:
        try:
            sess_row = persistent_store.get_session(local_session_id)
            profile_for_session = sess_row.profile if sess_row else None
        except Exception:
            logging.exception("failed to read profile for session %s", local_session_id)
    append_override = build_append(profile=profile_for_session) + authority_override_for_profile(profile_for_session)

    session.submit(
        None, text,  # image_b64=None — text-only turn
        model_override=model_override,
        resume_session_id=resume_session_id,
        local_session_id=local_session_id,
        local_turn_id=local_turn_id,
        system_prompt_append_override=append_override,
    )


class _InvokeOnUiThread(QtCore.QObject):
    requested_press = QtCore.Signal()
    requested_release = QtCore.Signal()
    # Esc / ⌘Z during a hold (HotkeyListener on_cancel / on_undo).
    requested_cancel = QtCore.Signal()
    requested_undo = QtCore.Signal()


class _CaptureUiSignals(QtCore.QObject):
    """Worker → GUI: a dispatch (transcribe → submit) finished; carries the
    token from _TranscribingStatus.launch."""
    transcribe_finished = QtCore.Signal(int)


def _signal_when_done(target, signal, token):
    """Run `target(...)`, then emit `signal(token)` however it ended."""
    def _run(*args, **kwargs):
        try:
            return target(*args, **kwargs)
        finally:
            try:
                signal.emit(token)
            except Exception:
                pass
    return _run


class _WarmthSignal(QtCore.QObject):
    warmth = QtCore.Signal()


class _SessionsInvoker(QtCore.QObject):
    """Marshals the Ctrl+Shift+H global hotkey from `keyboard` thread into Qt."""
    requested = QtCore.Signal()


class _MicLevelSignal(QtCore.QObject):
    """Pumps mic-level (0..1) from the audio callback thread to the overlay."""
    level = QtCore.Signal(float)


if __name__ == "__main__":
    main()
