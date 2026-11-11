"""Bake red stroke polylines onto a snapshot image and emit PNG bytes."""

from __future__ import annotations

import base64
import io
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

# Minimum stroke thickness (in pixels of the final, downscaled image) that
# compose() guarantees when it knows the downscale target (`max_long_edge`).
MIN_OUTPUT_STROKE_PX = 2.0

# Supersampling budget for anti-aliasing: the stroke mask is rendered at up to
# _SS_MAX× over the strokes' bounding box, with the factor reduced so the mask
# stays under _SS_MAX_PIXELS (~32 MB of 8-bit mask) on huge captures.
_SS_MAX = 4
_SS_MAX_PIXELS = 32_000_000


def stroke_width_px(
    stroke_width: float,
    snapshot_size: Tuple[int, int],
    logical_size: Optional[Tuple[int, int]] = None,
    max_long_edge: Optional[int] = None,
    min_output_px: float = MIN_OUTPUT_STROKE_PX,
) -> float:
    """Stroke width in snapshot pixels for a `stroke_width` given in points.

    With `logical_size` (the (w, h) in logical points the overlay showed the
    snapshot at) the width is scaled by snapshot-pixels-per-point, so a 4pt
    overlay stroke is 8px on a 2× Retina capture — the same visual thickness
    the user drew. Without it the width is taken as snapshot pixels (legacy).
    With `max_long_edge`, the width is raised so that after
    `screen.downscale(image, max_long_edge)` it is still >= `min_output_px`.
    """
    w, h = snapshot_size
    width = float(stroke_width)
    if logical_size is not None:
        lw, lh = logical_size
        if lw > 0 and lh > 0 and w > 0 and h > 0:
            width *= ((w / lw) + (h / lh)) / 2.0
    if max_long_edge is not None and max_long_edge > 0:
        long_edge = max(w, h)
        if long_edge > max_long_edge:
            width = max(width, min_output_px * long_edge / max_long_edge)
        else:
            width = max(width, min_output_px)
    return max(1.0, width)


def compose(
    snapshot: Image.Image,
    strokes: List[List[Tuple[int, int]]],
    stroke_color: str = "#FF2E2E",
    stroke_width: int = 4,
    *,
    logical_size: Optional[Tuple[int, int]] = None,
    max_long_edge: Optional[int] = None,
) -> Image.Image:
    """Return a new RGB image with the strokes drawn on top of the snapshot.

    `strokes` are polylines in snapshot-pixel coordinates (overlay.get_strokes()).
    Lines are anti-aliased (supersampled) with round caps and joins.

    Optional keyword-only arguments (omit both for the legacy behaviour where
    `stroke_width` is in snapshot pixels):
      * `logical_size` — (w, h) in logical points the overlay displayed the
        snapshot at, i.e. `WindowSnapshot.logical_size`. Makes `stroke_width`
        mean points, matching the overlay pen on HiDPI displays.
      * `max_long_edge` — the long edge the caller will downscale to
        (cfg.max_screenshot_long_edge); guarantees strokes stay at least
        MIN_OUTPUT_STROKE_PX thick after that downscale. compose() does NOT
        downscale itself.
    """
    if snapshot.mode != "RGB":
        snapshot = snapshot.convert("RGB")
    # No strokes → caller doesn't mutate, and convert("RGB") already gave us a
    # fresh image when conversion happened. Skip the .copy() (~10-50ms on a
    # 1280px image) and hand back the snapshot directly.
    strokes = [s for s in strokes if len(s) >= 2]
    if not strokes:
        return snapshot
    width = stroke_width_px(stroke_width, snapshot.size, logical_size, max_long_edge)
    out = snapshot.copy()
    _draw_strokes_aa(out, strokes, stroke_color, width)
    return out


def _draw_strokes_aa(
    out: Image.Image,
    strokes: Sequence[Sequence[Tuple[float, float]]],
    color: str,
    width: float,
) -> None:
    """Paint `strokes` onto `out` in place: supersampled coverage mask over the
    strokes' bounding box, then a single masked paste of the solid colour."""
    W, H = out.size
    r = width / 2.0
    xs = [p[0] for s in strokes for p in s]
    ys = [p[1] for s in strokes for p in s]
    # Integer bbox (clamped to the image) padded by the radius + AA fringe.
    x0 = max(0, int(min(xs) - r) - 2)
    y0 = max(0, int(min(ys) - r) - 2)
    x1 = min(W, int(max(xs) + r) + 3)
    y1 = min(H, int(max(ys) + r) + 3)
    if x1 <= x0 or y1 <= y0:
        return
    bw, bh = x1 - x0, y1 - y0
    k = _SS_MAX
    while k > 1 and bw * bh * k * k > _SS_MAX_PIXELS:
        k -= 1

    mask = Image.new("L", (bw * k, bh * k), 0)
    d = ImageDraw.Draw(mask)
    rk = r * k
    lw = max(1, int(round(width * k)))

    def _map(p):
        # Pixel (x, y) covers [x, x+1); its centre maps to (x + 0.5) * k.
        return ((p[0] - x0 + 0.5) * k, (p[1] - y0 + 0.5) * k)

    for stroke in strokes:
        pts = [_map(p) for p in stroke]
        d.line(pts, fill=255, width=lw)
        # Round caps + joins: a disc at every vertex.
        for x, y in pts:
            d.ellipse((x - rk, y - rk, x + rk, y + rk), fill=255)
    if k > 1:
        mask = mask.reduce(k)
    region = out.crop((x0, y0, x1, y1))
    region.paste(Image.new("RGB", (bw, bh), color), (0, 0), mask)
    out.paste(region, (x0, y0))


def to_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    # `optimize=True` costs 100-300ms and saves only ~5-15% bytes for these
    # screen-content PNGs. Skip it — first-token latency wins.
    image.save(buf, format="PNG")
    return buf.getvalue()


def to_png_b64(image: Image.Image) -> str:
    return base64.standard_b64encode(to_png_bytes(image)).decode("ascii")


def image_fingerprint(image: Image.Image) -> str:
    """Cheap hash over the raw pixel buffer (no PNG encode).

    Used by the dispatcher to dedup screenshots — if this PTT produces the
    exact same composed image as the previous PTT in the same session, we
    skip the PNG-encode + base64 + upload of an unchanged image (~200-400ms
    on the dispatcher hot path) and append a one-line marker to the
    transcript so Claude knows nothing visually changed.
    """
    import hashlib
    if image.mode != "RGB":
        image = image.convert("RGB")
    h = hashlib.blake2b(digest_size=16)
    h.update(image.size[0].to_bytes(4, "little"))
    h.update(image.size[1].to_bytes(4, "little"))
    h.update(image.tobytes())
    return h.hexdigest()
