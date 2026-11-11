"""Where a push-to-talk hold goes (operator rule, 2026-10-14), fixed at press:
  - a chain armed → every hold is added to it until "Start session";
  - a session selected → the hold goes into THAT session, whichever app is
    in front (a Canvas screenshot lands in the session you're working in);
  - nothing selected → a NEW session through the Superintendent (evidence →
    worker), or a plain new session with Superintendent off.
"""

from __future__ import annotations

import threading

import numpy as np
from PIL import Image

from holler.runners import full
from holler.store import Store
from holler.transcriber import Transcription


class _Tr:
    def __init__(self, text="look at this"):
        self.text = text

    def transcribe_detailed(self, audio):
        return Transcription(self.text, 2.0, 1.5, no_speech_prob=0.01)


class _Session:
    def __init__(self):
        self.calls = []

    def submit(self, image_b64, transcript, **kw):
        self.calls.append((image_b64, transcript, kw))
        return 1

    def will_reuse_client(self, *a, **k):
        return False


class _Cfg:
    debug_save_captures = False
    stroke_color = "#f00"
    stroke_width = 4
    max_screenshot_long_edge = 1280
    mic_device = ""
    claude_model = "m"
    def __init__(self, tmp):
        self.captures_dir = tmp
        self.claude_models: list[str] = []


class _Snap:
    image = Image.new("RGB", (200, 100))
    rect = (0, 0, 100, 50)
    permission_limited = False
    logical_size = (100, 50)


def _state(tmp_path, *, inbox: bool):
    store = Store(tmp_path / "holler.db")
    selected = store.create_session(model="m", transcript="unrelated work", screenshot_path=None)
    store.attach_sdk_session_id(selected, "sdk-selected")
    st = {
        "cfg": _Cfg(tmp_path),
        "persistent_store": store,
        "active_session_id": selected,  # the operator is looking at another session
        "session_chains": {},
        "session_chain_lock": threading.Lock(),
        "superintendent_draft": None,
        "superintendent_draft_lock": threading.Lock(),
    }
    if inbox:
        st["superintendent_inbox_sid"] = store.ensure_superintendent_inbox()
    return st, store, selected


def _hold(monkeypatch, st, sess, text="look at this", tmp_path=None):
    """One hold, routed exactly as the press handler does (target fixed at press)."""
    monkeypatch.setattr(full, "build_append", lambda **k: "")
    target = full._ptt_target_sid(st)
    full._dispatch_job(_Snap(), [], np.zeros(16000, dtype=np.float32), _Tr(text),
                       sess, st["cfg"], st, panel=None,
                       thumb_target=(tmp_path / f"shot-{len(sess.calls)}.png") if tmp_path else None,
                       ptt_target=target)


def _evidence_rows(store):
    return store._conn().execute("SELECT id, transcript FROM superintendent_evidence ORDER BY id").fetchall()


def test_hold_with_nothing_selected_goes_to_superintendent(monkeypatch, tmp_path):
    st, store, selected = _state(tmp_path, inbox=True)
    st["active_session_id"] = None
    sess = _Session()
    _hold(monkeypatch, st, sess, tmp_path=tmp_path)
    _hold(monkeypatch, st, sess, "a second, separate thing", tmp_path=tmp_path)

    assert sess.calls == [], "no turn may be submitted into the selected session"
    assert len(store.get_turns(selected)) == 0
    rows = _evidence_rows(store)
    assert [r["transcript"] for r in rows] == ["look at this", "a second, separate thing"]
    assert st["active_session_id"] is None  # dispatch doesn't pin a session


def test_without_superintendent_unselected_holds_are_new_sessions(monkeypatch, tmp_path):
    st, _store, selected = _state(tmp_path, inbox=False)
    st["active_session_id"] = None
    sess = _Session()
    _hold(monkeypatch, st, sess)
    _hold(monkeypatch, st, sess, "follow up on unrelated work please")

    sids = [kw["local_session_id"] for _img, _t, kw in sess.calls]
    assert len(sids) == 2 and selected not in sids and sids[0] != sids[1]
    assert all(kw["resume_session_id"] is None for _i, _t, kw in sess.calls)


def test_armed_chain_holds_every_capture_until_start_session(monkeypatch, tmp_path):
    st, store, selected = _state(tmp_path, inbox=True)
    full._superintendent_chain_control("arm", st, store, panel=None)
    sess = _Session()
    for text in ("first screen", "second screen", "third screen"):
        _hold(monkeypatch, st, sess, text, tmp_path=tmp_path)

    assert sess.calls == [] and _evidence_rows(store) == []
    assert full._capture_destination(st, full._ptt_target_sid(st)).startswith("→ Added to chain (4)")

    full._superintendent_chain_control("file", st, store, panel=None)  # ▶ Start session
    rows = _evidence_rows(store)
    assert len(rows) == 1
    assert all(t in rows[0]["transcript"] for t in ("first screen", "second screen", "third screen"))
    assert not st.get("superintendent_chain_armed")
    assert len(store.get_turns(selected)) == 0


def test_chain_without_superintendent_files_one_new_session(monkeypatch, tmp_path):
    st, store, selected = _state(tmp_path, inbox=False)
    sess = _Session()
    full._session_chain_control("arm", sess, st, store, panel=None)
    for text in ("one", "two"):
        _hold(monkeypatch, st, sess, text)
    assert sess.calls == []
    full._session_chain_control("file", sess, st, store, panel=None)
    assert len(sess.calls) == 1
    _img, text, kw = sess.calls[0]
    assert text == "one\n\ntwo" and kw["local_session_id"] != selected


def test_typed_text_still_continues_the_selected_session(tmp_path):
    st, _store, selected = _state(tmp_path, inbox=True)
    sess = _Session()
    full._dispatch_text_job("more on this", sess, st["cfg"], st, panel=None)
    assert sess.calls[0][2]["local_session_id"] == selected


def test_hold_from_any_app_continues_the_selected_session(monkeypatch, tmp_path):
    st, store, selected = _state(tmp_path, inbox=True)
    sess = _Session()
    _hold(monkeypatch, st, sess, "tweak that button")
    assert len(sess.calls) == 1
    _img, text, kw = sess.calls[0]
    assert kw["local_session_id"] == selected and kw["resume_session_id"] == "sdk-selected"
    assert text == "tweak that button"
    assert _evidence_rows(store) == []


def test_hold_with_nothing_selected_is_new(monkeypatch, tmp_path):
    st, store, _selected = _state(tmp_path, inbox=True)
    st["active_session_id"] = None
    sess = _Session()
    _hold(monkeypatch, st, sess)
    assert sess.calls == [] and len(_evidence_rows(store)) == 1


def test_hold_with_the_inbox_selected_files_evidence(monkeypatch, tmp_path):
    st, store, _selected = _state(tmp_path, inbox=True)
    st["active_session_id"] = st["superintendent_inbox_sid"]
    sess = _Session()
    _hold(monkeypatch, st, sess)
    assert sess.calls == [] and len(_evidence_rows(store)) == 1


def test_armed_chain_wins_over_the_selection(monkeypatch, tmp_path):
    st, store, selected = _state(tmp_path, inbox=True)
    full._superintendent_chain_control("arm", st, store, panel=None)
    sess = _Session()
    _hold(monkeypatch, st, sess, "from canvas", tmp_path=tmp_path)
    _hold(monkeypatch, st, sess, "peeked at holler", tmp_path=tmp_path)
    assert sess.calls == [] and _evidence_rows(store) == []
    full._superintendent_chain_control("file", st, store, panel=None)
    (row,) = _evidence_rows(store)
    assert "from canvas" in row["transcript"] and "peeked at holler" in row["transcript"]
    assert len(store.get_turns(selected)) == 0


def test_target_is_fixed_at_press_time(monkeypatch, tmp_path):
    """Clicking another session while the hold transcribes doesn't move it."""
    st, store, selected = _state(tmp_path, inbox=True)
    target = full._ptt_target_sid(st)
    other = store.create_session(model="m", transcript="other", screenshot_path=None)
    st["active_session_id"] = other
    sess = _Session()
    monkeypatch.setattr(full, "build_append", lambda **k: "")
    full._dispatch_job(_Snap(), [], np.zeros(16000, dtype=np.float32), _Tr(),
                       sess, st["cfg"], st, panel=None, ptt_target=target)
    assert sess.calls[0][2]["local_session_id"] == selected
