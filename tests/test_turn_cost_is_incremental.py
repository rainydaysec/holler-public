"""A turn's recorded cost is its own, not the CLI subprocess's running total.

`ResultMessage.total_cost_usd` counts every turn the subprocess has served.
Holler pools one subprocess across a session's turns, so storing the raw value
made `SUM(total_cost_usd)` over a session report roughly `turns x` the real
spend — the rail showed thousands of dollars for hundreds. These tests pin the
increment behaviour, including the reset when a fresh subprocess starts.
"""

from __future__ import annotations

import pytest

from holler.jobs import JobStore
from holler.sdk_session import SessionEvent


@pytest.fixture
def store(app):  # `app` gives the QObject base a live QApplication
    return JobStore()


def _submit(store: JobStore, job_id: int, *, sdk_session_id: str, local_session_id: int = 1):
    store.apply_event(
        SessionEvent(
            job_id=job_id,
            kind="submitted",
            data={"transcript": "t", "local_session_id": local_session_id},
        )
    )
    job = store.get(job_id)
    job.sdk_session_id = sdk_session_id
    return job


def _finish(store: JobStore, job_id: int, cumulative: float) -> float:
    store.apply_event(
        SessionEvent(job_id=job_id, kind="result", data={"total_cost_usd": cumulative})
    )
    return store.get(job_id).total_cost_usd


def test_pooled_turns_record_the_increment_not_the_running_total(store):
    """The real shape from a measured session: a monotonic climb per turn."""
    costs = []
    for seq, cumulative in enumerate((12.60, 14.36, 15.07, 15.43), start=1):
        _submit(store, seq, sdk_session_id="sdk-a")
        costs.append(_finish(store, seq, cumulative))
    assert costs == pytest.approx([12.60, 1.76, 0.71, 0.36])
    # The session total still equals the subprocess's final running total,
    # instead of 12.60 + 14.36 + 15.07 + 15.43 = 57.46.
    assert sum(costs) == pytest.approx(15.43)


def test_a_fresh_subprocess_resets_the_cursor(store):
    _submit(store, 1, sdk_session_id="sdk-a")
    _finish(store, 1, 10.0)
    _submit(store, 2, sdk_session_id="sdk-a")
    assert _finish(store, 2, 12.0) == pytest.approx(2.0)
    # New subprocess: its counter starts over, so the reported value is
    # already this turn's cost and must not be read as a decrease.
    _submit(store, 3, sdk_session_id="sdk-b")
    assert _finish(store, 3, 0.4) == pytest.approx(0.4)


def test_a_counter_that_drops_on_the_same_key_is_treated_as_a_restart(store):
    """Holler cannot see the subprocess swap when the sdk id is unavailable."""
    _submit(store, 1, sdk_session_id="")
    _finish(store, 1, 9.0)
    _submit(store, 2, sdk_session_id="")
    assert _finish(store, 2, 0.25) == pytest.approx(0.25)


def test_separate_sessions_do_not_share_a_cursor(store):
    _submit(store, 1, sdk_session_id="sdk-a")
    _finish(store, 1, 5.0)
    _submit(store, 2, sdk_session_id="sdk-b")
    assert _finish(store, 2, 5.0) == pytest.approx(5.0)


@pytest.mark.parametrize("reported", [None, 0, 0.0, "", "not-a-number"])
def test_a_missing_or_unusable_cost_records_zero(store, reported):
    _submit(store, 1, sdk_session_id="sdk-a")
    assert _finish(store, 1, reported) == 0.0


def test_the_cursor_stays_bounded(store):
    cap = JobStore._COST_CURSOR_CAP
    for seq in range(cap + 10):
        _submit(store, seq + 1, sdk_session_id=f"sdk-{seq}")
        _finish(store, seq + 1, 1.0)
    assert len(store._cost_cursor) <= cap
