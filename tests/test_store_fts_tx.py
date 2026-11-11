"""Regression tests for Store FTS bookkeeping, transactions, and per-thread
connection cleanup (catalogue items A1, A2, A3, A10 + the v13 FTS resync).

All DBs live under tmp_path — never the real app data dir.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from holler.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "holler.db")
    if not s._fts_enabled:
        pytest.skip("FTS5 not available in this sqlite build")
    yield s
    s.close()


def _ids(store: Store, q: str) -> set[int]:
    return {s.id for s in store.search(q, include_archived=True)}


def _integrity(store: Store) -> None:
    store._conn().execute(
        "INSERT INTO sessions_fts(sessions_fts) VALUES('integrity-check')"
    )


# ---- A1: set_title ----


def test_rename_drops_old_title_and_keeps_final_text(store: Store) -> None:
    sid = store.create_session(model="m", transcript="hello", screenshot_path=None,
                               title="zebra title")
    tid = store.add_turn(session_id=sid, transcript="t", screenshot_path=None, job_id=None)
    store.update_turn(tid, final_text="giraffe answer", ended_at=1.0)
    assert _ids(store, "giraffe") == {sid}

    store.set_title(sid, "walrus title")

    assert _ids(store, "zebra") == set()          # old title gone
    assert _ids(store, "walrus") == {sid}
    assert _ids(store, "giraffe") == {sid}        # final_text survives rename
    _integrity(store)

    # delete() must still be able to remove exactly what's indexed.
    store.delete(sid)
    for q in ("walrus", "giraffe", "hello"):
        assert _ids(store, q) == set()
    _integrity(store)


# ---- A2: update_turn with aggregated final_text ----


def test_update_turn_replaces_stale_final_text(store: Store) -> None:
    sid = store.create_session(model="m", transcript="hello", screenshot_path=None)
    t1 = store.add_turn(session_id=sid, transcript="a", screenshot_path=None, job_id=None)
    store.update_turn(t1, final_text="alpha", ended_at=1.0)
    t2 = store.add_turn(session_id=sid, transcript="b", screenshot_path=None, job_id=None)
    store.update_turn(t2, final_text="bravo", ended_at=2.0)
    assert _ids(store, "bravo") == {sid}

    store.update_turn(t2, final_text="charlie")

    assert _ids(store, "bravo") == set()
    assert _ids(store, "alpha") == {sid}
    assert _ids(store, "charlie") == {sid}
    _integrity(store)
    store.delete(sid)
    for q in ("alpha", "charlie", "hello"):
        assert _ids(store, q) == set()


# ---- v13: resync of an index corrupted by the old code ----


def test_v13_migration_resyncs_stale_index(tmp_path: Path) -> None:
    db = tmp_path / "holler.db"
    s = Store(db)
    if not s._fts_enabled:
        pytest.skip("FTS5 not available")
    sid = s.create_session(model="m", transcript="hello", screenshot_path=None,
                           title="newtitle")
    c = s._conn()
    # Simulate the legacy bug: a stale title left in the index.
    c.execute("INSERT INTO sessions_fts(rowid, title, transcript, final_text) "
              "VALUES (?, 'oldtitle', '', '')", (sid,))
    c.execute("PRAGMA user_version = 12")
    s.close()

    s2 = Store(db)
    assert _ids(s2, "oldtitle") == set()
    assert _ids(s2, "newtitle") == {sid}
    assert s2._conn().execute("PRAGMA user_version").fetchone()[0] >= 13
    s2.close()


# ---- A3: transactions ----


def test_tx_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store._tx() as c:
            c.execute("INSERT INTO sessions(title, created_at, last_activity_at) "
                      "VALUES ('x', 0, 0)")
            raise RuntimeError("boom")
    assert store.list_sessions(include_archived=True) == []
    assert not store._conn().in_transaction


def test_nested_tx_joins_outer(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store._tx():
            store.create_session(model="m", transcript="inner", screenshot_path=None)
            raise RuntimeError("boom")
    # The nested create_session was rolled back with the outer transaction.
    assert store.list_sessions(include_archived=True) == []


def test_set_title_is_atomic(store: Store, monkeypatch) -> None:
    sid = store.create_session(model="m", transcript="hello", screenshot_path=None,
                               title="original")

    def boom(*a, **k):
        raise sqlite3.OperationalError("simulated FTS failure")

    monkeypatch.setattr(Store, "_fts_insert", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        store.set_title(sid, "renamed")
    monkeypatch.undo()

    assert store.get_session(sid).title == "original"
    assert _ids(store, "original") == {sid}  # FTS delete rolled back too
    _integrity(store)


# ---- A10: per-thread connection cleanup ----


def test_close_thread_connection(store: Store) -> None:
    result = {}

    def worker():
        store.get_session(1)
        conn = store._tls.conn
        result["before"] = conn in store._all_conns
        store.close_thread_connection()
        result["after"] = conn in store._all_conns
        try:
            conn.execute("SELECT 1")
            result["closed"] = False
        except sqlite3.ProgrammingError:
            result["closed"] = True
        store.close_thread_connection()  # idempotent
        # Re-use transparently reopens.
        result["reopen"] = store.list_sessions() == []
        store.close_thread_connection()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result == {"before": True, "after": False, "closed": True, "reopen": True}
    # Main thread's connection unaffected.
    assert store.list_sessions() == []
    store.close_thread_connection()  # safe on main thread too
    assert store.list_sessions() == []
