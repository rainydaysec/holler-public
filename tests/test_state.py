"""Regression tests for holler.state concurrent saves (catalogue A9).

`_state_path` is monkeypatched to tmp_path — the real app data dir is never
touched.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from holler import state


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "state.json"
    monkeypatch.setattr(state, "_state_path", lambda: p)
    return p


def test_concurrent_merged_saves_lose_no_keys(state_file: Path) -> None:
    n_threads, per_thread = 8, 25
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(per_thread):
                state.merged_save({f"k{i}_{j}": j})
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(data) == n_threads * per_thread
    # No temp files left behind.
    assert [p.name for p in state_file.parent.iterdir()] == ["state.json"]


def test_debounced_and_sync_saves_both_land(state_file: Path) -> None:
    state.merged_save_debounced({"a": 1}, delay=0.01)
    state.merged_save({"b": 2})
    state.flush_pending()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data == {"a": 1, "b": 2}
