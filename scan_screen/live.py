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
    return thumb_of(gray(shot))


def thumb_of(gray_frame: np.ndarray) -> np.ndarray:
    """Small grayscale thumbnail (every THUMB_DIVISOR-th pixel) of a full-resolution frame."""
    return np.ascontiguousarray(gray_frame[::THUMB_DIVISOR, ::THUMB_DIVISOR])


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


def band_cols(tiles: np.ndarray, extra: int = 2) -> tuple[int, int] | None:
    """First and last tile column (inclusive) covering the given tiles, plus a margin."""
    cols = np.flatnonzero(tiles.any(axis=0))
    if not len(cols):
        return None
    return max(0, cols[0] - extra), min(tiles.shape[1] - 1, cols[-1] + extra)


def band_rows(tiles: np.ndarray, extra: int = 1) -> tuple[int, int] | None:
    """First and last tile row (inclusive) covering the given tiles, plus a margin."""
    rows = np.flatnonzero(tiles.any(axis=1))
    if not len(rows):
        return None
    return max(0, rows[0] - extra), min(tiles.shape[0] - 1, rows[-1] + extra)


# -- scroll detection ---------------------------------------------------------------
#
# When an area changes because its content moved up or down (scrolling), the
# translations there can simply move along with it; only the strip of new text
# that scrolled into view needs reading. Detection works on full-resolution
# grayscale frames so the shift is exact to the pixel: errors would otherwise
# add up over a long scroll.


def gray(shot: QPixmap) -> np.ndarray:
    """Full-resolution grayscale copy of a screenshot."""
    img = shot.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(bytes(ptr), np.uint8).reshape(img.height(), img.bytesPerLine())
    return arr[:, : img.width()].copy()


def tiles_box(tiles: np.ndarray, img_shape: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Physical-pixel box (x0, y0, x1, y1) around the given tiles."""
    rows = np.flatnonzero(tiles.any(axis=1))
    cols = np.flatnonzero(tiles.any(axis=0))
    if not len(rows):
        return None
    t = TILE * THUMB_DIVISOR
    return (int(cols[0] * t), int(rows[0] * t),
            min(img_shape[1], int((cols[-1] + 1) * t)), min(img_shape[0], int((rows[-1] + 1) * t)))


def detect_scroll(prev: np.ndarray, cur: np.ndarray, box: tuple[int, int, int, int],
                  ignore: np.ndarray | None = None, min_rows: int = 40) -> tuple[int, int, int] | None:
    """Find a vertical scroll inside ``box`` (physical px).

    Returns (dy, top, bottom): the content moved by dy (negative = up) and the
    rows top..bottom (in the current frame) are the part that moved. Rows that
    stayed put, such as a fixed header next to the scrolling area, are left out.
    Returns None when the change isn't a plain vertical scroll. ``ignore`` marks
    pixels to leave out of the comparison (e.g. our own overlay).
    """
    if prev is None or prev.shape != cur.shape:
        return None
    x0, y0, x1, y1 = box
    h = y1 - y0
    if h < min_rows * 2 or x1 - x0 < 40:
        return None
    cols = slice(x0, x1, 2)  # every other column is plenty and twice as fast
    # Trim to the rows that changed: static bars above or below the scrolling area
    # (a header, a message box) would otherwise dominate the comparison.
    diff_rows = np.abs(cur[y0:y1, cols].astype(np.int16) - prev[y0:y1, cols].astype(np.int16)).mean(axis=1)
    moved = np.flatnonzero(diff_rows > 3.0)
    if len(moved) < min_rows // 2:
        return None
    y0, y1 = y0 + int(moved[0]), y0 + int(moved[-1]) + 1
    h = y1 - y0
    if h < min_rows * 2:
        return None
    a = prev[y0:y1, cols].astype(np.float32)
    b = cur[y0:y1, cols].astype(np.float32)
    keep = None if ignore is None else ~ignore[y0:y1, cols]

    def row_err(dy: int) -> np.ndarray:
        """Mean difference per current-frame row if content moved by dy (NaN where unknown)."""
        out = np.full(h, np.nan, dtype=np.float32)
        if dy >= 0:
            cb, ca, rows = b[dy:], a[:h - dy], slice(dy, h)
            k = None if keep is None else keep[dy:] & keep[:h - dy]
        else:
            cb, ca, rows = b[:dy], a[-dy:], slice(0, h + dy)
            k = None if keep is None else keep[:dy] & keep[-dy:]
        d = np.abs(cb - ca)
        if k is None:
            out[rows] = d.mean(axis=1)
        else:
            cnt = k.sum(axis=1)
            sums = np.where(k, d, 0).sum(axis=1)
            vals = np.where(cnt >= k.shape[1] * 0.3, sums / np.maximum(cnt, 1), np.nan)
            out[rows] = vals
        return out

    # 1) Coarse: compare per-row brightness profiles for every possible shift.
    pa, pb = a.mean(axis=1), b.mean(axis=1)
    pa -= pa.mean()
    pb -= pb.mean()
    coarse = _profile_candidates(pa, pb, h, min_rows)
    if not coarse:
        return None

    # 2) Row by row: which rows changed at all, and how well does each shift explain them?
    still = np.nan_to_num(row_err(0), nan=0.0)
    changed_rows = still > 3.0
    if changed_rows.sum() < min_rows // 2:
        return None
    textured = b.std(axis=1) > 8.0  # rows of the new frame with visible content on them
    scored = []
    # Small shifts are always checked: slow scrolling moves a few pixels at a time.
    for dy in sorted({*range(-8, 9), *coarse}):
        # A real scroll lines up across most of the moving area; a chance match lines up a sliver.
        if dy == 0 or abs(dy) > h - max(min_rows, 0.4 * h):
            continue
        err = row_err(dy)
        known = changed_rows & ~np.isnan(err)
        if known.sum() < min_rows // 2:
            continue
        e = np.nan_to_num(err, nan=255.0)
        # A row is explained when the shift removes most of its difference. Judging each row
        # against its own change keeps small mismatches from hiding in a wide average.
        explained = known & (e < np.maximum(1.0, 0.25 * still))
        # Content that vanished (now blank) can line up with blank gaps by chance. A real
        # scroll lines up rows that have something on them in the new frame.
        if (explained & textured).sum() < max(min_rows // 4, 0.3 * explained.sum()):
            continue
        # Rank by the error over every overlapping row, not just the changed ones: flat
        # shapes (buttons, highlight bars) match several shifts if only their edges count.
        overlap = ~np.isnan(err)
        total = float(np.nan_to_num(err, nan=0.0)[overlap].mean())
        scored.append((round(total, 3), abs(dy), dy, explained.sum() / known.sum()))
    if not scored:
        return None
    _, _, dy, fit = min(scored)
    if fit < 0.85:
        return None
    rows = np.flatnonzero(changed_rows)
    return dy, y0 + int(rows[0]), y0 + int(rows[-1]) + 1


def _profile_candidates(pa: np.ndarray, pb: np.ndarray, h: int, min_rows: int, n: int = 3) -> set[int]:
    """The few best shifts by row-profile match (plus neighbours), to check properly."""
    errs = []
    for dy in range(-(h - min_rows), h - min_rows + 1):
        if dy == 0:
            continue
        e = np.abs(pb[dy:] - pa[:-dy]).mean() if dy > 0 else np.abs(pb[:dy] - pa[-dy:]).mean()
        errs.append((e, dy))
    errs.sort()
    out: set[int] = set()
    for _, dy in errs[:n]:
        out.update((dy - 1, dy, dy + 1))
    return out


# -- remembering what was translated -------------------------------------------------
#
# Every translation keeps a fingerprint: the grayscale pixels of the original text
# it covers. When that spot changes, comparing with the fingerprint tells apart
#   "same"     - nothing really changed under it (the change was next to it),
#   "restyled" - the same letters in another colour, underlined or highlighted
#                (hovering a link): just redraw the translation in the new colours,
#   "changed"  - different text: read it again.


def rgb(shot: QPixmap) -> np.ndarray:
    """Full-resolution RGB copy of a screenshot."""
    img = shot.toImage().convertToFormat(QImage.Format.Format_RGB888)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(bytes(ptr), np.uint8).reshape(img.height(), img.bytesPerLine())
    return arr[:, : img.width() * 3].reshape(img.height(), img.width(), 3).copy()


def edge_map(g: np.ndarray) -> np.ndarray:
    """Where the outlines of letters are, whatever their colours."""
    g = g.astype(np.int16)
    gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
    gy = np.abs(np.diff(g, axis=0, prepend=g[:1]))
    m = np.maximum(gx, gy)
    return m > max(20, int(m.max()) * 0.3)


def _grow(mask: np.ndarray, r: int = 1) -> np.ndarray:
    h, w = mask.shape
    p = np.pad(mask, r)
    out = np.zeros_like(mask)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out |= p[dy:dy + h, dx:dx + w]
    return out


def compare_region(fingerprint: np.ndarray | None, current: np.ndarray) -> str:
    """'same', 'restyled' or 'changed' (see above)."""
    if fingerprint is None or current.shape != fingerprint.shape or fingerprint.size == 0:
        return "changed"
    if float(np.abs(current.astype(np.int16) - fingerprint).mean()) < 4.0:
        return "same"
    a, b = edge_map(fingerprint), edge_map(current)
    if a.sum() < 10:
        return "changed"
    # Allow a pixel of slack; extra outlines (an underline, a highlight's edge) are fine,
    # missing ones (letters that aren't there any more) are not.
    recall = (a & _grow(b)).sum() / a.sum()
    precision = (b & _grow(a)).sum() / max(1, b.sum())
    return "restyled" if recall >= 0.85 and precision >= 0.55 else "changed"
