"""Every session closes with a readable brief — except the Investigation lane.

The operator's complaint: a finished session told him nothing. The superintendent
profile capped its last message at one sentence plus checkboxes, and the harness
base told every other session "one or two sentences max" — so there was nowhere
for a summary to live. The contract now lives in the base append (so it reaches
every profile) and the IR lane opts out of it on purpose: its deliverable is a
finding, not a change, and report scaffolding only slows it down.
"""
from __future__ import annotations

import pytest

from holler.profiles import builtin_dir, list_profiles
from holler.superintendent.override import (
    DEPLOY_AUTHORITY_OVERRIDE,
    IR_AUTHORITY_OVERRIDE,
)
from holler.sysprompt import _BASE_APPEND, build_append

ALL_PROFILES = sorted(p.name for p in list_profiles())


def _body(name: str) -> str:
    return (builtin_dir() / f"{name}.md").read_text(encoding="utf-8")


def test_base_append_asks_every_session_for_a_closing_report():
    text = _BASE_APPEND
    assert "Close every working turn with a real report" in text
    for block in ("**What changed**", "**Status**", "**Needs you**"):
        assert block in text, block
    # The headline is plain text because the completion toast shows only the
    # first line, unformatted (holler/panel.py).
    assert "ONE plain sentence, no markdown" in text
    assert "toast shows only this first" in text
    # The old blanket cap that left no room for a summary is gone.
    assert "End-of-turn summary is one or two sentences max" not in text


def test_base_append_still_keeps_plain_answers_conversational():
    """A question deserves an answer, not report scaffolding."""
    assert "merely answers a question stays conversational" in _BASE_APPEND
    assert 'never shrink real work down to a bare "done."' in _BASE_APPEND


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_profile_inherits_the_closing_report(name):
    """The contract is in the base layer, so it reaches every profile."""
    assert "Close every working turn with a real report" in build_append(name)


def test_investigation_opts_out_explicitly():
    """Opting out is stated, not implied — the profile layer wins over base."""
    text = _body("investigation")
    assert "REPLACES the harness's closing-report contract (base rule 7)" in text
    assert "**What changed**" in text and "does not use it" in text
    # Its own contract is intact.
    assert "At most 5 sentences" in text
    assert "Do NOT emit a status/result line" in text


def test_ir_authority_override_repeats_the_opt_out_as_the_last_layer():
    """The IR override is appended after the user's personal rules, so the
    opt-out has to survive there too."""
    text = " ".join(IR_AUTHORITY_OVERRIDE.split())  # ignore line wrapping
    assert "REPLACES the harness's closing-report contract (base rule 7)" in text
    assert "no report scaffolding of any kind" in text


def test_no_other_profile_opts_out():
    for name in ALL_PROFILES:
        if name == "investigation":
            continue
        assert "REPLACES the harness's closing-report contract" not in _body(name), name


def test_superintendent_report_is_a_briefing_with_the_status_line_last():
    text = _body("superintendent")
    assert "the operator's entire view of this run" in text
    assert "**Verified**" in text and "**Shipped**" in text
    assert "acceptance criterion from step 1 gets exactly one line" in text
    # The hard cap that produced the unreadable one-liner reports is gone.
    assert "HARD CAP" not in text
    assert "one sentence, one line per acceptance criterion" not in text
    # The machine-readable verdict still exists, and the worker is warned that
    # a second copy of it mis-files the run (dispatcher greps the whole text).
    assert "SUPERINTENDENT RESULT: DONE + PUSHED" in text
    assert "appears **exactly once**" in text


def test_superintendent_result_vocabulary_matches_the_dispatcher():
    """The three verdicts the profile teaches are the three the dispatcher
    parses (holler/superintendent/dispatcher.py `_worker_terminal_state`)."""
    import inspect

    from holler.superintendent import dispatcher

    src = inspect.getsource(dispatcher.SuperintendentDispatcher._worker_terminal_state)
    text = _body("superintendent")
    for verdict in ("DONE + PUSHED", "DONE + MR OPENED", "NOT DONE"):
        assert f"SUPERINTENDENT RESULT: {verdict}" in text, verdict
        assert f"SUPERINTENDENT RESULT: {verdict}" in src, verdict


def test_deploy_authority_override_does_not_fight_the_report_contract():
    """The last-wins lane block must not re-impose the old one-liner cap."""
    assert "HARD CAP" not in DEPLOY_AUTHORITY_OVERRIDE
    assert "one or two sentences" not in DEPLOY_AUTHORITY_OVERRIDE
