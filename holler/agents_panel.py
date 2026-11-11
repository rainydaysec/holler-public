"""Agents pane: the subagents Claude launched in the selected session.

One card per agent (Claude's Agent/Task tool), newest first: a status dot,
the agent's description, a meta line (type · elapsed · tokens · tool uses),
what it is doing right now, and — once it finishes — its result. Cards
expand to show the agent's own tool trail (which the transcript never shows:
subagent traffic is kept out of the parent conversation) and its prompt.
Running agents carry a Stop button.

Data comes from ``JobStore.agents_for_session``; the pane re-renders on
``JobStore.agents_changed`` and ticks elapsed times once a second while any
agent runs.
"""

from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from holler import agents as _agents
from holler import iconset, theme
from holler.agents import AgentRun
from holler.uifont import ui_font

_STATUS_COLOR = {
    _agents.RUNNING: theme.ACCENT,
    _agents.COMPLETED: theme.SUCCESS,
    _agents.FAILED: theme.DANGER,
    _agents.STOPPED: theme.FG_FAINT,
}
_STATUS_WORD = {
    _agents.RUNNING: "Running",
    _agents.COMPLETED: "Done",
    _agents.FAILED: "Failed",
    _agents.STOPPED: "Stopped",
}
_TRAIL_SHOWN = 30
_SUMMARY_COLLAPSED_CHARS = 220

PANE_MIN_W = 260
PANE_DEFAULT_W = 340


class _Dot(QtWidgets.QWidget):
    """Status dot; breathes while the agent runs."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(theme.ROW_DOT + 6, theme.ROW_DOT + 6)
        self._color = QtGui.QColor(theme.ACCENT)
        self._phase = 0
        self._running = False

    def set_state(self, color: str, running: bool, phase: int) -> None:
        self._color = QtGui.QColor(color)
        self._running = running
        self._phase = phase
        self.update()

    def paintEvent(self, _e: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(QtCore.Qt.NoPen)
        c = QtCore.QPointF(self.width() / 2, self.height() / 2)
        if self._running:
            halo = QtGui.QColor(self._color)
            halo.setAlpha(theme.PULSE_ALPHAS[self._phase % len(theme.PULSE_ALPHAS)] * 2)
            p.setBrush(halo)
            p.drawEllipse(c, self.width() / 2, self.height() / 2)
        p.setBrush(self._color)
        r = theme.ROW_DOT / 2
        p.drawEllipse(c, r, r)
        p.end()


class AgentCard(QtWidgets.QFrame):
    stop_requested = QtCore.Signal(str)        # tool_use_id
    expand_toggled = QtCore.Signal(str, bool)  # tool_use_id, expanded

    def __init__(self, run: AgentRun, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.tool_use_id = run.tool_use_id
        self._expanded = False
        self.setObjectName("agentCard")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM)
        outer.setSpacing(theme.SPACE_2XS)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(theme.SPACE_SM)
        self._dot = _Dot()
        head.addWidget(self._dot, 0, QtCore.Qt.AlignTop)
        self._title = QtWidgets.QLabel()
        self._title.setWordWrap(True)
        tf = ui_font(theme.FONT_SM)
        tf.setWeight(QtGui.QFont.DemiBold)
        self._title.setFont(tf)
        self._title.setStyleSheet(f"color: {theme.FG}; background: transparent;")
        self._title.setCursor(QtCore.Qt.PointingHandCursor)
        self._title.mousePressEvent = lambda _e: self._toggle()  # type: ignore[assignment]
        head.addWidget(self._title, 1)
        self._stop_btn = QtWidgets.QPushButton()
        self._stop_btn.setIcon(iconset.icon("stop", theme.DANGER, theme.ROW_ICON))
        self._stop_btn.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        self._stop_btn.setFixedSize(theme.ICON_BUTTON, theme.ICON_BUTTON)
        self._stop_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._stop_btn.setToolTip("Stop this agent")
        self._stop_btn.setAccessibleName("Stop agent")
        self._stop_btn.setStyleSheet(theme.button_qss("neutral", pad_v=0, pad_h=0))
        self._stop_btn.clicked.connect(lambda: self.stop_requested.emit(self.tool_use_id))
        head.addWidget(self._stop_btn, 0, QtCore.Qt.AlignTop)
        self._chevron = QtWidgets.QToolButton()
        self._chevron.setAutoRaise(True)
        self._chevron.setCursor(QtCore.Qt.PointingHandCursor)
        self._chevron.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        self._chevron.setAccessibleName("Show agent details")
        self._chevron.setStyleSheet("QToolButton { border: none; background: transparent; }")
        self._chevron.clicked.connect(self._toggle)
        head.addWidget(self._chevron, 0, QtCore.Qt.AlignTop)
        outer.addLayout(head)

        self._meta = QtWidgets.QLabel()
        self._meta.setWordWrap(True)
        self._meta.setFont(ui_font(theme.FONT_XS))
        self._meta.setStyleSheet(f"color: {theme.FG_MUTED}; background: transparent;")
        outer.addWidget(self._meta)

        self._doing = QtWidgets.QLabel()
        self._doing.setWordWrap(True)
        self._doing.setFont(ui_font(theme.FONT_XS))
        self._doing.setStyleSheet(f"color: {theme.FG_MUTED}; background: transparent;")
        outer.addWidget(self._doing)

        self._summary = QtWidgets.QLabel()
        self._summary.setWordWrap(True)
        self._summary.setTextFormat(QtCore.Qt.PlainText)
        self._summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._summary.setFont(ui_font(theme.FONT_SM))
        self._summary.setStyleSheet(f"color: {theme.FG}; background: transparent;")
        outer.addWidget(self._summary)

        self._details = QtWidgets.QLabel()
        self._details.setWordWrap(True)
        self._details.setTextFormat(QtCore.Qt.RichText)
        self._details.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._details.setFont(ui_font(theme.FONT_XS))
        self._details.setStyleSheet(f"color: {theme.FG_MUTED}; background: transparent;")
        outer.addWidget(self._details)

        # Wrapped labels must not report their unwrapped width as a minimum,
        # or a long summary widens the card past the pane.
        for lbl in (self._title, self._meta, self._doing, self._summary, self._details):
            lbl.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            lbl.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Maximum)
        self.update_from(run, phase=0)

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, on: bool) -> None:
        self._expanded = bool(on)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.expand_toggled.emit(self.tool_use_id, self._expanded)

    def update_from(self, run: AgentRun, *, phase: int, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        color = _STATUS_COLOR.get(run.status, theme.FG_MUTED)
        self._dot.set_state(color, run.running, phase)
        self._title.setText(run.title())
        self._title.setToolTip(run.prompt[:1200] if run.prompt else "")

        bits = [_STATUS_WORD.get(run.status, run.status.title())]
        if run.stop_requested and run.running:
            bits[0] = "Stopping…"
        if run.subagent_type and run.subagent_type != "general-purpose":
            bits.append(run.subagent_type)
        bits.append(_agents.format_elapsed(run.elapsed_s(now)))
        if run.total_tokens:
            bits.append(f"{_agents.format_tokens(run.total_tokens)} tok")
        if run.tool_uses:
            bits.append(f"{run.tool_uses} tool{'s' if run.tool_uses != 1 else ''}")
        if run.background:
            bits.append("background")
        self._meta.setText(" · ".join(bits))

        doing = run.doing if run.running else ""
        self._doing.setText(f"⎿  {doing}" if doing else "")
        self._doing.setVisible(bool(doing))

        summary = run.summary.strip() if not run.running else ""
        if summary and not self._expanded and len(summary) > _SUMMARY_COLLAPSED_CHARS:
            summary = summary[:_SUMMARY_COLLAPSED_CHARS].rstrip() + "…"
        self._summary.setText(summary)
        self._summary.setVisible(bool(summary))

        self._stop_btn.setVisible(run.running and not run.stop_requested)
        self._chevron.setIcon(iconset.icon(
            "chevron-down" if self._expanded else "chevron-right", theme.FG_MUTED, theme.ROW_ICON))
        self._chevron.setToolTip("Hide details" if self._expanded else "Show activity and prompt")

        if self._expanded:
            self._details.setText(_details_html(run))
            self._details.setVisible(True)
        else:
            self._details.setVisible(False)

        border = theme.ACCENT_PILL_BORDER if run.running else theme.BORDER_SUBTLE
        self.setStyleSheet(
            f"QFrame#agentCard {{ background: {theme.SURFACE}; border: 1px solid {border}; "
            f"border-radius: {theme.RADIUS_MD}px; }}"
        )


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _details_html(run: AgentRun) -> str:
    parts: list[str] = []
    trail = run.activity[-_TRAIL_SHOWN:]
    if trail:
        parts.append(f"<p style='margin:0 0 {theme.SPACE_2XS}px 0; color:{theme.FG};'>Activity</p>")
        rows = []
        for a in trail:
            color = theme.DANGER if a.kind == "error" else (theme.FG if a.kind == "text" else theme.FG_MUTED)
            mark = "●" if a.kind in ("tool", "task") else ("✗" if a.kind == "error" else "·")
            stamp = time.strftime("%H:%M:%S", time.localtime(a.ts))
            rows.append(
                f"<span style='color:{theme.FG_FAINT};'>{stamp}</span> "
                f"<span style='color:{color};'>{mark} {_esc(a.text)}</span>"
            )
        hidden = len(run.activity) - len(trail)
        if hidden > 0:
            rows.insert(0, f"<span style='color:{theme.FG_FAINT};'>… {hidden} earlier steps</span>")
        parts.append("<br>".join(rows))
    elif run.running:
        parts.append(f"<span style='color:{theme.FG_FAINT};'>No steps yet.</span>")
    if run.prompt:
        parts.append(
            f"<p style='margin:{theme.SPACE_SM}px 0 {theme.SPACE_2XS}px 0; color:{theme.FG};'>Prompt</p>"
            + _esc(run.prompt[:3000]).replace("\n", "<br>")
        )
    if run.output_file and not run.running:
        parts.append(
            f"<p style='margin:{theme.SPACE_SM}px 0 0 0; color:{theme.FG_FAINT};'>"
            f"Output: {_esc(run.output_file)}</p>"
        )
    return "".join(parts)


class AgentsPanel(QtWidgets.QFrame):
    """Right-hand pane listing one session's subagents."""

    stop_agent_requested = QtCore.Signal(int, str)  # local_session_id, tool_use_id
    close_requested = QtCore.Signal()

    def __init__(self, job_store, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._job_store = job_store
        self._session_id: int | None = None
        self._cards: dict[str, AgentCard] = {}
        self._expanded: set[str] = set()
        self._phase = 0
        self.setObjectName("agentsPanel")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setMinimumWidth(PANE_MIN_W)
        self.setStyleSheet(
            f"QFrame#agentsPanel {{ background: {theme.SUNKEN}; border-left: 1px solid {theme.BORDER_SUBTLE}; }}"
        )
        self.setAccessibleName("Agents")

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QtWidgets.QFrame()
        header.setObjectName("agentsHeader")
        header.setStyleSheet(
            f"QFrame#agentsHeader {{ background: transparent; border-bottom: 1px solid {theme.BORDER_SUBTLE}; }}"
        )
        h = QtWidgets.QHBoxLayout(header)
        h.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM)
        h.setSpacing(theme.SPACE_SM)
        icon = QtWidgets.QLabel()
        icon.setPixmap(iconset.icon("agents", theme.FG_MUTED, theme.ROW_ICON).pixmap(theme.ROW_ICON, theme.ROW_ICON))
        icon.setStyleSheet("background: transparent;")
        h.addWidget(icon, 0)
        self._heading = QtWidgets.QLabel("Agents")
        hf = ui_font(theme.FONT_SM)
        hf.setWeight(QtGui.QFont.DemiBold)
        self._heading.setFont(hf)
        self._heading.setStyleSheet(f"color: {theme.FG}; background: transparent;")
        h.addWidget(self._heading, 1)
        close = QtWidgets.QToolButton()
        close.setAutoRaise(True)
        close.setIcon(iconset.icon("close", theme.FG_MUTED, theme.ROW_ICON))
        close.setIconSize(QtCore.QSize(theme.ROW_ICON, theme.ROW_ICON))
        close.setCursor(QtCore.Qt.PointingHandCursor)
        close.setToolTip("Hide the Agents pane")
        close.setAccessibleName("Hide agents")
        close.setStyleSheet("QToolButton { border: none; background: transparent; }")
        close.clicked.connect(self.close_requested.emit)
        h.addWidget(close, 0)
        v.addWidget(header)

        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }" + theme.scrollbar_qss())
        body = QtWidgets.QWidget()
        body.setStyleSheet("background: transparent;")
        self._list = QtWidgets.QVBoxLayout(body)
        self._list.setContentsMargins(theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM)
        self._list.setSpacing(theme.SPACE_SM)
        self._empty = QtWidgets.QLabel(
            "No agents in this session yet.\n\nAsk Claude to research or build several "
            "things in parallel and each agent shows up here, live."
        )
        self._empty.setWordWrap(True)
        self._empty.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self._empty.setFont(ui_font(theme.FONT_SM))
        self._empty.setStyleSheet(f"color: {theme.FG_MUTED}; background: transparent; padding: {theme.SPACE_SM}px;")
        self._list.addWidget(self._empty)
        self._list.addStretch(1)
        self._scroll.setWidget(body)
        v.addWidget(self._scroll, 1)

        self._tick = QtCore.QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)

        job_store.agents_changed.connect(self._on_agents_changed)

    # ----- public -----

    def session_id(self) -> int | None:
        return self._session_id

    def set_session(self, local_session_id: int | None) -> None:
        if local_session_id != self._session_id:
            self._session_id = local_session_id
            for card in self._cards.values():
                card.deleteLater()
            self._cards.clear()
        self.refresh()

    def card_for(self, tool_use_id: str) -> AgentCard | None:
        return self._cards.get(tool_use_id)

    def refresh(self) -> None:
        runs = list(reversed(self._job_store.agents_for_session(self._session_id))) \
            if self._session_id is not None else []
        # Running first, then newest first (stable sort keeps launch order).
        runs.sort(key=lambda r: not r.running)
        live = {r.tool_use_id for r in runs}
        for tuid in [t for t in self._cards if t not in live]:
            self._cards.pop(tuid).deleteLater()
        now = time.time()
        for idx, run in enumerate(runs):
            card = self._cards.get(run.tool_use_id)
            if card is None:
                card = AgentCard(run)
                card.set_expanded(run.tool_use_id in self._expanded)
                card.stop_requested.connect(self._on_stop)
                card.expand_toggled.connect(self._on_expand)
                self._cards[run.tool_use_id] = card
            # Keep newest-first order (insert before the stretch).
            if self._list.indexOf(card) != idx + 1:
                self._list.insertWidget(idx + 1, card)
            card.update_from(run, phase=self._phase, now=now)
        self._empty.setVisible(not runs)
        running = sum(1 for r in runs if r.running)
        if runs:
            self._heading.setText(
                f"Agents · {running} running" if running else f"Agents · {len(runs)} finished"
            )
        else:
            self._heading.setText("Agents")
        if running and not self._tick.isActive():
            self._tick.start()
        elif not running and self._tick.isActive():
            self._tick.stop()

    # ----- slots -----

    @QtCore.Slot(int)
    def _on_agents_changed(self, local_session_id: int) -> None:
        if local_session_id == self._session_id or self._session_id is None:
            self.refresh()

    def _on_tick(self) -> None:
        self._phase = (self._phase + 1) % len(theme.PULSE_ALPHAS)
        self.refresh()

    def _on_stop(self, tool_use_id: str) -> None:
        if self._session_id is None:
            return
        self._job_store.mark_agent_stop_requested(tool_use_id)
        self.stop_agent_requested.emit(self._session_id, tool_use_id)
        self.refresh()

    def _on_expand(self, tool_use_id: str, on: bool) -> None:
        if on:
            self._expanded.add(tool_use_id)
        else:
            self._expanded.discard(tool_use_id)
        self.refresh()
