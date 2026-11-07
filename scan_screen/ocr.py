"""Read text and its position from a screenshot with Tesseract."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from PIL import Image, ImageOps, ImageStat

# Scripts that are written without spaces between words.
_NO_SPACE_LANGS = {"jpn", "jpn_vert", "chi_sim", "chi_tra", "chi_sim_vert", "chi_tra_vert", "tha", "lao", "khm", "mya"}


@dataclass
class TextBlock:
    """A paragraph of text and its bounding box, in pixels of the source image."""

    text: str
    x: int
    y: int
    w: int
    h: int
    line_height: int


def _joiner(langs: str) -> str:
    parts = set(langs.split("+"))
    return "" if parts and parts <= _NO_SPACE_LANGS else " "


def group_words(data: dict, min_conf: float = 40, scale: float = 1.0, joiner: str = " ") -> list[TextBlock]:
    """Turn pytesseract.image_to_data(output_type=DICT) output into paragraphs.

    Words are grouped into lines by (block, par, line) and lines into
    paragraphs by (block, par). Coordinates are divided by ``scale`` to undo
    any upscaling done before OCR.
    """
    lines: dict[tuple[int, int, int], list[int]] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if conf < min_conf:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    paragraphs: dict[tuple[int, int], list[list[int]]] = {}
    for key in sorted(lines):
        paragraphs.setdefault(key[:2], []).append(lines[key])

    blocks: list[TextBlock] = []
    for para_lines in paragraphs.values():
        idxs = [i for line in para_lines for i in line]
        left = min(data["left"][i] for i in idxs)
        top = min(data["top"][i] for i in idxs)
        right = max(data["left"][i] + data["width"][i] for i in idxs)
        bottom = max(data["top"][i] + data["height"][i] for i in idxs)
        line_heights = [
            max(data["top"][i] + data["height"][i] for i in line) - min(data["top"][i] for i in line)
            for line in para_lines
        ]
        text_lines = [joiner.join(data["text"][i].strip() for i in sorted(line, key=lambda i: data["left"][i])) for line in para_lines]
        # Hyphenated line breaks ("transla-" + "tion") are glued back together.
        text = ""
        for line in text_lines:
            if not text:
                text = line
            elif text.endswith("-") and joiner:
                text = text[:-1] + line
            else:
                text += joiner + line
        blocks.append(
            TextBlock(
                text=text,
                x=round(left / scale),
                y=round(top / scale),
                w=round((right - left) / scale),
                h=round((bottom - top) / scale),
                line_height=max(1, round(statistics.median(line_heights) / scale)),
            )
        )
    return blocks


def prepare_image(image: Image.Image) -> tuple[Image.Image, float]:
    """Grayscale and upscale small captures; Tesseract is much better at ~30px text."""
    gray = ImageOps.grayscale(image)
    # Light text on a dark background (dark mode, games) reads better inverted.
    if ImageStat.Stat(gray).mean[0] < 110:
        gray = ImageOps.invert(gray)
    scale = 2.0 if max(gray.size) < 2500 else 1.0
    if scale != 1.0:
        gray = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.Resampling.LANCZOS)
    return gray, scale


def extract_blocks(image: Image.Image, langs: str = "eng", min_conf: float = 40, tesseract_cmd: str | None = None) -> list[TextBlock]:
    import pytesseract

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    prepared, scale = prepare_image(image)
    data = pytesseract.image_to_data(prepared, lang=langs, output_type=pytesseract.Output.DICT)
    return group_words(data, min_conf=min_conf, scale=scale, joiner=_joiner(langs))
