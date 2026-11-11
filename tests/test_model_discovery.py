"""Model discovery: the picker list is data-driven, additive, and crash-proof.

Guards the promise the model badge now makes — a model Claude starts serving
appears in the cycle without a code edit — plus the three ways that could go
wrong: discovery junk leaking into the picker, discovery *removing* a baseline
model, and a broken cache taking the picker down with it.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from holler import model_discovery
from holler.config import KNOWN_MODELS, effective_models, load as load_config
from holler.model_prefix import cycle_next


@pytest.fixture
def cfg():
    return load_config()


def _transcript(dirpath, name, models):
    """Write a minimal Claude Code JSONL transcript naming `models`."""
    p = dirpath / name
    p.write_text(
        "\n".join(
            json.dumps({"type": "assistant", "message": {"model": m}}) for m in models
        ),
        encoding="utf-8",
    )
    return p


# ---------- transcript discovery ----------

def test_discovers_models_from_transcripts(tmp_path):
    proj = tmp_path / "projects" / "-some-repo"
    proj.mkdir(parents=True)
    _transcript(proj, "a.jsonl", ["claude-opus-9-1", "claude-haiku-4-5-20251001"])
    found = model_discovery.discover_from_transcripts(tmp_path / "projects")
    assert "claude-opus-9-1" in found
    assert "claude-haiku-4-5-20251001" in found


def test_discovery_rejects_non_canonical_ids(tmp_path):
    """`<synthetic>` and routing variants must never reach the operator's picker."""
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    _transcript(
        proj,
        "a.jsonl",
        ["<synthetic>", "claude-opus-4-6-v1", "claude-opus-4-6-fast", "claude-opus-4-6"],
    )
    assert model_discovery.discover_from_transcripts(tmp_path / "projects") == [
        "claude-opus-4-6"
    ]


def test_discovery_dedupes_preserving_first_seen_order(tmp_path):
    proj = tmp_path / "projects" / "-repo"
    proj.mkdir(parents=True)
    _transcript(proj, "a.jsonl", ["claude-opus-5", "claude-sonnet-4-6", "claude-opus-5"])
    assert model_discovery.discover_from_transcripts(tmp_path / "projects") == [
        "claude-opus-5",
        "claude-sonnet-4-6",
    ]


def test_missing_transcript_root_is_empty_not_an_error(tmp_path):
    assert model_discovery.discover_from_transcripts(tmp_path / "nope") == []


# ---------- effective_models merge ----------

def test_newly_discovered_model_reaches_the_picker(cfg, monkeypatch):
    """The whole point: a model nobody hard-coded becomes selectable."""
    monkeypatch.setattr(
        model_discovery, "cached_model_ids", lambda: ["claude-opus-9-9"]
    )
    models = effective_models(cfg)
    assert "claude-opus-9-9" in models
    assert "claude-opus-9-9" not in KNOWN_MODELS


def test_discovery_is_additive_never_subtractive(cfg, monkeypatch):
    """A thin transcript history must not shrink the picker."""
    monkeypatch.setattr(model_discovery, "cached_model_ids", lambda: ["claude-opus-5"])
    models = effective_models(cfg)
    for baseline in KNOWN_MODELS:
        assert baseline in models


def test_operator_list_wins_ordering(cfg, monkeypatch):
    monkeypatch.setattr(model_discovery, "cached_model_ids", lambda: ["claude-opus-9-9"])
    pinned = dataclasses.replace(cfg, claude_models=["claude-sonnet-4-6"])
    assert effective_models(pinned)[0] == "claude-sonnet-4-6"


def test_broken_cache_still_yields_a_usable_picker(cfg, monkeypatch):
    def boom():
        raise RuntimeError("corrupt cache")

    monkeypatch.setattr(model_discovery, "cached_model_ids", boom)
    assert effective_models(cfg) == KNOWN_MODELS


def test_unreadable_cache_file_is_ignored(tmp_path, monkeypatch):
    bad = tmp_path / "models_cache.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(model_discovery, "_cache_path", lambda: bad)
    assert model_discovery.load_cache() == {}
    assert model_discovery.cached_model_ids() == []


def test_cache_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "models_cache.json"
    monkeypatch.setattr(model_discovery, "_cache_path", lambda: path)
    model_discovery.save_cache(["claude-opus-5"], "claude-transcripts")
    assert model_discovery.cached_model_ids() == ["claude-opus-5"]
    assert model_discovery.last_refreshed() is not None


# ---------- the badge itself ----------

def test_badge_cycles_through_the_discovered_list():
    pool = ["claude-opus-9-9", "claude-sonnet-4-6"]
    assert cycle_next("claude-opus-9-9", pool) == "claude-sonnet-4-6"
    assert cycle_next("claude-sonnet-4-6", pool) == "claude-opus-9-9"


def test_badge_recovers_when_current_model_left_the_list():
    assert cycle_next("claude-retired-1", ["claude-opus-5"]) == "claude-opus-5"


def test_refresh_never_raises(monkeypatch):
    monkeypatch.setattr(
        model_discovery, "discover", lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    ids, err = model_discovery.refresh()
    assert err and "offline" in err
    assert isinstance(ids, list)


def test_cache_write_uses_unique_temp_files(tmp_path, monkeypatch):
    """Two concurrent writers must not share one `.tmp` path."""
    import tempfile as _tempfile

    path = tmp_path / "models_cache.json"
    monkeypatch.setattr(model_discovery, "_cache_path", lambda: path)
    made = []
    real_mkstemp = _tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = real_mkstemp(*a, **kw)
        made.append(name)
        return fd, name

    monkeypatch.setattr(model_discovery.tempfile, "mkstemp", spy)
    model_discovery.save_cache(["claude-opus-5"], "x")
    model_discovery.save_cache(["claude-opus-5-5"], "x")
    assert len(set(made)) == 2
    assert all(n != str(path) + ".tmp" for n in made)
    assert model_discovery.cached_model_ids() == ["claude-opus-5-5"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["models_cache.json"]
