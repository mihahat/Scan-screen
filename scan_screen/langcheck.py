"""Is this piece of screen text in the language we're translating from?

Screens mix languages: a Russian chat still has English buttons, names and
links. Text that isn't in the source language is left exactly as it is, instead
of being pushed through the wrong translation model (which reworded English
into slightly different English).
"""

from __future__ import annotations

import re

# Unicode ranges of the scripts that identify a language on their own.
_RANGES = {
    "cyrillic": [(0x0400, 0x04FF), (0x0500, 0x052F)],
    "greek": [(0x0370, 0x03FF)],
    "arabic": [(0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "hebrew": [(0x0590, 0x05FF)],
    "devanagari": [(0x0900, 0x097F)],
    "bengali": [(0x0980, 0x09FF)],
    "thai": [(0x0E00, 0x0E7F)],
    "hangul": [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)],
    "japanese": [(0x3040, 0x30FF), (0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    "chinese": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)],
}
SCRIPT_OF = {
    "ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic", "sr": "cyrillic", "mk": "cyrillic", "be": "cyrillic",
    "kk": "cyrillic", "ky": "cyrillic", "mn": "cyrillic", "el": "greek", "ar": "arabic", "fa": "arabic",
    "ur": "arabic", "he": "hebrew", "hi": "devanagari", "mr": "devanagari", "ne": "devanagari", "bn": "bengali",
    "th": "thai", "ko": "hangul", "ja": "japanese", "zh": "chinese", "zt": "chinese",
}

# Very common English words, plus words that are usually UI labels in English.
_EN_WORDS = set("""
a about after all also an and any are as at be been but by can could did do does for from get got had has
have he her here him his how i if in into is it its just like me more most my new no not now of on one only
or other our out over said she so some than that the their them then there these they this those to too up
us was we were what when where which who why will with would you your yes ok okay please thanks thank
home login log sign settings search menu help about contact profile account logout cancel save delete edit
close open next back previous more less show hide view share send reply copy paste cut undo redo file new
upload download add remove submit continue start stop play pause skip load loading error warning info
online offline members member chat message messages channel channels server servers friends friend today
yesterday reading vocabulary level texts text trained premium basic order free pro video videos image images
""".split())
_WORD = re.compile(r"[A-Za-z']+")


def _in_script(ch: str, script: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _RANGES[script])


def script_share(text: str, script: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(_in_script(c, script) for c in letters) / len(letters)


def looks_english(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters or any(not c.isascii() for c in letters):
        return False  # accented or non-Latin letters: not English
    words = [w.lower().strip("'") for w in _WORD.findall(text)]
    words = [w for w in words if w]
    if not words:
        return False
    common = sum(w in _EN_WORDS for w in words)
    if len(words) >= 3:
        return common / len(words) >= 0.3
    return common >= 1


def is_source_text(text: str, source: str | None, target: str) -> bool:
    """Should this text be translated from ``source``? (None: we don't know; assume yes.)"""
    if not source or source == "auto":
        return True
    script = SCRIPT_OF.get(source)
    if script:
        # Mostly in the source language's own alphabet. (Mixed lines such as
        # Russian with an English name in them still count.)
        return script_share(text, script) >= 0.5
    # Latin-alphabet source (Spanish, French, German...): leave English text alone,
    # unless English is what we're translating from.
    if source != "en" and looks_english(text):
        return False
    # Text in a different alphabet altogether isn't in this language either.
    letters = [c for c in text if c.isalpha()]
    latin = sum(c.isascii() or 0x00C0 <= ord(c) <= 0x024F for c in letters)
    return bool(letters) and latin / len(letters) >= 0.5


def ocr_languages_for(ocr_languages: str) -> str:
    """Always read English as well: it's on nearly every screen (buttons, names, links),
    and reading it with the wrong language pack turns it into look-alike garbage."""
    langs = [lang for lang in ocr_languages.split("+") if lang]
    if "eng" not in langs:
        langs.append("eng")
    return "+".join(langs)
