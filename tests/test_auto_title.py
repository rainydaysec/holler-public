"""AutoTitler: the lifetime cap budgets failures (not successes), and the
titler's SDK options load no user settings and allow large messages."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import claude_agent_sdk
import pytest

from holler import auto_title
from holler.auto_title import _MAX_LIFETIME_ATTEMPTS, AutoTitler


class _Store:
    def __init__(self) -> None:
        self.attempts = 0
        self.titles: list[str] = []

    def get_session(self, sid):
        return SimpleNamespace(auto_titled=True, title_attempts=self.attempts)

    def bump_title_attempt(self, sid):
        self.attempts += 1

    def reset_title_attempts(self, sid):
        self.attempts = 0

    def set_title(self, sid, title, auto=True):
        self.titles.append(title)


def _run(titler, retitle=True):
    asyncio.run(titler._run_inner(1, ["fix the login bug"], None, retitle=retitle))


def test_successful_titles_do_not_burn_the_lifetime_cap(monkeypatch):
    store = _Store()
    titler = AutoTitler(store, repo_cwd=".")

    async def ok(*a, **kw):
        return "Fix Login Bug"

    monkeypatch.setattr(titler, "_call_api", ok)
    for _ in range(_MAX_LIFETIME_ATTEMPTS + 3):
        _run(titler)
    assert len(store.titles) == _MAX_LIFETIME_ATTEMPTS + 3
    assert store.attempts == 0


def test_failures_still_hit_the_cap(monkeypatch):
    store = _Store()
    titler = AutoTitler(store, repo_cwd=".")

    async def fail(*a, **kw):
        raise RuntimeError("api down")

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(titler, "_call_api", fail)
    monkeypatch.setattr(auto_title.asyncio, "sleep", no_sleep)
    for _ in range(_MAX_LIFETIME_ATTEMPTS + 2):
        _run(titler)
    # each failed attempt writes a local-heuristic fallback until the cap
    assert store.attempts == _MAX_LIFETIME_ATTEMPTS
    assert len(store.titles) == _MAX_LIFETIME_ATTEMPTS


def test_titler_options_skip_user_settings_and_raise_buffer(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, options):
            captured["opts"] = options

        async def connect(self):
            raise RuntimeError("stop here")

        async def disconnect(self):
            pass

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _Client)
    titler = AutoTitler(_Store(), repo_cwd=".")
    with pytest.raises(RuntimeError):
        asyncio.run(titler._call_api(["x"], None, retitle=False))
    opts = captured["opts"]
    assert opts.setting_sources == []
    assert opts.max_buffer_size >= 32 * 1024 * 1024


# ── The title screenshot is a thumbnail, not the full capture ───────────────
#
# A title is five to seven words. Anthropic bills an image at roughly
# width*height/750 tokens, so attaching the 1280px composite spent ~1300
# tokens per title request; `title_image_b64` downscales it first.


def _png(tmp_path, size):
    from PIL import Image

    path = tmp_path / "shot.png"
    Image.new("RGB", size, (20, 30, 40)).save(path)
    return str(path)


def test_title_image_is_downscaled_to_the_thumbnail_edge(tmp_path):
    import base64
    import io

    from PIL import Image

    b64 = auto_title.title_image_b64(_png(tmp_path, (1280, 800)))
    assert b64
    decoded = Image.open(io.BytesIO(base64.standard_b64decode(b64)))
    assert max(decoded.size) == auto_title.TITLE_IMAGE_LONG_EDGE
    # ~10x fewer image tokens than the 1280px capture it came from.
    before = 1280 * 800 / 750
    after = decoded.size[0] * decoded.size[1] / 750
    assert after < before / 8


def test_an_image_already_under_the_edge_is_not_upscaled(tmp_path):
    import base64
    import io

    from PIL import Image

    b64 = auto_title.title_image_b64(_png(tmp_path, (200, 120)))
    decoded = Image.open(io.BytesIO(base64.standard_b64decode(b64)))
    assert decoded.size == (200, 120)


@pytest.mark.parametrize("path", ["", None])
def test_no_screenshot_is_not_an_error(path):
    assert auto_title.title_image_b64(path) is None


def test_an_unreadable_screenshot_is_logged_and_titling_continues(tmp_path, caplog):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not a png")
    with caplog.at_level("WARNING"):
        assert auto_title.title_image_b64(str(bad)) is None
    assert "unusable screenshot" in caplog.text
