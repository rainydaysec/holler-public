"""Session profiles — pre-baked operating-mode prompts loaded at session start.

A profile is a single Markdown file with YAML frontmatter:

    ---
    name: ui-ux
    display: UI / UX
    description: Frontend visual and interaction work
    default_model: claude-opus-4-8   # optional
    ---

    <body — appended to the system prompt for sessions created with this profile>

Two source dirs are scanned and merged. User dir wins on name collision so an
operator can override a built-in by dropping a same-named file in their dir.

    Built-in:  holler/profiles/*.md         (shipped with the package)
    User:      %LOCALAPPDATA%\\holler\\profiles\\*.md   (operator-editable)

The picker UI in `sessions_window.py` calls `list_profiles()`; the session
dispatcher calls `load_profile_body(name)` to get the prompt text to inject.

Profiles are intentionally simple: prompt-shaped, not tool-shaped. Each is
locked to the session at creation time and never mutates mid-flight.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileSpec:
    name: str            # filename without .md — also the persisted key
    display: str         # human-readable label for the picker
    description: str     # one-line subtitle in the picker
    default_model: Optional[str]
    source: str          # "builtin" | "user"
    path: Path


def builtin_dir() -> Path:
    """Where shipped profiles live (relative to this module)."""
    return Path(__file__).parent / "profiles"


def user_dir() -> Path:
    from holler.appdir import app_data_dir
    return app_data_dir() / "profiles"


def ensure_user_dir() -> Path:
    p = user_dir()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.mkdir(exist_ok=True)
    return p


# ----- frontmatter parser (no PyYAML dep — frontmatter shape is fixed) -----

_FM_BOUNDARY = re.compile(r"^---\s*$", re.MULTILINE)


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Return ({key: value}, body). Tolerant of missing frontmatter."""
    if not text.startswith("---"):
        return {}, text
    # Find the closing --- (must be on its own line).
    rest = text[3:]
    m = _FM_BOUNDARY.search(rest)
    if m is None:
        return {}, text
    fm_block = rest[: m.start()]
    body = rest[m.end():].lstrip("\n")
    meta: Dict[str, str] = {}
    for line in fm_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip("'").strip('"')
    return meta, body


def _load_one(path: Path, source: str) -> Optional[ProfileSpec]:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        log.exception("failed to read profile %s", path)
        return None
    meta, _body = _parse_frontmatter(raw)
    name = meta.get("name") or path.stem
    display = meta.get("display") or name
    description = meta.get("description") or ""
    default_model = meta.get("default_model") or None
    return ProfileSpec(
        name=name,
        display=display,
        description=description,
        default_model=default_model,
        source=source,
        path=path,
    )


# Cache for list_profiles() output keyed by (dir_mtime_tuple, file_mtimes).
# Cheap stat-and-compare on every call; only re-reads file contents when an
# .md actually changes. Editing a profile takes effect immediately because the
# file's mtime advances.
_PROFILES_CACHE: dict = {"key": None, "value": []}
# Cache for profile body text by path, keyed on mtime.
_PROFILE_BODY_CACHE: dict[Path, tuple[float, str]] = {}


def _profiles_cache_key() -> tuple:
    """Stat the two profile dirs + each .md file. Tuple is hashable, comparable."""
    parts: list = []
    for d in (builtin_dir(), user_dir()):
        if not d.exists():
            parts.append((str(d), 0.0, ()))
            continue
        try:
            dmtime = d.stat().st_mtime
        except Exception:
            dmtime = 0.0
        files: list[tuple[str, float]] = []
        try:
            for p in sorted(d.glob("*.md")):
                try:
                    files.append((p.name, p.stat().st_mtime))
                except Exception:
                    files.append((p.name, 0.0))
        except Exception:
            pass
        parts.append((str(d), dmtime, tuple(files)))
    return tuple(parts)


def list_profiles() -> List[ProfileSpec]:
    """Return all profiles, user-dir entries overriding same-named built-ins.

    Order: alphabetized by display name. The "no profile" sentinel is NOT
    included — the picker UI adds it as a pinned top row.
    """
    key = _profiles_cache_key()
    if _PROFILES_CACHE["key"] == key:
        return _PROFILES_CACHE["value"]
    found: Dict[str, ProfileSpec] = {}
    for d, source in ((builtin_dir(), "builtin"), (user_dir(), "user")):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            spec = _load_one(p, source)
            if spec is None:
                continue
            found[spec.name] = spec  # user dir scanned last → wins
    value = sorted(found.values(), key=lambda s: s.display.lower())
    _PROFILES_CACHE["key"] = key
    _PROFILES_CACHE["value"] = value
    return value


def get_profile(name: Optional[str]) -> Optional[ProfileSpec]:
    if not name:
        return None
    for p in list_profiles():
        if p.name == name:
            return p
    return None


def load_profile_body(name: Optional[str]) -> str:
    """Return the body (no frontmatter) for the named profile, or "" if not
    found or no name given. Used by `sysprompt.build_append()` to inject the
    profile layer between base and user-override.

    Body text is mtime-cached so repeated calls (one per PTT) don't re-read
    the file unless it actually changed.
    """
    spec = get_profile(name)
    if spec is None:
        return ""
    try:
        mtime = spec.path.stat().st_mtime
    except Exception:
        mtime = 0.0
    cached = _PROFILE_BODY_CACHE.get(spec.path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        raw = spec.path.read_text(encoding="utf-8")
    except Exception:
        log.exception("failed to read profile body %s", spec.path)
        return ""
    _meta, body = _parse_frontmatter(raw)
    body_stripped = body.strip()
    _PROFILE_BODY_CACHE[spec.path] = (mtime, body_stripped)
    return body_stripped


def profile_path(name: Optional[str]) -> Optional[Path]:
    """Return the resolved Path for a profile name, or None.

    Lighter than load_profile_body — just an mtime lookup target.
    """
    spec = get_profile(name)
    return spec.path if spec is not None else None
