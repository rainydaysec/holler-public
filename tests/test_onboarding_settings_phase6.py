"""Phase 6 — sectioned Settings, stepped first run, push-to-talk key recorder.

sounddevice, mac_permissions and config writes are always stubbed; nothing
touches the real app-data folder or config.toml."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from holler.config import Config, load


@pytest.fixture
def cfg(tmp_path):
    return dataclasses.replace(load(tmp_path / "none.toml"), repo_cwd=tmp_path)


@pytest.fixture
def env(app, monkeypatch, tmp_path):
    import holler.first_run as fr
    import holler.settings_dialog as sd

    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setattr("holler.appdir.app_data_dir", lambda: appdata)
    monkeypatch.setattr("holler.audio.refresh_devices", lambda: True)
    monkeypatch.setattr("sounddevice.query_devices", lambda *a, **k: [])
    monkeypatch.setattr(sd, "effective_models", lambda c: ["claude-fable-5", "claude-opus-5"])
    monkeypatch.setattr(fr, "effective_models", lambda c: ["claude-fable-5", "claude-opus-5"])
    monkeypatch.setattr(fr, "find_claude_cli", lambda: None)
    saved: list[Config] = []
    monkeypatch.setattr(sd, "save_config", lambda c: saved.append(c))
    monkeypatch.setattr(fr, "save_config", lambda c: saved.append(c))
    modal: list[str] = []
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _n=name, **k: modal.append(_n)))
    return sd, fr, saved, modal


def _pump(ms: int = 30) -> None:
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _init_fields() -> list[str]:
    return [f.name for f in dataclasses.fields(Config) if f.init]


# ── Settings: every field surfaced and round-tripped ────────────────────────

def _odd_config(cfg, tmp_path) -> Config:
    """Non-default value in every field, including off-list ones."""
    return dataclasses.replace(
        cfg, repo_cwd=tmp_path, claude_model="claude-opus-9-9", hotkey="f13",
        mic_device="Scarlett", ui_scale=1.33, whisper_model="/models/ct2",
        whisper_compute="auto", whisper_device="mps", whisper_language="de",
        panel_corner="center", panel_width=1999, max_screenshot_long_edge=5000,
        stroke_color="#00ff00", stroke_width=30, debug_save_captures=False,
        sessions_window_hotkey="ctrl+alt+j", auto_archive_days=400,
        purge_reviewed_days=0, mini_panel_max_sessions=99,
        open_sessions_on_launch=False, concurrency_cap=99,
        per_session_worktrees=False, superintendent_enabled=False,
        superintendent_max_workers=77, superintendent_model="claude-haiku-4-5-20251001",
        claude_models=["claude-opus-5", "claude-x-1"],
        projects={"canvas": "/x/canvas"}, profile_projects={"superintendent": "canvas"},
    )


# Edited in config.toml only, still round-tripped unchanged by Save.
#   projects / profile_projects — structured tables, no Settings UI yet (adding
#     one is a visual change of its own).
#   superintendent_ship_policy — the ship-safety switch the deploy gate reads
#     straight from config.toml. Deliberately not one click away in a dialog:
#     flipping it changes whether autonomous workers may reach production.
_CONFIG_TOML_ONLY = {"projects", "profile_projects", "superintendent_ship_policy"}


def test_every_config_field_has_a_settings_control(env, cfg):
    sd, *_ = env
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    assert set(d._fields) == set(_init_fields()) - _CONFIG_TOML_ONLY
    d.reject()


@pytest.mark.parametrize("which", ["defaults", "odd"])
def test_every_config_field_round_trips_unchanged(env, cfg, tmp_path, which):
    sd, _fr, saved, modal = env
    src = cfg if which == "defaults" else _odd_config(cfg, tmp_path)
    got: list[set] = []
    d = sd.SettingsDialog(src, on_save=lambda c, ch: got.append(ch))
    for key in _init_fields():
        assert d._read_value(key) == getattr(src, key) or (
            key == "repo_cwd" and Path(d._read_value(key)) == src.repo_cwd), key
    d._on_save_clicked()
    assert len(saved) == 1 and got == [set()] and not modal
    for key in _init_fields():
        assert getattr(saved[0], key) == getattr(src, key), key


def test_new_fields_edit_and_save(env, cfg):
    sd, _fr, saved, _ = env
    changes: list[set] = []
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: changes.append(ch))
    f = d._fields
    f["per_session_worktrees"].setChecked(not cfg.per_session_worktrees)
    f["superintendent_enabled"].setChecked(not cfg.superintendent_enabled)
    f["open_sessions_on_launch"].setChecked(not cfg.open_sessions_on_launch)
    f["concurrency_cap"].setValue(3)
    f["superintendent_max_workers"].setValue(4)
    sup = f["superintendent_model"]
    sup.setCurrentIndex(sup.findData("claude-opus-5"))
    scale = f["ui_scale"]
    scale.setCurrentIndex(scale.findData(1.4))
    f["claude_models"].setText(" claude-opus-5 , claude-fable-5,,claude-opus-5")
    d._on_save_clicked()
    s = saved[-1]
    assert s.per_session_worktrees is (not cfg.per_session_worktrees)
    assert s.superintendent_enabled is (not cfg.superintendent_enabled)
    assert s.open_sessions_on_launch is (not cfg.open_sessions_on_launch)
    assert (s.concurrency_cap, s.superintendent_max_workers) == (3, 4)
    assert s.superintendent_model == "claude-opus-5"
    assert s.ui_scale == 1.4
    assert s.claude_models == ["claude-opus-5", "claude-fable-5"]
    assert {"ui_scale", "claude_models", "concurrency_cap"} <= changes[-1]


def test_friendly_labels_keep_raw_values(env, cfg):
    sd, _fr, saved, _ = env
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    ws = d._fields["whisper_model"]
    ws.setCurrentIndex(ws.findData("medium"))
    assert ws.currentText() == "Accurate (medium)"
    assert d._read_value("whisper_model") == "medium"
    ws.setEditText("/opt/models/whisper-ct2")  # a typed local model folder
    assert d._read_value("whisper_model") == "/opt/models/whisper-ct2"
    ws.setEditText("medium")  # a typed raw preset name works too
    assert d._read_value("whisper_model") == "medium"
    # The raw config key is discoverable from every field's tooltip.
    form = d._form
    for key, w in d._fields.items():
        label = form.labelForField(w) or next(
            form.itemAt(r, QtWidgets.QFormLayout.LabelRole).widget()
            for r in range(form.rowCount())
            if (it := form.itemAt(r, QtWidgets.QFormLayout.FieldRole)) is not None
            and it.layout() is not None
            and any(it.layout().itemAt(i).widget() is w for i in range(it.layout().count())))
        assert f"Config key: {key}" in label.toolTip(), key
    d.reject()


def test_speech_device_choices_follow_platform(monkeypatch):
    import holler.settings_dialog as sd
    monkeypatch.setattr(sd, "_IS_MAC", True)
    assert [v for _l, v in sd._whisper_devices()] == ["cpu"]
    monkeypatch.setattr(sd, "_IS_MAC", False)
    assert [v for _l, v in sd._whisper_devices()] == ["cuda", "cpu"]


def test_mac_config_with_cuda_keeps_it_selectable(env, cfg, monkeypatch):
    sd, *_ = env
    monkeypatch.setattr(sd, "_IS_MAC", True)
    d = sd.SettingsDialog(dataclasses.replace(cfg, whisper_device="cuda"), on_save=lambda c, ch: None)
    assert d._read_value("whisper_device") == "cuda"
    d.reject()


# ── Settings: inline validation ─────────────────────────────────────────────

def test_missing_project_folder_is_an_inline_error(env, cfg, tmp_path):
    sd, _fr, saved, modal = env
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    d._fields["repo_cwd"].setText(str(tmp_path / "nope"))
    d._on_save_clicked()
    assert saved == [] and modal == []
    assert "doesn't exist" in d._errors["repo_cwd"].text()
    d._fields["repo_cwd"].setText("")
    d._on_save_clicked()
    assert saved == [] and d._errors["repo_cwd"].text()
    d._fields["repo_cwd"].setText(str(tmp_path))
    assert d._errors["repo_cwd"].text() == ""
    d._on_save_clicked()
    assert len(saved) == 1


def test_hotkey_hint_and_live_error(env, cfg, monkeypatch):
    sd, *_ = env
    monkeypatch.setattr(sd, "_IS_MAC", True)
    d = sd.SettingsDialog(dataclasses.replace(cfg, hotkey="right alt"), on_save=lambda c, ch: None)
    assert "Right ⌥ Option" in d._hotkey_hint.text()
    d._fields["hotkey"].setText("ctrl+space")
    assert "single key" in d._errors["hotkey"].text()
    d._fields["hotkey"].setText("f12")
    assert d._errors["hotkey"].text() == "" and "F12" in d._hotkey_hint.text()
    d.reject()


# ── Settings: restart prompt ────────────────────────────────────────────────

def test_restart_now_saves_then_calls_callback(env, cfg):
    sd, _fr, saved, modal = env
    restarts: list[int] = []
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None, on_restart=lambda: restarts.append(1))
    assert d._restart_btn is not None and d._restart_btn.isHidden()
    ws = d._fields["whisper_model"]
    ws.setCurrentIndex(ws.findData("tiny"))
    assert not d._restart_btn.isHidden()
    assert "Speech model" in d._restart_note.text()
    ws.setCurrentIndex(ws.findData(cfg.whisper_model))  # back to unchanged → hidden
    assert d._restart_btn.isHidden()
    ws.setCurrentIndex(ws.findData("tiny"))
    d._restart_btn.click()
    assert len(saved) == 1 and saved[0].whisper_model == "tiny"
    assert d.result() == QtWidgets.QDialog.Accepted
    _pump()
    assert restarts == [1] and modal == []


def test_without_restart_callback_keeps_the_restart_message(env, cfg):
    sd, _fr, saved, modal = env
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    assert d._restart_btn is None
    d._fields["concurrency_cap"].setValue(cfg.concurrency_cap + 1)
    assert "Max running sessions" in d._restart_note.text()
    d._on_save_clicked()
    assert len(saved) == 1 and modal == ["information"]


def test_hot_settings_do_not_ask_for_restart(env, cfg):
    sd, _fr, saved, modal = env
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None, on_restart=lambda: None)
    d._fields["stroke_width"].setValue(cfg.stroke_width + 1)
    assert d._restart_btn.isHidden()
    d._on_save_clicked()
    assert len(saved) == 1 and modal == []


# ── Settings: sections + fit ────────────────────────────────────────────────

class _FakeScreen:
    def __init__(self, w, h):
        self._rect = QtCore.QRect(0, 0, w, h)

    def availableGeometry(self):
        return self._rect


def test_section_nav_scrolls_and_follows_scroll(env, cfg, monkeypatch):
    sd, *_ = env
    monkeypatch.setattr(sd.SettingsDialog, "_target_screen", lambda self: _FakeScreen(1400, 560))
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    d.show()
    _pump()
    assert [d._nav.item(i).text() for i in range(d._nav.count())] == list(sd.SettingsDialog.SECTIONS)
    bar = d._scroll.verticalScrollBar()
    assert bar.value() == 0 and d._nav.currentRow() == 0
    d._nav.setCurrentRow(2)  # Capture
    assert bar.value() > 0
    content = d._scroll.widget()
    cap_y = d._headings[2].mapTo(content, QtCore.QPoint()).y()
    assert abs(bar.value() - cap_y) <= 8 or bar.value() == bar.maximum()
    bar.setValue(bar.maximum())
    assert d._nav.currentRow() == len(sd.SettingsDialog.SECTIONS) - 1
    bar.setValue(0)
    assert d._nav.currentRow() == 0
    d.reject()


def test_settings_usable_at_1_4_on_a_798pt_screen(env, cfg, monkeypatch):
    """ui_scale 1.4 on a screen with 798 pt of usable height: ~570 logical px."""
    sd, *_ = env
    screen = _FakeScreen(int(1280 / 1.4), int(798 / 1.4))
    monkeypatch.setattr(sd.SettingsDialog, "_target_screen", lambda self: screen)
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    assert d.height() <= screen.availableGeometry().height()
    assert d.width() <= screen.availableGeometry().width()
    assert d.minimumHeight() <= d.height() and d.minimumWidth() <= d.width()
    d.show()
    _pump()
    # Footer buttons inside the dialog, every section reachable by scrolling.
    for b in d.findChildren(QtWidgets.QPushButton):
        if b.text() in ("Save", "Cancel"):
            assert b.mapTo(d, b.rect().bottomRight()).y() <= d.height()
    bar = d._scroll.verticalScrollBar()
    assert bar.maximum() > 0
    d._nav.setCurrentRow(d._nav.count() - 1)
    last = d._fields["mini_panel_max_sessions"]
    top = last.mapTo(d._scroll.viewport(), QtCore.QPoint()).y()
    assert 0 <= top and top + last.height() <= d._scroll.viewport().height()
    d.reject()


# ── push-to-talk key recorder ───────────────────────────────────────────────

def _key(key, *, vk=0, scan=0, text="", et=QtCore.QEvent.KeyPress):
    return QtGui.QKeyEvent(et, key, QtCore.Qt.NoModifier, scan, vk, 0, text)


@pytest.mark.parametrize("key,vk,text,expect", [
    (QtCore.Qt.Key_Alt, 61, "", "right alt"),
    (QtCore.Qt.Key_Alt, 58, "", "left alt"),
    (QtCore.Qt.Key_Control, 54, "", "right cmd"),
    (QtCore.Qt.Key_Meta, 62, "", "right ctrl"),
    (QtCore.Qt.Key_F12, 111, "", "f12"),
    (QtCore.Qt.Key_Space, 49, " ", "space"),
    (QtCore.Qt.Key_K, 40, "k", "k"),
])
def test_spec_from_key_event_mac(monkeypatch, app, key, vk, text, expect):
    import holler.settings_dialog as sd
    monkeypatch.setattr(sd, "_IS_MAC", True)
    assert sd.spec_from_key_event(_key(key, vk=vk, text=text)) == expect


def test_spec_from_key_event_windows_sides(monkeypatch, app):
    import holler.settings_dialog as sd
    monkeypatch.setattr(sd, "_IS_MAC", False)
    monkeypatch.setattr(sd.sys, "platform", "win32")
    assert sd.spec_from_key_event(_key(QtCore.Qt.Key_Alt, scan=0x138)) == "right alt"
    assert sd.spec_from_key_event(_key(QtCore.Qt.Key_Control, scan=0x1D)) == "left ctrl"
    assert sd.spec_from_key_event(_key(QtCore.Qt.Key_Alt)) == "alt"


def test_every_recorded_spec_is_a_valid_ptt_key(monkeypatch, app):
    """Whatever the recorder writes must parse to the same key in hotkey.py
    (no silent fallback to Right Alt)."""
    import logging

    import holler.settings_dialog as sd
    from holler.hotkey import _parse_pynput_key, validate_ptt_hotkey
    monkeypatch.setattr(sd, "_IS_MAC", True)
    specs = set(sd._MAC_VK.values()) | {"alt", "shift", "cmd", "ctrl", "space", "tab",
                                         "enter", "f1", "f20", "k", "7"}
    for spec in specs:
        assert validate_ptt_hotkey(spec) is None, spec
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logging.getLogger("holler.hotkey").addHandler(handler)
        try:
            _parse_pynput_key(spec)
        finally:
            logging.getLogger("holler.hotkey").removeHandler(handler)
        assert not records, f"{spec!r} not understood by the listener"


def test_recorder_captures_key_and_swallows_it(env, cfg, monkeypatch):
    sd, _fr, saved, _ = env
    monkeypatch.setattr(sd, "_IS_MAC", True)
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    d.show()
    _pump()
    rec = d._hotkey_record
    rec.click()
    assert rec.is_recording() and rec.text() == "Press a key…"
    # Return while recording is recorded as "enter" — it must NOT hit Save.
    QtWidgets.QApplication.sendEvent(d._fields["hotkey"], _key(QtCore.Qt.Key_Return, vk=36))
    assert saved == [] and d.isVisible()
    assert d._fields["hotkey"].text() == "enter" and not rec.is_recording()
    rec.click()
    QtWidgets.QApplication.sendEvent(rec, _key(QtCore.Qt.Key_Alt, vk=61))
    assert d._fields["hotkey"].text() == "right alt" and rec.text() == "Record"
    # Esc cancels recording without closing the dialog or changing the key.
    rec.click()
    QtWidgets.QApplication.sendEvent(rec, _key(QtCore.Qt.Key_Escape, vk=53))
    assert not rec.is_recording() and d.isVisible()
    assert d._fields["hotkey"].text() == "right alt"
    # Closing the dialog mid-recording removes the app-wide filter.
    rec.click()
    d.reject()
    assert not rec.is_recording()


def test_key_label(monkeypatch):
    import holler.settings_dialog as sd
    monkeypatch.setattr(sd, "_IS_MAC", True)
    assert sd.key_label("right alt") == "Right ⌥ Option"
    assert sd.key_label("f12") == "F12"
    monkeypatch.setattr(sd, "_IS_MAC", False)
    assert sd.key_label("right alt") == "Right Alt"
    assert sd.key_label("caps lock") == "Caps Lock"


def test_voice_commands_card_uses_real_phrases(env, cfg):
    sd, *_ = env
    from holler.model_prefix import extract
    from holler.superintendent.drafts import COMMIT_CUES, DISCARD_CUES, HOLD_CUES
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    text = d._voice_help.text()
    assert "with sonnet" in text and "using haiku" in text and "on opus" in text
    assert extract("with sonnet, tidy this")[1] is not None
    assert HOLD_CUES[0] in text and COMMIT_CUES[0] in text and DISCARD_CUES[0] in text
    d.reject()


# ── First run ───────────────────────────────────────────────────────────────

class _FakePerms:
    NOT_DETERMINED, RESTRICTED, DENIED, AUTHORIZED = 0, 1, 2, 3

    def __init__(self):
        self.mic = self.AUTHORIZED
        self.ax = False
        self.screen = "denied"
        self.calls: list[str] = []

    def install(self, monkeypatch):
        import holler.mac_permissions as mp
        monkeypatch.setattr(mp, "mic_authorization_status", lambda: self.mic)
        monkeypatch.setattr(mp, "accessibility_trusted", lambda: self.ax)
        monkeypatch.setattr(mp, "screen_recording_status", lambda: self.screen)
        for name in ("request_mic_access", "open_mic_privacy_settings", "request_accessibility",
                     "open_accessibility_settings", "request_screen_recording",
                     "open_screen_recording_settings"):
            monkeypatch.setattr(mp, name, lambda *a, _n=name, **k: self.calls.append(_n))
        return self


@pytest.fixture
def perms(monkeypatch):
    import holler.first_run as fr
    monkeypatch.setattr(fr, "mac_permissions_available", lambda: True)
    return _FakePerms().install(monkeypatch)


def _names(d):
    return [n for n, _p in d._steps]


def test_first_run_steps_and_validation(env, cfg, perms, tmp_path):
    _sd, fr, saved, modal = env
    d = fr.FirstRunDialog(cfg)
    assert _names(d) == ["Project folder", "Permissions", "Push-to-talk key", "Model"]
    assert "Step 1 of 4" in d._step_label.text()
    assert d._back_btn.isHidden() and d._save_btn.isHidden()
    d._cwd_edit.setText(str(tmp_path / "missing"))
    d._next()
    assert d._pages.currentIndex() == 0 and "doesn't exist" in d._cwd_error.text()
    d._cwd_edit.setText(str(tmp_path))
    assert d._cwd_error.isHidden()
    d._next()
    assert _names(d)[d._pages.currentIndex()] == "Permissions"
    d._next()
    d._hotkey_edit.setText("ctrl+k")
    d._next()
    assert _names(d)[d._pages.currentIndex()] == "Push-to-talk key" and d._hk_error.text()
    d._hotkey_edit.setText("right cmd")
    d._next()
    assert not d._save_btn.isHidden() and d._next_btn.isHidden()
    d._model.setCurrentIndex(d._model.findData("claude-fable-5"))
    d._save()
    assert modal == [] and len(saved) == 1
    s = saved[0]
    assert (s.repo_cwd, s.hotkey, s.claude_model) == (tmp_path, "right cmd", "claude-fable-5")
    assert d.result() == QtWidgets.QDialog.Accepted


def test_first_run_save_jumps_to_bad_step(env, cfg, perms, tmp_path):
    _sd, fr, saved, _ = env
    d = fr.FirstRunDialog(cfg)
    d._go(3)
    d._cwd_edit.setText(str(tmp_path / "missing"))
    d._save()
    assert saved == [] and d._pages.currentIndex() == 0 and d._cwd_error.text()


def test_permissions_rows_live_and_polling(env, cfg, perms):
    _sd, fr, *_ = env
    d = fr.FirstRunDialog(cfg)
    d.show()
    _pump()
    assert not d._perm_timer.isActive()  # not on the permissions step yet
    d._go(1)
    assert d._perm_timer.isActive() and d._perm_timer.interval() == 1000
    rows = d._perm_rows
    assert rows["mic"].status.text() == "Allowed" and rows["mic"].button.isHidden()
    assert rows["accessibility"].status.text() == "Not allowed"
    assert not rows["screen"].button.isHidden()
    assert "restarts" in d._perm_note.text()
    perms.ax, perms.screen = True, "granted"
    d._refresh_permissions()  # what the 1 s timer does
    assert rows["accessibility"].status.text() == "Allowed"
    assert all(r.button.isHidden() for r in rows.values())
    d._go(2)
    assert not d._perm_timer.isActive()
    d._go(1)
    d.hide()
    assert not d._perm_timer.isActive()
    d.reject()


def test_permission_buttons_call_the_right_apis(env, cfg, perms):
    _sd, fr, *_ = env
    d = fr.FirstRunDialog(cfg)
    d._go(1)
    perms.mic = perms.NOT_DETERMINED
    d._refresh_permissions()
    assert d._perm_rows["mic"].status.text() == "Not asked yet"
    d._perm_rows["mic"].button.click()
    assert perms.calls == ["request_mic_access"]
    perms.mic = perms.DENIED
    d._refresh_permissions()
    d._perm_rows["mic"].button.click()
    d._perm_rows["accessibility"].button.click()
    d._perm_rows["screen"].button.click()
    assert perms.calls[1:] == ["open_mic_privacy_settings", "request_accessibility",
                               "open_accessibility_settings", "request_screen_recording",
                               "open_screen_recording_settings"]
    d.reject()


def test_relaunch_saves_first_then_calls_callback(env, cfg, perms):
    _sd, fr, saved, _ = env
    order: list[str] = []
    fr_save = fr.save_config
    fr.save_config = lambda c: (order.append("save"), fr_save(c))
    try:
        d = fr.FirstRunDialog(cfg, on_relaunch=lambda: order.append("relaunch"))
        d._go(1)
        d._relaunch_btn.click()
    finally:
        fr.save_config = fr_save
    assert order == ["save", "relaunch"] and len(saved) == 1
    assert d.result() == QtWidgets.QDialog.Accepted


def test_relaunch_failure_keeps_dialog_open(env, cfg, perms):
    _sd, fr, saved, _ = env

    def boom():
        raise OSError("spawn failed")

    d = fr.FirstRunDialog(cfg, on_relaunch=boom)
    d._go(1)
    d._relaunch_btn.click()
    assert len(saved) == 1 and d.result() != QtWidgets.QDialog.Accepted
    assert "Couldn't relaunch" in d._relaunch_error.text()
    d.reject()


def test_no_relaunch_button_without_callback(env, cfg, perms):
    _sd, fr, *_ = env
    d = fr.FirstRunDialog(cfg)
    assert d._relaunch_btn is None
    d._go(1)
    assert "quit and reopen" in d._perm_note.text()
    d.reject()


def test_first_run_without_permissions_step(env, cfg, monkeypatch):
    _sd, fr, *_ = env
    monkeypatch.setattr(fr, "mac_permissions_available", lambda: False)
    d = fr.FirstRunDialog(cfg)
    assert _names(d) == ["Project folder", "Push-to-talk key", "Model"]
    d._go(1)
    assert "Step 2 of 3" in d._step_label.text()
    d.reject()


def test_first_run_constructs_without_touching_permissions(env, cfg, monkeypatch):
    """Opening the dialog (and the visual goldens) must not query TCC until
    the permissions step is shown."""
    _sd, fr, *_ = env
    import holler.mac_permissions as mp
    hits = []
    monkeypatch.setattr(fr, "mac_permissions_available", lambda: True)
    for n in ("mic_authorization_status", "accessibility_trusted", "screen_recording_status"):
        monkeypatch.setattr(mp, n, lambda *a, _n=n: hits.append(_n))
    d = fr.FirstRunDialog(cfg)
    d.show()
    _pump()
    assert hits == []
    d.reject()


# ── Review fixes ────────────────────────────────────────────────────────────

def test_recorder_stops_when_user_clicks_into_another_field(env, cfg, monkeypatch):
    """Click Record, click "Speech language", type "de": the language changes
    and the push-to-talk key does NOT become "d"."""
    from PySide6.QtTest import QTest
    sd, *_ = env
    monkeypatch.setattr(sd, "_IS_MAC", True)
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    d.show()
    d.activateWindow()
    _pump()
    hk, lang, rec = d._fields["hotkey"], d._fields["whisper_language"], d._hotkey_record
    before = hk.text()
    lang.clear()
    rec.click()
    assert rec.is_recording()
    d._scroll.ensureWidgetVisible(lang)
    _pump()
    QTest.mouseClick(lang, QtCore.Qt.LeftButton)
    assert not rec.is_recording() and rec.text() == "Record"
    lang.setFocus()
    QTest.keyClicks(lang, "de")
    assert lang.text() == "de" and hk.text() == before
    d.reject()


def test_recorder_stops_on_focus_loss_and_deactivation(env, cfg, monkeypatch):
    sd, *_ = env
    monkeypatch.setattr(sd, "_IS_MAC", True)
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    d.show()
    _pump()
    rec = d._hotkey_record
    # Focus moving elsewhere (Tab, a click that doesn't press the mouse on
    # this window, …) ends recording.
    rec.start()
    QtWidgets.QApplication.sendEvent(rec, QtGui.QFocusEvent(QtCore.QEvent.FocusOut, QtCore.Qt.TabFocusReason))
    assert not rec.is_recording()
    # The window losing activation ends it too.
    rec.start()
    QtWidgets.QApplication.sendEvent(d, QtCore.QEvent(QtCore.QEvent.WindowDeactivate))
    assert not rec.is_recording()
    # A key press aimed at another window is not taken.
    other = QtWidgets.QLineEdit()
    rec.start()
    QtWidgets.QApplication.sendEvent(other, _key(QtCore.Qt.Key_D, vk=2, text="d"))
    assert rec.is_recording() and d._fields["hotkey"].text() != "d"
    rec.stop()
    d.reject()


def test_checkbox_rows_keep_their_own_text_in_the_accessible_name(env, cfg):
    sd, *_ = env
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    box = d._fields["open_sessions_on_launch"]
    assert box.accessibleName() == "At launch: Open the Sessions window when Holler starts"
    assert d._fields["hotkey"].accessibleName() == "Push-to-talk key"
    d.reject()


def test_linux_super_key_records_as_a_name_the_listener_knows(monkeypatch, app):
    import holler.settings_dialog as sd
    monkeypatch.setattr(sd, "_IS_MAC", False)
    monkeypatch.setattr(sd.sys, "platform", "linux")
    assert sd.spec_from_key_event(_key(QtCore.Qt.Key_Meta)) == "super"
    assert sd.spec_from_key_event(_key(QtCore.Qt.Key_Super_L)) == "left super"
    assert sd.spec_from_key_event(_key(QtCore.Qt.Key_Super_R)) == "right super"
    monkeypatch.setattr(sd.sys, "platform", "win32")
    assert sd.spec_from_key_event(_key(QtCore.Qt.Key_Meta)) == "windows"


def test_recorder_rejects_a_key_the_listener_cannot_parse(env, cfg, monkeypatch):
    sd, *_ = env
    monkeypatch.setattr(sd, "_IS_MAC", False)
    monkeypatch.setattr(sd.sys, "platform", "linux")
    monkeypatch.setattr(sd, "spec_from_key_event", lambda e: "hyper")
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None)
    d.show()
    _pump()
    rec, hk = d._hotkey_record, d._fields["hotkey"]
    before = hk.text()
    rec.click()
    QtWidgets.QApplication.sendEvent(rec, _key(QtCore.Qt.Key_Hyper_L))
    assert hk.text() == before and rec.is_recording()
    assert "can't" in rec.text().lower() or "another" in rec.text().lower()
    rec.stop()
    d.reject()


def test_first_run_permission_rows_not_squashed_on_short_screen(env, cfg, perms):
    """~570 pt of usable height (ui_scale 1.4 on a 798-pt screen): the dialog
    fits, and at its minimum height no word-wrapped text is squashed."""
    _sd, fr, *_ = env
    d = fr.FirstRunDialog(cfg)
    d._go(1)
    d.show()
    _pump()
    avail = int(798 / 1.4)
    assert d.minimumHeight() <= avail - 60 and d.height() <= avail - 60
    d.resize(d.minimumWidth(), d.minimumHeight())
    _pump()
    for row in d._perm_rows.values():
        for lab in row.findChildren(QtWidgets.QLabel):
            if lab.wordWrap() and lab.isVisible():
                assert lab.height() >= lab.heightForWidth(lab.width()), lab.text()
    # Every row still reachable: the step scrolls instead.
    last = d._perm_rows["screen"]
    d._step_scroll.ensureWidgetVisible(last)
    _pump()
    vp = d._step_scroll.viewport()
    top = last.mapTo(vp, QtCore.QPoint()).y()
    assert 0 <= top and top + last.height() <= vp.height()
    d.reject()


def test_restart_now_still_fires_after_the_dialog_is_deleted(env, cfg):
    """The real caller drops the dialog right after exec() returns; the
    deferred restart must not be tied to (and cancelled with) the dialog."""
    import shiboken6

    sd, _fr, saved, modal = env
    restarts: list[int] = []
    d = sd.SettingsDialog(cfg, on_save=lambda c, ch: None, on_restart=lambda: restarts.append(1))
    ws = d._fields["whisper_model"]
    ws.setCurrentIndex(ws.findData("tiny"))
    d._restart_btn.click()
    shiboken6.delete(d)
    _pump()
    assert restarts == [1]
