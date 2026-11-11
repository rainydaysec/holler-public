"""Holler runs the newest installed Claude Code, not the SDK's stale bundle.

A turn on a model newer than the bundled CLI failed with `API Error: 400 Claude
Code 2.1.139 does not support this model; version 2.1.280 or newer is required`
while the operator's own `claude` was current. These guard the selection, the
picker learning from that error, and the failure banner showing it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from holler import claude_cli, detail, model_discovery
from holler.config import effective_models, load as load_config

TOO_OLD = (
    "API Error: 400 Claude Code 2.1.139 does not support this model; version "
    "2.1.280 or newer is required. Run 'claude update', or update the Claude "
    "desktop app, then try again."
)


@pytest.fixture(autouse=True)
def _fresh_selection():
    claude_cli.reset()
    yield
    claude_cli.reset()


def _fake_install(monkeypatch, bundled, installed):
    """bundled=(path, version); installed={path: version}."""
    monkeypatch.setattr(claude_cli, "_bundled_cli", lambda: Path(bundled[0]))
    monkeypatch.setattr(claude_cli, "_bundled_version", lambda: claude_cli.parse_version(bundled[1]))
    monkeypatch.setattr(claude_cli, "_candidates", lambda: [Path(p) for p in installed])
    monkeypatch.setattr(claude_cli, "_probe", lambda p: claude_cli.parse_version(installed[str(p)]))


def test_prefers_newer_installed_cli_over_bundle(monkeypatch):
    _fake_install(monkeypatch, ("/sdk/claude", "2.1.139"), {"/opt/homebrew/bin/claude": "2.1.285"})
    assert claude_cli.cli_path() == "/opt/homebrew/bin/claude"
    assert claude_cli.cli_version() == (2, 1, 285)


def test_keeps_bundle_when_it_is_newest(monkeypatch):
    _fake_install(monkeypatch, ("/sdk/claude", "2.1.300"), {"/usr/local/bin/claude": "2.1.285"})
    assert claude_cli.cli_path() == "/sdk/claude"


def test_parse_too_old():
    assert claude_cli.parse_too_old(TOO_OLD) == ("2.1.139", "2.1.280")
    assert claude_cli.parse_too_old("API Error: 529 overloaded") is None


def test_picker_hides_model_the_cli_is_too_old_for(tmp_path, monkeypatch):
    path = tmp_path / "models_cache.json"
    monkeypatch.setattr(model_discovery, "_cache_path", lambda: path)
    model_discovery.save_cache(["claude-opus-9-9", "claude-opus-5"], "claude-transcripts")
    model_discovery.record_cli_requirement("claude-opus-9-9", "2.1.280")
    # A later refresh must not forget what the API told us.
    model_discovery.save_cache(["claude-opus-9-9", "claude-opus-5"], "claude-transcripts")
    assert json.loads(path.read_text())["cli_requirements"] == {"claude-opus-9-9": "2.1.280"}

    cfg = dataclasses.replace(load_config(), claude_model="claude-opus-5", claude_models=[])
    _fake_install(monkeypatch, ("/sdk/claude", "2.1.139"), {})
    assert "claude-opus-9-9" not in effective_models(cfg)

    claude_cli.reset()
    _fake_install(monkeypatch, ("/sdk/claude", "2.1.139"), {"/opt/homebrew/bin/claude": "2.1.285"})
    assert "claude-opus-9-9" in effective_models(cfg)


def test_api_error_text_renders_in_failure_banner_not_as_reply():
    turn = SimpleNamespace(
        id=1, is_error=True, final_text=TOO_OLD,
        blocks_json=json.dumps([{"t": "text", "text": TOO_OLD}]),
        duration_ms=500, input_tokens=0, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
        total_cost_usd=0, model="", started_at=None, ended_at=None,
    )
    html = detail._render_committed_turn_html(turn, [])
    assert "No error details were saved" not in html
    assert html.count("does not support this model") == 1
    assert "run `claude update`" in html
