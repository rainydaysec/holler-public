"""Named project repos — which repo a new session works in.

Holler's default repo is `cfg.repo_cwd`. `cfg.projects` names other repos
(e.g. `myproject = "/Users/me/code/myproject"`) and `cfg.profile_projects` gives
a profile a default project (e.g. `superintendent = "myproject"`). A request can
name a project explicitly (a Superintendent evidence row's `project` field),
which wins over the profile default.

`resolve_session_repo` returns the path to stamp on the new session row, or
None for "the default repo" — stored as '' so a default session keeps
following `repo_cwd` changes exactly as before projects existed. With no
projects configured it always returns None: behaviour is unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class UnknownProjectError(ValueError):
    """An explicit project name that isn't configured in `cfg.projects`."""


def _projects(cfg: Any) -> dict:
    p = getattr(cfg, "projects", None) if cfg is not None else None
    return p if isinstance(p, dict) else {}


def project_path(cfg: Any, name: str) -> Optional[Path]:
    """Configured path for project `name` (`~` expanded), or None."""
    raw = _projects(cfg).get(name)
    if not raw or not isinstance(raw, str):
        return None
    return Path(raw).expanduser()


def project_for_profile(cfg: Any, profile: Optional[str]) -> str:
    """The profile's default project name, or '' when it has none."""
    if not profile or cfg is None:
        return ""
    mapping = getattr(cfg, "profile_projects", None)
    if not isinstance(mapping, dict):
        return ""
    name = mapping.get(profile)
    return name if isinstance(name, str) else ""


def resolve_project_name(
    cfg: Any, *, project: Optional[str] = None, profile: Optional[str] = None,
) -> str:
    """Project name a new session targets: explicit request, else the profile
    default, else '' (the default repo)."""
    return (project or "").strip() or project_for_profile(cfg, profile)


def resolve_session_repo(
    cfg: Any, *, project: Optional[str] = None, profile: Optional[str] = None,
) -> Optional[str]:
    """Repo path to record on a new session row, or None for the default repo.

    An explicitly requested project that isn't configured raises
    `UnknownProjectError` — silently running the work in some other repo is
    exactly the failure this module exists to prevent. A profile default that
    doesn't resolve is a config slip, so it logs and falls back to default.
    """
    explicit = (project or "").strip()
    if explicit:
        path = project_path(cfg, explicit)
        if path is None:
            raise UnknownProjectError(
                f"project {explicit!r} is not configured (known: "
                f"{', '.join(sorted(_projects(cfg))) or 'none'})"
            )
        return str(path)
    name = project_for_profile(cfg, profile)
    if not name:
        return None
    path = project_path(cfg, name)
    if path is None:
        log.warning(
            "profile %r maps to project %r, which isn't in `projects` — "
            "using the default repo", profile, name,
        )
        return None
    return str(path)


def project_name_for_repo(cfg: Any, repo: Optional[str]) -> str:
    """Reverse lookup: the configured project name for `repo`, or ''."""
    if not repo:
        return ""
    try:
        target = Path(repo).expanduser().resolve()
    except OSError:
        return ""
    for name, raw in _projects(cfg).items():
        try:
            if isinstance(raw, str) and Path(raw).expanduser().resolve() == target:
                return name
        except OSError:
            continue
    return ""
