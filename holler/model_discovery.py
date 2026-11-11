"""Discover which Claude models are actually available, instead of hardcoding them.

The model badge, the settings combo and the first-run combo used to iterate the
hard-coded `config.KNOWN_MODELS` list, so a freshly-shipped model only became
selectable after someone edited Python. This module makes that list data-driven.

Two sources, tried in order:

  1. **Anthropic models API** — `GET /v1/models`, the documented catalogue.
     Only usable when an `ANTHROPIC_API_KEY` is in the environment.
  2. **Claude Code session transcripts** — every model id that has actually
     served a turn on this machine, read out of the JSONL transcripts the
     `claude` CLI writes. This is the path that works for a cloud-subscription
     operator with no API key at all, and it is authoritative by construction:
     a model that answered is a model that is available.

Neither source is trusted to *remove* a model — `config.effective_models()`
unions the result with the baseline, so discovery can only ever add. That keeps
a thin transcript history (a fresh install has none) from shrinking the picker.

The result is cached at `app_data_dir() / "models_cache.json"`:

    {
      "refreshed_at": "2026-10-01T16:40:00+00:00",
      "models": ["claude-opus-5", ...],
      "source": "anthropic-api" | "claude-transcripts",
      "cli_requirements": {"claude-opus-5-5": "2.1.280"}   # optional
    }

The runner kicks `refresh()` on a daemon thread at startup; the picker reads the
cache, so a newly-discovered model shows up on the operator's next click.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CACHE_FILENAME = "models_cache.json"
_MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
_ANTHROPIC_VERSION = "2023-06-01"
# Long enough for a GET to api.anthropic.com on a healthy link, short enough
# that an offline launch doesn't leave the daemon thread hanging around.
_TIMEOUT_SECS = 10.0

# Canonical model ids only: `claude-opus-5`, `claude-opus-4-8`,
# `claude-haiku-4-5-20251001`. Deliberately rejects the `-v1` / `-fast` routing
# variants and the `<synthetic>` placeholder the CLI writes for locally
# generated messages, so neither ends up in the operator's picker.
_CANONICAL_ID = re.compile(r"^claude-[a-z]+-\d+(?:-\d+)?(?:-\d{8})?$")

# Transcripts are append-only JSONL; the model id appears on every assistant
# message, so the head of a file is enough to identify it. Caps keep a long
# history from turning a startup refresh into a disk crawl.
_MAX_TRANSCRIPTS = 60
_MAX_TRANSCRIPT_BYTES = 1 << 20
_MODEL_FIELD = re.compile(rb'"model"\s*:\s*"([^"]{1,80})"')


def _cache_path() -> Path:
    from holler.appdir import app_data_dir

    return app_data_dir() / _CACHE_FILENAME


def load_cache() -> dict:
    """Read the cached discovery result, or `{}` when absent/unreadable.

    Returns a dict (never None) so callers can `.get()` without a guard. A
    corrupt cache is logged and treated as empty — bad JSON must not break the
    model picker.
    """
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("models cache at %s unreadable: %s — ignoring", p, e)
        return {}
    return data if isinstance(data, dict) else {}


def cached_model_ids() -> list[str]:
    """Model ids from the last successful refresh, or `[]` if never refreshed."""
    models = load_cache().get("models")
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, str) and m]


def cli_requirements() -> dict[str, str]:
    """`{model_id: min_cli_version}` learned from "this CLI is too old" errors."""
    reqs = load_cache().get("cli_requirements")
    if not isinstance(reqs, dict):
        return {}
    return {k: v for k, v in reqs.items() if isinstance(k, str) and isinstance(v, str)}


def record_cli_requirement(model_id: str, min_version: str) -> None:
    """Remember that `model_id` needs Claude Code >= `min_version`.

    A transcript-discovered model may have been served by a newer CLI than the
    one Holler can run (e.g. the desktop app's), so the API's rejection is the
    only authoritative signal. The picker hides the model until a new-enough
    CLI is installed.
    """
    cache = load_cache()
    reqs = cli_requirements()
    if reqs.get(model_id) == min_version:
        return
    reqs[model_id] = min_version
    try:
        _write_cache({**cache, "cli_requirements": reqs})
    except OSError as e:
        log.warning("could not record CLI requirement for %s: %s", model_id, e)


def servable(model_ids: list[str]) -> list[str]:
    """Drop models known to need a newer Claude Code than Holler will run."""
    reqs = cli_requirements()
    if not reqs:
        return model_ids
    from holler import claude_cli

    have = claude_cli.cli_version()
    if have is None:
        return model_ids
    return [
        m for m in model_ids
        if (need := claude_cli.parse_version(reqs.get(m, ""))) is None or have >= need
    ]


def save_cache(model_ids: list[str], source: str) -> None:
    """Persist the discovered ids atomically (temp + replace)."""
    payload: dict = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": model_ids,
        "source": source,
    }
    reqs = cli_requirements()
    if reqs:
        payload["cli_requirements"] = reqs
    _write_cache(payload)


def _write_cache(payload: dict) -> None:
    p = _cache_path()
    # Unique temp per writer: the startup refresh thread and the Settings
    # "refresh" button can save concurrently, and a shared `.tmp` name lets
    # one writer replace/unlink the other's half-written file.
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def last_refreshed() -> Optional[str]:
    """ISO timestamp of the last successful cache write, or None."""
    ts = load_cache().get("refreshed_at")
    return ts if isinstance(ts, str) else None


def _dedupe(ids) -> list[str]:
    """Keep canonical ids only, first-seen order, no repeats."""
    seen: set[str] = set()
    out: list[str] = []
    for m in ids:
        if isinstance(m, str) and _CANONICAL_ID.match(m) and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def discover_from_api(api_key: str) -> list[str]:
    """Fetch the documented model catalogue. Raises RuntimeError on failure.

    The API returns newest-first, which is the order the picker wants.
    """
    req = urllib.request.Request(
        _MODELS_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"/v1/models returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach api.anthropic.com: {e.reason}") from e

    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"/v1/models did not return JSON: {body[:200]}") from e

    data = doc.get("data")
    if not isinstance(data, list):
        raise RuntimeError("/v1/models response has no 'data' array")
    return _dedupe(entry.get("id") for entry in data if isinstance(entry, dict))


def _transcript_root() -> Path:
    """Where the `claude` CLI keeps session transcripts."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def discover_from_transcripts(root: Path | None = None) -> list[str]:
    """Model ids that have actually served a turn on this machine.

    Newest-transcript-first, so the model the operator ran most recently leads
    the picker. Returns `[]` when no transcripts exist (fresh install) — the
    caller unions with the baseline, so an empty result is harmless.
    """
    root = root if root is not None else _transcript_root()
    if not root.is_dir():
        return []
    try:
        files = sorted(
            root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError as e:
        log.warning("could not list transcripts under %s: %s", root, e)
        return []

    found: list[str] = []
    for f in files[:_MAX_TRANSCRIPTS]:
        try:
            with f.open("rb") as fh:
                blob = fh.read(_MAX_TRANSCRIPT_BYTES)
        except OSError:
            # A transcript being rotated out from under us is routine; the
            # remaining files still give us a list.
            continue
        found.extend(m.decode("utf-8", "replace") for m in _MODEL_FIELD.findall(blob))
    return _dedupe(found)


def discover() -> tuple[list[str], str]:
    """Return `(model_ids, source)`. Raises RuntimeError when nothing is found."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return discover_from_api(api_key), "anthropic-api"

    ids = discover_from_transcripts()
    if not ids:
        raise RuntimeError(
            "no ANTHROPIC_API_KEY and no Claude Code transcripts to learn from"
        )
    return ids, "claude-transcripts"


def refresh() -> tuple[list[str], Optional[str]]:
    """Discover and cache. Returns `(model_ids, error_message_or_None)`.

    On failure returns the previously cached ids plus a short message for the
    Settings status line. Never raises — this runs on a startup daemon thread
    and on a dialog button, and neither should be able to take Holler down.
    """
    try:
        ids, source = discover()
    except Exception as e:
        log.warning("model discovery failed: %s", e)
        return cached_model_ids(), str(e) or type(e).__name__

    try:
        save_cache(ids, source)
    except OSError as e:
        log.warning("could not write models cache: %s", e)
        return ids, f"discovered {len(ids)} models but cache write failed: {e}"

    log.info("model discovery: %d models cached from %s", len(ids), source)
    return ids, None
