"""Regression tests for the engineering-standard layer in sysprompt.build_append.

The expert engineering standard (holler/standards/engineering.md) must be
injected into EVERY session — every built-in profile, and the no-profile case —
sitting between the base harness rules and the profile body, with the user
override still last (and therefore able to win on conflict). These tests lock
that invariant so the standard can never silently fall out of the prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holler import sysprompt  # noqa: E402
from holler.profiles import list_profiles  # noqa: E402

STANDARD_HEADER = "# === Engineering standard (non-negotiable) ==="
STANDARD_MARKER = "# Definition of Done"      # distinctive line from the body
BASE_MARKER = "# Working rules"               # from _BASE_APPEND
PROFILE_HEADER_PREFIX = "# === Profile:"
OVERRIDE_SECTION = "# Personal rules"

PROFILE_NAMES = [s.name for s in list_profiles()]


@pytest.fixture(autouse=True)
def _clear_cache():
    """build_append caches keyed on file mtimes; monkeypatching a loader won't
    bump an mtime, so clear the cache around every test to avoid stale hits."""
    sysprompt._BUILD_APPEND_CACHE.clear()
    yield
    sysprompt._BUILD_APPEND_CACHE.clear()


def test_all_builtin_profiles_discovered():
    """Guard: if a profile is renamed/removed the parametrized coverage below
    still means what we think it does. The six operator-selectable profiles plus
    the two autonomous worker lanes (superintendent deploy, investigation/IR)
    ship today."""
    assert set(PROFILE_NAMES) == {
        "debugging", "engineering", "gtm-business",
        "planning", "research-audit", "ui-ux",
        "superintendent", "investigation",
    }


@pytest.mark.parametrize("profile", PROFILE_NAMES + [None])
def test_standard_present_in_every_profile(profile):
    out = sysprompt.build_append(profile=profile)
    assert STANDARD_HEADER in out
    assert STANDARD_MARKER in out


def test_no_profile_has_standard_but_no_profile_header():
    out = sysprompt.build_append(profile=None)
    assert STANDARD_HEADER in out
    assert PROFILE_HEADER_PREFIX not in out


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_layer_order_base_then_standard_then_profile(profile):
    out = sysprompt.build_append(profile=profile)
    i_base = out.index(BASE_MARKER)
    i_std = out.index(STANDARD_HEADER)
    i_prof = out.index(PROFILE_HEADER_PREFIX)
    assert i_base < i_std < i_prof


def test_user_override_sits_after_standard_and_profile(monkeypatch):
    sentinel = "\n\n# Personal rules\n\nZZZ_OVERRIDE_SENTINEL\n"
    monkeypatch.setattr(sysprompt, "_load_user_override", lambda: sentinel)
    out = sysprompt.build_append(profile="planning")
    i_std = out.index(STANDARD_HEADER)
    i_prof = out.index(PROFILE_HEADER_PREFIX)
    i_override = out.index("ZZZ_OVERRIDE_SENTINEL")
    # Override wins by sitting last — after both the standard and the profile.
    assert i_std < i_override
    assert i_prof < i_override


def test_no_override_means_no_personal_rules_section(monkeypatch):
    monkeypatch.setattr(sysprompt, "_load_user_override", lambda: "")
    out = sysprompt.build_append(profile="engineering")
    assert OVERRIDE_SECTION not in out
    assert STANDARD_HEADER in out


def test_blank_standard_file_drops_the_layer(monkeypatch):
    """Proves the header's presence is genuinely driven by file content: blank
    the standard and the layer vanishes. This is the teeth behind the
    'present in every profile' guarantee above."""
    monkeypatch.setattr(sysprompt, "_load_standard_layer", lambda: "")
    out = sysprompt.build_append(profile="planning")
    assert STANDARD_HEADER not in out
    assert STANDARD_MARKER not in out


def test_standard_loader_reads_the_shipped_file():
    layer = sysprompt._load_standard_layer()
    assert STANDARD_HEADER in layer
    assert STANDARD_MARKER in layer
