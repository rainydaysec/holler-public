"""Guards the PTT capture-target decision during evidence/interactive chaining.

Regression: while a chain was armed or holding captures, the operator would
click into holler's inbox to watch it grow, making holler frontmost. The next
PTT hold then saw is_self_frontmost()==True and captured holler's own UI instead
of the external window (Canvas) they were documenting. `_chain_capture_active`
tells the capture path to keep targeting the real window while a chain is live.
"""

from __future__ import annotations

from holler.runners.full import _chain_capture_active
from holler import session_chain as _sc
from holler.superintendent import drafts as _drafts


def test_no_chain_is_inactive():
    assert _chain_capture_active({}) is False
    assert _chain_capture_active({"session_chains": {}}) is False


def test_superintendent_armed_is_active():
    assert _chain_capture_active({"superintendent_chain_armed": True}) is True


def test_superintendent_held_draft_is_active():
    draft = _drafts.new_draft("deploy")
    _drafts.append_to_draft(draft, shot="a.png", transcript="hi", turn_id=1)
    state = {"superintendent_chain_armed": False, "superintendent_draft": draft}
    assert _chain_capture_active(state) is True


def test_empty_draft_is_inactive():
    # A freshly opened (empty) draft holds nothing yet -> not active.
    assert _chain_capture_active({"superintendent_draft": _drafts.new_draft("deploy")}) is False


def test_interactive_chain_armed_empty_is_active():
    # Armed-but-empty is a real state (operator clicked Start chain).
    state = {"session_chains": {7: _sc.new_chain()}}
    assert _chain_capture_active(state) is True


def test_interactive_chain_holding_is_active():
    chain = _sc.new_chain()
    chain["armed"] = False
    _sc.append(chain, shot="a.png", image_b64="b", transcript="hi")
    state = {"session_chains": {7: chain}}
    assert _chain_capture_active(state) is True


def test_malformed_state_is_inactive_not_raising():
    # Best-effort: junk in the state dict must never raise on the hot PTT path.
    assert _chain_capture_active({"session_chains": None}) is False
    assert _chain_capture_active({"superintendent_draft": "junk"}) is False
