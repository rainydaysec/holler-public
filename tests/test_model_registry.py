"""Model registry wiring — fable 5 selectable, voice-prefix resolvable, labels clean.

Guards the three surfaces that let Holler actually run a model: the KNOWN_MODELS
list (settings/first-run combos + cycler), the voice-prefix alias resolver, and
the short badge label formatter.
"""

from __future__ import annotations

import pytest

from holler import model_discovery
from holler.config import DEFAULT_MODEL, KNOWN_MODELS, _DEFAULTS
from holler.first_run import _pretty_model
from holler.model_prefix import (
    _ALIAS_TO_FULL,
    cycle_next,
    extract,
    format_model_label,
    newest_of_family,
    resolve_alias,
)


@pytest.fixture(autouse=True)
def _no_discovery_cache(monkeypatch):
    """Alias resolution reads the discovery cache; never touch the real one."""
    monkeypatch.setattr(model_discovery, "cached_model_ids", lambda: [])


def test_fable_is_a_known_model():
    assert "claude-fable-5" in KNOWN_MODELS


def test_voice_prefix_resolves_fable():
    stripped, full = extract("with fable, fix the login bug")
    assert full == "claude-fable-5"
    assert stripped == "fix the login bug"


def test_voice_prefix_resolves_newest_of_each_family_from_baseline():
    # KNOWN_MODELS carries opus-5-5, opus-5 and opus-4-8: the alias follows the newest.
    assert extract("using opus, do X")[1] == "claude-opus-5-5"
    assert extract("on sonnet: do Y")[1] == "claude-sonnet-5-5"
    assert extract("with haiku do Z")[1] == "claude-haiku-4-5-20251001"


def test_voice_alias_follows_newly_discovered_model(monkeypatch):
    monkeypatch.setattr(
        model_discovery, "cached_model_ids", lambda: ["claude-sonnet-6", "claude-opus-5-5"]
    )
    assert extract("with opus, go")[1] == "claude-opus-5-5"
    assert extract("with sonnet, go")[1] == "claude-sonnet-6"


def test_explicit_model_pool_is_respected():
    pool = ["claude-opus-4-8", "claude-opus-4-1"]
    assert extract("with opus, go", pool)[1] == "claude-opus-4-8"


def test_alias_falls_back_to_static_map_when_family_absent():
    assert resolve_alias("sonnet", ["claude-opus-5"]) == _ALIAS_TO_FULL["sonnet"]
    assert resolve_alias("haiku", []) == _ALIAS_TO_FULL["haiku"]
    assert resolve_alias("gpt", ["claude-opus-5"]) is None


def test_newest_orders_by_parsed_version_not_list_order():
    pool = ["claude-opus-4-8", "claude-opus-5", "claude-opus-5-5", "claude-opus-4-20250514"]
    assert newest_of_family("opus", pool) == "claude-opus-5-5"
    assert newest_of_family("opus", pool[:2]) == "claude-opus-5"
    # a date snapshot is not a huge minor version
    assert newest_of_family("opus", ["claude-opus-4-20250514", "claude-opus-4-8"]) == "claude-opus-4-8"
    # same version: newer snapshot date wins
    assert (
        newest_of_family("haiku", ["claude-haiku-4-5-20251001", "claude-haiku-4-5-20260101"])
        == "claude-haiku-4-5-20260101"
    )


def test_alias_requires_word_boundary():
    text = "with sonnets of love, write a poem"
    assert extract(text) == (text, None)
    assert extract("on opusmagnum do it") == ("on opusmagnum do it", None)


def test_label_formats_fable_without_minor():
    assert format_model_label("claude-fable-5") == "fable 5"


def test_label_preserves_existing_families():
    assert format_model_label("claude-opus-4-8") == "opus 4.8"
    assert format_model_label("claude-haiku-4-5-20251001") == "haiku 4.5"


def test_label_does_not_read_date_as_minor_version():
    assert format_model_label("claude-opus-4-20250514") == "opus 4"
    assert format_model_label("claude-sonnet-4-20250514") == "sonnet 4"


def test_default_model_single_source_of_truth():
    assert DEFAULT_MODEL in KNOWN_MODELS
    assert _DEFAULTS["claude_model"] == DEFAULT_MODEL


def test_first_run_pretty_label_handles_fable():
    assert _pretty_model("claude-fable-5") == "Fable 5  (claude-fable-5)"
    assert _pretty_model("claude-opus-4-8") == "Opus 4.8  (claude-opus-4-8)"


def test_cycle_reaches_fable():
    # cycling from every known model must eventually land on fable
    seen = set()
    cur = KNOWN_MODELS[0]
    for _ in range(len(KNOWN_MODELS)):
        seen.add(cur)
        cur = cycle_next(cur)
    assert "claude-fable-5" in seen


# ── voice aliases see the operator's own model list ───────────────────────────

def _cfg(**over):
    from holler.config import Config, _DEFAULTS

    return Config(**{**_DEFAULTS, **over})


def test_alias_resolves_against_an_operator_added_model():
    """A model id the operator typed into `claude_models` must be reachable by
    voice. Without a Config, `extract()` only sees discovery ∪ KNOWN_MODELS, so
    "with opus" couldn't pick an id only their list carries."""
    from holler.config import effective_models

    cfg = _cfg(claude_models=["claude-opus-9-9"])
    stripped, full = extract("with opus, ship it", models=effective_models(cfg))
    assert full == "claude-opus-9-9"
    assert stripped == "ship it"


def test_an_operator_list_cannot_shrink_alias_resolution():
    """`effective_models` unions rather than replaces, so a list naming only
    opus must leave "with haiku" working off the baseline."""
    from holler.config import effective_models

    cfg = _cfg(claude_models=["claude-opus-9-9"])
    _stripped, full = extract("with haiku, ship it", models=effective_models(cfg))
    assert full == _ALIAS_TO_FULL["haiku"]


def test_both_dispatch_paths_resolve_aliases_with_a_config():
    """The fix is the `models=` argument; a future edit dropping it silently
    reverts aliases to the Config-blind pool."""
    import inspect

    from holler.runners import full as runner

    calls = [
        line.strip() for line in inspect.getsource(runner).splitlines()
        if "extract_model_prefix(" in line and "import" not in line
    ]
    assert len(calls) == 2, f"call sites changed: {calls}"
    for line in calls:
        assert "models=effective_models(cfg)" in line, line
