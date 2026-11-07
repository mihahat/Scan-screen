"""Erase original text from a screenshot so the translation can be drawn in its place.

For each paragraph we
  1. estimate the background colour from a thin ring around the text,
  2. mark pixels inside the text lines that differ from it (the glyphs),
  3. take the original text colour from the most "inky" of those pixels,
  4. remove the glyphs: flat backgrounds (most UI) are filled with their exact
     colour, anything else (gradients, photos) is filled in smoothly from the
     surrounding pixels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .ocr import TextBlock

MARGIN = 6          # px of surrounding background kept around each paragraph
LINE_PAD = 3        # glyph edges often stick out of Tesseract's boxes by a pixel or two
INK_THRESHOLD = 40  # colour distance from the background that counts as text
FLAT_STD = 6        # background noise below this counts as a single flat colour


@dataclass
class Patch:
    """A cleaned piece of the screen plus what to write on it (physical pixels)."""

    x: int
    y: int
    image: Image.Image  # background with the original text removed
    block: TextBlock    # original text position, relative to the screenshot
    translation: str
    text_color: tuple[int, int, int]
    bg_color: tuple[int, int, int]


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def needs_translation(text: str) -> bool:
    """Skip OCR noise: numbers, symbols and single characters."""
    letters = sum(ch.isalpha() for ch in text)
    return letters >= 2


def is_unchanged(original: str, translation: str) -> bool:
    """True when the text was already in the target language."""
    return _normalize(original) == _normalize(translation)


def make_patch(screenshot: np.ndarray, block: TextBlock, translation: str) -> Patch:
    h_img, w_img = screenshot.shape[:2]
    x0 = max(0, block.x - MARGIN)
    y0 = max(0, block.y - MARGIN)
    x1 = min(w_img, block.x + block.w + MARGIN)
    y1 = min(h_img, block.y + block.h + MARGIN)
    crop = screenshot[y0:y1, x0:x1].copy()

    # 1. Background: median of the ring between the text box and the margin.
    ring = np.ones(crop.shape[:2], dtype=bool)
    ring[max(0, block.y - y0 - LINE_PAD):block.y - y0 + block.h + LINE_PAD,
         max(0, block.x - x0 - LINE_PAD):block.x - x0 + block.w + LINE_PAD] = False
    ring_px = crop[ring] if ring.any() else crop.reshape(-1, 3)
    bg = np.median(ring_px, axis=0)
    flat = bool(ring_px.std(axis=0).max() < FLAT_STD)

    # 2. Glyph mask: pixels inside the line boxes that differ from the background.
    in_lines = np.zeros(crop.shape[:2], dtype=bool)
    for lx, ly, lw, lh in block.lines or [(block.x, block.y, block.w, block.h)]:
        in_lines[max(0, ly - y0 - LINE_PAD):ly - y0 + lh + LINE_PAD,
                 max(0, lx - x0 - LINE_PAD):lx - x0 + lw + LINE_PAD] = True
    dist = np.abs(crop.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
    ink = in_lines & (dist > INK_THRESHOLD)

    # 3. Text colour: the strongest ink pixels (anti-aliased edges are lighter).
    if ink.any():
        strong = ink & (dist >= dist[ink].max() * 0.7)
        text_color = tuple(int(c) for c in np.median(crop[strong], axis=0))
    else:
        lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        text_color = (20, 20, 20) if lum > 128 else (240, 240, 240)

    # 4. Remove the glyphs. Dilate so anti-aliased fringes go too.
    mask = _dilate(ink, 2) & in_lines
    if flat:
        # Exact colour, no blending: anything else leaves faint outlines of the old words.
        crop[in_lines] = bg.astype(np.uint8)
    elif mask.any():
        crop = inpaint(crop, mask)

    return Patch(
        x=x0, y=y0, image=Image.fromarray(crop),
        block=block, translation=translation,
        text_color=text_color, bg_color=tuple(int(c) for c in bg),
    )


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Grow a boolean mask by ``radius`` pixels (square neighbourhood)."""
    h, w = mask.shape
    padded = np.pad(mask, radius)
    out = np.zeros_like(mask)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out |= padded[dy:dy + h, dx:dx + w]
    return out


def inpaint(img: np.ndarray, mask: np.ndarray, iterations: int = 60) -> np.ndarray:
    """Fill masked pixels from their surroundings.

    Letter strokes are thin, so each masked pixel is first set by blending,
    along its row, the nearest known pixels on either side. That is already
    exact for horizontal gradients. A few rounds of neighbour averaging then
    smooth out vertical changes, with known pixels held fixed.
    """
    out = img.astype(np.float32)
    h, w = mask.shape
    cols = np.arange(w)
    for y in np.flatnonzero(mask.any(axis=1)):
        known = ~mask[y]
        if not known.any():
            continue
        for c in range(out.shape[2]):
            out[y, mask[y], c] = np.interp(cols[mask[y]], cols[known], out[y, known, c])
    # Rows that were masked end to end: borrow from the nearest row above/below.
    full_rows = np.flatnonzero(mask.all(axis=1))
    good_rows = np.flatnonzero(~mask.all(axis=1))
    if len(full_rows) and len(good_rows):
        for y in full_rows:
            out[y] = out[good_rows[np.abs(good_rows - y).argmin()]]
    for _ in range(iterations):
        p = np.pad(out, ((1, 1), (1, 1), (0, 0)), mode="edge")
        avg = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) * 0.25
        out[mask] = avg[mask]
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def make_patches(image: Image.Image, blocks: list[TextBlock], translations: list[str]) -> list[Patch]:
    arr = np.ascontiguousarray(np.asarray(image.convert("RGB")))
    return [
        make_patch(arr, b, t)
        for b, t in zip(blocks, translations)
        if t.strip() and not is_unchanged(b.text, t)
    ]
