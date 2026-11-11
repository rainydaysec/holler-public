"""First-run setup wizard.

Shown when holler launches with no `config.toml` (or one pointing at a
non-existent project folder). A short stepped flow in one dialog:
1. project folder (cwd for Claude)
2. macOS only: Microphone / Accessibility / Screen Recording permissions —
   live ✓/✗ rows re-checked every second while the step is showing
3. push-to-talk key (record by pressing it, or type its name)
4. Claude model preference

Also pre-flights:
- whether `claude` CLI is installed
- whether the user has run `claude /login` (best-effort)
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from holler import iconset, theme
from holler.config import DEFAULT_MODEL, Config, effective_models, save as save_config


def needs_first_run(cfg: Config) -> bool:
    """True if we should pop the first-run dialog before launching.

    Two triggers:
      1. No `config.toml` next to the install (fresh clone, never configured)
      2. `repo_cwd` points to a path that doesn't exist on this machine
         (config copied/cloned from a different user's setup)
    """
    from holler.config import _config_path_default
    if not _config_path_default().exists():
        return True
    p = Path(str(cfg.repo_cwd))
    return not p.is_dir()


_QSS = f"""
QDialog {{ background: {theme.CANVAS}; color: {theme.FG}; }}
QLabel {{ color: {theme.FG}; font-size: {theme.FONT_SM}pt; }}
QLabel.hint {{ color: {theme.FG_MUTED}; font-size: {theme.FONT_XS}pt; }}
QLabel.error {{ color: {theme.DANGER}; font-size: {theme.FONT_SM}pt; }}
QLabel.bigtitle {{ color: {theme.FG}; font-size: {theme.FONT_XL}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; }}
QLabel.sub {{ color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt; }}
QLabel#stepLabel {{ color: {theme.FG_MUTED}; font-size: {theme.FONT_XS}pt; }}
QLineEdit, QComboBox {{ font-size: {theme.FONT_SM}pt; }}
QPushButton {{ padding: {theme.SPACE_XS}px {theme.SPACE_LG}px; font-size: {theme.FONT_SM}pt; }}
QLabel#authOk {{ color: {theme.SUCCESS}; font-size: {theme.FONT_SM}pt; }}
QLabel#authMiss {{ color: {theme.WARNING}; font-size: {theme.FONT_SM}pt; }}
QLabel#authChecking {{ color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt; font-style: italic; }}
QFrame#headerRule {{ background: {theme.BORDER_SUBTLE}; border: none; }}
QFrame#permRow {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER_SUBTLE}; border-radius: {theme.RADIUS_MD}px; }}
QLabel#permName {{ color: {theme.FG}; font-weight: {theme.WEIGHT_SEMIBOLD}; }}
QLabel#permWhy {{ color: {theme.FG_MUTED}; font-size: {theme.FONT_XS}pt; }}
"""

_HINT_STYLE = f"color: {theme.FG_MUTED}; font-size: {theme.FONT_XS}pt;"
_ERROR_STYLE = f"color: {theme.DANGER}; font-size: {theme.FONT_SM}pt;"

# Where npm / Homebrew / the native installer commonly put `claude`. A Finder-
# launched app gets a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so
# shutil.which alone misses these.
_CLAUDE_FALLBACK_DIRS = (
    "~/.npm-global/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.claude/local",
)

_AUTH_STYLES = {
    "authOk": f"color: {theme.SUCCESS}; font-size: {theme.FONT_SM}pt;",
    "authMiss": f"color: {theme.WARNING}; font-size: {theme.FONT_SM}pt;",
    "authChecking": f"color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt; font-style: italic;",
}


def find_claude_cli() -> Optional[str]:
    """Path to the `claude` CLI: PATH first, then common install folders."""
    found = shutil.which("claude")
    if found:
        return found
    dirs = [os.path.expanduser(d) for d in _CLAUDE_FALLBACK_DIRS]
    return shutil.which("claude", path=os.pathsep.join(dirs))


def probe_claude_cli(claude_path: str) -> tuple[str, str]:
    """Run `claude --version` (blocking, up to 5s) → (label text, style key).

    Call off the GUI thread."""
    # Best-effort: run `claude --version`. Anything more requires the CLI
    # to be authed.
    try:
        r = subprocess.run(
            [claude_path, "--version"], capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return (
                f"✓ Claude Code found ({(r.stdout or '').strip() or 'ready'}). "
                f"If you haven't signed in yet, run claude /login in Terminal.",
                "authOk",
            )
    except Exception:
        pass
    return (
        "⚠ Claude Code didn't respond. Run claude /login in Terminal, then reopen Holler.",
        "authMiss",
    )


class _AuthProbe(QtCore.QObject):
    """Carries a worker thread's probe result to the GUI thread.

    Owned by the QApplication (not the dialog), so the worker can still emit
    safely if the dialog is closed and destroyed first — Qt then simply has no
    receiver left. Deletes itself on the GUI thread once delivered."""

    done = QtCore.Signal(str, str)


class _StepDots(QtWidgets.QWidget):
    """Painted step indicator: one dot per step, the current one filled
    accent and wider, finished ones muted, upcoming ones outlined."""

    def __init__(self, count: int, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._count = count
        self._current = 0
        d = theme.STEP_DOT
        self.setFixedSize(count * d + (count - 1) * theme.SPACE_SM + d, d)

    def set_current(self, index: int) -> None:
        self._current = index
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802 (Qt override)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        d = theme.STEP_DOT
        x = 0.0
        for i in range(self._count):
            w = 2 * d if i == self._current else d
            rect = QtCore.QRectF(x + 0.5, 0.5, w - 1, d - 1)
            if i == self._current:
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(QtGui.QColor(theme.ACCENT))
            elif i < self._current:
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(QtGui.QColor(theme.FG_MUTED))
            else:
                p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER_STRONG), 1))
                p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(rect, (d - 1) / 2, (d - 1) / 2)
            x += w + theme.SPACE_SM
        p.end()


# Permission row states → (icon, colour token, status text). Never colour
# alone: every state has its own icon AND words.
_PERM_GRANTED = "granted"
_PERM_DENIED = "denied"
_PERM_NOT_ASKED = "notDetermined"
_PERM_UNKNOWN = "unknown"
_PERM_LOOK = {
    _PERM_GRANTED: ("check-circle", theme.SUCCESS, "Allowed"),
    _PERM_DENIED: ("alert-triangle", theme.WARNING, "Not allowed"),
    _PERM_NOT_ASKED: ("alert-triangle", theme.WARNING, "Not asked yet"),
    _PERM_UNKNOWN: ("clock", theme.FG_MUTED, "Can't check"),
}

# (key, name, why it's needed)
_PERMISSIONS = (
    ("mic", "Microphone", "To hear you while you hold the push-to-talk key."),
    ("accessibility", "Accessibility", "To notice the push-to-talk key in any app."),
    ("screen", "Screen Recording", "To include a screenshot of the window you're talking about."),
)


def _permission_states() -> dict[str, str]:
    """Live macOS permission states (no prompts). Cheap; GUI thread."""
    from holler import mac_permissions as macperm

    mic = macperm.mic_authorization_status()
    if mic is None:
        mic_state = _PERM_UNKNOWN
    elif mic == macperm.AUTHORIZED:
        mic_state = _PERM_GRANTED
    elif mic == macperm.NOT_DETERMINED:
        mic_state = _PERM_NOT_ASKED
    else:
        mic_state = _PERM_DENIED
    screen = macperm.screen_recording_status()
    return {
        "mic": mic_state,
        "accessibility": _PERM_GRANTED if macperm.accessibility_trusted() else _PERM_DENIED,
        "screen": {"granted": _PERM_GRANTED, "denied": _PERM_DENIED}.get(screen, _PERM_UNKNOWN),
    }


class _PermissionRow(QtWidgets.QFrame):
    """✓/✗ status + name + why + Open Settings, for one macOS permission."""

    def __init__(self, key: str, name: str, why: str, on_open: Callable[[str], None]) -> None:
        super().__init__()
        self.setObjectName("permRow")
        self.key = key
        self.state = _PERM_UNKNOWN
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_SM, theme.SPACE_SM)
        lay.setSpacing(theme.SPACE_MD)
        self._icon = QtWidgets.QLabel()
        self._icon.setFixedSize(theme.STATUS_GLYPH, theme.STATUS_GLYPH)
        lay.addWidget(self._icon, 0, QtCore.Qt.AlignVCenter)
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(theme.SPACE_2XS)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(theme.SPACE_SM)
        self._name = QtWidgets.QLabel(name)
        self._name.setObjectName("permName")
        self.status = QtWidgets.QLabel()
        top.addWidget(self._name)
        top.addWidget(self.status)
        top.addStretch(1)
        why_label = QtWidgets.QLabel(why)
        why_label.setObjectName("permWhy")
        why_label.setWordWrap(True)
        text.addLayout(top)
        text.addWidget(why_label)
        lay.addLayout(text, 1)
        self.button = QtWidgets.QPushButton("Open Settings")
        self.button.setToolTip(f"Allow {name} for Holler in System Settings")
        self.button.setAccessibleName(f"Open {name} settings")
        self.button.clicked.connect(lambda: on_open(key))
        lay.addWidget(self.button, 0, QtCore.Qt.AlignVCenter)
        self._name_text = name

    def set_state(self, state: str) -> None:
        self.state = state
        icon, color, words = _PERM_LOOK.get(state, _PERM_LOOK[_PERM_UNKNOWN])
        dpr = self.devicePixelRatioF() if self.isVisible() else 2.0
        self._icon.setPixmap(iconset.pixmap(icon, color, theme.STATUS_GLYPH, max(1.0, dpr)))
        self.status.setText(words)
        self.status.setStyleSheet(f"color: {color}; font-size: {theme.FONT_SM}pt;")
        self.button.setVisible(state != _PERM_GRANTED)
        self.setAccessibleName(f"{self._name_text}: {words}")


class _StepScroll(QtWidgets.QScrollArea):
    """Holds the step pages. Sized exactly like the pages it wraps (no
    QScrollArea size-hint cap, same height-for-width), so with room to spare
    it looks and lays out as if it weren't there; on a short screen the step
    scrolls instead of squashing its word-wrapped text."""

    def __init__(self, inner: QtWidgets.QWidget) -> None:
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # Not a Tab stop itself (focus still goes to the step's first field);
        # tabbing to a field below the fold scrolls it into view.
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.viewport().setAutoFillBackground(False)
        self.setWidget(inner)
        inner.setAutoFillBackground(False)  # (setWidget turns it on)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt override)
        return self.widget().sizeHint()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt override)
        return self.widget().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:  # noqa: N802 (Qt override)
        return self.widget().heightForWidth(width)


class FirstRunDialog(QtWidgets.QDialog):
    """Stepped first-run setup: project folder → (macOS) permissions →
    push-to-talk key → model. Nothing is written until the last step's
    "Save and launch"; every step can be revisited with Back."""

    def __init__(
        self,
        cfg: Config,
        parent: Optional[QtWidgets.QWidget] = None,
        on_relaunch: Optional[Callable[[], None]] = None,
    ) -> None:
        """`on_relaunch` (optional) restarts Holler. When given, the
        permissions step offers "Save and relaunch" (macOS applies an
        Accessibility / Screen Recording grant only to a fresh process): the
        settings are saved, then it is called. It should not return (it
        replaces the process); if it does, the dialog accepts as for "Save
        and launch". Without it the step tells the user to reopen Holler."""
        super().__init__(parent)
        self.setWindowTitle("Welcome to Holler")
        self.setStyleSheet(_QSS)
        # Lowered min height so we fit on a 1366x768 laptop at 150% Windows
        # scaling (~410px logical). Width kept generous for the prose layout.
        self.setMinimumSize(560, 380)
        self._cfg = cfg
        self._on_relaunch = on_relaunch
        self._show_permissions = mac_permissions_available()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_XL, theme.SPACE_LG, theme.SPACE_XL, theme.SPACE_LG)
        layout.setSpacing(theme.SPACE_MD)

        # Header: painted wordmark (BRAND.md) + where we are.
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(theme.SPACE_MD)
        header.addWidget(_wordmark(), 0, QtCore.Qt.AlignVCenter)
        header.addStretch(1)
        self._step_label = QtWidgets.QLabel()
        self._step_label.setObjectName("stepLabel")
        header.addWidget(self._step_label, 0, QtCore.Qt.AlignVCenter)
        self._dots = _StepDots(4 if self._show_permissions else 3)
        header.addWidget(self._dots, 0, QtCore.Qt.AlignVCenter)
        layout.addLayout(header)
        rule = QtWidgets.QFrame()
        rule.setObjectName("headerRule")
        rule.setFixedHeight(1)
        layout.addWidget(rule)

        self._pages = QtWidgets.QStackedWidget()
        # The minimum height (above) is below the steps' natural height, so
        # the step content scrolls rather than squashes on a short screen.
        # (Wrapped before the pages exist: setWidget lays the stack out, and
        # doing that to unpolished pages left stale, too-wide size hints.)
        self._step_scroll = _StepScroll(self._pages)
        self._steps: list[tuple[str, QtWidgets.QWidget]] = []
        self._build_folder_page(cfg)
        if self._show_permissions:
            self._build_permissions_page()
        self._build_hotkey_page(cfg)
        self._build_model_page(cfg)
        layout.addWidget(self._step_scroll, 1)

        # Buttons.
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(theme.SPACE_SM)
        quit_btn = QtWidgets.QPushButton("Quit")
        quit_btn.setToolTip("Quit without setting up Holler")
        quit_btn.clicked.connect(self.reject)
        btn_row.addWidget(quit_btn)
        btn_row.addStretch(1)
        self._back_btn = QtWidgets.QPushButton("Back")
        self._back_btn.clicked.connect(lambda: self._go(self._pages.currentIndex() - 1))
        self._next_btn = QtWidgets.QPushButton("Next")
        self._next_btn.setObjectName("primary")
        self._next_btn.clicked.connect(self._next)
        self._save_btn = QtWidgets.QPushButton("Save and launch")
        self._save_btn.setObjectName("primary")
        self._save_btn.clicked.connect(self._save)
        btn_row.addWidget(self._back_btn)
        btn_row.addWidget(self._next_btn)
        btn_row.addWidget(self._save_btn)
        layout.addLayout(btn_row)

        self._perm_timer = QtCore.QTimer(self)
        self._perm_timer.setInterval(theme.PERMISSION_POLL_MS)
        self._perm_timer.timeout.connect(self._refresh_permissions)

        self._go(0)
        # Context-object overload: cancelled if the dialog is destroyed first.
        QtCore.QTimer.singleShot(0, self, self._check_auth)

    # ── pages ────────────────────────────────────────────────────────────────

    def _page(self, name: str, title: str, sub: str) -> tuple[QtWidgets.QWidget, QtWidgets.QVBoxLayout]:
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(0, theme.SPACE_SM, 0, 0)
        lay.setSpacing(theme.SPACE_MD)
        t = QtWidgets.QLabel(title)
        t.setProperty("class", "bigtitle")
        t.setStyleSheet(f"color: {theme.FG}; font-size: {theme.FONT_XL}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        t.setWordWrap(True)
        lay.addWidget(t)
        if sub:
            s = QtWidgets.QLabel(sub)
            s.setWordWrap(True)
            s.setStyleSheet(f"color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt;")
            lay.addWidget(s)
        self._pages.addWidget(page)
        self._steps.append((name, page))
        return page, lay

    @staticmethod
    def _form() -> QtWidgets.QFormLayout:
        form = QtWidgets.QFormLayout()
        form.setSpacing(theme.SPACE_SM)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        # macOS defaults to FieldsStayAtSizeHint (ragged field widths).
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        return form

    @staticmethod
    def _small(text: str, style: str) -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel(text)
        lab.setWordWrap(True)
        lab.setStyleSheet(style)
        return lab

    def _build_folder_page(self, cfg: Config) -> None:
        _page, lay = self._page(
            "Project folder", "Welcome to Holler",
            "Hold a key, talk and scribble on your screen, and Holler sends it to Claude. "
            "A few one-time settings first.",
        )
        form = self._form()
        cwd_row = QtWidgets.QHBoxLayout()
        cwd_row.setSpacing(theme.SPACE_SM)
        default_cwd = str(cfg.repo_cwd)
        if not Path(default_cwd).is_dir():
            default_cwd = str(Path.home())
        self._cwd_edit = QtWidgets.QLineEdit(default_cwd)
        self._cwd_edit.setAccessibleName("Project folder")
        browse = QtWidgets.QPushButton("Choose…")
        browse.setAccessibleName("Choose project folder")
        browse.clicked.connect(self._browse_cwd)
        cwd_row.addWidget(self._cwd_edit, 1)
        cwd_row.addWidget(browse)
        form.addRow("Project folder:", cwd_row)
        hint = self._small("Claude reads and edits files here — usually your project's root folder.",
                           _HINT_STYLE)
        hint.setProperty("class", "hint")
        form.addRow("", hint)
        self._cwd_error = self._small("", _ERROR_STYLE)
        self._cwd_error.setVisible(False)
        form.addRow("", self._cwd_error)
        self._cwd_edit.textChanged.connect(lambda _t: self._set_error(self._cwd_error, None))
        lay.addLayout(form)
        lay.addStretch(1)

        # Claude Code pre-flight strip.
        self._auth_label = QtWidgets.QLabel()
        self._auth_label.setWordWrap(True)
        self._auth_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._set_auth_state("Checking for Claude Code…", "authChecking")
        lay.addWidget(self._auth_label)

    def _build_permissions_page(self) -> None:
        _page, lay = self._page(
            "Permissions", "Allow access",
            "macOS asks before an app can hear you, notice your keys and see your screen. "
            "Holler needs all three.",
        )
        self._perm_rows: dict[str, _PermissionRow] = {}
        for key, name, why in _PERMISSIONS:
            row = _PermissionRow(key, name, why, self._open_permission)
            self._perm_rows[key] = row
            lay.addWidget(row)
        relaunch_row = QtWidgets.QHBoxLayout()
        relaunch_row.setSpacing(theme.SPACE_SM)
        self._perm_note = self._small("", _HINT_STYLE)
        relaunch_row.addWidget(self._perm_note, 1)
        self._relaunch_btn: Optional[QtWidgets.QPushButton] = None
        if self._on_relaunch is not None:
            self._relaunch_btn = QtWidgets.QPushButton("Save and relaunch")
            self._relaunch_btn.setToolTip(
                "Save these settings and restart Holler so macOS applies the new permissions")
            self._relaunch_btn.clicked.connect(self._relaunch)
            relaunch_row.addWidget(self._relaunch_btn, 0, QtCore.Qt.AlignTop)
        lay.addLayout(relaunch_row)
        self._relaunch_error = self._small("", _ERROR_STYLE)
        self._relaunch_error.setVisible(False)
        lay.addWidget(self._relaunch_error)
        lay.addStretch(1)

    def _build_hotkey_page(self, cfg: Config) -> None:
        from holler.settings_dialog import HotkeyRecordButton

        _page, lay = self._page(
            "Push-to-talk key", "Choose your push-to-talk key",
            "Hold it while you talk, let go to send. Click Record and press the key, "
            "or type its name.",
        )
        form = self._form()
        self._hotkey_edit = QtWidgets.QLineEdit(cfg.hotkey or "right alt")
        self._hotkey_edit.setAccessibleName("Push-to-talk key")
        self._hotkey_record = HotkeyRecordButton(self._hotkey_edit)
        hk_row = QtWidgets.QHBoxLayout()
        hk_row.setSpacing(theme.SPACE_SM)
        hk_row.addWidget(self._hotkey_edit, 1)
        hk_row.addWidget(self._hotkey_record)
        form.addRow("Push-to-talk key:", hk_row)
        self._hk_hint = self._small("", _HINT_STYLE)
        self._hk_hint.setProperty("class", "hint")
        form.addRow("", self._hk_hint)
        self._hk_error = self._small("", _ERROR_STYLE)
        self._hk_error.setVisible(False)
        form.addRow("", self._hk_error)
        self._hotkey_edit.textChanged.connect(self._update_hotkey_hint)
        self._update_hotkey_hint(self._hotkey_edit.text())
        lay.addLayout(form)
        lay.addStretch(1)

    def _build_model_page(self, cfg: Config) -> None:
        _page, lay = self._page(
            "Model", "Pick a model",
            "The Claude model new sessions use. You can switch any time from the model pill "
            "in the Sessions window.",
        )
        form = self._form()
        self._model = QtWidgets.QComboBox()
        self._model.setAccessibleName("Model")
        models = effective_models(cfg)
        for m in models:
            self._model.addItem(_pretty_model(m), userData=m)
        cur_idx = next((i for i, m in enumerate(models) if m == cfg.claude_model), 0)
        self._model.setCurrentIndex(cur_idx)
        form.addRow("Model:", self._model)
        lay.addLayout(form)
        lay.addStretch(1)

    # ── navigation ───────────────────────────────────────────────────────────

    def _step_index(self, name: str) -> int:
        return next(i for i, (n, _p) in enumerate(self._steps) if n == name)

    def _go(self, index: int) -> None:
        index = max(0, min(index, len(self._steps) - 1))
        if index != self._pages.currentIndex():
            self._hotkey_record.stop()
        self._pages.setCurrentIndex(index)
        last = index == len(self._steps) - 1
        name = self._steps[index][0]
        self._step_label.setText(f"Step {index + 1} of {len(self._steps)} · {name}")
        self._dots.set_current(index)
        self._dots.setAccessibleName(f"Step {index + 1} of {len(self._steps)}: {name}")
        self._back_btn.setVisible(index > 0)
        self._next_btn.setVisible(not last)
        self._save_btn.setVisible(last)
        # Return advances; on the last step it saves.
        self._next_btn.setDefault(not last)
        self._save_btn.setDefault(last)
        on_perms = name == "Permissions"
        if on_perms:
            self._refresh_permissions()
        self._update_perm_polling()
        page = self._steps[index][1]
        first_input = page.findChild(QtWidgets.QLineEdit) or page.findChild(QtWidgets.QComboBox)
        if first_input is not None and self.isVisible():
            first_input.setFocus(QtCore.Qt.OtherFocusReason)

    def _next(self) -> None:
        i = self._pages.currentIndex()
        name = self._steps[i][0]
        if name == "Project folder" and self._folder_error() is not None:
            self._set_error(self._cwd_error, self._folder_error())
            return
        if name == "Push-to-talk key" and self._hotkey_error() is not None:
            self._set_error(self._hk_error, self._hotkey_error())
            return
        self._go(i + 1)

    # ── permissions (macOS) ──────────────────────────────────────────────────

    def _on_permissions_page(self) -> bool:
        i = self._pages.currentIndex()
        return 0 <= i < len(self._steps) and self._steps[i][0] == "Permissions"

    def _update_perm_polling(self) -> None:
        # Re-check every second only while the step is actually on screen.
        if self._show_permissions and self.isVisible() and self._on_permissions_page():
            if not self._perm_timer.isActive():
                self._perm_timer.start()
        else:
            self._perm_timer.stop()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._update_perm_polling()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._perm_timer.stop()
        super().hideEvent(event)

    def _refresh_permissions(self) -> None:
        if not self._show_permissions:
            return
        try:
            states = _permission_states()
        except Exception:
            import logging
            logging.exception("permission check failed")
            states = {}
        for key, row in self._perm_rows.items():
            row.set_state(states.get(key, _PERM_UNKNOWN))
        missing = [k for k in ("accessibility", "screen") if states.get(k) != _PERM_GRANTED]
        if not any(r.state != _PERM_GRANTED for r in self._perm_rows.values()):
            note = "✓ All set."
        elif missing:
            note = ("macOS applies Accessibility and Screen Recording only after Holler restarts. "
                    + ("Allow them, then save and relaunch." if self._on_relaunch is not None
                       else "Allow them, then quit and reopen Holler.")
                    + " You can also continue and allow them later.")
        else:
            note = "You can also continue and allow access later."
        self._perm_note.setText(note)

    def _open_permission(self, key: str) -> None:
        from holler import mac_permissions as macperm

        try:
            if key == "mic":
                if self._perm_rows["mic"].state == _PERM_NOT_ASKED:
                    macperm.request_mic_access()  # the system prompt
                else:
                    macperm.open_mic_privacy_settings()
            elif key == "accessibility":
                # Registers Holler in the list (so there is a switch to turn
                # on), then shows the pane.
                macperm.request_accessibility()
                macperm.open_accessibility_settings()
            elif key == "screen":
                macperm.request_screen_recording()
                macperm.open_screen_recording_settings()
        except Exception:
            import logging
            logging.exception("could not open %s permission settings", key)
        self._refresh_permissions()

    # ── push-to-talk key ─────────────────────────────────────────────────────

    def _hotkey_error(self) -> Optional[str]:
        from holler.settings_dialog import ptt_key_error
        return ptt_key_error((self._hotkey_edit.text() or "").strip())

    def _update_hotkey_hint(self, text: str) -> None:
        from holler.settings_dialog import key_label

        err = self._hotkey_error()
        self._set_error(self._hk_error, err)
        self._hk_hint.setVisible(err is None)
        if err is None:
            self._hk_hint.setText(
                f"{key_label(text)} — one key only, such as “right alt” or “f12”.")

    # ── shared helpers ───────────────────────────────────────────────────────

    def _folder_error(self) -> Optional[str]:
        raw = self._cwd_edit.text().strip()
        if not raw:
            return "Choose a project folder."
        if not Path(raw).is_dir():
            return f"“{raw}” doesn't exist. Choose another folder."
        return None

    def _set_error(self, label: QtWidgets.QLabel, message: Optional[str]) -> None:
        label.setText(message or "")
        label.setVisible(bool(message))

    def _browse_cwd(self) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose project folder", self._cwd_edit.text() or str(Path.home())
        )
        if chosen:
            self._cwd_edit.setText(chosen)

    def _set_auth_state(self, text: str, kind: str) -> None:
        self._auth_label.setText(text)
        self._auth_label.setObjectName(kind)
        self._auth_label.setStyleSheet(_AUTH_STYLES.get(kind, ""))

    def _check_auth(self) -> None:
        # The path lookup is a few stat() calls — fine here. `claude --version`
        # can take seconds (or hang until the timeout), so it runs on a worker
        # thread and the result comes back via a queued signal.
        claude_path = find_claude_cli()
        if not claude_path:
            self._set_auth_state(
                "⚠ Claude Code isn't installed. In Terminal, run: "
                "npm install -g @anthropic-ai/claude-code",
                "authMiss",
            )
            return
        self._set_auth_state("Checking for Claude Code…", "authChecking")
        probe = _AuthProbe(QtCore.QCoreApplication.instance())
        # Receiver lives on the GUI thread → queued delivery. Auto-disconnects
        # if the dialog is destroyed before the result arrives.
        probe.done.connect(self._on_auth_result)
        probe.done.connect(probe.deleteLater)

        def _work() -> None:
            text, kind = probe_claude_cli(claude_path)
            try:
                probe.done.emit(text, kind)
            except RuntimeError:
                pass  # app torn down while the probe ran

        threading.Thread(target=_work, name="holler-claude-probe", daemon=True).start()

    @QtCore.Slot(str, str)
    def _on_auth_result(self, text: str, kind: str) -> None:
        self._set_auth_state(text, kind)

    def done(self, result: int) -> None:  # Qt override: accept()/reject()/Esc
        self._perm_timer.stop()
        self._hotkey_record.stop()
        super().done(result)

    # ── save ─────────────────────────────────────────────────────────────────

    def _write_config(self) -> bool:
        """Validate every step (jumping to the first bad one) and save.
        True when config.toml was written."""
        folder_err = self._folder_error()
        if folder_err:
            self._go(self._step_index("Project folder"))
            self._set_error(self._cwd_error, folder_err)
            return False
        hk_err = self._hotkey_error()
        if hk_err:
            self._go(self._step_index("Push-to-talk key"))
            self._set_error(self._hk_error, hk_err)
            return False
        cwd = Path(self._cwd_edit.text().strip())
        hotkey = (self._hotkey_edit.text() or "right alt").strip()
        model = self._model.currentData() or DEFAULT_MODEL
        new_cfg = dataclasses.replace(
            self._cfg, repo_cwd=cwd, hotkey=hotkey, claude_model=str(model)
        )
        try:
            save_config(new_cfg)
        except Exception:
            import logging
            logging.exception("first-run config save failed")
            from holler.config import _config_path_default
            QtWidgets.QMessageBox.critical(
                self, "Couldn't save settings",
                f"Holler couldn't write the settings file. Check that Holler can "
                f"write to {_config_path_default()}, then try again.",
            )
            return False
        return True

    def _save(self) -> None:
        if self._write_config():
            self.accept()

    def _relaunch(self) -> None:
        if self._on_relaunch is None or not self._write_config():
            return
        try:
            self._on_relaunch()
        except Exception:
            import logging
            logging.exception("relaunch from first run failed")
            self._set_error(
                self._relaunch_error,
                "Couldn't relaunch Holler. Your settings are saved — quit and reopen Holler.",
            )
            return
        self.accept()


def mac_permissions_available() -> bool:
    """True where the permissions step applies (macOS TCC)."""
    return sys.platform == "darwin"


def _wordmark() -> QtWidgets.QWidget:
    """The painted HOLLER wordmark (sessions_window.HollerWordmark — the one
    brand implementation), or a plain label if that module can't load."""
    try:
        from holler.sessions_window import HollerWordmark
        w = HollerWordmark()
    except Exception:
        import logging
        logging.exception("wordmark unavailable")
        w = QtWidgets.QLabel("HOLLER")
        w.setStyleSheet(f"color: {theme.BRAND_AMBER}; font-weight: {theme.WEIGHT_BOLD};")
    w.setAccessibleName("Holler")
    return w


def _pretty_model(model_id: str) -> str:
    import re
    m = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,3}))?(?!\d)", model_id)
    if not m:
        return model_id
    family = m.group(1).capitalize()
    minor = m.group(3)
    version = f"{m.group(2)}.{minor}" if minor else m.group(2)
    return f"{family} {version}  ({model_id})"


def maybe_migrate_legacy_sysprompt(
    appdata: Optional[Path] = None, config_path: Optional[Path] = None
) -> None:
    """One-shot migration: if a legacy version of holler shipped a baked-in
    project-specific system prompt, copy that text into the user's override
    file so behavior doesn't visibly change after the genericization.

    Only an EXISTING (legacy) install is migrated — evidence is a holler.db in
    app-data or a config.toml, both of which predate this flag on any install
    that ran the old build. A fresh install just writes the flag: it must not
    inherit the original owner's workflow rules.

    Idempotent via a flag file at ``<app-data>/migration_v1.flag``.
    """
    if appdata is None:
        from holler.appdir import app_data_dir
        appdata = app_data_dir()
    if config_path is None:
        from holler.config import _config_path_default
        config_path = _config_path_default()
    flag = appdata / "migration_v1.flag"
    if flag.exists():
        return
    override = appdata / "sysprompt.md"
    if override.exists():
        # User already has their own; don't clobber.
        flag.write_text("already-present", encoding="utf-8")
        return
    legacy_install = (appdata / "holler.db").exists() or config_path.exists()
    if not legacy_install:
        flag.write_text("fresh-install", encoding="utf-8")
        return
    legacy_text = _LEGACY_PRE_V3_APPEND
    override.write_text(legacy_text, encoding="utf-8")
    flag.write_text("migrated", encoding="utf-8")


# Legacy system-prompt snippet used to migrate pre-V3 installs. New installs
# never see this file — the constant is only read by maybe_migrate_legacy_sysprompt().
_LEGACY_PRE_V3_APPEND = """\
# Branch workflow

For ANY edit under `<your-project>/`, ensure the working tree is on a feature
branch first. Holler's per-session git worktree gives you branch isolation
automatically; if you're not in a worktree and the current branch is `master`,
switch to a feature branch before editing. Land work with plain git. NEVER
commit directly to master under `<your-project>/`.
Other edits (business/, website/, tools/, etc.) can use the current branch.

# No auto-push

Holler is used for fast iteration and rapid-fire experiments — DO NOT
`git push` automatically. Commit locally only. Push when you have tested.
"""
