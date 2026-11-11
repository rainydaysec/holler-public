"""Golden-image comparison helpers (numpy, tolerance-based)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6 import QtGui

GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"
CHANNEL_TOLERANCE = 3        # max |delta| per channel still counted as equal
MAX_DIFF_FRACTION = 0.002    # ≤ 0.2 % of pixels may exceed the channel tolerance


def update_requested() -> bool:
    return os.environ.get("HOLLER_UPDATE_GOLDENS", "") not in ("", "0", "false", "no")


def qimage_to_array(img: QtGui.QImage) -> np.ndarray:
    img = img.convertToFormat(QtGui.QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    return buf.reshape(h, img.bytesPerLine())[:, : w * 4].reshape(h, w, 4).copy()


def load_png(path: Path) -> np.ndarray:
    img = QtGui.QImage(str(path))
    if img.isNull():
        raise FileNotFoundError(path)
    return qimage_to_array(img)


def save_png(arr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w, _ = arr.shape
    arr = np.ascontiguousarray(arr)
    img = QtGui.QImage(arr.data, w, h, w * 4, QtGui.QImage.Format_RGBA8888).copy()
    if not img.save(str(path), "PNG"):
        raise OSError(f"failed to write {path}")


@dataclass
class Diff:
    ok: bool
    reason: str
    diff_pixels: int = 0
    total_pixels: int = 0
    max_delta: int = 0
    mask: "np.ndarray | None" = None


def compare(actual: np.ndarray, expected: np.ndarray) -> Diff:
    if actual.shape != expected.shape:
        return Diff(False, f"size mismatch: actual {actual.shape[1]}x{actual.shape[0]} "
                           f"vs expected {expected.shape[1]}x{expected.shape[0]}")
    delta = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    mask = (delta > CHANNEL_TOLERANCE).any(axis=2)
    n = int(mask.sum())
    total = mask.size
    ok = n <= MAX_DIFF_FRACTION * total
    return Diff(ok, f"{n}/{total} pixels ({100.0 * n / total:.3f}%) exceed channel delta "
                    f"{CHANNEL_TOLERANCE} (limit {100 * MAX_DIFF_FRACTION:.1f}%), max delta "
                    f"{int(delta.max())}", n, total, int(delta.max()), mask)


def diff_image(actual: np.ndarray, expected: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Dimmed expected image with differing pixels painted magenta."""
    out = (expected.astype(np.uint16) // 3).astype(np.uint8)
    out[..., 3] = 255
    out[mask] = (255, 0, 255, 255)
    return out
