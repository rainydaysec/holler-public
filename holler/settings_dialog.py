"""Settings dialog for runtime config tweaks.

Opens from the panel's `⋯ → Settings…` menu. Reads the current Config, lets the
user edit, on Save: writes back to config.toml + invokes the on_save callback
with (new_cfg, changed_fields). The runner decides which changes are hot-
applicable vs. need-restart.

Layout: a section list on the left (General · Voice & Mic · Capture · Sessions
· Advanced · Legacy panel) and ONE scrolling form on the right with a heading
per section — clicking a section scrolls to it, scrolling selects it. One form
(not a page per section) keeps every row's label discoverable and lets the
dialog open showing everything on a tall screen.

Also home to the push-to-talk key recorder (`HotkeyRecordButton`) and the
platform-aware key names (`key_label`) that the first-run dialog shares.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from holler import theme
from holler.audio import LOW_RMS, SILENT_RMS
from holler.config import Config, effective_models, save as save_config
from holler.hotkey import set_key_recording, validate_ptt_hotkey

_IS_MAC = sys.platform == "darwin"


def _ensure_combo_value(combo: QtWidgets.QComboBox, value: str) -> None:
    """Select `value`, adding it first if it isn't a listed choice.

    A non-editable QComboBox silently ignores setCurrentText() for an unlisted
    value and keeps item 0 — so saving the dialog would overwrite e.g.
    whisper_compute="auto" with "float16" without the user touching it.

    Combos here show a friendly label and carry the raw config value in
    itemData; the match is on that value. An unlisted value is added as its
    own (raw) label so it stays selectable and round-trips.
    """
    value = "" if value is None else value
    idx = combo.findData(value)
    if idx < 0:
        combo.addItem(str(value), userData=value)
        idx = combo.count() - 1
    combo.setCurrentIndex(idx)


def _combo_value(combo: QtWidgets.QComboBox):
    """The config value a combo currently holds.

    A listed choice reads as its itemData (the raw value behind the friendly
    label). Text typed into an editable combo that isn't one of the labels is
    the value itself — e.g. a local speech-model folder or a model id.
    """
    text = combo.currentText().strip()
    if combo.isEditable():
        idx = combo.findText(combo.currentText())
        if idx < 0:
            return text
    else:
        idx = combo.currentIndex()
    if idx < 0:
        return text
    data = combo.itemData(idx)
    return text if data is None else data


def _spin_with_value(spin: QtWidgets.QSpinBox, value: int) -> None:
    """Set `value`, widening the range if needed so it isn't silently clamped
    (and rewritten to disk) on a Save the user didn't mean to change it in."""
    value = int(value)
    if value < spin.minimum():
        spin.setMinimum(value)
    if value > spin.maximum():
        spin.setMaximum(value)
    spin.setValue(value)


def stroke_color_error(sc: str) -> "str | None":
    """User-facing error if `sc` isn't a hex colour BOTH Qt and PIL accept.

    The overlay paints with QColor and the composite with PIL; a value like
    "#GGGGGG" passed a length-only check and then crashed PIL compose in the
    dispatch worker.
    """
    sc = (sc or "").strip()
    bad = f"Pen color must be a hex color such as #FF2E2E (#RGB or #RRGGBB), not “{sc}”."
    if not (sc.startswith("#") and len(sc) in (4, 7, 9)):
        return bad
    if not QtGui.QColor(sc).isValid():
        return bad
    try:
        from PIL import ImageColor
        ImageColor.getrgb(sc)
    except Exception:
        return bad
    return None


def display_path(path: "Path | str", max_chars: int = 64) -> str:
    """`path` for display: home shown as ~, overlong paths middle-elided.

    The full path belongs in a tooltip; this keeps a hint line from running
    off the dialog's edge on an unusual app-data location.
    """
    p = Path(path)
    try:
        text = str(Path("~") / p.relative_to(Path.home()))
    except ValueError:
        text = str(p)
    if len(text) > max_chars:
        head = (max_chars - 1) // 2
        tail = max_chars - 1 - head
        text = text[:head] + "…" + text[-tail:]
    return text


# ── push-to-talk key: names + recorder ───────────────────────────────────────

# Spec → platform-aware display name. Specs are the names hotkey.py accepts
# (pynput on macOS/Linux, the `keyboard` lib on Windows).
_MAC_NAMES = {
    "right alt": "Right ⌥ Option", "ralt": "Right ⌥ Option", "alt gr": "Right ⌥ Option",
    "left alt": "Left ⌥ Option", "lalt": "Left ⌥ Option",
    "alt": "⌥ Option", "option": "⌥ Option",
    "right cmd": "Right ⌘ Command", "right command": "Right ⌘ Command",
    "left cmd": "Left ⌘ Command", "left command": "Left ⌘ Command",
    "cmd": "⌘ Command", "command": "⌘ Command",
    "right ctrl": "Right ⌃ Control", "rctrl": "Right ⌃ Control",
    "left ctrl": "Left ⌃ Control", "lctrl": "Left ⌃ Control",
    "ctrl": "⌃ Control", "control": "⌃ Control",
    "right shift": "Right ⇧ Shift", "rshift": "Right ⇧ Shift",
    "left shift": "Left ⇧ Shift", "lshift": "Left ⇧ Shift", "shift": "⇧ Shift",
    "enter": "Return", "esc": "Esc", "escape": "Esc",
}


def key_label(spec: str) -> str:
    """`right alt` → `Right ⌥ Option` on macOS, `Right Alt` elsewhere;
    `f12` → `F12`. Unknown specs are title-cased."""
    s = (spec or "").strip().lower()
    if not s:
        return ""
    if _IS_MAC and s in _MAC_NAMES:
        return _MAC_NAMES[s]
    if s.startswith("f") and s[1:].isdigit():
        return s.upper()
    if len(s) == 1:
        return s.upper()
    return " ".join(w.capitalize() for w in s.split())


# macOS virtual key codes (kVK_*) of the side-specific modifiers.
_MAC_VK = {
    58: "left alt", 61: "right alt", 55: "left cmd", 54: "right cmd",
    56: "left shift", 60: "right shift", 59: "left ctrl", 62: "right ctrl",
    57: "caps lock",
}
# Windows scan codes (Qt keeps the extended-key bit as 0x100).
_WIN_SCAN = {
    0x38: "left alt", 0x138: "right alt", 0x1D: "left ctrl", 0x11D: "right ctrl",
    0x2A: "left shift", 0x36: "right shift", 0x15B: "left windows", 0x15C: "right windows",
}


def spec_from_key_event(event: QtGui.QKeyEvent) -> Optional[str]:
    """The push-to-talk spec for a key press, or None if the key can't be one.

    Modifiers are recorded by side (Right ⌥ vs Left ⌥) from the native key
    code — that's what makes a modifier usable as a push-to-talk key without
    clashing with ordinary shortcuts.
    """
    key = event.key()
    Qt = QtCore.Qt
    if _IS_MAC:
        named = _MAC_VK.get(int(event.nativeVirtualKey()))
        if named:
            return named
    elif sys.platform == "win32":
        named = _WIN_SCAN.get(int(event.nativeScanCode()))
        if named and key in (Qt.Key_Alt, Qt.Key_AltGr, Qt.Key_Control, Qt.Key_Shift, Qt.Key_Meta):
            return named
    # The Windows / Super key: the `keyboard` lib (Windows) calls it
    # "windows"; pynput (Linux) only knows "super".
    win = sys.platform == "win32"
    generic = {
        Qt.Key_Alt: "alt", Qt.Key_AltGr: "right alt", Qt.Key_Shift: "shift",
        # Qt maps ⌘ to Key_Control and ⌃ to Key_Meta on macOS.
        Qt.Key_Control: "cmd" if _IS_MAC else "ctrl",
        Qt.Key_Meta: "ctrl" if _IS_MAC else ("windows" if win else "super"),
        Qt.Key_Super_L: "left windows" if win else "left super",
        Qt.Key_Super_R: "right windows" if win else "right super",
        Qt.Key_CapsLock: "caps lock", Qt.Key_Space: "space", Qt.Key_Tab: "tab",
        Qt.Key_Return: "enter", Qt.Key_Enter: "enter",
    }
    if key in generic:
        return generic[key]
    if Qt.Key_F1 <= key <= Qt.Key_F20:
        return f"f{key - Qt.Key_F1 + 1}"
    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        return chr(key).lower()
    text = event.text()
    if len(text) == 1 and text.isprintable() and not text.isspace() and text != "+":
        return text.lower()
    return None


def ptt_key_error(spec: str) -> Optional[str]:
    """`hotkey.validate_ptt_hotkey` with user-facing wording (no repr, curly
    quotes) — None when `spec` can be a push-to-talk key."""
    if validate_ptt_hotkey(spec) is None:
        return None
    s = (spec or "").strip()
    if not s:
        return "Enter a push-to-talk key, such as “right alt” or “f12”."
    return (f"“{s}” is a key combination. The push-to-talk key must be a single key, "
            "such as “right alt” or “f12”.")


def listener_accepts_key(spec: str) -> bool:
    """True if hotkey.py's listener will hold exactly `spec` (no silent
    fallback to Right Alt). On macOS/Linux that's pynput's parser; on Windows
    the `keyboard` lib takes the names the recorder produces."""
    if validate_ptt_hotkey(spec) is not None:
        return False
    if sys.platform == "win32":
        return True
    try:
        from pynput import keyboard as kb

        from holler.hotkey import _parse_pynput_key
    except Exception:
        return True  # no listener to ask (e.g. pynput missing) — don't block
    # _parse_pynput_key falls back to Right Alt for a name it doesn't know.
    return _parse_pynput_key(spec) != kb.Key.alt_r or spec.strip().lower() in ("right alt", "ralt", "alt gr")


class HotkeyRecordButton(QtWidgets.QPushButton):
    """"Record" button that fills a push-to-talk key field by key press.

    Click it, press the key (Esc cancels, clicking again cancels): the key's
    spec ("right alt", "f12", …) is written into `target`, which stays a plain
    editable text field so a name can still be typed. While recording, an
    application event filter takes every key press aimed at this window so
    nothing else (the dialog's default button, Esc-to-close, shortcuts)
    reacts to it. Recording ends as soon as the user moves on — focus leaves
    the button, a mouse press lands outside it, or the window / app goes
    inactive — so typing into another field never records a key.
    """

    recorded = QtCore.Signal(str)
    IDLE_TEXT = "Record"

    def __init__(self, target: QtWidgets.QLineEdit, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(self.IDLE_TEXT, parent)
        self._target = target
        self._recording = False
        self.setCheckable(True)
        self.setToolTip("Click, then press the key you want to hold while you talk")
        self.setAccessibleName("Record push-to-talk key")
        self.setStyleSheet(
            f"QPushButton:checked {{ border-color: {theme.ACCENT}; color: {theme.ACCENT}; }}")
        self.clicked.connect(self._on_clicked)

    def is_recording(self) -> bool:
        return self._recording

    def _on_clicked(self) -> None:
        if self._recording:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self._recording:
            return
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        self._recording = True
        self.setChecked(True)
        self.setText("Press a key…")
        self.setAccessibleDescription("Recording. Press the key, or Esc to cancel.")
        app.installEventFilter(self)
        # Pause push-to-talk: pressing a key here must not start a capture.
        set_key_recording(True)
        self.setFocus(QtCore.Qt.OtherFocusReason)

    def stop(self) -> None:
        if self._recording:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            from holler.hotkey import KEY_RECORDING_GRACE_S
            set_key_recording(False, grace_s=KEY_RECORDING_GRACE_S)
        self._recording = False
        self.setChecked(False)
        self.setText(self.IDLE_TEXT)
        self.setAccessibleDescription("")

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.stop()
        super().hideEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.stop()
        super().focusOutEvent(event)

    def _in_my_window(self, obj) -> bool:
        win = self.window()
        if isinstance(obj, QtWidgets.QWidget):
            return obj.window() is win
        if isinstance(obj, QtGui.QWindow):
            return obj is win.windowHandle()
        return False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        if not self._recording:
            return False
        et = event.type()
        if (et in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonDblClick)
                and isinstance(event, QtGui.QMouseEvent)):
            # A click anywhere but this button (another field, another
            # window) ends recording; the click itself goes through.
            pos = self.mapFromGlobal(event.globalPosition().toPoint())
            if not self.rect().contains(pos):
                self.stop()
            return False
        if et == QtCore.QEvent.WindowDeactivate and obj is self.window():
            self.stop()
            return False
        if (et == QtCore.QEvent.ApplicationStateChange
                and QtGui.QGuiApplication.applicationState() != QtCore.Qt.ApplicationActive):
            self.stop()
            return False
        if et in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress,
                  QtCore.QEvent.KeyRelease) and not self._in_my_window(obj):
            return False  # another window's keys are none of ours
        if et == QtCore.QEvent.ShortcutOverride:
            # Claim the key so no shortcut fires; the KeyPress follows.
            event.accept()
            return True
        if et == QtCore.QEvent.KeyPress:
            if event.isAutoRepeat():
                return True
            if event.key() == QtCore.Qt.Key_Escape:
                self.stop()
                return True
            spec = spec_from_key_event(event)
            if spec is None or not listener_accepts_key(spec):
                self.setText("Pick another key…")
                self.setAccessibleDescription(
                    "That key can't be the push-to-talk key. Press another key, or Esc to cancel.")
                return True
            self.stop()
            self._target.setText(spec)
            self.recorded.emit(spec)
            return True
        if et == QtCore.QEvent.KeyRelease:
            return True
        return False


# ── settings dialog ──────────────────────────────────────────────────────────

# Stock controls (inputs, combos, spin boxes, check boxes, buttons incl.
# #primary, scrollbars) come from the app stylesheet (theme.app_qss); only
# dialog-specific rules live here.
_DARK_QSS = f"""
QDialog {{ background: {theme.CANVAS}; color: {theme.FG}; }}
QLabel {{ color: {theme.FG}; }}
QLabel.note {{ color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt; }}
QLabel.error {{ color: {theme.DANGER}; font-size: {theme.FONT_SM}pt; }}
QLabel#sectionHeading {{ color: {theme.FG}; font-size: {theme.FONT_MD}pt; font-weight: {theme.WEIGHT_SEMIBOLD}; }}
QWidget#settingsContent {{ background: transparent; }}
QScrollArea#settingsScroll {{ border-bottom: 1px solid {theme.BORDER_SUBTLE}; }}
QListWidget#settingsNav {{ background: transparent; border: none; border-right: 1px solid {theme.BORDER_SUBTLE};
    outline: none; font-size: {theme.FONT_SM}pt; padding-right: {theme.SPACE_SM}px; }}
QListWidget#settingsNav::item {{ color: {theme.FG_MUTED}; padding: {theme.SPACE_XS + theme.SPACE_2XS}px {theme.SPACE_SM}px;
    border-radius: {theme.RADIUS_SM}px; margin-bottom: {theme.SPACE_2XS}px; }}
QListWidget#settingsNav::item:hover {{ background: {theme.SURFACE}; color: {theme.FG}; }}
QListWidget#settingsNav::item:selected {{ background: {theme.SELECTION_BG}; color: {theme.FG}; }}
QListWidget#settingsNav:focus {{ border-right: 1px solid {theme.ACCENT}; }}
QFrame#helpCard {{ background: {theme.SUNKEN}; border: 1px solid {theme.BORDER_SUBTLE}; border-radius: {theme.RADIUS_MD}px; }}
QLabel#helpBody {{ color: {theme.FG_MUTED}; font-size: {theme.FONT_XS}pt; }}
QLabel#restartNote {{ color: {theme.WARNING}; font-size: {theme.FONT_SM}pt; }}
"""

_NOTE_STYLE = f"color: {theme.FG_MUTED}; font-size: {theme.FONT_SM}pt;"
_ERROR_STYLE = f"color: {theme.DANGER}; font-size: {theme.FONT_SM}pt;"

# Settings that only take effect when Holler next launches: changing one shows
# the "Restart now" prompt. (Everything else is applied live by the runner's
# on_save callback, or read fresh where it's used.)
RESTART_KEYS: dict[str, str] = {
    "whisper_model": "Speech model",
    "whisper_compute": "Speech precision",
    "whisper_device": "Speech device",
    "whisper_language": "Speech language",
    "ui_scale": "Interface scale",
    "sessions_window_hotkey": "Sessions window shortcut",
    "concurrency_cap": "Max running sessions",
    "per_session_worktrees": "Worktree per session",
    "superintendent_enabled": "Superintendent",
    "auto_archive_days": "Archive idle sessions",
    "purge_reviewed_days": "Delete Done sessions",
}

# (label, raw value) — friendly label first, the raw config value kept visible.
_WHISPER_MODELS = (
    ("Fastest (tiny)", "tiny"),
    ("Fast (base)", "base"),
    ("Balanced (small)", "small"),
    ("Accurate (medium)", "medium"),
    ("Most accurate (large-v3)", "large-v3"),
    ("Accurate, faster (large-v3-turbo)", "large-v3-turbo"),
)
_WHISPER_COMPUTE = (
    ("Half precision — GPU (float16)", "float16"),
    ("Mixed — GPU (int8_float16)", "int8_float16"),
    ("Compact — CPU (int8)", "int8"),
    ("Full precision (float32)", "float32"),
)
_PANEL_CORNERS = (
    ("Top right", "top-right"), ("Top left", "top-left"),
    ("Bottom right", "bottom-right"), ("Bottom left", "bottom-left"),
)


def _whisper_devices() -> tuple[tuple[str, str], ...]:
    # faster-whisper only has a GPU path on CUDA (Windows/Linux + NVIDIA); on a
    # Mac the CPU is the only real choice, so don't lead with cuda there.
    if _IS_MAC:
        return (("CPU", "cpu"),)
    return (("GPU (CUDA)", "cuda"), ("CPU", "cpu"))


def _ui_scales() -> tuple[tuple[str, float], ...]:
    steps = tuple((f"{p}%", round(p / 100, 3)) for p in range(100, 201, 10))
    return (("Auto", 0.0),) + steps


def _model_label(model_id: str) -> str:
    from holler.first_run import _pretty_model
    return _pretty_model(model_id)


def _models_checked_text() -> str:
    from holler.model_discovery import last_refreshed
    ts = last_refreshed()
    if not ts:
        return "Model list hasn't been checked yet."
    try:
        when = datetime.fromisoformat(ts).strftime("%d %b %Y, %H:%M")
    except ValueError:
        when = ts
    return f"Model list last checked {when}."


def _parse_model_list(text: str) -> list[str]:
    out: list[str] = []
    for part in str(text or "").replace("\n", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _voice_commands_html() -> str:
    """The real spoken phrases (model_prefix + superintendent drafts), so the
    help can't drift from what the parsers accept."""
    try:
        from holler.model_prefix import _FAMILIES
        families = list(_FAMILIES)
    except Exception:
        families = ["opus", "sonnet", "haiku"]
    try:
        from holler.superintendent.drafts import COMMIT_CUES, DISCARD_CUES, HOLD_CUES
        hold, commit, discard = HOLD_CUES[:2], COMMIT_CUES[:2], DISCARD_CUES[:1]
    except Exception:
        hold, commit, discard = ["more coming"], ["file it"], ["scratch that"]

    def q(words) -> str:
        return " or ".join(f"“{w}”" for w in words)

    fams = ", ".join(families)
    return (
        f"<b style='color:{theme.FG}'>Pick the model for one turn</b> — start with "
        f"“with sonnet, …”, “using haiku, …” or “on opus, …” ({fams}). "
        "The rest of what you say is sent; the newest model of that family answers."
        f"<br><br><b style='color:{theme.FG}'>Chain captures</b> — in the Superintendent inbox, end with "
        f"{q(hold)} to hold a capture, {q(commit)} to send the held captures as one item, "
        f"or {q(discard)} to drop them. In any session, Start chain → File it does the same "
        "from the chain bar."
    )


class SettingsDialog(QtWidgets.QDialog):
    # Emitted from the mic-test audio callback thread; marshals the live level
    # (0..1) to the GUI thread to drive the meter.
    _mic_level = QtCore.Signal(float)
    # Emitted from the model-refresh worker thread: (model_ids, error_or_empty).
    _models_refreshed = QtCore.Signal(list, str)

    SECTIONS = ("General", "Voice & Mic", "Capture", "Sessions", "Advanced", "Legacy panel")

    def __init__(
        self,
        cfg: Config,
        on_save: Callable[[Config, set[str]], None],
        parent: QtWidgets.QWidget | None = None,
        on_restart: Optional[Callable[[], None]] = None,
    ) -> None:
        """`on_restart` (optional) relaunches Holler. When given, changing a
        setting in RESTART_KEYS offers a "Restart now" button that saves and
        then calls it; without it a saved restart-only change just says the
        change applies after the next launch."""
        super().__init__(parent)
        self.setWindowTitle("Settings" if _IS_MAC else "Holler Settings")
        self.setStyleSheet(_DARK_QSS)

        self._cfg = cfg
        self._on_save = on_save
        self._on_restart = on_restart
        self._fields: dict[str, QtWidgets.QWidget] = {}
        self._errors: dict[str, QtWidgets.QLabel] = {}
        self._parsers: dict[str, Callable[[object], object]] = {}
        self._headings: list[QtWidgets.QLabel] = []

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACE_XL, theme.SPACE_XL, theme.SPACE_XL, theme.SPACE_LG)
        outer.setSpacing(theme.SPACE_LG)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(theme.SPACE_LG)
        self._body = body

        # Section list. Keyboard: Tab to it, Up/Down moves between sections.
        self._nav = QtWidgets.QListWidget()
        self._nav.setObjectName("settingsNav")
        self._nav.setAccessibleName("Settings sections")
        self._nav.setFixedWidth(theme.SETTINGS_NAV_WIDTH)
        self._nav.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._nav.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._nav.setFrameShape(QtWidgets.QFrame.NoFrame)
        for name in self.SECTIONS:
            self._nav.addItem(name)
        self._nav.currentRowChanged.connect(self._on_nav_row)
        body.addWidget(self._nav)

        # Everything but the button row scrolls: at a large ui_scale the form
        # is taller than many screens, and a fixed-height dialog would squash
        # every control instead. Vertical scrolling only — the dialog's
        # minimum width (set below) always fits the content.
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setObjectName("settingsScroll")
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        content = QtWidgets.QWidget()
        content.setObjectName("settingsContent")
        content.setAutoFillBackground(False)
        layout = QtWidgets.QVBoxLayout(content)
        # Right margin keeps fields clear of the scrollbar when it shows.
        layout.setContentsMargins(0, 0, theme.SPACE_MD, 0)
        layout.setSpacing(theme.SPACE_LG)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        # macOS defaults to FieldsStayAtSizeHint, which left every field a
        # different (ragged) width; grow them to one column edge everywhere.
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.DontWrapRows)
        form.setSpacing(theme.SPACE_SM)
        self._form = form

        self._build_general(cfg)
        self._build_voice(cfg)
        self._build_capture(cfg)
        self._build_sessions(cfg)
        self._build_advanced(cfg)
        self._build_legacy(cfg)

        layout.addLayout(form)
        layout.addStretch(1)
        self._scroll.setWidget(content)
        body.addWidget(self._scroll, 1)
        outer.addLayout(body, 1)
        self._scroll.verticalScrollBar().valueChanged.connect(self._sync_nav_to_scroll)
        self._scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self._sync_nav_to_scroll())

        # Footer: restart note (+ Restart now when the runner can relaunch),
        # then Cancel / Save.
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(theme.SPACE_SM)
        self._restart_note = QtWidgets.QLabel()
        self._restart_note.setObjectName("restartNote")
        self._restart_note.setWordWrap(True)
        self._restart_note.setVisible(False)
        btn_row.addWidget(self._restart_note, 1)
        self._restart_btn: Optional[QtWidgets.QPushButton] = None
        if on_restart is not None:
            self._restart_btn = QtWidgets.QPushButton("Restart now")
            self._restart_btn.setToolTip("Save your settings and restart Holler to apply them")
            self._restart_btn.setVisible(False)
            self._restart_btn.clicked.connect(lambda: self._on_save_clicked(restart=True))
            btn_row.addWidget(self._restart_btn)
        else:
            btn_row.addStretch(0)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QtWidgets.QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(lambda: self._on_save_clicked())
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        outer.addLayout(btn_row)

        self._connect_change_tracking()
        self._nav.setCurrentRow(0)
        self._fit_to_screen(content, outer, btn_row)

    # ── form building ────────────────────────────────────────────────────────

    def _section(self, title: str) -> None:
        heading = QtWidgets.QLabel(title)
        heading.setObjectName("sectionHeading")
        heading.setAccessibleName(f"{title} section")
        if self._headings:
            # Air above every section but the first.
            heading.setContentsMargins(0, theme.SPACE_LG, 0, 0)
        self._headings.append(heading)
        self._form.addRow(heading)

    def _note(self, text: str, tooltip: str = "") -> QtWidgets.QLabel:
        note = QtWidgets.QLabel(text)
        note.setProperty("class", "note")
        note.setStyleSheet(_NOTE_STYLE)
        note.setWordWrap(True)
        if tooltip:
            note.setToolTip(tooltip)
        self._form.addRow("", note)
        return note

    def _row(
        self,
        key: str,
        label: str,
        widget: QtWidgets.QWidget,
        tip: str,
        field: "QtWidgets.QWidget | QtWidgets.QLayout | None" = None,
    ) -> QtWidgets.QLabel:
        """One labelled config row. `field` (default `widget`) is what sits in
        the form's field column — a row layout when the field has a button.
        The raw config key is in the tooltip; an inline error row (hidden
        until validation fails) follows."""
        full_tip = tip
        if key in RESTART_KEYS:
            full_tip += "\nApplies after Holler restarts."
        full_tip += f"\n\nConfig key: {key}"
        lab = QtWidgets.QLabel(label)
        lab.setToolTip(full_tip)
        lab.setBuddy(widget)
        if not isinstance(widget, QtWidgets.QAbstractButton):
            # (A check box's tooltip would sit on top of its own label text;
            # the row label carries it instead.)
            widget.setToolTip(full_tip)
        else:
            widget.setAccessibleDescription(tip)
        name = label.rstrip(":")
        if isinstance(widget, QtWidgets.QAbstractButton) and widget.isCheckable() and widget.text():
            # A check box's own text says what it does ("At launch: Open
            # the Sessions window…"), not just the row label.
            name = f"{name}: {widget.text()}"
        widget.setAccessibleName(name)
        self._form.addRow(lab, field if field is not None else widget)
        self._fields[key] = widget
        err = QtWidgets.QLabel()
        err.setProperty("class", "error")
        err.setStyleSheet(_ERROR_STYLE)
        err.setWordWrap(True)
        self._form.addRow("", err)
        self._form.setRowVisible(err, False)
        self._errors[key] = err
        return lab

    @staticmethod
    def _hbox(*widgets: QtWidgets.QWidget, stretch_first: bool = True) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(theme.SPACE_SM)
        for i, w in enumerate(widgets):
            row.addWidget(w, 1 if (i == 0 and stretch_first) else 0)
        return row

    def _combo(self, choices, value, *, editable: bool = False) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.setEditable(editable)
        for label, data in choices:
            combo.addItem(label, userData=data)
        _ensure_combo_value(combo, value)
        return combo

    def _build_general(self, cfg: Config) -> None:
        self._section("General")

        cwd_edit = QtWidgets.QLineEdit(str(cfg.repo_cwd))
        cwd_browse = QtWidgets.QPushButton("Choose…")
        cwd_browse.setAccessibleName("Choose project folder")
        cwd_browse.clicked.connect(lambda: self._browse_dir(cwd_edit))
        self._row("repo_cwd", "Project folder:", cwd_edit,
                  "The folder Claude reads and edits — usually your project's root.",
                  self._hbox(cwd_edit, cwd_browse))

        # Friendly "Opus 5  (claude-opus-5)" labels; a typed id that isn't
        # listed is kept as-is.
        model_combo = self._combo(
            [(_model_label(m), m) for m in effective_models(cfg)], cfg.claude_model, editable=True)
        self._row("claude_model", "Model:", model_combo,
                  "The model new sessions use. Click the model pill in the Sessions window to switch quickly.")

        hotkey_edit = QtWidgets.QLineEdit(cfg.hotkey)
        hotkey_edit.setPlaceholderText("right alt")
        self._hotkey_record = HotkeyRecordButton(hotkey_edit)
        self._row("hotkey", "Push-to-talk key:", hotkey_edit,
                  "Hold this key while you talk. One key only — click Record and press it, "
                  "or type a name such as “right alt” or “f12”.",
                  self._hbox(hotkey_edit, self._hotkey_record))
        self._hotkey_hint = self._note("")
        hotkey_edit.textChanged.connect(self._update_hotkey_hint)
        self._update_hotkey_hint(hotkey_edit.text())

        sw_hot_edit = QtWidgets.QLineEdit(cfg.sessions_window_hotkey)
        self._row("sessions_window_hotkey", "Show Sessions window:", sw_hot_edit,
                  "Global shortcut that shows or hides the Sessions window, e.g. “ctrl+shift+h”.")

        open_check = QtWidgets.QCheckBox("Open the Sessions window when Holler starts")
        open_check.setChecked(bool(cfg.open_sessions_on_launch))
        self._row("open_sessions_on_launch", "At launch:", open_check,
                  "Show the Sessions window on launch instead of starting hidden.")

        scale = getattr(cfg, "ui_scale", 0.0) or 0.0
        scales = list(_ui_scales())
        if not any(v == scale for _l, v in scales):
            # A value A−/A+ or a hand edit left between the steps: keep it.
            scales.append((f"{round(float(scale) * 100)}%", scale))
        scale_combo = self._combo(scales, scale)
        self._row("ui_scale", "Interface scale:", scale_combo,
                  "Size of all of Holler's text and controls. Auto picks 100% on a Retina display, "
                  "larger on a standard one. A− / A+ in the Sessions window change it too.")

    def _build_voice(self, cfg: Config) -> None:
        self._section("Voice & Mic")

        # --- microphone ---
        # Stores a device-name string ("" == follow the OS default input).
        # itemData holds the value, so the blank "System default" choice
        # round-trips as "" rather than its label.
        mic_combo = QtWidgets.QComboBox()
        # Device names can be long; don't let them set the dialog's minimum
        # width (the popup still lists them in full).
        mic_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        mic_combo.setMinimumContentsLength(20)
        mic_combo.addItem("System default (follows your headset)", userData="")
        try:
            # Rebuild PortAudio's device list first so a mic plugged in after
            # holler launched shows up here (PortAudio caches it otherwise).
            from holler.audio import refresh_devices
            refresh_devices()
            import sounddevice as sd
            for d in sd.query_devices():
                if d.get("max_input_channels", 0) > 0:
                    name = d["name"]
                    mic_combo.addItem(name, userData=name)
        except Exception:
            pass  # sounddevice unavailable — just the "System default" option
        want = cfg.mic_device or ""
        sel = next(
            (i for i in range(mic_combo.count()) if mic_combo.itemData(i) == want), -1
        )
        if sel < 0 and want:
            # Configured device isn't currently connected — keep it as an
            # option so saving the dialog doesn't silently drop the setting.
            mic_combo.addItem(f"{want} (not connected)", userData=want)
            sel = mic_combo.count() - 1
        mic_combo.setCurrentIndex(max(0, sel))
        self._row("mic_device", "Microphone:", mic_combo,
                  "The input Holler records from while you hold the push-to-talk key.")

        # --- mic test (live level meter for the selected device) ---
        self._mic_stream = None
        self._mic_peak_rms = 0.0
        # Auto-stop for a forgotten test. A member timer (not singleShot) so a
        # stale timeout from an earlier test can't cut a later one short.
        self._mic_auto_stop = QtCore.QTimer(self)
        self._mic_auto_stop.setSingleShot(True)
        self._mic_auto_stop.setInterval(4000)
        self._mic_auto_stop.timeout.connect(lambda: self._stop_mic_test(report=True))
        self._mic_test_btn = QtWidgets.QPushButton("Test mic")
        self._mic_test_btn.setAccessibleName("Test microphone")
        self._mic_test_btn.clicked.connect(self._toggle_mic_test)
        self._mic_meter = QtWidgets.QProgressBar()
        self._mic_meter.setRange(0, 100)
        self._mic_meter.setValue(0)
        self._mic_meter.setTextVisible(False)
        self._mic_meter.setFixedHeight(theme.SPACE_SM + theme.SPACE_XS)
        self._mic_meter.setAccessibleName("Mic level")
        self._mic_meter.setStyleSheet(
            f"QProgressBar {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_SM}px; }}"
            f"QProgressBar::chunk {{ background: {theme.SUCCESS}; border-radius: {theme.RADIUS_SM}px; }}"
        )
        test_row = QtWidgets.QHBoxLayout()
        test_row.setSpacing(theme.SPACE_SM)
        test_row.addWidget(self._mic_test_btn)
        test_row.addWidget(self._mic_meter, 1)
        self._form.addRow("", test_row)
        self._mic_test_status = self._note("Mic changes apply on the next push-to-talk.")
        self._mic_test_status.setAccessibleName("Mic test result")
        self._mic_level.connect(self._on_mic_level)

        # --- speech (whisper) ---
        # Editable so whisper_model can be a local CTranslate2 model directory
        # path, not just one of the preset size names that download from HF.
        # A configured value that isn't a preset survives the round-trip.
        ws_combo = self._combo(_WHISPER_MODELS, cfg.whisper_model, editable=True)
        self._row("whisper_model", "Speech model:", ws_combo,
                  "Speech-to-text model (Whisper). Bigger is more accurate but slower; you can also "
                  "type a local model folder.")
        wc_combo = self._combo(_WHISPER_COMPUTE, cfg.whisper_compute)
        self._row("whisper_compute", "Speech precision:", wc_combo,
                  "Numeric precision for speech-to-text. int8 is fastest on a CPU; float16 needs a GPU.")
        wd_combo = self._combo(_whisper_devices(), cfg.whisper_device)
        self._row("whisper_device", "Speech device:", wd_combo,
                  "Where speech-to-text runs. GPU needs an NVIDIA card with CUDA.")
        wl_edit = QtWidgets.QLineEdit(cfg.whisper_language)
        self._row("whisper_language", "Speech language:", wl_edit,
                  "Language code you speak in, such as “en” or “de”.")
        self._note("Speech settings apply after you restart Holler.")

        # --- voice commands help ---
        card = QtWidgets.QFrame()
        card.setObjectName("helpCard")
        card_lay = QtWidgets.QVBoxLayout(card)
        card_lay.setContentsMargins(theme.SPACE_MD, theme.SPACE_SM, theme.SPACE_MD, theme.SPACE_SM)
        card_lay.setSpacing(theme.SPACE_XS)
        card_title = QtWidgets.QLabel("Voice commands")
        card_title.setStyleSheet(
            f"color: {theme.FG}; font-size: {theme.FONT_SM}pt; font-weight: {theme.WEIGHT_SEMIBOLD};")
        card_body = QtWidgets.QLabel(_voice_commands_html())
        card_body.setObjectName("helpBody")
        card_body.setTextFormat(QtCore.Qt.RichText)
        card_body.setWordWrap(True)
        card_lay.addWidget(card_title)
        card_lay.addWidget(card_body)
        # Rich-text labels over-estimate their height-for-width; keep any
        # slack below the text, not split around it.
        card_lay.setAlignment(QtCore.Qt.AlignTop)
        card.setAccessibleName("Voice commands help")
        self._voice_help = card_body
        self._form.addRow("", card)

    def _build_capture(self, cfg: Config) -> None:
        self._section("Capture")

        edge_spin = QtWidgets.QSpinBox()
        edge_spin.setRange(640, 3840)
        edge_spin.setSingleStep(64)
        edge_spin.setSuffix(" px")
        _spin_with_value(edge_spin, cfg.max_screenshot_long_edge)
        self._row("max_screenshot_long_edge", "Max screenshot size:", edge_spin,
                  "Screenshots are scaled down so their longer side is at most this many pixels. "
                  "Larger is sharper but costs more to send.")

        stroke_color_edit = QtWidgets.QLineEdit(cfg.stroke_color)
        color_btn = QtWidgets.QPushButton("Choose…")
        color_btn.setAccessibleName("Choose pen color")
        color_btn.clicked.connect(lambda: self._pick_color(stroke_color_edit))
        self._color_btn = color_btn
        self._row("stroke_color", "Pen color:", stroke_color_edit,
                  "Color of what you draw on the screen while talking (hex, e.g. #FF2E2E).",
                  self._hbox(stroke_color_edit, color_btn))
        stroke_color_edit.textChanged.connect(self._update_color_swatch)
        self._update_color_swatch(stroke_color_edit.text())

        sw_spin = QtWidgets.QSpinBox()
        sw_spin.setRange(1, 24)
        sw_spin.setSuffix(" px")
        _spin_with_value(sw_spin, cfg.stroke_width)
        self._row("stroke_width", "Pen width:", sw_spin, "Thickness of the pen.")

        # Path on its own wrapping hint line (not in the checkbox label, which
        # ran off the dialog's edge) — the real per-OS location, not a
        # Windows-only %LOCALAPPDATA% string.
        debug_check = QtWidgets.QCheckBox("Keep a copy of every annotated screenshot")
        debug_check.setChecked(cfg.debug_save_captures)
        self._row("debug_save_captures", "Save screenshots:", debug_check,
                  "Save each screenshot you send, with your drawing on it.")
        captures_dir = getattr(cfg, "captures_dir", None) or self._app_data_subdir("captures")
        self._captures_note = self._note(
            f"Saved to {display_path(captures_dir)}", tooltip=str(captures_dir))

    def _build_sessions(self, cfg: Config) -> None:
        self._section("Sessions")

        archive_spin = QtWidgets.QSpinBox()
        archive_spin.setRange(1, 365)
        archive_spin.setSuffix(" days")
        _spin_with_value(archive_spin, cfg.auto_archive_days)
        self._row("auto_archive_days", "Archive idle sessions after:", archive_spin,
                  "Sessions untouched this long move to Archived (you can unarchive them).")

        purge_spin = QtWidgets.QSpinBox()
        purge_spin.setRange(0, 365)
        purge_spin.setSuffix(" days")
        purge_spin.setSpecialValueText("Never")  # 0 = disabled
        _spin_with_value(purge_spin, cfg.purge_reviewed_days)
        self._row("purge_reviewed_days", "Delete Done sessions after:", purge_spin,
                  "Permanently delete sessions this long after you mark them Done — transcript and "
                  "screenshots included. Pinned and running sessions are kept. Never keeps them forever.")

        cap_spin = QtWidgets.QSpinBox()
        cap_spin.setRange(1, 64)
        _spin_with_value(cap_spin, cfg.concurrency_cap)
        self._row("concurrency_cap", "Max running sessions:", cap_spin,
                  "How many sessions can run a turn at the same time; the rest wait in a queue.")

        wt_check = QtWidgets.QCheckBox("Give each session its own git worktree")
        wt_check.setChecked(bool(cfg.per_session_worktrees))
        self._row("per_session_worktrees", "Isolation:", wt_check,
                  "Each session works on its own branch in its own checkout, so parallel sessions "
                  "can't commit over each other. Recommended.")

        # --- session profiles ---
        open_prof_btn = QtWidgets.QPushButton("Open profiles folder…")
        open_prof_btn.clicked.connect(self._open_profiles_folder)
        prof_row = QtWidgets.QHBoxLayout()
        prof_row.addWidget(open_prof_btn)
        prof_row.addStretch(1)
        prof_label = QtWidgets.QLabel("Session profiles:")
        self._form.addRow(prof_label, prof_row)
        profiles_dir = self._app_data_subdir("profiles")
        self._prof_note = self._note(
            f"Add Markdown files to {display_path(profiles_dir)} to create or override "
            "profiles. They appear the next time you start a new session.",
            tooltip=str(profiles_dir),
        )

    def _build_advanced(self, cfg: Config) -> None:
        self._section("Advanced")

        sup_check = QtWidgets.QCheckBox("Enable the Superintendent evidence inbox")
        sup_check.setChecked(bool(cfg.superintendent_enabled))
        self._row("superintendent_enabled", "Superintendent:", sup_check,
                  "An inbox session: captures you send there start autonomous worker sessions that "
                  "fix, test and deploy. Its workers may push and deploy without asking.")

        models = effective_models(cfg)
        sup_model = self._combo(
            [("Same as Model", "")] + [(_model_label(m), m) for m in models],
            cfg.superintendent_model or "", editable=True)
        self._row("superintendent_model", "Worker model:", sup_model,
                  "Model the Superintendent's worker sessions run on. Applies to the next worker.")

        sup_workers = QtWidgets.QSpinBox()
        sup_workers.setRange(1, 50)
        _spin_with_value(sup_workers, cfg.superintendent_max_workers)
        self._row("superintendent_max_workers", "Max workers:", sup_workers,
                  "How many Superintendent workers run at once. Applies within a few seconds.")

        models_edit = QtWidgets.QLineEdit(", ".join(cfg.claude_models or []))
        models_edit.setPlaceholderText("Automatic — every model available to you")
        self._row("claude_models", "Model list:", models_edit,
                  "Optional. Comma-separated model ids to list first in the Model menu and the "
                  "model pill's cycle, in this order.")
        self._parsers["claude_models"] = _parse_model_list

        self._models_refresh_btn = QtWidgets.QPushButton("Check for new models")
        self._models_refresh_btn.setAccessibleName("Check for new models")
        self._models_refresh_btn.clicked.connect(self._start_model_refresh)
        refresh_row = QtWidgets.QHBoxLayout()
        refresh_row.setSpacing(theme.SPACE_SM)
        refresh_row.addWidget(self._models_refresh_btn)
        refresh_row.addStretch(1)
        self._form.addRow("", refresh_row)
        self._models_status = self._note(_models_checked_text())
        self._models_status.setAccessibleName("Model list status")
        self._models_refreshed.connect(self._on_models_refreshed)

    def _start_model_refresh(self) -> None:
        """Discovery otherwise runs only on a startup daemon thread, so a Holler
        left open for days never notices a newly released model. `refresh()` can
        block for the API timeout, so it runs off the GUI thread."""
        self._models_refresh_btn.setEnabled(False)
        self._models_status.setText("Checking for new models…")

        def work() -> None:
            from holler import model_discovery
            ids, err = model_discovery.refresh()
            self._models_refreshed.emit(list(ids), err or "")

        threading.Thread(target=work, name="settings-model-refresh", daemon=True).start()

    def _on_models_refreshed(self, ids: list, error: str) -> None:
        self._models_refresh_btn.setEnabled(True)
        if error:
            self._models_status.setText(f"Couldn't check for models: {error}")
            return
        added = self._merge_discovered_models(ids)
        self._models_status.setText(
            _models_checked_text() + (f" Added {added}." if added else " Nothing new.")
        )

    def _merge_discovered_models(self, ids: list) -> int:
        """List ids the model combos don't offer yet, leaving the operator's
        current choice (and anything they typed) untouched."""
        new: set[str] = set()
        for key in ("claude_model", "superintendent_model"):
            combo = self._fields.get(key)
            if not isinstance(combo, QtWidgets.QComboBox):
                continue
            known = {combo.itemData(i) for i in range(combo.count())}
            for mid in ids:
                if mid and mid not in known:
                    combo.addItem(_model_label(mid), userData=mid)
                    new.add(mid)
        return len(new)

    def _build_legacy(self, cfg: Config) -> None:
        # The floating panel is currently never shown, so these have no
        # visible effect. Kept (same config keys, same round-trip) so the
        # settings survive until the panel returns.
        self._section("Legacy floating panel")
        self._note("The floating panel is currently hidden; these settings take effect only if it returns.")

        corner_combo = self._combo(_PANEL_CORNERS, cfg.panel_corner)
        self._row("panel_corner", "Panel corner:", corner_combo,
                  "Screen corner the panel opens in the first time.")

        width_spin = QtWidgets.QSpinBox()
        width_spin.setRange(280, 1200)
        width_spin.setSingleStep(10)
        width_spin.setSuffix(" px")
        _spin_with_value(width_spin, cfg.panel_width)
        self._row("panel_width", "Panel width:", width_spin, "Width of the floating panel.")

        mp_max_spin = QtWidgets.QSpinBox()
        mp_max_spin.setRange(3, 50)
        _spin_with_value(mp_max_spin, cfg.mini_panel_max_sessions)
        self._row("mini_panel_max_sessions", "Sessions listed:", mp_max_spin,
                  "Most sessions the mini panel lists.")

    # ── small live helpers ───────────────────────────────────────────────────

    def _update_hotkey_hint(self, text: str) -> None:
        err = ptt_key_error(text)
        if err:
            self._hotkey_hint.setText("")
            self._form.setRowVisible(self._hotkey_hint, False)
            self._set_error("hotkey", err)
            return
        self._set_error("hotkey", None)
        self._hotkey_hint.setText(f"{key_label(text)} — hold it while you talk.")
        self._form.setRowVisible(self._hotkey_hint, True)

    def _update_color_swatch(self, text: str) -> None:
        c = QtGui.QColor((text or "").strip())
        if stroke_color_error(text) is None and c.isValid():
            pm = QtGui.QPixmap(theme.SPACE_MD, theme.SPACE_MD)
            pm.fill(c)
            self._color_btn.setIcon(QtGui.QIcon(pm))
        else:
            self._color_btn.setIcon(QtGui.QIcon())

    def _set_error(self, key: str, message: Optional[str]) -> None:
        err = self._errors.get(key)
        if err is None:
            return
        err.setText(message or "")
        self._form.setRowVisible(err, bool(message))
        field = self._fields.get(key)
        if field is not None:
            field.setAccessibleDescription(message or "")

    def _validate(self) -> dict[str, str]:
        """Field key → error message for every invalid field (inline, not modal)."""
        errors: dict[str, str] = {}
        raw_cwd = str(self._read_value("repo_cwd"))
        cwd = Path(raw_cwd)
        if not raw_cwd:
            errors["repo_cwd"] = "Choose a project folder."
        elif not cwd.is_dir():
            errors["repo_cwd"] = f"“{cwd}” doesn't exist. Choose another project folder."
        hk_err = ptt_key_error(str(self._read_value("hotkey")))
        if hk_err:
            errors["hotkey"] = hk_err
        sc_err = stroke_color_error(str(self._read_value("stroke_color")))
        if sc_err:
            errors["stroke_color"] = sc_err
        return errors

    def _show_errors(self, errors: dict[str, str]) -> None:
        for key in ("repo_cwd", "hotkey", "stroke_color"):
            self._set_error(key, errors.get(key))
        if not errors:
            return
        first = next(k for k in self._fields if k in errors)
        widget = self._fields[first]
        self._scroll.ensureWidgetVisible(widget, 0, theme.SPACE_XL * 2)
        widget.setFocus(QtCore.Qt.OtherFocusReason)

    # ── change tracking (restart prompt) ─────────────────────────────────────

    def _connect_change_tracking(self) -> None:
        for w in self._fields.values():
            if isinstance(w, QtWidgets.QLineEdit):
                w.textChanged.connect(self._on_field_changed)
            elif isinstance(w, QtWidgets.QComboBox):
                w.currentIndexChanged.connect(self._on_field_changed)
                if w.isEditable():
                    w.editTextChanged.connect(self._on_field_changed)
            elif isinstance(w, QtWidgets.QSpinBox):
                w.valueChanged.connect(self._on_field_changed)
            elif isinstance(w, QtWidgets.QCheckBox):
                w.toggled.connect(self._on_field_changed)
        for key in ("repo_cwd", "stroke_color"):
            w = self._fields[key]
            # Clear a shown error as soon as the field is fixed.
            w.textChanged.connect(lambda _t, k=key: self._recheck_error(k))

    def _recheck_error(self, key: str) -> None:
        if self._errors[key].text():
            self._set_error(key, self._validate().get(key))

    def _pending_restart_labels(self) -> list[str]:
        out = []
        for key, label in RESTART_KEYS.items():
            if key in self._fields and self._read_value(key) != getattr(self._cfg, key):
                out.append(label)
        return out

    def _on_field_changed(self, *_a) -> None:
        labels = self._pending_restart_labels()
        if labels:
            self._restart_note.setText("Applies after restart: " + ", ".join(labels) + ".")
        self._restart_note.setVisible(bool(labels))
        if self._restart_btn is not None:
            self._restart_btn.setVisible(bool(labels))

    # ── section navigation ───────────────────────────────────────────────────

    def _on_nav_row(self, row: int) -> None:
        if getattr(self, "_syncing_nav", False) or not (0 <= row < len(self._headings)):
            return
        heading = self._headings[row]
        content = self._scroll.widget()
        if content is None:
            return
        y = heading.mapTo(content, QtCore.QPoint(0, 0)).y()
        self._syncing_nav = True
        try:
            self._scroll.verticalScrollBar().setValue(max(0, y - theme.SPACE_XS))
        finally:
            self._syncing_nav = False

    def _sync_nav_to_scroll(self, *_a) -> None:
        if getattr(self, "_syncing_nav", False) or not self._headings:
            return
        bar = self._scroll.verticalScrollBar()
        content = self._scroll.widget()
        if content is None:
            return
        top = bar.value() + theme.SPACE_XL
        row = 0
        for i, h in enumerate(self._headings):
            if h.mapTo(content, QtCore.QPoint(0, 0)).y() <= top:
                row = i
        if bar.maximum() > 0 and bar.value() >= bar.maximum():
            # Bottom reached: a short last section never reaches the top.
            row = len(self._headings) - 1
        self._syncing_nav = True
        try:
            self._nav.setCurrentRow(row)
        finally:
            self._syncing_nav = False

    def _app_data_subdir(self, name: str) -> Path:
        from holler import appdir
        return appdir.app_data_dir() / name

    def _target_screen(self) -> "QtGui.QScreen | None":
        """The screen the dialog will open on: its parent's, else the one
        under the cursor, else the primary."""
        parent = self.parentWidget()
        screen = parent.screen() if parent is not None else None
        if screen is None:
            screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
        return screen or QtGui.QGuiApplication.primaryScreen()

    def _fit_to_screen(
        self,
        content: QtWidgets.QWidget,
        outer: QtWidgets.QVBoxLayout,
        btn_row: QtWidgets.QHBoxLayout,
    ) -> None:
        """Size the dialog to show all content without scrolling, capped to
        the available screen area (the rest scrolls)."""
        # Size hints are only accurate once the style sheet has been applied;
        # polish now and drop the layouts' cached (pre-polish) hints.
        self.ensurePolished()
        for w in content.findChildren(QtWidgets.QWidget):
            w.ensurePolished()
        for lay in content.findChildren(QtWidgets.QLayout):
            lay.invalidate()
        if content.layout() is not None:
            content.layout().invalidate()
        m = outer.contentsMargins()
        bar = self._scroll.verticalScrollBar().sizeHint().width()
        nav_w = self._nav.width() + self._body.spacing()
        chrome_w = m.left() + m.right() + bar + nav_w
        chrome_h = m.top() + m.bottom() + outer.spacing() + btn_row.sizeHint().height()
        # Width: never narrower than the content's minimum (there is no
        # horizontal scrollbar, so anything narrower would clip fields).
        min_w = max(theme.SETTINGS_MIN_WIDTH, content.minimumSizeHint().width() + chrome_w)
        want_w = max(min_w, content.sizeHint().width() + chrome_w)
        # Word-wrapped notes make the content's height depend on its width.
        content_w = want_w - chrome_w
        lay = content.layout()
        content_h = content.sizeHint().height()
        if lay is not None and lay.hasHeightForWidth():
            content_h = max(content_h, lay.totalHeightForWidth(content_w))
        want_h = content_h + chrome_h
        min_h = 360
        screen = self._target_screen()
        if screen is not None:
            avail = screen.availableGeometry()
            # Leave room for the window frame / title bar.
            max_w = max(min_w, avail.width() - 40)
            max_h = max(200, avail.height() - 60)
            want_w = min(want_w, max_w)
            want_h = min(want_h, max_h)
            min_h = min(min_h, max_h)
        self.setMinimumSize(min_w, min_h)
        self.resize(want_w, max(want_h, min_h))

    def _open_profiles_folder(self) -> None:
        try:
            from holler.profiles import ensure_user_dir, user_dir
            ensure_user_dir()
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(user_dir())))
        except Exception:
            import logging
            logging.exception("failed to open profiles folder")

    # --- mic test ---
    def _toggle_mic_test(self) -> None:
        if self._mic_stream is not None:
            self._stop_mic_test(report=True)
            return

        # macOS permission status up front — a denied grant is the likeliest
        # reason a test would show a flat meter.
        try:
            from holler import mac_permissions as macperm
            if macperm.is_macos() and macperm.is_denied():
                self._mic_test_status.setText(
                    "macOS is blocking Holler's mic. Allow it in System Settings → "
                    "Privacy & Security → Microphone, then reopen Holler."
                )
                macperm.open_mic_privacy_settings()
                return
        except Exception:
            pass

        device = self._fields["mic_device"].currentData() or None
        try:
            import numpy as np
            import sounddevice as sd
            from holler.audio import refresh_devices
            refresh_devices()  # pick up a mic plugged in since the dialog opened
        except Exception:
            import logging
            logging.exception("mic test: audio backend unavailable")
            self._mic_test_status.setText(
                "Can't access audio devices. Restart Holler; if it keeps happening, check the log."
            )
            return

        self._mic_peak_rms = 0.0

        def _cb(indata, _frames, _t, _status) -> None:
            try:
                rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
                self._mic_peak_rms = max(self._mic_peak_rms, rms)
                self._mic_level.emit(min(1.0, rms * 6.0))
            except Exception:
                pass

        stream = None
        try:
            stream = sd.InputStream(
                samplerate=16_000, channels=1, dtype="float32",
                callback=_cb, blocksize=0, device=device,
            )
            stream.start()
            self._mic_stream = stream
        except Exception as e:
            if stream is not None:
                # Opened but failed to start — close it so it doesn't leak.
                try:
                    stream.close()
                except Exception:
                    pass
            self._mic_stream = None
            self._mic_meter.setValue(0)
            code = getattr(e, "args", (None, None))
            is_perm = len(code) >= 2 and code[1] == -9986
            import logging
            logging.warning("mic test: couldn't open %r: %r", device, e)
            name = device or "the default mic"
            self._mic_test_status.setText(
                "macOS is blocking the mic. Allow Holler in System Settings → Privacy & "
                "Security → Microphone." if is_perm
                else f"Couldn't open “{name}”. Reconnect it or pick another mic."
            )
            return

        self._mic_test_btn.setText("Stop test")
        self._mic_test_status.setText("Listening — say something…")
        # Auto-stop after 4s so a forgotten test doesn't hold the device.
        self._mic_auto_stop.start()

    def _on_mic_level(self, level: float) -> None:
        self._mic_meter.setValue(int(level * 100))

    def _stop_mic_test(self, report: bool = False) -> None:
        self._mic_auto_stop.stop()
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        self._mic_meter.setValue(0)
        self._mic_test_btn.setText("Test mic")
        if report:
            peak = self._mic_peak_rms
            if peak < SILENT_RMS:
                self._mic_test_status.setText(
                    "✗ No sound — wrong mic, muted, or blocked by macOS." if _IS_MAC
                    else "✗ No sound — wrong mic or muted."
                )
            elif peak < LOW_RMS:
                self._mic_test_status.setText("! Very quiet — move closer or raise the input level.")
            else:
                self._mic_test_status.setText("✓ Mic works.")
            self._mic_test_status.setToolTip(f"Peak level (RMS): {peak:.4f}")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._stop_mic_test(report=False)
        super().closeEvent(event)

    def done(self, result: int) -> None:  # Qt override: accept()/reject()/Esc
        # closeEvent isn't delivered on accept()/reject(), so a running mic
        # test would otherwise keep the input stream open after Save/Cancel.
        self._stop_mic_test(report=False)
        self._hotkey_record.stop()
        super().done(result)

    def _browse_dir(self, edit: QtWidgets.QLineEdit) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose project folder", edit.text() or str(Path.home())
        )
        if chosen:
            edit.setText(chosen)

    def _pick_color(self, edit: QtWidgets.QLineEdit) -> None:
        initial = QtGui.QColor(edit.text() or "#FF2E2E")
        chosen = QtWidgets.QColorDialog.getColor(initial, self, "Pen color")
        if chosen.isValid():
            edit.setText(chosen.name().upper())

    # --- save ---

    def _read_value(self, key: str):
        # Config fields that this dialog doesn't surface a widget for pass
        # through unchanged — the Save handler iterates every Config field, so
        # a missing widget must not crash it.
        widget = self._fields.get(key)
        if widget is None:
            return getattr(self._cfg, key)
        if isinstance(widget, QtWidgets.QLineEdit):
            value = widget.text().strip()
        elif isinstance(widget, QtWidgets.QComboBox):
            # Combos carry their real value in itemData so e.g. the blank
            # "System default" mic or "Same as Model" round-trips as "".
            value = _combo_value(widget)
            if isinstance(value, str):
                value = value.strip()
        elif isinstance(widget, QtWidgets.QSpinBox):
            value = int(widget.value())
        elif isinstance(widget, QtWidgets.QCheckBox):
            value = bool(widget.isChecked())
        else:
            raise RuntimeError(f"unhandled widget for {key}")
        parser = self._parsers.get(key)
        return parser(value) if parser is not None else value

    def _on_save_clicked(self, restart: bool = False) -> None:
        errors = self._validate()
        self._show_errors(errors)
        if errors:
            return

        # Build a new Config from current values + non-init derived fields.
        kwargs: dict[str, object] = {}
        for f in dataclasses.fields(Config):
            if not f.init:
                continue
            v = self._read_value(f.name)
            if f.name == "repo_cwd":
                v = Path(str(v))
            kwargs[f.name] = v

        new_cfg = Config(**kwargs)  # type: ignore[arg-type]

        # diff
        changed: set[str] = set()
        for f in dataclasses.fields(Config):
            if not f.init:
                continue
            old = getattr(self._cfg, f.name)
            new = getattr(new_cfg, f.name)
            if isinstance(old, Path) or isinstance(new, Path):
                if str(old) != str(new):
                    changed.add(f.name)
            elif old != new:
                changed.add(f.name)

        try:
            save_config(new_cfg)
        except Exception:
            import logging
            logging.exception("settings save failed")
            from holler.config import _config_path_default
            QtWidgets.QMessageBox.critical(
                self, "Couldn't save settings",
                f"Holler couldn't write the settings file. Check that Holler can "
                f"write to {_config_path_default()}, then try again.",
            )
            return

        try:
            self._on_save(new_cfg, changed)
        except Exception:
            import logging
            logging.exception("hot-apply of settings failed")
            QtWidgets.QMessageBox.warning(
                self, "Restart to apply settings",
                "Holler saved your settings but couldn't apply them yet. "
                "Restart Holler to use them.",
            )

        restart_labels = [RESTART_KEYS[k] for k in RESTART_KEYS if k in changed]
        if restart and self._on_restart is not None:
            self.accept()
            # After exec() returns, so the relaunch (and any "sessions still
            # running" confirm) isn't nested inside this dialog.
            # No context object: the dialog is usually deleted right after
            # exec() returns, which would cancel the restart.
            QtCore.QTimer.singleShot(0, self._on_restart)
            return
        if restart_labels and self._on_restart is None:
            QtWidgets.QMessageBox.information(
                self, "Restart to apply",
                "These settings apply after you restart Holler: "
                + ", ".join(restart_labels) + ".",
            )

        self.accept()
