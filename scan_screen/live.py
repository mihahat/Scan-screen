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


# -- tile-based tracking -------------------------------------------------------------
#
# Live mode compares each thumbnail with the previous one (not with the screen
# as it was before we drew anything), tile by tile. Only translations on tiles
# that changed are removed and re-read. Tiles we changed ourselves (by drawing or
# removing a translation) are ignored for a couple of checks, so the overlay can
# never trigger its own refresh, whether or not screenshots include it.

TILE = 16       # tile size in thumbnail pixels (48 physical px)
TILE_MIN = 8    # changed pixels needed in one tile (a blinking caret is fewer)


def tile_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Boolean grid of tiles that differ between two thumbnails of the same size."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL_DELTA
    h, w = d.shape
    th, tw = -(-h // TILE), -(-w // TILE)
    padded = np.zeros((th * TILE, tw * TILE), dtype=bool)
    padded[:h, :w] = d
    counts = padded.reshape(th, TILE, tw, TILE).sum(axis=(1, 3))
    return counts >= TILE_MIN


class ChangeTracker:
    def __init__(self) -> None:
        self.prev: np.ndarray | None = None
        self.dirty: np.ndarray | None = None   # changed and not yet re-read
        self.quiet: np.ndarray | None = None   # checks since the tile last changed
        self.ignore: np.ndarray | None = None  # checks left to ignore (our own drawing)

    @property
    def shape(self) -> tuple[int, int] | None:
        return None if self.dirty is None else self.dirty.shape

    def update(self, thumb: np.ndarray) -> np.ndarray | None:
        """Feed a new thumbnail. Returns the tiles that changed since the last one."""
        if self.prev is None or self.prev.shape != thumb.shape:
            self.prev = thumb
            grid = tile_diff(thumb, thumb).shape
            self.dirty = np.zeros(grid, dtype=bool)
            self.quiet = np.zeros(grid, dtype=np.int32)
            self.ignore = np.zeros(grid, dtype=np.int32)
            return None
        changed = tile_diff(thumb, self.prev) & (self.ignore == 0)
        self.ignore = np.maximum(self.ignore - 1, 0)
        self.prev = thumb
        self.dirty |= changed
        self.quiet = np.where(changed, 0, self.quiet + 1)
        return changed

    def ready(self, quiet_checks: int = 1) -> np.ndarray:
        """Dirty tiles that have stopped changing: time to read them again."""
        if self.dirty is None:
            return np.zeros((0, 0), dtype=bool)
        return self.dirty & (self.quiet >= quiet_checks)

    def ignore_tiles(self, tiles: np.ndarray, checks: int = 2) -> None:
        if self.ignore is not None and tiles.shape == self.ignore.shape:
            self.ignore = np.where(tiles, np.maximum(self.ignore, checks), self.ignore)

    def mark_dirty(self, tiles: np.ndarray) -> None:
        if self.dirty is not None and tiles.shape == self.dirty.shape:
            self.dirty |= tiles

    def clear(self, tiles: np.ndarray) -> None:
        if self.dirty is not None and tiles.shape == self.dirty.shape:
            self.dirty &= ~tiles


def tile_px(dpr: float) -> float:
    """Size of one tile in logical (Qt) pixels."""
    return TILE * THUMB_DIVISOR / dpr


def rects_to_tiles(rects, shape: tuple[int, int], dpr: float, pad: float = 0) -> np.ndarray:
    """Tiles touched by rectangles given in logical pixels (relative to the screen)."""
    mask = np.zeros(shape, dtype=bool)
    t = tile_px(dpr)
    for r in rects:
        x0 = max(0, int((r.x() - pad) // t))
        y0 = max(0, int((r.y() - pad) // t))
        x1 = min(shape[1] - 1, int((r.x() + r.width() + pad) // t))
        y1 = min(shape[0] - 1, int((r.y() + r.height() + pad) // t))
        if x0 <= x1 and y0 <= y1:
            mask[y0:y1 + 1, x0:x1 + 1] = True
    return mask


def band_rows(tiles: np.ndarray, extra: int = 1) -> tuple[int, int] | None:
    """First and last tile row (inclusive) covering the given tiles, plus a margin."""
    rows = np.flatnonzero(tiles.any(axis=1))
    if not len(rows):
        return None
    return max(0, rows[0] - extra), min(tiles.shape[0] - 1, rows[-1] + extra)
