"""Cost round 2: workers follow the badge once re-picked, and every session
pins a 5-minute prompt-cache TTL unless the operator's environment says
otherwise."""

from __future__ import annotations

from holler import sdk_session
from holler.config import worker_model_setting


def test_picking_the_badge_model_unpins_workers():
    assert worker_model_setting("claude-opus-5-5", "claude-opus-5-5") == ""


def test_picking_another_model_pins_it():
    assert worker_model_setting("claude-opus-5", "claude-opus-5-5") == "claude-opus-5"


def test_sessions_pin_a_five_minute_cache_ttl(monkeypatch):
    for var in sdk_session._PROMPT_CACHE_TTL_VARS:
        monkeypatch.delenv(var, raising=False)
    env = sdk_session._prompt_cache_env()
    assert env == {
        "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
        "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "5m",
    }


def test_operator_environment_overrides_the_ttl(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_PROMPT_CACHE_TTL", "1h")
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", raising=False)
    env = sdk_session._prompt_cache_env()
    assert env["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"
    assert env["CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"] == "5m"
