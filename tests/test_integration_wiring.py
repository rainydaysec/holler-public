"""Coordinator wiring between phases in runners/full.py."""
import inspect

import holler.runners.full as full


def test_first_run_relaunch_spawns_replacement_then_exits(monkeypatch):
    calls = []
    monkeypatch.setattr(full, "build_relaunch_command", lambda: (["py", "-m", "holler"], {"A": "1"}))
    monkeypatch.setattr(full.subprocess, "Popen", lambda cmd, **kw: calls.append(("popen", cmd, kw)))
    monkeypatch.setattr(full.os, "_exit", lambda code: calls.append(("exit", code)))
    full._first_run_relaunch()
    assert calls[0][0] == "popen" and calls[0][1] == ["py", "-m", "holler"]
    assert calls[0][2]["start_new_session"] is True and calls[0][2]["env"] == {"A": "1"}
    assert calls[-1] == ("exit", 0)


def test_main_wires_phase_hooks():
    src = inspect.getsource(full.main)
    assert "FirstRunDialog(cfg, on_relaunch=_first_run_relaunch)" in src
    # Settings → Restart now goes through the shared relauncher (one relaunch
    # at a time; Cancel on the busy prompt says when changes apply).
    assert "on_restart=_restart_from_settings" in src
    assert 'state["relauncher"].restart(from_settings=True)' in src
    assert "win.restart_requested.connect(relauncher.restart)" in src
    assert "relauncher.schedule_text_scale()" in src
    assert "sessions_hotkey=cfg.sessions_window_hotkey" in src
    assert "if _ptt_press_ignored(state):" in src and "if _ptt_release_ignored(state):" in src
    assert "DetailView.set_capture_active_predicate(_capture_in_progress)" in src
    assert "set_toast_click_blocker(_capture_in_progress)" in src


def test_settings_hotkey_change_updates_sessions_window_label():
    src = inspect.getsource(full.main)
    i = src.index('if "hotkey" in changed:')
    assert "hk_win.set_hotkey(new_cfg.hotkey)" in src[i:i + 1500]


def test_sessions_window_set_hotkey_refreshes_hints(app, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state
    from holler.jobs import JobStore
    from holler.sessions_window import SessionsWindow
    from holler.store import Store

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)
    store = Store(tmp_path / "holler.db")
    w = SessionsWindow(store, JobStore(store), tmp_path / "captures", tmp_path, hotkey="right alt")
    try:
        w.set_hotkey("f12")
        assert "F12" in w._ptt_status.text()
        assert "F12" in w._text_input.placeholderText()
        sid = store.create_session(model="m", transcript="t", screenshot_path=None)
        w._refresh_list()
        w.select_session(sid)
        w.set_hotkey("right ctrl")
        assert "Right Ctrl" in w._ptt_status.text() or "⌃" in w._ptt_status.text()
    finally:
        w.close()
        store.close()


def test_selecting_a_session_clears_runner_pending_profile():
    src = inspect.getsource(full.main)
    i = src.index("def _on_sw_select(local_id: int) -> None:")
    j = src.index("win.session_selected.connect(_on_sw_select)")
    body = src[i:j]
    assert 'state["pending_profile"] = None' in body
    assert body.index("if local_id < 0:") < body.index('state["pending_profile"] = None')
