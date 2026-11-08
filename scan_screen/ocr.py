"""Read text and its position from a screenshot with Tesseract."""

from __future__ import annotations

import io
import os
import shutil
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image, ImageOps, ImageStat


class OcrError(RuntimeError):
    pass


# Environment for Tesseract processes only. Tesseract's own threads busy-wait, so
# several multi-threaded Tesseracts at once (our passes run in parallel) fight over
# the CPU and crawl; one thread each is much faster. This must NOT be set for our own
# process: the translator uses the same threading library and would be held to 1 thread.
_TESS_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1"}
# Windows: no console window flashing for every call, and below-normal priority so
# reading text never makes the translations lag behind while you scroll.
_NO_WINDOW = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0
# Elsewhere: run Tesseract through `nice` for the same reason, where available.
_NICE = ["nice", "-n", "10"] if sys.platform != "win32" and shutil.which("nice") else []


def _no_space(ch: str) -> bool:
    """Scripts written without spaces between words (Chinese, Japanese, Thai, ...)."""
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0x0E00 <= o <= 0x0EFF or 0x1000 <= o <= 0x109F or 0x1780 <= o <= 0x17FF or 0xFF00 <= o <= 0xFFEF)


def join_words(words: list[str]) -> str:
    """Join OCR words with spaces, except between characters of no-space scripts."""
    out = ""
    for w in words:
        if out and not (_no_space(out[-1]) and _no_space(w[0])):
            out += " "
        out += w
    return out


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
    # The text of each line, in the same order as ``lines``.
    line_texts: list[str] = field(default_factory=list)


def group_words(data: dict, min_conf: float = 40, scale: float = 1.0) -> list[TextBlock]:
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
        text_lines = [join_words([data["text"][i].strip() for i in sorted(line, key=lambda i: data["left"][i])])
                      for line in para_lines]
        # Hyphenated line breaks ("transla-" + "tion") are glued back together.
        text = ""
        for line in text_lines:
            if not text:
                text = line
            elif text.endswith("-") and len(text) > 1 and text[-2].isalpha():
                text = text[:-1] + line
            else:
                text = join_words([text, line])
        blocks.append(
            TextBlock(
                text=text,
                x=round(left / scale),
                y=round(top / scale),
                w=round((right - left) / scale),
                h=round((bottom - top) / scale),
                line_height=max(1, round(statistics.median(line_heights))),
                lines=line_boxes,
                line_texts=text_lines,
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
    """Grayscale and enlarge; Tesseract reads small UI text much better a bit bigger.

    1.5x reads typical 12-16px screen text as accurately as 2x and is ~20% faster.
    """
    gray = ImageOps.grayscale(image)
    # Light text on a dark background (dark mode, games) reads better inverted.
    if ImageStat.Stat(gray).mean[0] < 110:
        gray = ImageOps.invert(gray)
    scale = 1.5 if max(gray.size) < 2500 else 1.0
    if scale != 1.0:
        gray = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.Resampling.LANCZOS)
    return gray, scale


def run_tesseract(img: Image.Image, langs: str, psm: int, cmd: str | None = None) -> dict:
    """Run Tesseract on an image held in memory and return its word table.

    The image goes in through stdin and the result comes back through stdout:
    no temporary files, which saves time on every read.
    """
    buf = io.BytesIO()
    img.save(buf, format="PPM")  # uncompressed: the cheapest format to write and read
    try:
        proc = subprocess.run(
            [*_NICE, cmd or "tesseract", "stdin", "stdout", "-l", langs, "--psm", str(psm), "tsv"],
            input=buf.getvalue(), capture_output=True, env=_TESS_ENV, creationflags=_NO_WINDOW, timeout=60,
        )
    except FileNotFoundError as e:
        raise OcrError("Tesseract isn't installed or isn't on PATH (see README).") from e
    except subprocess.TimeoutExpired as e:
        raise OcrError("Tesseract took too long to read the screen.") from e
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        if "Failed loading language" in err or "Error opening data file" in err:
            raise OcrError(f"Tesseract language pack missing for '{langs}'.")
        raise OcrError(f"Tesseract failed: {err.strip()[:200]}")
    keys = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
            "left", "top", "width", "height", "conf", "text"]
    data: dict[str, list] = {k: [] for k in keys}
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    for row in lines[1:]:
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        for k, v in zip(keys[:11], cols[:11]):
            data[k].append(float(v) if k == "conf" else int(v))
        data["text"].append("\t".join(cols[11:]))
    return data


def extract_blocks(image: Image.Image, langs: str = "eng", min_conf: float = 40, tesseract_cmd: str | None = None) -> list[TextBlock]:
    prepared, scale = prepare_image(image)

    def read(img: Image.Image, psm: int) -> list[TextBlock]:
        return group_words(run_tesseract(img, langs, psm, tesseract_cmd), min_conf=min_conf, scale=scale)

    # The normal layout mode keeps paragraphs together, which translates best.
    passes = [(prepared, 3)]
    if max(image.size) >= 400:
        # Larger captures (whole screens) mix layouts and light/dark areas, which
        # one pass misses. Fill the gaps with sparse-text mode (isolated labels)
        # and an inverted pass (text the other polarity), keeping only new finds.
        passes += [(prepared, 11), (ImageOps.invert(prepared), 3)]
    # Each pass is a separate Tesseract process, so they run in parallel on separate cores.
    with ThreadPoolExecutor(max_workers=len(passes)) as pool:
        results = list(pool.map(lambda job: read(*job), passes))
    blocks = results[0]
    for extra in results[1:]:
        blocks += [b for b in extra if not any(_overlaps(b, o) for o in blocks)]
    blocks.sort(key=lambda b: (b.y, b.x))
    return blocks


def _overlaps(a: TextBlock, b: TextBlock) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


def split_block(block: TextBlock, keep) -> list[TextBlock]:
    """Split a paragraph where its lines switch between kept and not-kept text.

    Tesseract sometimes joins a username or an English label to the message next
    to it; those lines become blocks of their own so they can be left untouched.
    """
    if len(block.lines) <= 1 or len(block.line_texts) != len(block.lines):
        return [block]
    flags = [bool(keep(t)) for t in block.line_texts]
    if all(flags) or not any(flags):
        return [block]
    out, start = [], 0
    for i in range(1, len(flags) + 1):
        if i == len(flags) or flags[i] != flags[start]:
            boxes, texts = block.lines[start:i], block.line_texts[start:i]
            x0 = min(b[0] for b in boxes)
            y0 = min(b[1] for b in boxes)
            x1 = max(b[0] + b[2] for b in boxes)
            y1 = max(b[1] + b[3] for b in boxes)
            text = ""
            for t in texts:
                text = t if not text else (text[:-1] + t if text.endswith("-") else join_words([text, t]))
            out.append(TextBlock(text, x0, y0, x1 - x0, y1 - y0,
                                 max(1, round(statistics.median(b[3] for b in boxes))), list(boxes), list(texts)))
            start = i
    return out
