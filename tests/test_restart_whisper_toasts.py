"""Runtime-wiring regressions:

1. A-/A+ and the restart button relaunch holler, killing queued/running turns —
   they must ask first when anything is running (and not when idle).
3. A whisper load failure must be reported as such, not as "found no speech",
   and must not mark audio ready.
4. Completion toasts are suppressed while the Sessions window is active.
5. Toast / routing-pill stylesheets are scoped to their own widget.
"""
from __future__ import annotations

import numpy as np
import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from holler import transcriber as tr_mod
from holler.jobs import Job, JobStore
from holler.runners import full
from holler.transcriber import Transcriber, Transcription, WhisperUnavailable


# ── 1. restart confirmation ─────────────────────────────────────────────────

class _Jobs:
    def __init__(self, jobs):
        self._jobs = jobs

    def all(self):
        return list(self._jobs)


def _job(jid, status, sid=None):
    j = Job(id=jid, transcript="t", submitted_at=0.0, status=status)
    j.local_session_id = sid
    return j


def test_busy_count_groups_by_session_and_ignores_finished():
    jobs = _Jobs([
        _job(1, "running", sid=7),
        _job(2, "queued", sid=7),      # same session → one
        _job(3, "queued", sid=9),
        _job(4, "running", sid=None),  # not yet tied to a session → own entry
        _job(5, "done", sid=11),
        _job(6, "error", sid=12),
    ])
    assert full._busy_session_count(jobs) == 3
    assert full._busy_session_count(_Jobs([_job(1, "done", 1)])) == 0


def test_idle_restart_does_not_ask(app, monkeypatch):
    def _boom(self):
        raise AssertionError("must not prompt when nothing is running")

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", _boom)
    assert full._confirm_restart_if_busy(_Jobs([_job(1, "done", 1)]), None) is True
    assert full._confirm_restart_if_busy(_Jobs([]), None) is True


def test_busy_restart_dialog_text_and_default_cancel(app):
    parent = QtWidgets.QWidget()
    box, restart_btn = full._build_restart_confirm(parent, 3)
    try:
        assert box.parent() is parent
        assert box.text() == "3 sessions are still working. Restarting stops them."
        assert restart_btn.text() == "Restart now"
        default = box.defaultButton()
        assert default is not restart_btn
        assert box.standardButton(default) == QtWidgets.QMessageBox.Cancel
        assert box.escapeButton() is default
    finally:
        box.deleteLater()
        parent.deleteLater()


@pytest.mark.parametrize("choice, expected", [("restart", True), ("cancel", False)])
def test_busy_restart_follows_the_choice(app, monkeypatch, choice, expected):
    shown = []

    def _exec(self):
        shown.append(self.text())
        if choice == "restart":
            btn = next(b for b in self.buttons() if b.text() == "Restart now")
        else:
            btn = self.defaultButton()
        btn.click()
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", _exec)
    jobs = _Jobs([_job(1, "running", sid=4)])
    assert full._confirm_restart_if_busy(jobs, None) is expected
    assert shown == ["1 session is still working. Restarting stops it."]


# ── 3. whisper load failure ─────────────────────────────────────────────────

def _failing_transcriber(monkeypatch) -> Transcriber:
    monkeypatch.setattr(
        tr_mod, "_try_load",
        lambda *a, **k: (None, "cpu", OSError("model.bin: No such file or directory")),
    )
    return Transcriber(model_size="tiny", device="cpu", compute_type="int8")


def test_transcribe_raises_specific_error_when_model_failed(monkeypatch):
    t = _failing_transcriber(monkeypatch)
    with pytest.raises(WhisperUnavailable) as ei:
        t.transcribe_detailed(np.zeros(16_000, dtype=np.float32))
    assert isinstance(ei.value, RuntimeError)  # old callers still catch it
    assert ei.value.reason == "OSError: model.bin: No such file or directory"


def test_warmup_does_not_mark_audio_ready_on_failure(monkeypatch):
    t = _failing_transcriber(monkeypatch)
    state: dict = {"ready_audio": False}
    notices: list[str] = []
    full._warm_whisper(t, state, notices.append)
    assert state["ready_audio"] is False
    assert state["audio_error"] == "OSError: model.bin: No such file or directory"
    assert notices == [full._speech_model_failed_message(state["audio_error"])]
    # Plain language — what to do, never the exception class.
    assert "speech model didn't load" in notices[0]
    assert "OSError" not in notices[0]


def test_warmup_marks_ready_on_success(monkeypatch):
    monkeypatch.setattr(tr_mod, "_try_load", lambda *a, **k: (object(), "cpu", None))
    t = Transcriber(model_size="tiny", device="cpu", compute_type="int8")
    state: dict = {"ready_audio": False}
    notices: list[str] = []
    full._warm_whisper(t, state, notices.append)
    assert state["ready_audio"] is True
    assert "audio_error" not in state and notices == []


def test_short_load_reason_is_one_line_and_bounded():
    long = RuntimeError("x" * 300 + "\nsecond line")
    r = tr_mod.short_load_reason(long)
    assert "\n" not in r and len(r) <= 90 and r.endswith("…")
    assert tr_mod.short_load_reason(ValueError()) == "ValueError"


class _NoticeSink(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.msgs: list[str] = []

    @QtCore.Slot(str)
    def show_inline_notice(self, text: str) -> None:
        self.msgs.append(text)


class _Stats:
    seconds = 3.0
    rms = 0.05
    quality = "ok"


def _dispatch(app, monkeypatch, transcriber):
    monkeypatch.setattr(full, "compose", lambda img, *a, **k: img)
    monkeypatch.setattr(full, "downscale", lambda img, *a, **k: img)
    sink = _NoticeSink()

    class _Cfg:
        debug_save_captures = False
        stroke_color = "#f00"
        stroke_width = 3
        max_screenshot_long_edge = 100
        mic_device = ""

    class _Snap:
        image = object()

    state = {"cfg": _Cfg(), "sessions_window": sink, "persistent_store": None}
    full._dispatch_job(_Snap(), [], np.zeros(48_000, dtype=np.float32), transcriber,
                       session=None, cfg=_Cfg(), state=state, panel=sink,
                       signal_stats=_Stats(), device_name="Mic")
    app.processEvents()
    return sink.msgs


def test_dispatch_reports_model_failure_not_silence(app, monkeypatch):
    msgs = _dispatch(app, monkeypatch, _failing_transcriber(monkeypatch))
    assert len(msgs) == 1
    assert "speech model didn't load" in msgs[0]
    assert "no speech" not in msgs[0]


def test_dispatch_genuine_silence_keeps_no_speech_message(app, monkeypatch):
    class _Silent:
        def transcribe_detailed(self, audio):
            return Transcription("", 3.0, 0.0)

    msgs = _dispatch(app, monkeypatch, _Silent())
    assert len(msgs) == 1
    assert "Didn't hear speech" in msgs[0]


def test_failure_message_model_error_after_mic_faults():
    msg, perm = full._audio_failure_message("permission", None, None, None, model_error="X")
    assert perm is True
    msg, perm = full._audio_failure_message(None, _Stats(), "Mic", Transcription("", 3.0, 0.0),
                                            model_error="OSError: gone")
    assert msg == full._speech_model_failed_message("OSError: gone")
    assert "speech model didn't load" in msg and "OSError" not in msg
    assert perm is False


# ── 4. completion toast suppression ─────────────────────────────────────────

def _panel_with_finished_job(app, monkeypatch):
    from holler.panel import Panel

    store = JobStore(None)
    p = Panel(store, corner="top-right", width=380, model_label="M")
    job = Job(id=1, transcript="t", submitted_at=0.0, status="running")
    store.add(job)
    toasts: list[str] = []
    monkeypatch.setattr(p._toast, "show_message", lambda text, *a, **k: toasts.append(text))
    job.status = "done"
    job.final_text = "all good"
    return p, toasts


def test_completion_toast_suppressed_while_sessions_window_active(app, monkeypatch):
    p, toasts = _panel_with_finished_job(app, monkeypatch)
    p.set_completion_toast_suppressor(lambda: True)
    p._on_job_updated(1)
    assert toasts == []
    p.deleteLater()


def test_completion_toast_shown_when_in_background(app, monkeypatch):
    p, toasts = _panel_with_finished_job(app, monkeypatch)
    p.set_completion_toast_suppressor(lambda: False)
    p._on_job_updated(1)
    assert toasts == ["✓ “t”: all good"]
    p.deleteLater()


def test_completion_toast_default_and_broken_predicate_still_notify(app, monkeypatch):
    p, toasts = _panel_with_finished_job(app, monkeypatch)
    p._on_job_updated(1)  # no predicate installed

    def _boom():
        raise RuntimeError("window gone")

    p.set_completion_toast_suppressor(_boom)
    p._on_job_updated(1)
    assert len(toasts) == 2
    p.deleteLater()


# ── 5. scoped stylesheets ───────────────────────────────────────────────────

_ACCENT = QtGui.QColor("#58a6ff")


def _has_color(img: QtGui.QImage, color: QtGui.QColor, tol: int = 12) -> bool:
    img = img.convertToFormat(QtGui.QImage.Format_ARGB32)
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 200 and all(
                abs(a - b) <= tol for a, b in
                ((c.red(), color.red()), (c.green(), color.green()), (c.blue(), color.blue()))
            ):
                return True
    return False


def test_toast_border_not_drawn_by_label(app):
    from holler.panel import Toast

    t = Toast()
    t.show_message("hello there", (900, 500))
    app.processEvents()
    try:
        # The card draws the accent frame; the label inside must not.
        assert not _has_color(t._label.grab().toImage(), _ACCENT)
        assert _has_color(t.grab().toImage(), _ACCENT)
        # Padding: the label sits inside the card, not flush with its edge.
        assert t._label.geometry().left() >= 10
    finally:
        t.hide()
        t.deleteLater()


def test_routing_pill_rule_does_not_style_label(app):
    from holler.panel import Panel

    p = Panel(JobStore(None), corner="top-right", width=380, model_label="M")
    try:
        pill_rule = p._routing_pill.styleSheet()
        assert "QFrame#routingPill" in pill_rule
        p.resize(380, 400)
        p.show()
        app.processEvents()
        img = p._routing_label.grab().toImage().convertToFormat(QtGui.QImage.Format_ARGB32)
        from holler import theme
        border = QtGui.QColor(theme.BORDER_SUBTLE)
        bottom = [img.pixelColor(x, img.height() - 1) for x in range(img.width())]
        assert not all(
            abs(c.red() - border.red()) <= 3 and abs(c.green() - border.green()) <= 3
            and abs(c.blue() - border.blue()) <= 3 for c in bottom
        ), "routing label still draws the pill's bottom border"
    finally:
        p.hide()
        p.deleteLater()
