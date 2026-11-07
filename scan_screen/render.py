"""Erase original text from a screenshot so the translation can be drawn in its place.

For each paragraph we
  1. estimate the background colour from a thin ring around the text,
  2. mark pixels inside the text lines that differ from it (the glyphs),
  3. take the original text colour from the most "inky" of those pixels,
  4. remove the glyphs: flat backgrounds (most UI) are filled with their exact
     colour, anything else (gradients, photos) is inpainted from its surroundings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
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
    mask = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((3, 3), np.uint8), iterations=2)
    mask[~in_lines] = 0
    if flat:
        # Exact colour, no blending: anything else leaves faint outlines of the old words.
        crop[in_lines] = bg.astype(np.uint8)
    elif mask.any():
        crop = cv2.inpaint(crop, mask, 3, cv2.INPAINT_TELEA)

    return Patch(
        x=x0, y=y0, image=Image.fromarray(crop),
        block=block, translation=translation,
        text_color=text_color, bg_color=tuple(int(c) for c in bg),
    )


def make_patches(image: Image.Image, blocks: list[TextBlock], translations: list[str]) -> list[Patch]:
    arr = np.ascontiguousarray(np.asarray(image.convert("RGB")))
    return [
        make_patch(arr, b, t)
        for b, t in zip(blocks, translations)
        if t.strip() and not is_unchanged(b.text, t)
    ]
