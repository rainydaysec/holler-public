"""Golden-image screenshot tests for every holler surface.

Each scenario in `scenarios.SCENARIOS` is rendered offscreen in a hermetic,
frozen environment (see scenarios.VisualEnv) and compared against
`goldens/<name>.png` with a small per-pixel tolerance.

    HOLLER_UPDATE_GOLDENS=1 .venv/bin/python -m pytest tests/visual   # regenerate
    .venv/bin/python tests/visual/render_review.py /tmp/review       # eyeball

On mismatch, actual / expected / diff PNGs are written to a temp dir whose
paths are printed in the failure message.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="goldens are macOS-rendered")

from tests.visual import compare as cmp  # noqa: E402
from tests.visual import scenarios as sc  # noqa: E402


@pytest.mark.parametrize("name", sorted(sc.SCENARIOS))
def test_golden(name, app, tmp_path):
    with sc.VisualEnv(tmp_path / "env") as env:
        actual = cmp.qimage_to_array(sc.render(name, env))

    golden = cmp.GOLDENS_DIR / f"{name}.png"
    if cmp.update_requested():
        cmp.save_png(actual, golden)
        print(f"updated golden {golden}")
        return
    if not golden.exists():
        pytest.fail(f"missing golden {golden} — run with HOLLER_UPDATE_GOLDENS=1 to create it")

    expected = cmp.load_png(golden)
    result = cmp.compare(actual, expected)
    if result.ok:
        return
    out = Path(tempfile.mkdtemp(prefix=f"holler-visual-{name}-"))
    cmp.save_png(actual, out / f"{name}.actual.png")
    cmp.save_png(expected, out / f"{name}.expected.png")
    paths = [out / f"{name}.actual.png", out / f"{name}.expected.png"]
    if result.mask is not None:
        cmp.save_png(cmp.diff_image(actual, expected, result.mask), out / f"{name}.diff.png")
        paths.append(out / f"{name}.diff.png")
    listing = "\n".join(f"  {p}" for p in paths)
    print(f"visual diff for {name}:\n{listing}")
    pytest.fail(
        f"{name}: rendered output differs from golden — {result.reason}\n{listing}\n"
        f"If the change is intended, regenerate with HOLLER_UPDATE_GOLDENS=1.",
        pytrace=False,
    )


def test_every_golden_has_a_scenario():
    """A deleted scenario must not leave a stale golden silently unchecked."""
    if cmp.update_requested():
        return
    orphans = sorted(p.stem for p in cmp.GOLDENS_DIR.glob("*.png") if p.stem not in sc.SCENARIOS)
    assert not orphans, f"goldens without a scenario (surface removed?): {orphans}"
