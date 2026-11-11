"""Regression tests for committed-turn transcript rendering / persistence.

Bug: a committed turn rendered from the DB as "all tool calls, then
final_text". final_text is only the SDK result, so assistant text written
before / between tool calls vanished once the finished Job left memory (next
turn started, or app restart). v14 persists the ordered block sequence in
turns.blocks_json and the committed renderer replays it.

Also covers the DetailView single-job header reset, the monospace font stack
and the thumbnail stylesheet scoping.

Runs real (offscreen) widgets against a temp Store; the app-data dir is
redirected to tmp_path so nothing touches ~/Library/Application Support/holler.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtGui, QtWidgets  # noqa: E402

import holler.jobs  # noqa: E402,F401
import holler.sessions_window  # noqa: E402,F401
import holler.store  # noqa: E402,F401
from holler.sdk_session import SessionEvent  # noqa: E402

TEXT_A = "ALPHA preamble before running the command"
FINAL_B = "BRAVO final answer after the command"


@pytest.fixture
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)

    from holler.store import Store

    store = Store(tmp_path / "holler.db")
    windows = []

    def make_window(jobs):
        from holler.sessions_window import SessionsWindow

        w = SessionsWindow(store, jobs, tmp_path / "captures", tmp_path)
        windows.append(w)
        return w

    yield store, make_window
    for w in windows:
        w.close()
        w.deleteLater()
    store.close()


def _run_turn(jobs, store, sid, job_id, events, *, result="", is_error=False):
    """Drive one Job through JobStore exactly as sdk_session would."""
    tid = store.add_turn(session_id=sid, transcript=f"turn {job_id}",
                         screenshot_path=None, job_id=job_id)
    jobs.apply_event(SessionEvent(job_id, "submitted", {
        "transcript": f"turn {job_id}", "local_session_id": sid, "local_turn_id": tid,
    }))
    jobs.apply_event(SessionEvent(job_id, "started", {}))
    for kind, data in events:
        jobs.apply_event(SessionEvent(job_id, kind, data))
    if result is not None:
        jobs.apply_event(SessionEvent(job_id, "result", {
            "result": result, "is_error": is_error, "duration_ms": 1500,
        }))
    return tid


_TEXT_BASH_FINAL = [
    ("text_delta", {"text": TEXT_A}),
    ("tool_use", {"id": "tu1", "name": "Bash", "input": {"command": "echo CHARLIE_CMD"}}),
    ("tool_result", {"tool_use_id": "tu1", "content": "CHARLIE_OUT", "is_error": False}),
    ("text_delta", {"text": FINAL_B}),
]


def _plain(w) -> str:
    return w._detail._reply_browser.toPlainText()


def _assert_in_order(text: str, *needles: str) -> None:
    pos = [text.find(n) for n in needles]
    assert all(p >= 0 for p in pos), f"missing one of {needles}: {pos}\n{text}"
    assert pos == sorted(pos), f"out of order {needles}: {pos}\n{text}"


def test_text_between_tools_survives_new_turn_and_fresh_window(env):
    from holler.jobs import JobStore

    store, make_window = env
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    jobs = JobStore(store)
    t1 = _run_turn(jobs, store, sid, 1, _TEXT_BASH_FINAL, result=FINAL_B)

    turn = store.get_turns(sid)[0]
    assert turn.id == t1
    # final_text is unchanged: still exactly the SDK result.
    assert turn.final_text == FINAL_B
    assert json.loads(turn.blocks_json) == [
        {"t": "text", "text": TEXT_A}, {"t": "tool", "i": 0}, {"t": "text", "text": FINAL_B},
    ]

    # A new turn starts → turn 1 is now rendered from the DB, not the Job.
    _run_turn(jobs, store, sid, 2, [("text_delta", {"text": "DELTA live"})], result=None)
    w = make_window(jobs)
    w._render_session_detail(store.get_session(sid))
    text = _plain(w)
    _assert_in_order(text, TEXT_A, "Bash", FINAL_B, "DELTA live")
    assert text.count(FINAL_B) == 1, "final answer must not be duplicated"

    # Fresh window / app restart: no Job in memory at all.
    w2 = make_window(JobStore(store))
    w2._render_session_detail(store.get_session(sid))
    text2 = _plain(w2)
    _assert_in_order(text2, TEXT_A, "Bash", FINAL_B)
    assert text2.count(FINAL_B) == 1
    assert text2.count(TEXT_A) == 1


def test_legacy_row_without_blocks_renders_tools_then_final(env):
    from holler.jobs import JobStore

    store, make_window = env
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    t1 = _run_turn(JobStore(store), store, sid, 1, _TEXT_BASH_FINAL, result=FINAL_B)
    store.update_turn(t1, blocks_json="")  # simulate a pre-v14 row

    w = make_window(JobStore(store))
    w._render_session_detail(store.get_session(sid))
    text = _plain(w)
    _assert_in_order(text, "Bash", FINAL_B)
    assert TEXT_A not in text  # legacy behavior: only final_text is known


@pytest.mark.parametrize("bad", [
    "{not json",
    json.dumps({"t": "text"}),
    json.dumps([{"t": "tool", "i": 5}]),                     # out of range
    json.dumps([{"t": "tool", "i": 0}, {"t": "tool", "i": 0}]),  # repeated
    json.dumps([{"t": "text", "text": "ZULU only"}]),         # drops the tool
    json.dumps([{"t": "weird"}]),
])
def test_malformed_blocks_fall_back_to_legacy(env, bad):
    from holler.detail import _render_committed_turn_html
    from holler.jobs import JobStore

    store, _ = env
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    t1 = _run_turn(JobStore(store), store, sid, 1, _TEXT_BASH_FINAL, result=FINAL_B)
    store.update_turn(t1, blocks_json=bad)
    turn = store.get_turns(sid)[0]
    calls = store.get_tool_calls(t1)
    legacy = turn.__class__(**{**turn.__dict__, "blocks_json": ""})
    assert _render_committed_turn_html(turn, calls) == _render_committed_turn_html(legacy, calls)


def test_merged_turns_unaffected(env):
    from holler.jobs import JobStore

    store, make_window = env
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    jobs = JobStore(store)
    t1 = store.add_turn(session_id=sid, transcript="turn 1", screenshot_path=None, job_id=1)
    t2 = store.add_turn(session_id=sid, transcript="turn 2 merged", screenshot_path=None, job_id=None)
    jobs.apply_event(SessionEvent(1, "submitted", {
        "transcript": "turn 1", "local_session_id": sid, "local_turn_id": t1,
    }))
    jobs.apply_event(SessionEvent(1, "merged_in", {"local_turn_id": t2, "transcript": "turn 2 merged"}))
    for kind, data in _TEXT_BASH_FINAL:
        jobs.apply_event(SessionEvent(1, kind, data))
    jobs.apply_event(SessionEvent(1, "result", {"result": FINAL_B, "duration_ms": 10}))

    turns = {t.id: t for t in store.get_turns(sid)}
    assert turns[t2].final_text == FINAL_B
    assert turns[t2].blocks_json == ""  # merged turn keeps the legacy shape
    assert store.get_tool_calls(t2) == []
    assert turns[t1].blocks_json


def test_anchor_before_final_answer(env):
    from holler.detail import _render_committed_turn_html
    from holler.jobs import JobStore

    store, _ = env
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    t1 = _run_turn(JobStore(store), store, sid, 1, _TEXT_BASH_FINAL, result=FINAL_B)
    turn = store.get_turns(sid)[0]
    html = _render_committed_turn_html(turn, store.get_tool_calls(t1), anchor_before_reply=True)
    assert html.count("latest-reply-top") == 1
    a = html.index("latest-reply-top")
    assert html.index(TEXT_A) < html.index("CHARLIE_CMD") < a < html.index(FINAL_B)


def test_error_turn_streamed_text_not_duplicated(env):
    """No result → final_text is all streamed text concatenated; the blocks
    already contain it, so nothing is appended twice."""
    from holler.detail import _render_committed_turn_html
    from holler.jobs import JobStore

    store, _ = env
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    jobs = JobStore(store)
    t1 = _run_turn(jobs, store, sid, 1, _TEXT_BASH_FINAL, result=None)
    jobs.apply_event(SessionEvent(1, "error", {"error": "boom"}))
    turn = store.get_turns(sid)[0]
    assert turn.final_text == TEXT_A + FINAL_B
    html = _render_committed_turn_html(turn, store.get_tool_calls(t1))
    assert html.count(FINAL_B) == 1 and html.count(TEXT_A) == 1


def test_tool_log_cap_drops_trimmed_tool_blocks():
    from holler.jobs import _TOOL_LOG_CAP, Job, JobStore

    jobs = JobStore(None)
    jobs.add(Job(id=1, transcript="t", submitted_at=0.0))
    for n in range(_TOOL_LOG_CAP + 3):
        jobs.apply_event(SessionEvent(1, "tool_use", {"id": f"u{n}", "name": "Read", "input": {}}))
    job = jobs.get(1)
    blocks = json.loads(holler.jobs._blocks_json(job))
    assert [b["i"] for b in blocks] == list(range(_TOOL_LOG_CAP))


def test_v14_migration_upgrades_v13_db(tmp_path):
    from holler.store import _MIGRATIONS, Store

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    for fn in _MIGRATIONS[:13]:
        fn(c)
    c.execute("PRAGMA user_version = 13")
    c.execute(
        "INSERT INTO sessions(title, transcript, model, created_at, last_activity_at, "
        "status, pinned, archived, notes) VALUES ('s', 't', 'm', 0, 0, 'idle', 0, 0, '')"
    )
    c.execute(
        "INSERT INTO turns(session_id, seq, transcript, final_text, duration_ms, "
        "started_at, ended_at, is_error) VALUES (1, 1, 'q', 'old answer', 0, 1, 2, 0)"
    )
    c.commit()
    c.close()

    s = Store(db)
    try:
        turn = s.get_turns(1)[0]
        assert turn.final_text == "old answer" and turn.blocks_json == ""
        assert s._conn().execute("PRAGMA user_version").fetchone()[0] == len(_MIGRATIONS)
    finally:
        s.close()
    # Re-open: idempotent.
    Store(db).close()


# ---------------------------------------------------------------- header

def test_show_job_resets_stale_session_header(env):
    from holler.detail import DetailView
    from holler.jobs import Job

    store, _ = env
    sid = store.create_session(model="m", transcript="x", screenshot_path=None,
                               title="OLD SESSION TITLE")
    view = DetailView()
    view.show_session(store.get_session(sid), [], {})
    job = Job(id=7, transcript="popup job transcript", submitted_at=0.0, status="running",
              started_at=1.0)
    view.show_job(job)
    view._tick_spinner()
    assert view._transcript.text() == "popup job transcript"


def test_show_job_of_same_session_keeps_session_header(env):
    """Sessions window first-turn path: show_session(S) then show_job(job of
    S) — the header must stay the session's, not flip to the transcript."""
    from holler.detail import DetailView
    from holler.jobs import Job

    store, _ = env
    sid = store.create_session(model="m", transcript="x", screenshot_path=None,
                               title="SAME SESSION TITLE")
    view = DetailView()
    view.show_session(store.get_session(sid), [], {})
    job = Job(id=8, transcript="first turn transcript", submitted_at=0.0, status="running",
              started_at=1.0, local_session_id=sid)
    view.show_job(job)
    view._tick_spinner()
    assert "SAME SESSION TITLE" in view._transcript.text()


# ---------------------------------------------------------------- fonts

@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps Cascadia/Consolas")
def test_mono_font_is_a_real_fixed_pitch_family(qapp):
    from holler.markdown_render import mono_font_family, mono_font_stack, render

    fam = mono_font_family()
    assert fam in QtGui.QFontDatabase.families()
    assert QtGui.QFontInfo(QtGui.QFont(fam)).fixedPitch()
    assert "Cascadia" not in mono_font_stack()
    assert "Cascadia" not in (render("`x`\n\n```\ncode\n```") or "")


def test_detail_html_and_qss_use_mono_stack(qapp):
    from holler import detail
    from holler.markdown_render import mono_font_stack

    html = detail._render_tool_block_html("Bash", {"command": "ls"}, "out", False, False)
    assert mono_font_stack('"') in html
    assert mono_font_stack() in detail._detail_qss()
    assert "__MONO__" not in detail._detail_qss()


# ---------------------------------------------------------------- thumbnail

def test_thumbnail_stylesheet_is_one_scoped_rule(qapp):
    from holler.detail import _ClickableThumbnail

    t = _ClickableThumbnail()
    qss = t.styleSheet().strip()
    assert qss.startswith("QLabel {") and qss.count("{") == 1 and qss.endswith("}")
    t.ensurePolished()
    from holler import theme
    assert t.font().pointSizeF() == theme.FONT_XS


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps the Cascadia/Consolas stack")
def test_tool_block_text_actually_renders_mono(qapp):
    """Inline font-family doesn't cascade into child spans/cells in
    QTextDocument once the stylesheet sets body's font; the class='mono'
    rule must make the tool head + result rows fixed-pitch."""
    from holler import detail
    from holler.markdown_render import mono_font_family, wrap_document

    html = wrap_document(detail._render_tool_block_html(
        "Bash", {"command": "lsQQ"}, "outZZ\nmore", False, False))
    doc = QtGui.QTextDocument()
    doc.setHtml(html)
    text = doc.toPlainText()
    for needle in ("Bash", "lsQQ", "outZZ"):
        cur = QtGui.QTextCursor(doc)
        cur.setPosition(text.index(needle) + 1)
        assert cur.charFormat().fontFamilies() == [mono_font_family()], needle


@pytest.mark.parametrize("blocks,final,needles", [
    # Consecutive deltas glued into one block; the result is only the tail.
    ([{"t": "text", "text": "ALPHA-ONE BETA-TWO"}, {"t": "tool", "i": 0}],
     "BETA-TWO", ["ALPHA-ONE", "BETA-TWO"]),
    # Errored turn: final_text is all streamed text, including a
    # whitespace-only segment blocks_json dropped.
    ([{"t": "text", "text": "ALPHA-ONE"}, {"t": "tool", "i": 0},
      {"t": "text", "text": "GAMMA-THREE"}],
     "ALPHA-ONE\n\nGAMMA-THREE", ["ALPHA-ONE", "GAMMA-THREE"]),
])
def test_final_text_not_duplicated_when_blocks_glued_or_whitespace_dropped(
        env, blocks, final, needles):
    from holler.detail import _render_committed_turn_html

    store, _ = env
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    tid = store.add_turn(session_id=sid, transcript="t", screenshot_path=None, job_id=None)
    tc = store.add_tool_call(turn_id=tid, name="Bash", input_json='{"command": "ls"}', started_at=1.0)
    store.update_tool_call(tc, result_json="ok", is_error=0, ended_at=2.0)
    store.update_turn(tid, final_text=final, blocks_json=json.dumps(blocks), ended_at=3.0)
    turn = store.get_turns(sid)[0]
    html = _render_committed_turn_html(turn, store.get_tool_calls(tid))
    for n in needles:
        assert html.count(n) == 1, (n, html.count(n))


def test_final_text_absent_from_blocks_is_still_appended(env):
    from holler.detail import _render_committed_turn_html

    store, _ = env
    sid = store.create_session(model="m", transcript="t", screenshot_path=None)
    tid = store.add_turn(session_id=sid, transcript="t", screenshot_path=None, job_id=None)
    blocks = [{"t": "text", "text": "STREAMED-ONLY"}]
    store.update_turn(tid, final_text="RESULT-ONLY", blocks_json=json.dumps(blocks), ended_at=3.0)
    html = _render_committed_turn_html(store.get_turns(sid)[0], [])
    assert html.count("STREAMED-ONLY") == 1 and html.count("RESULT-ONLY") == 1


def test_reply_persists_even_if_blocks_json_write_fails(env, monkeypatch):
    """A DB without the blocks_json column must still get final_text."""
    from holler.jobs import JobStore

    store, _ = env
    sid = store.create_session(model="m", transcript="turn 1", screenshot_path=None)
    real = store.update_turn

    def picky(turn_id, **fields):
        if "blocks_json" in fields:
            raise RuntimeError("no such column: blocks_json")
        return real(turn_id, **fields)

    monkeypatch.setattr(store, "update_turn", picky)
    jobs = JobStore(store)
    t1 = _run_turn(jobs, store, sid, 1, _TEXT_BASH_FINAL, result=FINAL_B)
    turn = store.get_turns(sid)[0]
    assert turn.final_text == FINAL_B
    assert turn.blocks_json == ""
    assert len(store.get_tool_calls(t1)) == 1
