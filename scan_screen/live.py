"""Cheap screen-change detection for live mode.

Each check grabs the screen, shrinks it to a small grayscale thumbnail and
compares it with the previous one. Only when enough pixels change is the
expensive OCR + translation run again. Areas covered by our own overlay can
be masked out, so the overlay itself never counts as a change.
"""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QImage, QPixmap

THUMB_DIVISOR = 3     # thumbnail is 1/3 of the screen size in each direction
PIXEL_DELTA = 24      # grey-level difference that counts as a changed pixel
MIN_CHANGED = 12      # changed thumbnail pixels needed (a blinking caret is ~6)


def thumbnail(shot: QPixmap) -> np.ndarray:
    img = shot.toImage()
    w = max(1, img.width() // THUMB_DIVISOR)
    h = max(1, img.height() // THUMB_DIVISOR)
    small = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    small = small.convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = small.constBits()
    ptr.setsize(small.sizeInBytes())
    arr = np.frombuffer(bytes(ptr), np.uint8).reshape(small.height(), small.bytesPerLine())
    return arr[:, : small.width()].copy()


def overlay_mask(shape: tuple[int, int], rects: list[QRectF], logical_width: float, pad: int = 2) -> np.ndarray:
    """Boolean mask (thumbnail coordinates) of the areas our overlay draws on."""
    mask = np.zeros(shape, dtype=bool)
    if logical_width <= 0:
        return mask
    s = shape[1] / logical_width
    for r in rects:
        x0 = max(0, int(r.x() * s) - pad)
        y0 = max(0, int(r.y() * s) - pad)
        x1 = min(shape[1], int((r.x() + r.width()) * s) + pad + 1)
        y1 = min(shape[0], int((r.y() + r.height()) * s) + pad + 1)
        mask[y0:y1, x0:x1] = True
    return mask


def changed(a: np.ndarray | None, b: np.ndarray | None, mask: np.ndarray | None = None) -> bool:
    if a is None or b is None or a.shape != b.shape:
        return True
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL_DELTA
    if mask is not None:
        diff &= ~mask
    return int(diff.sum()) >= MIN_CHANGED
