"""config.example.toml must mirror the built-in defaults (and be CPU-safe)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from holler.config import _DEFAULTS, DEFAULT_MODEL

_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"
# Machine-specific or platform-tuned keys: the example shows a placeholder /
# the CPU-safe choice instead of the running platform's default.
_PLATFORM_KEYS = {"repo_cwd", "whisper_model", "whisper_compute", "whisper_device"}


def _example() -> dict:
    with _EXAMPLE.open("rb") as fh:
        return tomllib.load(fh)


def test_example_has_exactly_the_current_keys():
    assert set(_example()) == set(_DEFAULTS)


def test_example_values_match_defaults():
    ex = _example()
    for key, default in _DEFAULTS.items():
        if key in _PLATFORM_KEYS:
            continue
        assert ex[key] == default, key
    assert ex["claude_model"] == DEFAULT_MODEL


def test_example_whisper_settings_are_cpu_safe():
    ex = _example()
    assert ex["whisper_device"] == "cpu"
    assert ex["whisper_compute"] == "int8"
