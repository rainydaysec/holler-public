"""Regression tests for SessionsWindow selection / archive / quota bugs
(catalogue group C: C1–C11).

Runs a real (offscreen) SessionsWindow against a temp Store + JobStore. The
app-data dir is redirected to tmp_path so nothing touches the operator's real
~/Library/Application Support/holler.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtGui, QtWidgets  # noqa: E402

# Import the real modules at collection time: test_sidebar_organization stubs
# PySide6 / claude_agent_sdk in sys.modules if they aren't loaded yet.
import holler.jobs  # noqa: E402,F401
import holler.sessions_window  # noqa: E402,F401
import holler.store  # noqa: E402,F401


@pytest.fixture
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    import holler.appdir as appdir
    from holler import state as _state

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **kw: QtWidgets.QMessageBox.Yes),
    )

    from holler.jobs import JobStore
    from holler.sessions_window import SessionsWindow
    from holler.store import Store

    store = Store(tmp_path / "holler.db")
    jobs = JobStore(store)
    w = SessionsWindow(store, jobs, tmp_path / "captures", tmp_path)
    w._test_emitted = []
    w.session_selected.connect(w._test_emitted.append)
    w._test_notices = []
    monkeypatch.setattr(
        w, "show_inline_notice",
        lambda text, **kw: w._test_notices.append(text),
    )
    yield w
    w.close()
    w.deleteLater()
    store.close()


def _mk(w, n: int, *, status: str = "idle") -> list[int]:
    ids = [
        w._store.create_session(
            model="m", transcript=f"session {i}", screenshot_path=None, status=status
        )
        for i in range(n)
    ]
    w._refresh_list()
    return ids


def _listed_ids(w) -> list[int]:
    out = []
    for i in range(w._list.count()):
        it = w._list.item(i)
        s = it.data(0x0100)  # Qt.UserRole
        if s is not None and not it.data(0x0101):
            out.append(s.id)
    return out


def _header_count(w) -> int:
    return sum(1 for i in range(w._list.count()) if w._list.item(i).data(0x0101))


def _add_job(w, job_id: int, sid: int, status: str):
    from holler.jobs import Job

    j = Job(id=job_id, transcript="t", submitted_at=time.time(), status=status)
    j.local_session_id = sid
    w._job_store.add(j)
    return j


def _assert_cleared(w):
    assert w._active_session_id is None
    assert w._test_emitted and w._test_emitted[-1] == -1
    assert w._routing_pill.text().startswith("→ New")
    assert "start a new session" in w._text_input.placeholderText()
    assert not any(rw._selected for rw in w._row_widgets.values())


# ── C1 / C5: archive + delete of the active session clear routing ───────────


def test_c5_row_archive_of_active_clears_routing(win):
    ids = _mk(win, 2)
    win.select_session(ids[0])
    assert win._active_session_id == ids[0]
    assert "continue this session" in win._text_input.placeholderText()
    win._on_archive_requested(ids[0])
    _assert_cleared(win)


def test_c1_context_menu_archive_of_active_clears_routing(win, monkeypatch):
    ids = _mk(win, 2)
    win.select_session(ids[1])
    row = win._list.row(win._list.currentItem())
    pos = win._list.visualItemRect(win._list.item(row)).center()

    class _AutoMenu(QtWidgets.QMenu):
        def exec(self, *_a, **_kw):  # pick "Archive" instead of blocking
            return next(a for a in self.actions() if a.text() == "Archive")

    monkeypatch.setattr(QtWidgets, "QMenu", _AutoMenu)
    win._show_context_menu(pos)
    assert win._store.get_session(ids[1]).archived
    _assert_cleared(win)


def test_c1_delete_of_active_clears_routing(win):
    ids = _mk(win, 2)
    win.select_session(ids[0])
    win._delete_session(win._store.get_session(ids[0]))
    assert win._store.get_session(ids[0]) is None
    _assert_cleared(win)


def test_c1_delete_refused_while_turn_running(win):
    ids = _mk(win, 1)
    _add_job(win, 1, ids[0], "running")
    win._delete_session(win._store.get_session(ids[0]))
    assert win._store.get_session(ids[0]) is not None
    assert any("while it's working" in n for n in win._test_notices)


# ── C2: bulk archive clears active session ──────────────────────────────────


def test_c2_bulk_archive_including_active_clears_routing(win):
    ids = _mk(win, 3)
    win.select_session(ids[0])
    items = [
        win._list.item(i) for i in range(win._list.count())
        if win._list.item(i).data(0x0100) is not None
        and win._list.item(i).data(0x0100).id in (ids[0], ids[1])
    ]
    win._bulk_archive(items)
    assert win._store.get_session(ids[0]).archived
    _assert_cleared(win)


# ── C3: select_session reveals rows hidden by filter / search ───────────────


def test_c3_select_session_switches_filter_to_show_hidden_row(win):
    ids = _mk(win, 2)
    win._store.mark_reviewed(ids[0], True)
    win._set_filter("active")
    assert ids[0] not in _listed_ids(win)
    win.select_session(ids[0])
    assert win._current_filter == "done"
    assert win._active_session_id == ids[0]


def test_c3_select_session_clears_non_matching_search(win):
    ids = _mk(win, 2)
    win._search.setText("zzz-no-match")
    win._search_timer.stop()
    win._refresh_list()
    assert ids[1] not in _listed_ids(win)
    win.select_session(ids[1])
    assert win._search.text() == ""
    assert win._active_session_id == ids[1]


def test_c3_select_archived_session_switches_to_archived(win):
    ids = _mk(win, 2)
    win._store.archive(ids[0], True)
    win._refresh_list()
    win.select_session(ids[0])
    assert win._current_filter == "archived"
    assert win._active_session_id == ids[0]


# ── C4: same-session reselect still restores cheap chrome ───────────────────


def test_c4_refresh_after_multiselect_restores_status_and_row_state(win):
    ids = _mk(win, 3)
    win.select_session(ids[0])
    win._list.selectAll()
    assert "selected" in win._ptt_status.text()
    win._refresh_list()  # rebuild → re-selects the same (active) row
    assert "continue" in win._ptt_status.text()
    assert win._row_widgets[ids[0]]._selected is True


# ── C6: chained Delete lands on the row that followed the archived one ──────


def test_c6_chained_delete_selects_following_row(win):
    ids = _mk(win, 4)  # listed newest-first
    order = _listed_ids(win)
    win.select_session(order[0])
    win._archive_focused_row()
    assert win._active_session_id == order[1]
    win._archive_focused_row()
    assert win._active_session_id == order[2]


# ── C7: no time-bucket headers under non-recency sort ───────────────────────


def test_c7_no_bucket_headers_under_title_sort(win):
    _mk(win, 3)
    win._set_filter("active")
    assert _header_count(win) >= 1  # recency sort buckets
    win._sort_key = "title"
    win._refresh_list()
    assert _header_count(win) == 0


# ── C8: ⏹ / ticker target the running job, not a queued follow-up ──────────


def test_c8_live_job_prefers_running_over_newer_queued(win):
    ids = _mk(win, 1)
    _add_job(win, 10, ids[0], "running")
    _add_job(win, 11, ids[0], "queued")
    assert win._live_job_for(ids[0]).id == 10
    cancelled = []
    win.cancel_job_requested.connect(cancelled.append)
    win._on_row_cancel_requested(ids[0])
    assert cancelled == [10]


# ── C9: ElidedLabel re-pins its height when the font changes ────────────────


def test_c9_elided_label_height_tracks_font(qapp):
    from holler.sessions_window import ElidedLabel

    lbl = ElidedLabel(max_lines=2)
    f = QtGui.QFont(lbl.font())
    f.setPointSize(f.pointSize() * 3 if f.pointSize() > 0 else 36)
    lbl.setFont(f)
    qapp.processEvents()
    assert lbl.height() == 2 * lbl.fontMetrics().lineSpacing()


# ── C10: stuck-detection map pruned once a job finishes ─────────────────────


def test_c10_tool_first_seen_pruned_on_finish(win):
    ids = _mk(win, 1)
    job = _add_job(win, 20, ids[0], "running")
    win._live_state_for(ids[0], time.time())
    assert 20 in win._tool_first_seen
    job.status = "done"
    win._on_job_updated(20)
    assert 20 not in win._tool_first_seen


# ── C11: quota chip with utilization=None and stale windows ─────────────────


def test_c11_status_only_snapshot_is_shown(win):
    win.resize(1220, 760)  # wide header: quota has its own chip
    now = time.time()
    win._job_store._rate_limits = {
        "seven_day": {"status": "rejected", "utilization": None,
                      "resets_at": now + 3600, "seen_at": now},
        "five_hour": {"status": "allowed", "utilization": None,
                      "resets_at": now + 7200, "seen_at": now},
    }
    win._refresh_quota()
    assert not win._quota.isHidden()
    assert "7d limit reached" in win._quota.text()
    assert "5h OK" in win._quota.text()
    assert win._quota.text().startswith("⚠")  # not by colour alone
    from holler import sessions_window as sw

    assert sw._RED in win._quota.styleSheet()


def test_c11_past_reset_window_not_rejected(win):
    from holler import sessions_window as sw

    now = time.time()
    win._job_store._rate_limits = {
        "seven_day": {"status": "rejected", "utilization": None,
                      "resets_at": now - 3600, "seen_at": now - 90000},
        "five_hour": {"status": "allowed", "utilization": 0.4,
                      "resets_at": now + 7200, "seen_at": now},
    }
    win._refresh_quota()
    text = win._quota.text()
    assert "limit reached" not in text
    assert "7d reset" in text
    assert "5h 40%" in text
    assert sw._RED not in win._quota.styleSheet()


def test_selecting_session_renders_detail_without_slot_exception(win, monkeypatch, tmp_path):
    """Qt swallows exceptions raised inside slots (they only reach
    sys.excepthook), so a crash in _render_session_detail left the detail pane
    blank while every other assertion still passed. Capture them explicitly."""
    import sys

    errors: list[BaseException] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: errors.append(v))
    seen: list = []
    monkeypatch.setattr(win._detail, "set_repo_cwd", seen.append)
    ids = _mk(win, 2)
    for sid in ids:  # a session with no turns short-circuits before rendering
        win._store.add_turn(session_id=sid, transcript="hi", screenshot_path=None, job_id=None)
    win._active_session_id = None
    for sid in ids:
        win.select_session(sid)
    assert not errors, errors
    assert seen and all(p == tmp_path for p in seen), \
        "relative links must resolve against the repo"

    # A session with a live worktree resolves links inside that worktree.
    wt = tmp_path / "wt"
    wt.mkdir()
    from holler.sessions_window import _link_root
    sess = win._store.get_session(ids[1])
    import dataclasses
    sess = dataclasses.replace(sess, worktree_state="active", worktree_path=str(wt))
    assert _link_root(sess, tmp_path) == wt
