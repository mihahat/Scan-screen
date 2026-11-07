"""Read text and its position from a screenshot with Tesseract."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

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
    # Bounding box (x, y, w, h) of each line, used to erase exactly the original text.
    lines: list[tuple[int, int, int, int]] = field(default_factory=list)


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

    grouped: dict[tuple[int, int], list[list[int]]] = {}
    for key in sorted(lines):
        grouped.setdefault(key[:2], []).append(lines[key])

    # Menus, toolbars and table rows put separate items on one "line" with wide
    # gaps between them. Split those so each item is translated and placed on its own.
    paragraphs: list[list[list[int]]] = []
    for para_lines in grouped.values():
        split = [_split_on_gaps(data, line) for line in para_lines]
        if any(len(segments) > 1 for segments in split):
            paragraphs.extend([seg] for segments in split for seg in segments)
        else:
            paragraphs.append(para_lines)

    blocks: list[TextBlock] = []
    for para_lines in paragraphs:
        idxs = [i for line in para_lines for i in line]
        left = min(data["left"][i] for i in idxs)
        top = min(data["top"][i] for i in idxs)
        right = max(data["left"][i] + data["width"][i] for i in idxs)
        bottom = max(data["top"][i] + data["height"][i] for i in idxs)
        line_boxes = []
        for line in para_lines:
            lx = min(data["left"][i] for i in line)
            ly = min(data["top"][i] for i in line)
            lr = max(data["left"][i] + data["width"][i] for i in line)
            lb = max(data["top"][i] + data["height"][i] for i in line)
            line_boxes.append((round(lx / scale), round(ly / scale), round((lr - lx) / scale), round((lb - ly) / scale)))
        line_heights = [b[3] for b in line_boxes]
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
                line_height=max(1, round(statistics.median(line_heights))),
                lines=line_boxes,
            )
        )
    return blocks


def _split_on_gaps(data: dict, line: list[int], gap_factor: float = 2.0) -> list[list[int]]:
    words = sorted(line, key=lambda i: data["left"][i])
    height = statistics.median(data["height"][i] for i in words)
    segments = [[words[0]]]
    for prev, cur in zip(words, words[1:]):
        gap = data["left"][cur] - (data["left"][prev] + data["width"][prev])
        if gap > gap_factor * height:
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


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
    joiner = _joiner(langs)

    def read(img: Image.Image, psm: int) -> list[TextBlock]:
        data = pytesseract.image_to_data(img, lang=langs, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
        return group_words(data, min_conf=min_conf, scale=scale, joiner=joiner)

    # The normal layout mode keeps paragraphs together, which translates best.
    blocks = read(prepared, 3)
    if max(image.size) >= 400:
        # Larger captures (whole screens) mix layouts and light/dark areas, which
        # one pass misses. Fill the gaps with sparse-text mode (isolated labels)
        # and an inverted pass (text the other polarity), keeping only new finds.
        for img, psm in [(prepared, 11), (ImageOps.invert(prepared), 3)]:
            blocks += [b for b in read(img, psm) if not any(_overlaps(b, o) for o in blocks)]
    blocks.sort(key=lambda b: (b.y, b.x))
    return blocks


def _overlaps(a: TextBlock, b: TextBlock) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h
