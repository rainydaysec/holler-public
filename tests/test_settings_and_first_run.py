"""Settings dialog / first-run fixes: E4 mic-test bands, E6 colour validation,
E7 PTT combo rejection, E8 legacy sysprompt migration, E9 unlisted combo
values, E10 mic-test stream lifecycle. sounddevice is always stubbed."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from holler.config import load


@pytest.fixture
def cfg(tmp_path):
    return dataclasses.replace(load(tmp_path / "none.toml"), repo_cwd=tmp_path)


class FakeStream:
    instances: list = []
    fail_start = False

    def __init__(self, **kw):
        self.started = self.stopped = self.closed = False
        FakeStream.instances.append(self)

    def start(self):
        if FakeStream.fail_start:
            raise RuntimeError("start failed")
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


@pytest.fixture
def dialog_env(app, monkeypatch):
    import holler.settings_dialog as sd_mod
    from PySide6 import QtWidgets

    FakeStream.instances = []
    FakeStream.fail_start = False
    monkeypatch.setattr("holler.audio.refresh_devices", lambda: True)
    monkeypatch.setattr("sounddevice.query_devices", lambda *a, **k: [])
    monkeypatch.setattr("sounddevice.InputStream", FakeStream)
    monkeypatch.setattr(sd_mod, "effective_models", lambda cfg: ["claude-fable-5"])
    monkeypatch.setattr("holler.mac_permissions.is_denied", lambda: False, raising=False)
    saved, warnings = [], []
    monkeypatch.setattr(sd_mod, "save_config", lambda c: saved.append(c))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warnings.append(a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    return sd_mod, saved, warnings


def _dialog(sd_mod, cfg):
    return sd_mod.SettingsDialog(cfg, on_save=lambda c, ch: None)


# ---- E6 -----------------------------------------------------------------

@pytest.mark.parametrize("bad", ["#GGGGGG", "#12", "red", "#FF2E2Z", "#XYZ"])
def test_invalid_stroke_colors_rejected(app, bad):
    from holler.settings_dialog import stroke_color_error
    assert stroke_color_error(bad)


@pytest.mark.parametrize("good", ["#FF2E2E", "#f00", "#FF2E2E80"])
def test_valid_stroke_colors_accepted(app, good):
    from holler.settings_dialog import stroke_color_error
    assert stroke_color_error(good) is None


def test_save_refuses_unparseable_color(dialog_env, cfg):
    sd_mod, saved, warnings = dialog_env
    d = _dialog(sd_mod, cfg)
    d._fields["stroke_color"].setText("#GGGGGG")
    d._on_save_clicked()
    # Inline, not modal: the message sits under the field.
    assert saved == [] and not warnings
    assert "hex" in d._errors["stroke_color"].text()
    assert not d._errors["stroke_color"].isHidden()
    d._fields["stroke_color"].setText("#00FF00")  # fixing it clears the message
    assert d._errors["stroke_color"].text() == ""
    d._on_save_clicked()
    assert len(saved) == 1 and saved[0].stroke_color == "#00FF00"


# ---- E7 -----------------------------------------------------------------

def test_settings_rejects_combo_ptt_hotkey(dialog_env, cfg):
    sd_mod, saved, warnings = dialog_env
    d = _dialog(sd_mod, cfg)
    d._fields["hotkey"].setText("ctrl+space")
    d._on_save_clicked()
    assert saved == [] and not warnings
    assert "single key" in d._errors["hotkey"].text()


def test_settings_combo_sessions_hotkey_still_allowed(dialog_env, cfg):
    sd_mod, saved, warnings = dialog_env
    d = _dialog(sd_mod, cfg)
    d._fields["sessions_window_hotkey"].setText("ctrl+shift+j")
    d._on_save_clicked()
    assert len(saved) == 1 and saved[0].sessions_window_hotkey == "ctrl+shift+j"


def test_first_run_rejects_combo_and_hint_has_no_combo(app, cfg, monkeypatch):
    import holler.first_run as fr
    from PySide6 import QtWidgets

    monkeypatch.setattr(fr, "effective_models", lambda c: ["claude-fable-5"])
    monkeypatch.setattr(fr.FirstRunDialog, "_check_auth", lambda self: None)
    saved, warnings = [], []
    monkeypatch.setattr(fr, "save_config", lambda c: saved.append(c))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warnings.append(a[2])))
    d = fr.FirstRunDialog(cfg)
    labels = " ".join(l.text() for l in d.findChildren(QtWidgets.QLabel))
    assert "ctrl+space" not in labels
    d._hotkey_edit.setText("ctrl+space")
    d._save()
    # Inline error on the push-to-talk step (the dialog jumps there), no modal.
    assert saved == [] and not warnings
    assert "single key" in d._hk_error.text()
    assert d._steps[d._pages.currentIndex()][0] == "Push-to-talk key"
    d._hotkey_edit.setText("f12")
    d._save()
    assert len(saved) == 1 and saved[0].hotkey == "f12"


# ---- E9 -----------------------------------------------------------------

def test_unlisted_values_survive_round_trip(dialog_env, cfg):
    sd_mod, saved, _ = dialog_env
    odd = dataclasses.replace(cfg, whisper_compute="auto", whisper_device="mps",
                              panel_corner="center", panel_width=2000,
                              max_screenshot_long_edge=5120)
    d = _dialog(sd_mod, odd)
    for key in ("whisper_compute", "whisper_device", "panel_corner",
                "panel_width", "max_screenshot_long_edge"):
        assert d._read_value(key) == getattr(odd, key), key


def test_listed_values_still_selected(dialog_env, cfg):
    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, dataclasses.replace(cfg, whisper_compute="float32"))
    assert d._read_value("whisper_compute") == "float32"
    assert d._fields["whisper_compute"].count() == 4


# ---- E10 / E4 -----------------------------------------------------------

def test_mic_test_stream_closed_on_reject_and_accept(dialog_env, cfg):
    sd_mod, _, _ = dialog_env
    for finish in ("reject", "accept"):
        d = _dialog(sd_mod, cfg)
        d._toggle_mic_test()
        s = FakeStream.instances[-1]
        assert d._mic_stream is s and d._mic_auto_stop.isActive()
        getattr(d, finish)()
        assert s.stopped and s.closed, finish
        assert d._mic_stream is None and not d._mic_auto_stop.isActive()


def test_mic_test_stream_closed_when_start_fails(dialog_env, cfg):
    sd_mod, _, _ = dialog_env
    FakeStream.fail_start = True
    d = _dialog(sd_mod, cfg)
    d._toggle_mic_test()
    assert d._mic_stream is None
    assert FakeStream.instances[-1].closed


def test_stale_auto_stop_cannot_cut_next_test_short(dialog_env, cfg):
    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    d._toggle_mic_test()
    d._toggle_mic_test()  # manual stop
    assert not d._mic_auto_stop.isActive()
    d._toggle_mic_test()  # new test gets a fresh full window
    assert d._mic_auto_stop.isActive() and d._mic_auto_stop.remainingTime() > 3000
    d.reject()


@pytest.mark.parametrize("peak,expect", [(1e-4, "No sound"), (0.011, "Very quiet"),
                                         (0.08, "Mic works")])
def test_mic_test_verdict_uses_shared_thresholds(dialog_env, cfg, peak, expect):
    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    d._mic_peak_rms = peak
    d._stop_mic_test(report=True)
    assert expect in d._mic_test_status.text()


# ---- E8 -----------------------------------------------------------------

def test_fresh_install_does_not_get_legacy_sysprompt(tmp_path):
    from holler.first_run import maybe_migrate_legacy_sysprompt

    appdata = tmp_path / "appdata"
    appdata.mkdir()
    maybe_migrate_legacy_sysprompt(appdata, tmp_path / "config.toml")
    assert not (appdata / "sysprompt.md").exists()
    assert (appdata / "migration_v1.flag").exists()


@pytest.mark.parametrize("evidence", ["db", "config"])
def test_legacy_install_is_migrated(tmp_path, evidence):
    from holler.first_run import _LEGACY_PRE_V3_APPEND, maybe_migrate_legacy_sysprompt

    appdata = tmp_path / "appdata"
    appdata.mkdir()
    cfg_path = tmp_path / "config.toml"
    (appdata / "holler.db" if evidence == "db" else cfg_path).write_text("x")
    maybe_migrate_legacy_sysprompt(appdata, cfg_path)
    assert (appdata / "sysprompt.md").read_text() == _LEGACY_PRE_V3_APPEND


def test_flagged_install_untouched(tmp_path):
    from holler.first_run import maybe_migrate_legacy_sysprompt

    appdata = tmp_path / "appdata"
    appdata.mkdir()
    (appdata / "migration_v1.flag").write_text("migrated")
    (appdata / "holler.db").write_text("x")
    maybe_migrate_legacy_sysprompt(appdata, tmp_path / "config.toml")
    assert not (appdata / "sysprompt.md").exists()


# ---- Settings layout: scroll area, no squashed controls ------------------

class _FakeScreen:
    def __init__(self, w, h):
        from PySide6 import QtCore
        self._rect = QtCore.QRect(0, 0, w, h)

    def availableGeometry(self):
        return self._rect


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    d = tmp_path / "appdata"
    d.mkdir()
    monkeypatch.setattr("holler.appdir.app_data_dir", lambda: d)
    return d


def _input_widgets(d):
    from PySide6 import QtWidgets
    return [w for w in d.findChildren(QtWidgets.QWidget)
            if isinstance(w, (QtWidgets.QLineEdit, QtWidgets.QComboBox, QtWidgets.QSpinBox))
            and not isinstance(w.parent(), QtWidgets.QAbstractSpinBox)  # spinbox's inner edit
            and not isinstance(w.parent(), QtWidgets.QComboBox)]        # editable combo's edit


def test_settings_controls_never_squashed_at_small_size(dialog_env, cfg, appdata):
    from PySide6 import QtCore, QtWidgets

    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    d.resize(300, 200)  # clamped to the minimum size
    d.show()
    QtWidgets.QApplication.processEvents()
    widgets = _input_widgets(d)
    assert len(widgets) >= 15
    for w in widgets:
        assert w.height() >= w.minimumSizeHint().height(), (type(w).__name__, w.height())
    # Vertical scrolling only, and the content is never clipped horizontally.
    assert d._scroll.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff
    assert d._scroll.verticalScrollBar().maximum() > 0
    assert d._scroll.widget().width() <= d._scroll.viewport().width()
    # The button row stays outside the scroll area.
    for b in d.findChildren(QtWidgets.QPushButton):
        if b.text() in ("Save", "Cancel"):
            assert not d._scroll.isAncestorOf(b)
    d.reject()


def test_settings_initial_size_fits_screen(dialog_env, cfg, appdata, monkeypatch):
    from PySide6 import QtWidgets

    sd_mod, _, _ = dialog_env
    monkeypatch.setattr(sd_mod.SettingsDialog, "_target_screen", lambda self: _FakeScreen(1400, 560))
    d = _dialog(sd_mod, cfg)
    assert d.height() <= 560 and d.width() <= 1400

    monkeypatch.setattr(sd_mod.SettingsDialog, "_target_screen", lambda self: _FakeScreen(2000, 3000))
    d2 = _dialog(sd_mod, cfg)
    d2.show()
    QtWidgets.QApplication.processEvents()
    # Tall screen: opens as tall as the content needs — nothing to scroll.
    assert d2._scroll.verticalScrollBar().maximum() == 0
    d.reject()
    d2.reject()


def test_settings_shows_real_paths_not_localappdata(dialog_env, cfg, appdata):
    from PySide6 import QtWidgets

    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    d.show()
    QtWidgets.QApplication.processEvents()
    texts = [w.text() for w in d.findChildren(QtWidgets.QLabel)]
    texts += [w.text() for w in d.findChildren(QtWidgets.QAbstractButton)]
    assert not any("LOCALAPPDATA" in t for t in texts)
    assert sd_mod.display_path(cfg.captures_dir) in d._captures_note.text()
    assert d._captures_note.toolTip() == str(cfg.captures_dir)
    assert sd_mod.display_path(appdata / "profiles") in d._prof_note.text()
    # The debug checkbox's label fits (doesn't run off the right edge).
    check = d._fields["debug_save_captures"]
    assert check.width() >= check.sizeHint().width()
    assert check.mapTo(d, check.rect().topRight()).x() <= d.width()
    d.reject()


def test_display_path(monkeypatch, tmp_path):
    import os
    from holler.settings_dialog import display_path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert display_path(tmp_path / "a" / "b") == os.path.join("~", "a", "b")
    long = Path("/x" * 60)
    shown = display_path(long, max_chars=40)
    assert len(shown) == 40 and "…" in shown and shown.endswith(str(long)[-4:])


def test_legacy_panel_settings_grouped_and_round_trip(dialog_env, cfg, appdata):
    from PySide6 import QtWidgets

    sd_mod, saved, _ = dialog_env
    d = _dialog(sd_mod, dataclasses.replace(cfg, panel_corner="bottom-left", panel_width=500,
                                            mini_panel_max_sessions=7))
    form = d._scroll.widget().findChild(QtWidgets.QFormLayout)

    def row_of(widget):
        return form.getWidgetPosition(widget)[0]

    header = next(l for l in d.findChildren(QtWidgets.QLabel) if l.text() == "Legacy floating panel")
    hdr_row = row_of(header)
    for key in ("panel_corner", "panel_width", "mini_panel_max_sessions"):
        assert row_of(d._fields[key]) > hdr_row, key
    assert any("currently hidden" in l.text() for l in d.findChildren(QtWidgets.QLabel))
    d._on_save_clicked()
    assert len(saved) == 1
    s = saved[0]
    assert (s.panel_corner, s.panel_width, s.mini_panel_max_sessions) == ("bottom-left", 500, 7)


# ---- First run: CLI probe off the GUI thread ------------------------------

def _first_run(app, cfg, monkeypatch):
    import holler.first_run as fr
    monkeypatch.setattr(fr, "effective_models", lambda c: ["claude-fable-5"])
    return fr


def _pump_until(pred, timeout=5.0):
    import time
    from PySide6 import QtWidgets
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_first_run_probe_runs_off_gui_thread(app, cfg, monkeypatch):
    import threading
    fr = _first_run(app, cfg, monkeypatch)
    release = threading.Event()
    seen = {}

    def slow_probe(path):
        seen["thread"] = threading.current_thread()
        release.wait(5)
        return ("✓ Claude CLI found (9.9.9).", "authOk")

    monkeypatch.setattr(fr, "find_claude_cli", lambda: "/fake/claude")
    monkeypatch.setattr(fr, "probe_claude_cli", slow_probe)
    d = fr.FirstRunDialog(cfg)
    assert _pump_until(lambda: "thread" in seen)  # deferred check started
    # GUI thread isn't blocked while the probe runs; "checking" state shown.
    assert "Checking for Claude Code" in d._auth_label.text()
    assert seen["thread"] is not threading.main_thread()
    release.set()
    assert _pump_until(lambda: "9.9.9" in d._auth_label.text())
    assert d._auth_label.objectName() == "authOk"
    d.reject()


def test_first_run_probe_result_after_dialog_destroyed(app, cfg, monkeypatch):
    import threading
    import shiboken6
    fr = _first_run(app, cfg, monkeypatch)
    release, finished = threading.Event(), threading.Event()

    def slow_probe(path):
        release.wait(5)
        finished.set()
        return ("late", "authOk")

    monkeypatch.setattr(fr, "find_claude_cli", lambda: "/fake/claude")
    monkeypatch.setattr(fr, "probe_claude_cli", slow_probe)
    d = fr.FirstRunDialog(cfg)
    d._check_auth()
    d.reject()
    shiboken6.delete(d)
    release.set()
    assert finished.wait(5)
    _pump_until(lambda: False, timeout=0.2)  # deliver the queued result: must not raise


def test_first_run_not_found_message(app, cfg, monkeypatch):
    fr = _first_run(app, cfg, monkeypatch)
    monkeypatch.setattr(fr, "find_claude_cli", lambda: None)
    d = fr.FirstRunDialog(cfg)
    d._check_auth()
    assert "isn't installed" in d._auth_label.text()
    assert d._auth_label.objectName() == "authMiss"
    d.reject()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="posix exec bits")
@pytest.mark.parametrize("sub", [".npm-global/bin", ".local/bin", ".claude/local"])
def test_find_claude_cli_checks_common_locations(tmp_path, monkeypatch, sub):
    import os
    import holler.first_run as fr

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Keep the system-wide fallbacks out of the picture.
    monkeypatch.setattr(fr, "_CLAUDE_FALLBACK_DIRS",
                        tuple(d for d in fr._CLAUDE_FALLBACK_DIRS if d.startswith("~")))
    assert fr.find_claude_cli() is None
    exe = tmp_path / sub / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert fr.find_claude_cli() == str(exe)


# ---- model refresh (2026-10-02) -----------------------------------------

def _refresh(dialog, monkeypatch, result, error=""):
    """Click "Check for new models" with `model_discovery.refresh` stubbed, and
    pump until the worker thread's result lands on the GUI thread."""
    import holler.model_discovery as md

    seen = {}

    def fake_refresh():
        import threading
        seen["thread"] = threading.current_thread()
        return list(result), error

    monkeypatch.setattr(md, "refresh", fake_refresh)
    dialog._models_refresh_btn.click()
    assert _pump_until(lambda: dialog._models_refresh_btn.isEnabled())
    return seen


def test_refresh_lists_new_models_without_moving_the_current_choice(dialog_env, cfg, monkeypatch):
    """Discovery runs once on a startup thread, so a long-lived Holler never
    sees a new release. The button must add it without re-picking a model."""
    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    combo = d._fields["claude_model"]
    before = combo.currentData()

    _refresh(d, monkeypatch, ["claude-fable-5", "claude-opus-9-9"])

    assert combo.currentData() == before
    assert "claude-opus-9-9" in {combo.itemData(i) for i in range(combo.count())}
    assert "Added 1" in d._models_status.text()
    d.reject()


def test_refresh_runs_off_the_gui_thread(dialog_env, cfg, monkeypatch):
    """`refresh()` can block for the API timeout; on the GUI thread that freezes
    the dialog."""
    import threading

    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    seen = _refresh(d, monkeypatch, ["claude-opus-9-9"])
    assert seen["thread"] is not threading.main_thread()
    d.reject()


def test_refresh_reports_failure_and_stays_clickable(dialog_env, cfg, monkeypatch):
    """A failed check must say so rather than silently reading as "nothing new"."""
    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    combo = d._fields["claude_model"]
    count = combo.count()

    _refresh(d, monkeypatch, [], error="no API key")

    assert "no API key" in d._models_status.text()
    assert combo.count() == count  # nothing invented from a failed check
    assert d._models_refresh_btn.isEnabled()
    d.reject()


def test_refresh_finding_nothing_new_says_so(dialog_env, cfg, monkeypatch):
    sd_mod, _, _ = dialog_env
    d = _dialog(sd_mod, cfg)
    _refresh(d, monkeypatch, ["claude-fable-5"])
    assert "Nothing new" in d._models_status.text()
    d.reject()
