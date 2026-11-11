"""Holler config: TOML round-trip for the settings dialog.

`load()` reads `config.toml`, applies defaults, and filters out unknown keys so
deprecated config keys (e.g. `session_max_turns` from the pre-parallel era)
don't crash the dataclass constructor.

`save()` writes a Config back to TOML atomically via temp+replace. We use
`tomli-w` so Windows path escaping and value formatting stay correct without
hand-rolling.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


# Baseline Claude model IDs — the floor the model picker falls back to on a
# fresh install that has never discovered anything. Used by the per-job voice
# override resolver directly; every *picker* surface goes through
# `effective_models()` instead so newly-shipped models appear on their own.
#
# Order is picker order only; the fresh-config default is DEFAULT_MODEL below.
KNOWN_MODELS: list[str] = [
    "claude-fable-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

# THE default model for fresh configs — single source of truth. `_DEFAULTS`,
# the first-run dialog and config.example.toml all agree. Pinned to an id
# confirmed served on a real install rather than the head of KNOWN_MODELS
# (which is claude-fable-5, a pricier tier nobody asked a fresh install for).
# Opus 5.5 supersedes Opus 5 in the same tier and is strictly cheaper to run:
# $4/$20 per MTok against $5/$25, and cache reads at $0.20 against $0.50 —
# which dominates, because an agentic turn re-reads its whole context.
DEFAULT_MODEL: str = "claude-opus-5-5"


def effective_models(cfg: "Config") -> list[str]:
    """Model ids the badge cycler and the settings/first-run combos iterate.

    Unions, in priority order:

      1. `cfg.claude_models` — an explicit operator list, if they set one. Their
         ordering wins, so a pinned favourite stays first in the cycle.
      2. Discovered ids from `model_discovery` — the Anthropic catalogue, or the
         models this machine's Claude Code sessions have actually run. This is
         how a freshly-shipped model becomes selectable with no code edit.
      3. `KNOWN_MODELS` — the baseline, so an offline fresh install still has a
         usable picker.

    Union (never intersection): discovery can only add. A thin transcript
    history must not shrink the picker out from under the operator.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(ids) -> None:
        for m in ids:
            if isinstance(m, str) and m and m not in seen:
                seen.add(m)
                out.append(m)

    _add(cfg.claude_models)

    try:
        from holler.model_discovery import cached_model_ids

        _add(cached_model_ids())
    except Exception:
        # Discovery is best-effort. A partial install or unreadable cache must
        # never leave the operator without a model picker.
        logging.getLogger(__name__).warning(
            "model discovery cache unavailable", exc_info=True
        )

    _add(KNOWN_MODELS)

    # Hide models the installed Claude Code is known to be too old for — the
    # operator's explicit pins and the current pick stay, so nothing vanishes
    # out from under a deliberate choice.
    try:
        from holler.model_discovery import servable

        keep = set(cfg.claude_models) | {cfg.claude_model}
        ok = set(servable(out))
        out = [m for m in out if m in ok or m in keep]
    except Exception:
        logging.getLogger(__name__).warning(
            "model CLI-requirement filter unavailable", exc_info=True
        )
    return out


# faster-whisper GPU execution only exists on Windows/Linux + NVIDIA. On macOS
# (and any box without CUDA) default to the CPU path so a fresh config.toml
# doesn't trigger a load failure + fallback on every launch. Transcriber still
# auto-falls-back at runtime, but starting on the right device is cleaner.
#
# Model size is also platform-tuned: "medium" is a great default on a GPU
# (~300-500ms/clip) but on a CPU it's slow enough to hurt the push-to-talk
# feel, and the weights are ~1.5 GB. "small" is the right CPU default —
# ~480 MB and ~1-2s/clip on Apple Silicon. Operators can raise it in Settings.
_CUDA_PLATFORM = sys.platform == "win32"
_DEFAULT_WHISPER_DEVICE = "cuda" if _CUDA_PLATFORM else "cpu"
_DEFAULT_WHISPER_COMPUTE = "float16" if _CUDA_PLATFORM else "int8"
_DEFAULT_WHISPER_MODEL = "medium" if _CUDA_PLATFORM else "small"


_DEFAULTS: dict[str, object] = {
    "repo_cwd": str(Path.cwd()),
    "claude_model": DEFAULT_MODEL,
    "hotkey": "right alt",
    # Input device for push-to-talk capture. Empty string = follow the OS
    # default input device. Otherwise a device-name substring that
    # sounddevice resolves (e.g. "Scarlett 4i4 4th Gen", or just "Scarlett").
    "mic_device": "",
    # Global UI scale. 0.0 = auto: 1.0 on a Retina/HiDPI primary display, ~1.3
    # on a non-Retina (1x) primary so holler's fixed point-size fonts don't
    # render tiny on high-resolution external monitors. Set a positive value
    # (e.g. 1.0, 1.25, 1.5) to pin it. Applied via QT_SCALE_FACTOR at launch.
    "ui_scale": 0.0,
    "whisper_model": _DEFAULT_WHISPER_MODEL,
    "whisper_compute": _DEFAULT_WHISPER_COMPUTE,
    "whisper_device": _DEFAULT_WHISPER_DEVICE,
    "whisper_language": "en",
    "panel_corner": "top-right",
    "panel_width": 380,
    # 1280 is plenty for code/UI vision tasks and ~40% cheaper to encode than
    # 1920 on the dispatcher hot path. Operators can raise via Settings if
    # they want sharper visuals.
    "max_screenshot_long_edge": 1280,
    "stroke_color": "#FF2E2E",
    "stroke_width": 4,
    "debug_save_captures": True,
    # Phase-2 knobs
    "sessions_window_hotkey": "ctrl+shift+h",
    "auto_archive_days": 30,
    # Rolling retention: permanently purge a session (DB rows + capture PNGs +
    # SDK transcript JSONL on disk) this many days after it's marked Done
    # (reviewed). Pinned/running sessions are always exempt. 0 = disabled (keep
    # completed sessions forever). Default 3 — completed work stops accumulating
    # sensitive transcripts/screenshots on disk indefinitely.
    "purge_reviewed_days": 3,
    "mini_panel_max_sessions": 12,
    # Phase V3 knobs
    "open_sessions_on_launch": True,
    "concurrency_cap": 8,
    # Stage 1 concurrency model: every Holler session gets its own git worktree
    # at <main>/.claude/worktrees/holler-<id>-<slug>/ on branch holler/<id>-<slug>.
    # Default True since 2026-05-12 — leaving this off let parallel sessions
    # share the main checkout and land rogue commits on master (the
    # `git switch master` in session A swapped the branch out from under
    # session B's uncommitted WIP, which then committed to master directly).
    "per_session_worktrees": True,
    # Superintendent lane — operator-seeded evidence inbox that autonomously
    # spawns worker sessions running the fixed 6-step fix-to-deploy pipeline.
    # Distinct from Swarm (top-down discovery); this is bottom-up, operator IS
    # the sensor. ON by default — it's an operator-requested core capability,
    # not a dormant autonomous lane. Its workers OVERRIDE no-auto-push /
    # confirm-before-deploy, so the operator disables here if they ever want
    # the deploy gate closed.
    "superintendent_enabled": True,
    # Max concurrent Superintendent worker sessions. The rest of the queue
    # waits visibly in the ledger. Deploy is serialized regardless (see
    # holler/superintendent/deploy_gate.py) so this only caps analysis/impl.
    # Read HOT from config.toml by the dispatcher each tick — edit + save
    # applies within ~5s, no restart needed.
    "superintendent_max_workers": 10,
    # Model that Superintendent worker sessions run on. Empty = inherit the
    # global `claude_model` (the interactive badge). Set to a KNOWN_MODELS id to
    # pin autonomous workers to a different model than interactive PTT — e.g.
    # run cheaper Sonnet workers while keeping Opus for hands-on sessions.
    # Read live from state["cfg"] by the dispatcher, so a change applies to the
    # next spawned worker without a restart.
    "superintendent_model": "",
    # How a Superintendent worker ships a verified fix.
    #   "land"          (default) — always pushes a branch and opens a pull/merge
    #                   request against the repo's default branch; CI merges it.
    #   "merge_request" — worker may only open the PR; merging is the operator's call.
    "superintendent_ship_policy": "land",
    # Explicit model picker list. Empty (the default) = let `effective_models()`
    # discover what's available and fall back to the KNOWN_MODELS baseline. Set
    # this to pin the badge cycle to a specific set, in a specific order.
    "claude_models": [],
    # Named project repos sessions can target instead of `repo_cwd`, e.g.
    #   projects = { myproject = "/Users/me/code/myproject" }
    # A session pinned to a project gets its worktree under
    # <project>/.claude/worktrees/holler-<id>-<slug>/, forked from that repo's
    # default branch. Empty (the default) = every session uses `repo_cwd`.
    "projects": {},
    # Default project per profile, e.g.
    #   profile_projects = { superintendent = "myproject", investigation = "myproject" }
    # A Superintendent evidence item's own `project` overrides its profile's.
    # Names must exist in `projects`; unknown names fall back to `repo_cwd`.
    "profile_projects": {},
}



def worker_model_setting(choice: str, interactive_model: str) -> str:
    """The `superintendent_model` value to store when the operator picks `choice`.

    Picking the same model as the interactive badge stores "" (inherit), so the
    worker badge un-pins itself and keeps following the badge from then on.
    Storing the id instead would freeze workers on it: the next badge change
    would leave them behind on the older model.
    """
    return "" if choice == interactive_model else choice

@dataclass
class Config:
    repo_cwd: Path
    claude_model: str
    hotkey: str
    mic_device: str
    ui_scale: float
    whisper_model: str
    whisper_compute: str
    whisper_device: str
    whisper_language: str
    panel_corner: str
    panel_width: int
    max_screenshot_long_edge: int
    stroke_color: str
    stroke_width: int
    debug_save_captures: bool
    sessions_window_hotkey: str
    auto_archive_days: int
    purge_reviewed_days: int
    mini_panel_max_sessions: int
    open_sessions_on_launch: bool
    concurrency_cap: int
    per_session_worktrees: bool
    superintendent_enabled: bool = True
    superintendent_max_workers: int = 10
    superintendent_model: str = ""
    superintendent_ship_policy: str = "land"
    claude_models: list[str] = field(default_factory=list)
    projects: dict[str, str] = field(default_factory=dict)
    profile_projects: dict[str, str] = field(default_factory=dict)
    captures_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        from holler.appdir import app_data_dir
        self.captures_dir = app_data_dir() / "captures"
        self.captures_dir.mkdir(parents=True, exist_ok=True)


def _config_path_default() -> Path:
    return Path(__file__).resolve().parent.parent / "config.toml"


def load(path: Path | None = None) -> Config:
    """Load config.toml. Unknown keys are silently ignored (forward-compat)."""
    if path is None:
        path = _config_path_default()
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    merged: dict[str, Any] = {**_DEFAULTS, **raw}
    merged["repo_cwd"] = Path(str(merged["repo_cwd"]))

    init_fields = {f.name for f in dataclasses.fields(Config) if f.init}
    filtered = {k: v for k, v in merged.items() if k in init_fields}
    return Config(**filtered)


def save(cfg: Config, path: Path | None = None) -> None:
    """Atomic write back to config.toml. Comments in the source are NOT preserved."""
    if path is None:
        path = _config_path_default()

    payload: dict[str, Any] = {}
    for f in dataclasses.fields(Config):
        if not f.init:
            continue
        v = getattr(cfg, f.name)
        if isinstance(v, Path):
            payload[f.name] = str(v)
        else:
            payload[f.name] = v

    try:
        import tomli_w
    except ImportError:
        # Fall back to a hand-rolled minimal serializer rather than failing
        # the user's settings save. Only handles str/int/bool/Path scalars.
        text = _serialize_toml(payload)
    else:
        text = tomli_w.dumps(payload)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _serialize_toml(payload: dict[str, Any]) -> str:
    """Minimal flat-TOML serializer for the fallback path."""
    def _scalar(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    lines = []
    for k, v in payload.items():
        if isinstance(v, dict):
            # Inline table — `projects = { canvas = "/path" }`. Keys are
            # quoted so names with spaces or dots stay valid TOML.
            items = ", ".join(f"{_scalar(str(ik))} = {_scalar(iv)}" for ik, iv in v.items())
            lines.append(f"{k} = {{{items}}}" if items else f"{k} = {{}}")
        elif isinstance(v, (list, tuple)):
            # Python's repr uses single quotes, which TOML rejects — emitting it
            # would write a config.toml that the next load() can't parse.
            lines.append(f"{k} = [{', '.join(_scalar(i) for i in v)}]")
        else:
            lines.append(f"{k} = {_scalar(v)}")
    return "\n".join(lines) + "\n"
