"""Always-on-top job panel.

A frameless top-right (configurable) widget that lists jobs as rows. Each row
shows: thumbnail | transcript snippet + Claude reply preview + status badge.
Click a row to expand the full reply.

Toasts: when a job completes, if the panel isn't focused, a small bottom-right
toast displays the first line of the reply for ~6 sec.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

# On Windows we hand the titlebar drag to the OS via QWindow.startSystemMove()
# so that Aero Snap, cross-monitor handoff, and DPI-boundary transitions all
# work natively. On other platforms (or if startSystemMove returns False) we
# fall back to manual delta-based dragging.
_USE_NATIVE_MOVE = sys.platform == "win32"

from holler import theme
from holler.jobs import Job, JobStore
from holler.tool_format import summarize as summarize_tool


# --- styling ---

_BG = theme.CANVAS
_BG_ROW = theme.SURFACE
_BG_ROW_HOVER = theme.SURFACE_HOVER
_FG = theme.FG
_FG_DIM = theme.FG_MUTED
_ACCENT = theme.ACCENT
_RED = theme.DANGER
_GREEN = theme.SUCCESS
_YELLOW = theme.WARNING

_PANEL_QSS = f"""
QWidget#panelRoot {{ background: {_BG}; }}
QFrame#dragHeader {{ background: {_BG}; border-bottom: 1px solid {theme.BORDER_SUBTLE}; }}
QLabel#panelHeader {{ color: {_FG}; font-size: {theme.FONT_LG}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; padding: {theme.SPACE_SM}px {theme.SPACE_MD}px; }}
QLabel#statusDot {{ font-size: {theme.FONT_SM}pt; }}
QFrame#row {{ background: {_BG_ROW}; border-radius: {theme.RADIUS_MD}px; }}
QFrame#row[expanded="true"] {{ background: {_BG_ROW_HOVER}; }}
QLabel.transcript {{ color: {_FG}; font-size: {theme.FONT_SM}pt; }}
QLabel.preview {{ color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; }}
QLabel.meta {{ color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; }}
QTextBrowser.full {{ background: {_BG_ROW_HOVER}; color: {_FG}; border: none; font-size: {theme.FONT_XS}pt; padding: {theme.SPACE_SM}px; }}
QLabel#emptyHint {{ color: {_FG_DIM}; font-size: {theme.FONT_XS}pt; padding: {theme.SPACE_LG}px {theme.SPACE_MD}px; }}
QPushButton#warmBadge {{ color: {_FG_DIM}; background: transparent; border: none; font-size: {theme.FONT_XS}pt; }}
QPushButton#modelBadge {{ color: {_FG_DIM}; background: transparent; border: none; font-size: {theme.FONT_XS}pt; padding: 0 {theme.SPACE_SM}px; }}
QPushButton#modelBadge:hover {{ color: {_FG}; }}
QSizeGrip {{ background: transparent; }}
"""


def _icon_button(icon_name: str, name: str, tooltip: str, hover: str = _FG) -> QtWidgets.QPushButton:
    """Icon-only flat button with a tooltip and an accessible name."""
    from holler import iconset

    b = QtWidgets.QPushButton()
    b.setIcon(iconset.icon(icon_name, _FG_DIM, theme.FONT_LG + theme.SPACE_XS))
    b.setIconSize(QtCore.QSize(theme.FONT_LG + theme.SPACE_XS, theme.FONT_LG + theme.SPACE_XS))
    b.setToolTip(tooltip)
    b.setAccessibleName(name)
    b.setCursor(QtCore.Qt.PointingHandCursor)
    b.setStyleSheet(
        f"QPushButton {{ background: transparent; border: none; border-radius: {theme.RADIUS_SM}px; "
        f"padding: {theme.SPACE_2XS}px {theme.SPACE_XS}px; }}"
        f"QPushButton:hover {{ background: {theme.ACCENT_HOVER_WASH}; }}"
        f"QPushButton:pressed {{ background: {theme.SURFACE_HOVER}; }}"
    )
    return b


class _DragHeader(QtWidgets.QFrame):
    """Header bar that drags its parent window when clicked + dragged.

    Child widgets (buttons) consume their own clicks via Qt event propagation,
    so dragging only fires on the bare header area.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dragHeader")
        self.setCursor(QtCore.Qt.OpenHandCursor)
        self._press_global: QtCore.QPoint | None = None
        self._window_origin: QtCore.QPoint | None = None

    def mousePressEvent(self, ev: QtGui.QMouseEvent) -> None:
        if ev.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        ev.accept()
        # Prefer the OS-native move: Windows handles cross-monitor DPI
        # boundaries and Aero Snap correctly; manual move() drift jitter at
        # the seam between displays with different scale factors.
        if _USE_NATIVE_MOVE:
            wh = self.window().windowHandle()
            if wh is not None:
                try:
                    if wh.startSystemMove():
                        return
                except Exception:
                    pass
        # Fallback: manual delta-based drag (non-Windows, or startSystemMove
        # refused). This is what holler shipped before native move was wired.
        self._press_global = ev.globalPosition().toPoint()
        self._window_origin = self.window().pos()
        self.setCursor(QtCore.Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev: QtGui.QMouseEvent) -> None:
        if self._press_global is None or self._window_origin is None:
            return
        delta = ev.globalPosition().toPoint() - self._press_global
        self.window().move(self._window_origin + delta)
        ev.accept()

    def mouseReleaseEvent(self, ev: QtGui.QMouseEvent) -> None:
        self._press_global = None
        self._window_origin = None
        self.setCursor(QtCore.Qt.OpenHandCursor)
        ev.accept()


class JobRow(QtWidgets.QFrame):
    """A single row showing the user's transcript and Claude's full reply."""

    clicked = QtCore.Signal(int)  # job_id — emitted on a non-drag click
    cancel_requested = QtCore.Signal(int)  # job_id — × button clicked

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.setObjectName("row")
        self._job_id = job.id
        self._press_pos: QtCore.QPoint | None = None

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM)
        outer.setSpacing(theme.SPACE_XS)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(theme.SPACE_SM)
        self._dot = QtWidgets.QLabel("●")
        self._dot.setObjectName("statusDot")
        # Larger than the row text for at-a-glance scanning.
        self._dot.setStyleSheet(f"color: {_YELLOW}; font-size: {theme.FONT_LG}pt;")
        self._transcript = QtWidgets.QLabel(job.transcript or "(no speech recognized)")
        self._transcript.setTextFormat(QtCore.Qt.PlainText)
        self._transcript.setProperty("class", "transcript")
        self._transcript.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        self._transcript.setWordWrap(True)
        self._transcript.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        # Cancel button (× — only shown for running/queued jobs).
        self._cancel_btn = _icon_button("close", "Stop turn", "Stop this turn")
        self._cancel_btn.setFixedWidth(28)
        self._cancel_btn.clicked.connect(lambda: self.cancel_requested.emit(self._job_id))
        head.addWidget(self._dot, 0, QtCore.Qt.AlignTop)
        head.addWidget(self._transcript, 1)
        head.addWidget(self._cancel_btn, 0, QtCore.Qt.AlignTop)
        outer.addLayout(head)

        self._reply = QtWidgets.QLabel("Queued…")

        self._reply.setTextFormat(QtCore.Qt.PlainText)
        self._reply.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_SM}pt;")
        self._reply.setWordWrap(True)
        self._reply.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        outer.addWidget(self._reply)

        # Live activity ticker — shows current tool while running.
        self._ticker = QtWidgets.QLabel("")
        self._ticker.setTextFormat(QtCore.Qt.PlainText)
        self._ticker.setStyleSheet(
            f"color: {_ACCENT}; font-size: {theme.FONT_XS}pt; font-style: italic;"
        )
        self._ticker.setWordWrap(True)
        self._ticker.hide()
        outer.addWidget(self._ticker)

        self._meta = QtWidgets.QLabel("")

        self._meta.setTextFormat(QtCore.Qt.PlainText)
        self._meta.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt;")
        # Wraps: a long error in the meta line otherwise set the row's minimum
        # width past the panel's, clipping every label in the row.
        self._meta.setWordWrap(True)
        outer.addWidget(self._meta)

        self.refresh(job)

    def job_id(self) -> int:
        return self._job_id

    def mousePressEvent(self, ev: QtGui.QMouseEvent) -> None:
        if ev.button() == QtCore.Qt.LeftButton:
            self._press_pos = ev.position().toPoint()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev: QtGui.QMouseEvent) -> None:
        if (
            ev.button() == QtCore.Qt.LeftButton
            and self._press_pos is not None
            and (ev.position().toPoint() - self._press_pos).manhattanLength() < 6
        ):
            self.clicked.emit(self._job_id)
        self._press_pos = None
        super().mouseReleaseEvent(ev)

    def refresh(self, job: Job, queued_behind: Optional[int] = None) -> None:
        color = {
            "queued": _YELLOW,
            "running": _ACCENT,
            "done": _GREEN,
            "error": _RED,
        }.get(job.status, _FG_DIM)
        self._dot.setStyleSheet(f"color: {color}; font-size: {theme.FONT_LG}pt;")
        # Show × only while the job is still cancellable.
        self._cancel_btn.setVisible(job.status in ("queued", "running"))

        text = (job.streaming_text or job.final_text or "").strip()
        if not text:
            if job.status == "queued" and queued_behind is not None:
                text = "Waiting for the previous turn…"
            elif job.status == "queued":
                text = "Queued…"
            elif job.status == "error" and job.error == "cancelled":
                # A user Stop/Esc is reported as error "cancelled" — not a
                # failure (matches the transcript's neutral "Stopped").
                text = "Stopped"
            elif job.status == "error":
                # An errored job used to fall through to "running…".
                text = f"Failed: {job.error}" if job.error else "Failed"
            elif job.status == "done":
                text = "No reply"
            else:
                text = "Working…"
        self._reply.setText(text)

        # Live ticker
        if job.status == "running" and job.current_tool is not None:
            ct = job.current_tool
            self._ticker.setText("⟳ " + summarize_tool(ct.name, ct.input, max_len=72))
            self._ticker.show()
        else:
            self._ticker.hide()

        # State in words (the dot's colour alone doesn't say it); no internal
        # job id, no raw status string.
        bits = [{"queued": "Queued", "running": "Working", "done": "Done",
                 "error": "Stopped" if job.error == "cancelled" else "Failed"}.get(job.status, job.status)]
        if job.status in ("done", "error"):
            bits.append(f"{job.duration_ms / 1000:.1f}s")
        elif job.status == "running" and job.started_at:
            bits.append(f"{time.time() - job.started_at:.0f}s")
        if job.tool_log:
            bits.append(f"{len(job.tool_log)} tool" + ("s" if len(job.tool_log) != 1 else ""))
        if job.resume_session_id:
            bits.append("follow-up")
        if job.model:
            from holler.model_prefix import format_model_label
            bits.append(format_model_label(job.model))
        self._meta.setText(" · ".join(bits))


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


_ERROR_HINTS = ("couldn't", "can't", "failed", "blocking", "error", "no sound",
                "no audio", "didn't", "isn't connected", "not granted")


# A quoted span (“…”) — a session title, a file name: its words say nothing
# about the notice's kind ("Archived “Fix failed login flow”" is no error).
_QUOTED_RE = re.compile(r"“[^”]*”?")


def toast_kind(text: str) -> str:
    """'error' | 'success' | 'info' for a notice that didn't say. Errors stay
    up longer (theme.TOAST_MS_ERROR) and are marked with an icon + red rim.
    Text inside “…” quotes is ignored."""
    t = (text or "").strip()
    if t.startswith("✗"):
        return "error"
    if t.startswith("✓"):
        return "success"
    low = _QUOTED_RE.sub(" ", t).lower()
    if any(h in low for h in _ERROR_HINTS):
        return "error"
    return "info"


def toast_duration_ms(kind: str, has_button: bool) -> int:
    if kind == "error":
        return theme.TOAST_MS_ERROR
    return theme.TOAST_MS_ACTION if has_button else theme.TOAST_MS_INFO


class _ToastStack:
    """Every visible Toast card, app-wide (the Sessions window and the panel
    each own a Toast; both post here). Cards on the same screen form ONE
    stack — anchored at the corner of the oldest card there, so a panel
    toast and a Sessions-window notice a few px apart don't overlap — and
    stack upward, newest at the bottom; at most theme.TOAST_MAX_VISIBLE are up at
    once and the rest wait in a queue — while anything waits, the oldest card
    is cut short so the queue drains instead of stalling behind 10 s errors.
    A plain notice (no Undo / action / click target) identical to one already
    showing just restarts that card's timer.
    """

    def __init__(self) -> None:
        self.visible: list["Toast"] = []
        self.queue: list[tuple] = []
        # While this returns True, toasts ignore clicks (body and button) —
        # e.g. during a push-to-talk capture, when a click meant for the
        # overlay underneath must not open a session. See set_toast_click_blocker.
        self.click_blocker: Optional[Callable[[], bool]] = None
        # Cards just dropped from `visible`, kept referenced until control is
        # back in the event loop. An owner Toast whose host window is gone is
        # referenced only by this stack, so dropping it from `visible` inside
        # its own hideEvent would delete the C++ widget while
        # QWidget::hide_helper is still running on it (a segfault).
        self._retired: list["Toast"] = []
        self._release_pending = False

    def clicks_blocked(self) -> bool:
        pred = self.click_blocker
        if pred is None:
            return False
        try:
            return bool(pred())
        except Exception:
            return False

    def _prune(self) -> None:
        # A card deleted (or hidden by its owner's teardown) while listed may
        # never get a Python hideEvent — drop it here.
        keep = [c for c in self.visible if _alive(c) and c.isVisible()]
        if len(keep) != len(self.visible):
            self._retire([c for c in self.visible if c not in keep])
        self.visible = keep

    def _retire(self, cards: list["Toast"]) -> None:
        self._retired.extend(cards)
        if not self._release_pending:
            self._release_pending = True
            QtCore.QTimer.singleShot(0, self._release_retired)

    def _release_retired(self) -> None:
        # Runs from the event loop, never inside a card's own hide — dropping
        # the last reference to an orphaned card here deletes it safely.
        self._release_pending = False
        self._retired = []

    def post(self, owner: "Toast", text: str, corner: tuple[int, int], opts: dict) -> Optional["Toast"]:
        self._prune()
        screen = _screen_key(tuple(corner))
        # Merge only plain informational notices: a card with Undo / an action
        # / a click target is bound to ITS callbacks (e.g. which sessions an
        # "Archived 2 sessions" Undo restores), so an identical-looking new
        # one must get its own card.
        plain = not any(opts.get(k) is not None for k in ("undo_callback", "action", "on_click"))
        for card in self.visible if plain else ():
            if (card._text == text and _screen_key(card._corner) == screen and card.isVisible()
                    and card._undo_callback is None and card._on_click is None):
                card._restart_timer()
                return card
        if len(self.visible) >= theme.TOAST_MAX_VISIBLE:
            if opts.get("sticky"):
                return None  # a live status is stale by the time a slot frees
            self.queue.append((owner, text, tuple(corner), opts))
            self._drain_oldest()
            return None
        card = owner if (not owner.isVisible() and owner not in self.visible) else owner._spare_card()
        card._fill(text, tuple(corner), **opts)
        self.visible.append(card)
        card.show()
        self.reflow()
        card._announce()
        return card

    def _drain_oldest(self) -> None:
        if not self.visible:
            return
        oldest = self.visible[0]
        t = oldest._timer
        if not t.isActive() or t.remainingTime() > theme.TOAST_MS_DRAIN:
            t.start(theme.TOAST_MS_DRAIN)

    def closed(self, card: "Toast") -> None:
        if card in self.visible:
            self.visible.remove(card)
            self._retire([card])
        self._prune()
        self.reflow()
        while self.queue and len(self.visible) < theme.TOAST_MAX_VISIBLE:
            owner, text, corner, opts = self.queue.pop(0)
            if not _alive(owner):
                continue  # owner widget already deleted
            self.post(owner, text, corner, opts)
        if self.queue:
            self._drain_oldest()

    def reflow(self) -> None:
        self._prune()
        by_screen: dict[object, list[Toast]] = {}
        for card in self.visible:
            by_screen.setdefault(_screen_key(card._corner), []).append(card)
        for cards in by_screen.values():
            right, bottom = cards[0]._corner  # the oldest card's anchor
            y = bottom
            for i, card in enumerate(reversed(cards)):  # newest at the bottom
                # Scale the offset by the toast's own DPR so the visual gap
                # matches across monitors with different scale factors.
                dpr = card.devicePixelRatioF() or 1.0
                margin = round(16 * dpr)
                gap = margin if i == 0 else theme.SPACE_SM
                y = y - gap - card.height()
                card.move(right - card.width() - margin, y)


def _screen_key(corner: tuple[int, int]) -> object:
    """Which screen a toast anchor (global right, bottom) belongs to — the
    stacking group. An anchor just outside every screen (a window corner
    past the edge) goes to the nearest one."""
    try:
        x, y = int(corner[0]), int(corner[1])
        screens = QtGui.QGuiApplication.screens()
        screen = QtGui.QGuiApplication.screenAt(QtCore.QPoint(x, y))
        if screen is None and screens:
            def dist(sc) -> int:
                g = sc.geometry()
                dx = max(g.left() - x, 0, x - g.right())
                dy = max(g.top() - y, 0, y - g.bottom())
                return dx * dx + dy * dy
            screen = min(screens, key=dist)
        if screen is not None:
            g = screen.geometry()
            return (screen.name(), g.x(), g.y())
    except Exception:
        pass
    return None


def _undo_key_hint() -> str:
    """⌘Z on macOS, Ctrl+Z elsewhere."""
    return QtGui.QKeySequence(QtGui.QKeySequence.Undo).toString(QtGui.QKeySequence.NativeText) \
        or ("⌘Z" if sys.platform == "darwin" else "Ctrl+Z")


def set_toast_click_blocker(predicate: Optional[Callable[[], bool]]) -> None:
    """While `predicate()` is true, every toast ignores clicks — its body
    (on_click) and its button (undo / action) do nothing and it stays up.
    None restores normal clicks."""
    _STACK.click_blocker = predicate


def _alive(widget) -> bool:
    try:
        from shiboken6 import isValid
        return bool(isValid(widget))
    except Exception:
        return True


_STACK = _ToastStack()


class Toast(QtWidgets.QWidget):
    def __init__(self, _owner: "Toast | None" = None) -> None:
        super().__init__(
            None,
            QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool,
        )
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        # The rounded accent card is an inner frame on a translucent window, so
        # its corners really are round (a top-level's own stylesheet background
        # isn't painted once it's translucent). Scoped by objectName: the old
        # unscoped rule cascaded the border/radius onto the child label (which
        # then drew the frame, unpadded) and the Undo button.
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._card = QtWidgets.QFrame(self)
        self._card.setObjectName("toastCard")
        outer.addWidget(self._card)
        layout = QtWidgets.QHBoxLayout(self._card)
        layout.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_MD, theme.SPACE_SM)
        layout.setSpacing(theme.SPACE_MD)
        # Kind icon (error / success) — state is never shown by colour alone.
        self._icon = QtWidgets.QLabel(self._card)
        self._icon.setFixedSize(theme.FONT_LG + theme.SPACE_XS, theme.FONT_LG + theme.SPACE_XS)
        self._icon.setVisible(False)
        layout.addWidget(self._icon, 0, QtCore.Qt.AlignVCenter)
        self._label = QtWidgets.QLabel("")
        self._label.setTextFormat(QtCore.Qt.PlainText)
        self._label.setStyleSheet(f"color: {_FG}; font-size: {theme.FONT_SM}pt;")
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(theme.TOAST_MAX_TEXT_WIDTH)
        layout.addWidget(self._label, 1)
        self._undo_btn = QtWidgets.QPushButton("Undo")
        self._undo_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._undo_btn.setStyleSheet(
            f"QPushButton {{ color: {_ACCENT}; background: transparent; "
            f"border: 1px solid {_ACCENT}; border-radius: {theme.RADIUS_MD}px; "
            f"padding: {theme.SPACE_XS}px {theme.SPACE_MD}px; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; }}"
            f"QPushButton:hover {{ background: {theme.ACCENT_HOVER_WASH}; }}"
        )
        self._undo_btn.setVisible(False)
        layout.addWidget(self._undo_btn)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self._undo_callback = None
        self._is_undo = False
        self._on_click = None
        self._text = ""
        self._kind = "info"
        self._sticky = False
        self._duration = theme.TOAST_MS_INFO
        self._corner: tuple[int, int] = (0, 0)
        self._owner = _owner
        self._spares: list[Toast] = []
        self._undo_btn.clicked.connect(self._on_undo_clicked)
        self._style_card("info")

    # ----- public API -----

    def show_message(self, text: str, screen_corner: tuple[int, int], *, undo_callback=None,
                     action=None, on_click=None, kind: Optional[str] = None,
                     sticky: bool = False) -> "Optional[Toast]":
        """Show `text` anchored above `screen_corner` (global right, bottom).

        * `undo_callback` — adds an "Undo" button.
        * `action` — (label, callback): a button with another label (e.g.
          "Open Settings"). Takes precedence over `undo_callback`.
        * `on_click` — clicking the toast body calls it (e.g. open the
          session); any click on the body dismisses the toast.
        * `kind` — 'error' | 'success' | 'info'; inferred from the text when
          omitted. Errors stay 10 s, others 6 s (8 s with a button).
        * `sticky` — no auto-hide (callers dismiss() it, e.g. "Transcribing…").

        Toasts stack (up to theme.TOAST_MAX_VISIBLE, the rest queue). Returns
        the card showing the message, or None when it was queued.
        """
        opts = dict(undo_callback=undo_callback, action=action, on_click=on_click,
                    kind=kind, sticky=sticky)
        return _STACK.post(self, text, screen_corner, opts)

    def dismiss(self) -> None:
        self.hide()

    def _expire(self) -> None:
        if _alive(self):
            self.hide()

    def trigger_undo(self) -> bool:
        """Keyboard Undo for this toast family: run the newest visible card's
        Undo (this toast or one of its spare cards). False — nothing done —
        when no card offers Undo, or toast clicks are blocked (a capture is in
        progress: ⌘Z then belongs to the drawing)."""
        if _STACK.clicks_blocked():
            return False
        _STACK._prune()
        for card in reversed(_STACK.visible):
            if card is not self and card._owner is not self:
                continue
            if card.isVisible() and getattr(card, "_is_undo", False) and card._undo_callback is not None:
                card._on_undo_clicked()
                return True
        return False

    # ----- internals -----

    def _spare_card(self) -> "Toast":
        for spare in self._spares:
            if not spare.isVisible() and spare not in _STACK.visible:
                return spare
        spare = Toast(_owner=self)
        self._spares.append(spare)
        return spare

    def _style_card(self, kind: str) -> None:
        border = _RED if kind == "error" else _ACCENT
        self._card.setStyleSheet(
            f"QFrame#toastCard {{ background: {_BG_ROW}; "
            f"border: 1px solid {border}; border-radius: {theme.RADIUS_MD}px; }}"
        )

    def _fill(self, text: str, corner: tuple[int, int], *, undo_callback=None, action=None,
              on_click=None, kind: Optional[str] = None, sticky: bool = False) -> None:
        self._text = text
        self._corner = corner
        self._label.setText(text)
        self._kind = kind or toast_kind(text)
        self._style_card(self._kind)
        icon_name = {"error": "alert-triangle", "success": "check-circle"}.get(self._kind)
        if icon_name:
            from holler import iconset

            color = _RED if self._kind == "error" else _GREEN
            size = self._icon.width()
            self._icon.setPixmap(iconset.pixmap(icon_name, color, size, self.devicePixelRatioF() or 1.0))
        self._icon.setVisible(bool(icon_name))
        if action is not None:
            label, cb = action
            self._undo_btn.setText(label)
            self._undo_callback = cb
            self._undo_btn.setToolTip("")
        else:
            self._undo_btn.setText("Undo")
            self._undo_callback = undo_callback
            # The window's ⌘Z / Ctrl+Z reaches this button too (see
            # trigger_undo) — the toast never takes keyboard focus itself.
            self._undo_btn.setToolTip(f"Undo ({_undo_key_hint()})")
        # Only a real Undo answers the keyboard undo; an action button
        # ("Open Settings") never runs from ⌘Z.
        self._is_undo = action is None and undo_callback is not None
        has_button = self._undo_callback is not None
        self._undo_btn.setVisible(has_button)
        self._undo_btn.setAccessibleName(self._undo_btn.text())
        self._on_click = on_click
        self.setCursor(QtCore.Qt.PointingHandCursor if on_click is not None else QtCore.Qt.ArrowCursor)
        self.setAccessibleName(text)
        self._sticky = bool(sticky)
        self._duration = toast_duration_ms(self._kind, has_button)
        self.adjustSize()
        self._restart_timer()

    def _restart_timer(self) -> None:
        # Sticky toasts still expire eventually so a lost dismiss() can't pin
        # one on screen forever.
        self._timer.start(60_000 if self._sticky else self._duration)

    def _announce(self) -> None:
        try:
            ev = QtGui.QAccessibleEvent(self, QtGui.QAccessible.Alert)
            QtGui.QAccessible.updateAccessibility(ev)
        except Exception:
            pass

    def _on_undo_clicked(self) -> None:
        if _STACK.clicks_blocked():
            return
        cb = self._undo_callback
        self._undo_callback = None
        self.hide()
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def mousePressEvent(self, ev: QtGui.QMouseEvent) -> None:  # type: ignore[override]
        if ev.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        if _STACK.clicks_blocked():
            ev.accept()
            return
        cb = self._on_click
        self._on_click = None
        self.hide()
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def hideEvent(self, ev: QtGui.QHideEvent) -> None:  # type: ignore[override]
        self._timer.stop()
        super().hideEvent(ev)
        _STACK.closed(self)


class Panel(QtWidgets.QWidget):
    # Emitted when user clicks the model badge to cycle models. The runner
    # connects this and updates cfg + persists.
    model_cycle_requested = QtCore.Signal()
    # Emitted when user picks "Settings…" from the menu.
    settings_requested = QtCore.Signal()
    # Emitted when user picks "Open Sessions window" from the menu (Phase 2).
    sessions_window_requested = QtCore.Signal()

    detail_open_requested = QtCore.Signal(int)  # job_id; runner builds DetailWindow
    # Bound on finished (done/error) rows kept in the panel; older ones are
    # dropped so the widget tree doesn't grow without limit over a long run.
    MAX_FINISHED_ROWS = 50
    cancel_requested = QtCore.Signal(int)  # job_id — × button on a row
    routing_cleared = QtCore.Signal()  # user clicked [×] on the routing pill
    # A completion toast was clicked: open that job's session (runner selects
    # it in the Sessions window).
    toast_open_requested = QtCore.Signal(int)  # job_id

    def __init__(
        self,
        store: JobStore,
        corner: str = "top-right",
        width: int = 380,
        model_label: str = "",
        saved_geometry: tuple[int, int, int, int] | None = None,
        sessions_hotkey: str = "ctrl+shift+h",
    ) -> None:
        super().__init__(
            None,
            QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool,
        )
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setObjectName("panelRoot")
        self.setStyleSheet(_PANEL_QSS)
        self.setMinimumSize(280, 240)

        self._store = store
        self._corner = corner
        self._width = width
        # Global Sessions-window hotkey (cfg.sessions_window_hotkey) — shown
        # as the menu item's shortcut hint.
        self._sessions_hotkey = sessions_hotkey
        self._rows: dict[int, JobRow] = {}
        # Latest geometry sampled in moveEvent/resizeEvent. Saving on quit
        # reads this to avoid touching widget state during teardown.
        self._latest_geom: tuple[int, int, int, int] = (
            saved_geometry if saved_geometry else (0, 0, width, 600)
        )

        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # Header — wrapped in _DragHeader so the bar drags the window.
        self._header = _DragHeader(self)
        header_layout = QtWidgets.QHBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)
        title = QtWidgets.QLabel("Holler", self._header)
        title.setObjectName("panelHeader")
        self._warm = QtWidgets.QPushButton("Starting…", self._header)
        self._warm.setObjectName("warmBadge")
        self._warm.setEnabled(False)
        self._model_btn = QtWidgets.QPushButton(model_label or "", self._header)
        self._model_btn.setObjectName("modelBadge")
        self._model_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._model_btn.clicked.connect(self.model_cycle_requested.emit)
        self._menu_btn = _icon_button("more-horizontal", "Menu", "Menu")
        self._menu_btn.setParent(self._header)
        self._menu_btn.setFlat(True)
        self._menu_btn.clicked.connect(self._show_menu)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self._warm)
        header_layout.addWidget(self._model_btn)
        header_layout.addWidget(self._menu_btn)
        self._layout.addWidget(self._header)

        # ---- Routing pill: shows next-PTT mode ("→ new session" or "→ #N: title [×]")
        self._routing_pill = QtWidgets.QFrame(self)
        # Scoped by objectName — a bare `QFrame` rule also matched the child
        # QLabel (QLabel subclasses QFrame), giving it a stray bottom border.
        self._routing_pill.setObjectName("routingPill")
        self._routing_pill.setStyleSheet(
            f"QFrame#routingPill {{ background: {theme.SUNKEN}; border-bottom: 1px solid {theme.BORDER_SUBTLE}; }}"
        )
        rp_layout = QtWidgets.QHBoxLayout(self._routing_pill)
        rp_layout.setContentsMargins(theme.SPACE_MD, theme.SPACE_XS, theme.SPACE_SM, theme.SPACE_XS)
        rp_layout.setSpacing(theme.SPACE_SM)
        self._routing_label = QtWidgets.QLabel("→ New session")
        self._routing_label.setTextFormat(QtCore.Qt.PlainText)
        self._routing_label.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt;")
        self._routing_clear_btn = _icon_button(
            "close", "Start a new session instead", "Clear: next turn starts a new session",
        )
        self._routing_clear_btn.clicked.connect(self.routing_cleared.emit)
        self._routing_clear_btn.hide()
        rp_layout.addWidget(self._routing_label, 1)
        rp_layout.addWidget(self._routing_clear_btn)
        self._layout.addWidget(self._routing_pill)

        # Scroll area for rows
        body_wrapper = QtWidgets.QWidget(self)
        body_layout = QtWidgets.QVBoxLayout(body_wrapper)
        body_layout.setContentsMargins(theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM, 0)
        body_layout.setSpacing(theme.SPACE_SM)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setStyleSheet(f"background: {_BG}; border: none;")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._rows_host = QtWidgets.QWidget()
        self._rows_layout = QtWidgets.QVBoxLayout(self._rows_host)
        self._rows_layout.setSpacing(theme.SPACE_SM)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._rows_host)
        body_layout.addWidget(self._scroll, 1)
        self._layout.addWidget(body_wrapper, 1)

        self._empty = QtWidgets.QLabel(
            "Hold the push-to-talk key to talk. Replies appear here."
        )
        self._empty.setObjectName("emptyHint")
        self._empty.setWordWrap(True)
        self._rows_layout.insertWidget(0, self._empty)

        # Footer — size grip on the right.
        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(0)
        footer.addStretch(1)
        self._size_grip = QtWidgets.QSizeGrip(self)
        footer.addWidget(self._size_grip, 0, QtCore.Qt.AlignBottom | QtCore.Qt.AlignRight)
        self._layout.addLayout(footer)

        self._toast = Toast()
        # Optional "the user is already looking at it" predicate for the
        # completion toast (set by the runner: Sessions window is active).
        self._completion_toast_suppressed: Optional[Callable[[], bool]] = None

        store.job_added.connect(self._on_job_added)
        store.job_updated.connect(self._on_job_updated)

        if saved_geometry:
            x, y, w, h = saved_geometry
            self.setGeometry(*_clamp_to_any_screen(x, y, w, h, fallback_w=width))
        else:
            self._position()

        # ----- monitor-change awareness -----
        # If a monitor is hot-plugged / unplugged, or the window is dragged to
        # another monitor (or scaling changes), re-validate geometry so the
        # frameless panel never strands itself off-screen. Mirrors the pattern
        # used in SessionsWindow.
        self._last_screen_name: Optional[str] = None
        # Drag-aware refit state — _on_screens_changed defers setGeometry until
        # the user releases the mouse button so cross-monitor drags don't break.
        self._refit_retry_count: int = 0
        _app = QtGui.QGuiApplication.instance()
        if _app is not None:
            _app.screenAdded.connect(self._on_screens_changed)
            _app.screenRemoved.connect(self._on_screens_changed)

    def showEvent(self, ev: QtGui.QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(ev)
        # windowHandle() is only valid after the window has been shown. We
        # connect once: subsequent shows are no-ops because _last_screen_name
        # has been set.
        if self._last_screen_name is None:
            wh = self.windowHandle()
            if wh is not None:
                try:
                    wh.screenChanged.connect(self._on_window_screen_changed)
                    cur = wh.screen()
                    self._last_screen_name = cur.name() if cur else ""
                except Exception:
                    pass

    def _on_screens_changed(self, _screen=None) -> None:
        """A monitor was added/removed or window crossed a monitor — re-validate.

        Defers setGeometry until the mouse button is released so a cross-monitor
        drag isn't aborted by Qt yanking the window mid-WM_MOVING.
        """
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            self._refit_retry_count += 1
            if self._refit_retry_count > 20:  # ~4s ceiling at 200ms ticks
                self._refit_retry_count = 0
                return
            QtCore.QTimer.singleShot(200, self, self._on_screens_changed)
            return
        self._refit_retry_count = 0
        try:
            x, y, w, h = self._latest_geom
            nx, ny, nw, nh = _clamp_to_any_screen(x, y, w, h, fallback_w=self._width)
            if (nx, ny, nw, nh) != (x, y, w, h):
                self.setGeometry(nx, ny, nw, nh)
        except Exception:
            pass

    def _on_window_screen_changed(self, screen) -> None:
        """Window dragged to a different monitor — re-clamp if needed."""
        try:
            name = screen.name() if screen is not None else ""
            if name == self._last_screen_name:
                return
            self._last_screen_name = name
            self._on_screens_changed()
        except Exception:
            pass

    def set_warmth(self, text: str) -> None:
        self._warm.setText(text)

    @QtCore.Slot(str)
    def set_model_label(self, text: str) -> None:
        self._model_btn.setText(text)

    @QtCore.Slot(int, str)
    def set_routing_target(self, local_session_id, title: str = "") -> None:
        """Update the routing pill. None = next PTT starts a new session."""
        if local_session_id is None:
            self._routing_label.setText("→ New session")
            self._routing_label.setStyleSheet(f"color: {_FG_DIM}; font-size: {theme.FONT_XS}pt;")
            self._routing_clear_btn.hide()
        else:
            short = (title or "Untitled session").strip()
            if len(short) > 60:
                short = short[:57] + "…"
            self._routing_label.setText(f"→ Continuing “{short}”")
            self._routing_label.setStyleSheet(f"color: {_ACCENT}; font-size: {theme.FONT_XS}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
            self._routing_clear_btn.show()

    def current_geometry(self) -> tuple[int, int, int, int]:
        return self._latest_geom

    def moveEvent(self, ev: QtGui.QMoveEvent) -> None:
        super().moveEvent(ev)
        self._latest_geom = (self.x(), self.y(), self.width(), self.height())

    def resizeEvent(self, ev: QtGui.QResizeEvent) -> None:
        super().resizeEvent(ev)
        self._latest_geom = (self.x(), self.y(), self.width(), self.height())

    def flash_ptt_pulse(self, label: str = "•") -> None:
        """Briefly flash the warmth badge to confirm a PTT cycle was registered.
        After the pulse, restore via the canonical refresher so we don't clobber
        any counter updates that arrived during the flash window."""
        self._warm.setText(label)
        QtCore.QTimer.singleShot(900, self, self._refresh_warmth_counter)

    @QtCore.Slot(str)
    def show_inline_notice(self, text: str, kind: Optional[str] = None) -> None:
        """Show a short, non-job notice as a toast (e.g. 'PTT was empty — no audio').
        ``kind`` ('error' | 'success' | 'info') when the caller knows it;
        otherwise inferred from the text (see toast_kind)."""
        self._toast.show_message(text, self.toast_corner(), kind=kind)

    def show_notice(self, text: str, *, anchor: Optional[tuple[int, int]] = None, action=None,
                    on_click=None, kind: Optional[str] = None, sticky: bool = False) -> Optional[Toast]:
        """show_inline_notice with Toast's extras (action button, click
        handler, kind, sticky). `anchor` = global (right, bottom) to stack
        above — e.g. the Sessions window's corner; default the panel's
        screen corner. Returns the card (dismiss() it) or None if queued."""
        return self._toast.show_message(
            text, anchor or self.toast_corner(), action=action, on_click=on_click,
            kind=kind, sticky=sticky,
        )

    def _show_menu(self) -> None:
        menu = QtWidgets.QMenu(self)
        # The global toggle as registered (cfg.sessions_window_hotkey): its
        # modifiers are literal — "ctrl" is ⌃ on a Mac, not ⌘.
        from holler.hotkey import global_hotkey_hint
        hint = global_hotkey_hint(self._sessions_hotkey)
        sessions_act = menu.addAction(f"Open Sessions window\t{hint}" if hint else "Open Sessions window")
        settings_act = menu.addAction("Settings…")
        menu.addSeparator()
        clear_act = menu.addAction("Clear finished")
        menu.addSeparator()
        quit_act = menu.addAction("Quit Holler")
        chosen = menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))
        if chosen is sessions_act:
            self.sessions_window_requested.emit()
        elif chosen is settings_act:
            self.settings_requested.emit()
        elif chosen is clear_act:
            self._clear_completed()
        elif chosen is quit_act:
            from holler.quitting import quit_app
            quit_app()

    def _remove_row(self, jid: int) -> None:
        row = self._rows.pop(jid, None)
        if row is None:
            return
        self._rows_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()

    def _clear_completed(self) -> None:
        for jid in list(self._rows):
            job = self._store.get(jid)
            if job is not None and job.status in ("done", "error"):
                self._remove_row(jid)
        if not self._rows:
            self._empty.show()

    def _prune_finished_rows(self) -> None:
        """Keep at most MAX_FINISHED_ROWS finished rows (newest by job id).

        Running / queued rows are never removed.
        """
        finished = []
        for jid in self._rows:
            job = self._store.get(jid)
            if job is None or job.status in ("done", "error"):
                finished.append(jid)
        if len(finished) <= self.MAX_FINISHED_ROWS:
            return
        finished.sort(reverse=True)
        for jid in finished[self.MAX_FINISHED_ROWS:]:
            self._remove_row(jid)
        if not self._rows:
            self._empty.show()

    def _position(self) -> None:
        screen = QtWidgets.QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1920, 1080)
        h = int(avail.height() * 0.7)
        w = self._width
        margin = 16
        if self._corner == "top-right":
            x = avail.right() - w - margin
            y = avail.top() + margin
        elif self._corner == "top-left":
            x = avail.left() + margin
            y = avail.top() + margin
        elif self._corner == "bottom-right":
            x = avail.right() - w - margin
            y = avail.bottom() - h - margin
        else:
            x = avail.left() + margin
            y = avail.bottom() - h - margin
        self.setGeometry(x, y, w, h)

    def toast_corner(self) -> tuple[int, int]:
        # Anchor to the screen the panel is currently on, not the primary
        # screen. On multi-monitor setups this kept toasts off-screen when the
        # panel was dragged to a secondary display.
        center = self.frameGeometry().center()
        screen = QtGui.QGuiApplication.screenAt(center)
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1920, 1080)
        return (avail.right(), avail.bottom())

    @QtCore.Slot(int)
    def _on_job_added(self, job_id: int) -> None:
        job = self._store.get(job_id)
        if job is None:
            return
        if self._empty.isVisible():
            self._empty.hide()
        row = JobRow(job)
        row.clicked.connect(self.detail_open_requested.emit)
        row.cancel_requested.connect(self.cancel_requested.emit)
        self._rows[job_id] = row
        self._rows_layout.insertWidget(0, row)
        queued_behind = self._find_queued_behind(job) if job.status == "queued" else None
        row.refresh(job, queued_behind=queued_behind)
        self._refresh_warmth_counter()
        self._prune_finished_rows()

    @QtCore.Slot(int)
    def _on_job_updated(self, job_id: int) -> None:
        job = self._store.get(job_id)
        row = self._rows.get(job_id)
        if job is None or row is None:
            return
        queued_behind = self._find_queued_behind(job) if job.status == "queued" else None
        row.refresh(job, queued_behind=queued_behind)
        self._refresh_warmth_counter()
        if job.status in ("done", "error"):
            self._prune_finished_rows()
        if (
            job.status in ("done", "error")
            and not self.isActiveWindow()
            and not self._suppress_completion_toast()
        ):
            text, kind = self._completion_toast_text(job)
            self._toast.show_message(
                text, self.toast_corner(), kind=kind,
                on_click=lambda jid=job_id: self.toast_open_requested.emit(jid),
            )

    def set_session_title_resolver(self, resolver: Optional[Callable[[int], str]]) -> None:
        """`resolver(local_session_id)` → the session's title, so completion
        toasts name the session (never an internal job id)."""
        self._session_title_resolver = resolver

    def _completion_toast_text(self, job: Job) -> tuple[str, str]:
        """(text, kind) for the "a turn finished while you were away" toast."""
        title = ""
        resolver = getattr(self, "_session_title_resolver", None)
        if resolver is not None and job.local_session_id is not None:
            try:
                title = (resolver(job.local_session_id) or "").strip()
            except Exception:
                title = ""
        if not title:
            title = (job.transcript or "").strip().split("\n", 1)[0]
        if len(title) > 40:
            title = title[:39].rstrip() + "…"
        name = f"“{title}”" if title else "Your turn"
        first_line = (job.final_text or job.streaming_text or "").strip().split("\n", 1)[0]
        if len(first_line) > 200:
            first_line = first_line[:200] + "…"
        if job.status == "done":
            return (f"✓ {name}: {first_line}" if first_line else f"✓ {name} finished"), "success"
        if job.error == "cancelled":
            return f"Stopped {name}", "info"
        reason = first_line or (job.error or "").strip().split("\n", 1)[0][:200]
        return (f"✗ {name} failed: {reason}" if reason else f"✗ {name} failed"), "error"

    def set_completion_toast_suppressor(self, predicate: Optional[Callable[[], bool]]) -> None:
        """While `predicate()` is true, finished jobs don't pop the corner
        "✓ job #N" toast — e.g. the Sessions window is active and already
        shows the result. None restores always-toast."""
        self._completion_toast_suppressed = predicate

    def _suppress_completion_toast(self) -> bool:
        pred = self._completion_toast_suppressed
        if pred is None:
            return False
        try:
            return bool(pred())
        except Exception:
            return False

    def _find_queued_behind(self, job: Job) -> Optional[int]:
        """Find the most recent earlier in-flight job for the same SDK session."""
        if not job.resume_session_id:
            return None
        for other in self._store.all():
            if other.id >= job.id:
                continue
            if other.resume_session_id == job.resume_session_id and other.status in ("queued", "running"):
                return other.id
        return None

    def _refresh_warmth_counter(self) -> None:
        """Update the warmth label with running/queued counts."""
        running = sum(1 for j in self._store.all() if j.status == "running")
        queued = sum(1 for j in self._store.all() if j.status == "queued")
        parts = ["Ready"]
        if running:
            parts.append(f"{running} running")
        if queued:
            parts.append(f"{queued} queued")
        self._warm.setText(" · ".join(parts))


def _clamp_to_any_screen(x: int, y: int, w: int, h: int, fallback_w: int = 380) -> tuple[int, int, int, int]:
    """Ensure the rect intersects at least one available screen; else fall back."""
    screens = QtWidgets.QApplication.screens() or []
    rect = QtCore.QRect(x, y, max(280, w), max(240, h))
    for screen in screens:
        if screen.availableGeometry().intersects(rect):
            return (rect.x(), rect.y(), rect.width(), rect.height())
    primary = QtWidgets.QApplication.primaryScreen()
    if primary:
        avail = primary.availableGeometry()
        new_w = max(280, fallback_w)
        new_h = max(240, int(avail.height() * 0.7))
        return (avail.right() - new_w - 16, avail.top() + 16, new_w, new_h)
    return (100, 100, fallback_w, 600)
