"""Every unselected push-to-talk opens its own session.

Regression: dispatch used to pin the session it had just created as the routing
target, so the first hold started a session and every hold after that silently
continued it. The operator's model is the opposite — a hold on some other window
is new work. Grouping several holds into one session is what a chain is for, and
a chain armed with nothing selected files into a session created at that point.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from holler import session_chain as sc
from holler.runners.full import (
    _dispatch_text_job,
    _session_chain_control,
    _try_hold_session_chain,
)
from holler.session_routing import decide
from holler.store import Store


class FakeSession:
    """Stands in for the SDK session — records what would have been submitted."""

    def __init__(self) -> None:
        self.submitted: list[dict] = []

    def submit(self, image_b64, text, **kw) -> None:
        self.submitted.append({"image_b64": image_b64, "text": text, **kw})


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "holler.db")


@pytest.fixture()
def cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        claude_model="claude-opus-4-7", claude_models=[], captures_dir=tmp_path
    )


@pytest.fixture()
def state(store, cfg) -> dict:
    return {
        "cfg": cfg,
        "persistent_store": store,
        "active_session_id": None,
        "session_chains": {},
    }


# ----- routing default -----


def test_unselected_input_opens_a_new_session_every_time(state, store) -> None:
    session = FakeSession()
    _dispatch_text_job("first thing", session, state["cfg"], state, panel=None)
    _dispatch_text_job("second thing", session, state["cfg"], state, panel=None)

    sids = [s["local_session_id"] for s in session.submitted]
    assert len(set(sids)) == 2, "second input must not continue the first session"
    assert all(s["resume_session_id"] is None for s in session.submitted)
    assert len(store.list_sessions(limit=10)) == 2


def test_dispatch_does_not_pin_the_session_it_created(state) -> None:
    _dispatch_text_job("kick something off", FakeSession(), state["cfg"], state, panel=None)
    assert state["active_session_id"] is None


def test_explicit_selection_still_continues_that_session(state, store) -> None:
    sid = store.create_session(model="m", transcript="earlier work", screenshot_path=None)
    store.attach_sdk_session_id(sid, "sdk-abc")
    state["active_session_id"] = sid  # operator clicked the row

    session = FakeSession()
    _dispatch_text_job("more on this", session, state["cfg"], state, panel=None)

    assert session.submitted[0]["local_session_id"] == sid
    assert session.submitted[0]["resume_session_id"] == "sdk-abc"


def test_decide_without_a_selection_is_new() -> None:
    d = decide("just some words", active_local_session_id=None, candidates=[(1, "words", "s1")])
    assert d.kind == "new"


# ----- chaining into a session that doesn't exist yet -----


def test_chain_key_separates_the_not_yet_created_session() -> None:
    assert sc.chain_key(7) == 7
    assert sc.chain_key(None) == sc.NEW_SESSION


def test_unarmed_capture_falls_through_to_normal_dispatch(state) -> None:
    held = _try_hold_session_chain(
        target_sid=None, composed=None, cfg=state["cfg"],
        transcript="hi", state=state, notice=lambda _m: None,
    )
    assert held is False


def test_chain_armed_with_no_selection_holds_captures(state) -> None:
    _session_chain_control("arm", FakeSession(), state, state["persistent_store"], panel=None)
    assert sc.is_armed(state["session_chains"][sc.NEW_SESSION])

    for text in ("one", "two"):
        assert _try_hold_session_chain(
            target_sid=None, composed=None, cfg=state["cfg"],
            transcript=text, state=state, notice=lambda _m: None,
        ) is True
    assert sc.count(state["session_chains"][sc.NEW_SESSION]) == 2


def test_filing_a_no_selection_chain_creates_one_session(state, store) -> None:
    session = FakeSession()
    _session_chain_control("arm", session, state, store, panel=None)
    for text in ("one", "two", "three"):
        _try_hold_session_chain(
            target_sid=None, composed=None, cfg=state["cfg"],
            transcript=text, state=state, notice=lambda _m: None,
        )
    _session_chain_control("file", session, state, store, panel=None)

    assert len(session.submitted) == 1
    turn = session.submitted[0]
    assert turn["text"] == "one\n\ntwo\n\nthree"
    assert turn["resume_session_id"] is None

    sessions = store.list_sessions(limit=10)
    assert len(sessions) == 1
    assert sessions[0].id == turn["local_session_id"]
    assert state["session_chains"] == {}


def test_discarding_a_no_selection_chain_submits_nothing(state, store) -> None:
    session = FakeSession()
    _session_chain_control("arm", session, state, store, panel=None)
    _try_hold_session_chain(
        target_sid=None, composed=None, cfg=state["cfg"],
        transcript="throwaway", state=state, notice=lambda _m: None,
    )
    _session_chain_control("discard", session, state, store, panel=None)

    assert session.submitted == []
    assert state["session_chains"] == {}
    assert store.list_sessions(limit=10) == []
