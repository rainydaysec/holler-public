"""Render every golden scenario to PNGs for human review.

    .venv/bin/python tests/visual/render_review.py <out_dir> [scenario ...]

Uses the exact code path the golden tests use (scenarios.VisualEnv +
scenarios.render), so the output matches what the tests compare.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.visual import compare as cmp  # noqa: E402
from tests.visual import scenarios as sc  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    out = Path(argv[0]).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    names = argv[1:] or sorted(sc.SCENARIOS)
    unknown = [n for n in names if n not in sc.SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}; known: {sorted(sc.SCENARIOS)}")
        return 2
    sc.qapp()
    for name in names:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="holler-review-") as td:
            with sc.VisualEnv(Path(td)) as env:
                arr = cmp.qimage_to_array(sc.render(name, env))
        path = out / f"{name}.png"
        cmp.save_png(arr, path)
        golden = cmp.GOLDENS_DIR / f"{name}.png"
        status = "no golden"
        if golden.exists():
            r = cmp.compare(arr, cmp.load_png(golden))
            status = "matches golden" if r.ok else f"DIFFERS from golden: {r.reason}"
        print(f"{path}  ({arr.shape[1]}x{arr.shape[0]}, {time.monotonic() - t0:.2f}s)  {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
