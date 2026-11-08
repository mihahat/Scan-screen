"""Work out, once, which language the text on screen is in.

Used when "Text on screen is in" is set to Auto-detect. It runs once when you
start translating, never on every update:

1. The screenshot is read with each installed alphabet (Cyrillic, Latin,
   Chinese/Japanese, Korean, Arabic, ...) in parallel, and the alphabet that
   yields the most confidently-read letters wins.
2. Within that alphabet the language is picked from letters only it uses
   (Ukrainian і/ї/є, Spanish ñ, Polish ł, ...) and from its most common words.

English is never chosen as the language to translate from: it's on almost
every screen (buttons, names, links) and the point is the other language.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from PIL import Image, ImageOps

from .langcheck import SCRIPT_OF, script_share
from .ocr import _NO_WINDOW, _TESS_ENV, OcrError, run_tesseract
from .translate_local import TESS_TO_ISO

# Tesseract packs grouped by alphabet. The first one is the default for that alphabet.
SCRIPT_GROUPS = {
    "cyrillic": ["rus", "ukr", "bul"],
    "japanese": ["jpn"],
    "chinese": ["chi_sim", "chi_tra"],
    "hangul": ["kor"],
    "arabic": ["ara", "fas"],
    "hebrew": ["heb"],
    "devanagari": ["hin"],
    "thai": ["tha"],
    "greek": ["ell"],
}
_NON_LATIN = {p for packs in SCRIPT_GROUPS.values() for p in packs}

# A few of the most frequent words, and letters only (or mostly) this language uses.
_LATIN_HINTS = {
    "spa": ("el la los las de que y en un una por con para es no se del al lo como más pero su", "ñ¿¡"),
    "fra": ("le la les de des et en un une est que qui pour pas dans sur au du ce il elle ne avec", "çœèêëàâîïôûù"),
    "deu": ("der die das und ist nicht ein eine zu den mit von sich auf für dem des im auch es wir", "ßäöü"),
    "ita": ("il la di che e un una per non sono del della in con le gli è anche come ma più", "ìòàèé"),
    "por": ("o a os as de que e do da em um uma para com não no na por se mais", "ãõçâêôá"),
    "nld": ("de het een en van is dat niet ik je op te met voor zijn er maar ook", "ĳ"),
    "pol": ("i w nie na się z że do to jest jak co ale o po tak", "ąćęłńóśźż"),
    "tur": ("ve bir bu da de için ile ne çok daha gibi ama var mı olarak", "ığşİ"),
    "swe": ("och att det som en är på för med inte jag har av till den", "åäö"),
    "dan": ("og at det er en til på for med ikke jeg som har af den", "æøå"),
    "nor": ("og at det er en til på for med ikke jeg som har av den", "æøå"),
    "fin": ("ja on ei se että hän oli kun mutta tai joka niin myös", "äö"),
    "ces": ("a je se na že v to s z do jak ale by co", "ěščřžůý"),
    "ron": ("și în de la cu nu pe un o care este din mai", "ăâîșțş"),
    "hun": ("a az és hogy nem is egy van meg de már csak", "őűáéíóú"),
    "vie": ("và của là có không được cho người này những một", "ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệ"),
    "ind": ("dan yang di ini itu dengan untuk tidak ada dari ke akan juga", ""),
    "cat": ("el la els les de i que en un una per amb no és del", "çàèòï·"),
}
_LATIN_WORDS = {k: set(v[0].split()) for k, v in _LATIN_HINTS.items()}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


@lru_cache(maxsize=4)
def installed_packs(tesseract_cmd: str | None = None) -> tuple[str, ...]:
    """Tesseract language packs installed on this computer."""
    try:
        proc = subprocess.run([tesseract_cmd or "tesseract", "--list-langs"], capture_output=True,
                              env=_TESS_ENV, creationflags=_NO_WINDOW, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    out = proc.stdout.decode("utf-8", "replace").splitlines()
    return tuple(line.strip() for line in out[1:] if line.strip())


def _words(data: dict, min_conf: float = 55) -> list[str]:
    return [t.strip() for t, c in zip(data["text"], data["conf"]) if t.strip() and c >= min_conf]


def _pick_in_group(script: str, packs: list[str], text: str) -> str:
    """Which language of an alphabet, from letters it alone uses."""
    if script == "cyrillic":
        if "ukr" in packs and sum(text.count(c) for c in "іїєґІЇЄҐ") >= 3:
            return "ukr"
        if "bul" in packs and text.count("ъ") >= 3 and not any(c in text for c in "ыэЫЭ"):
            return "bul"
        if "rus" in packs:
            return "rus"
    if script == "chinese" and "chi_tra" in packs and "chi_sim" not in packs:
        return "chi_tra"
    return packs[0]


def _pick_latin(text: str, packs: list[str]) -> tuple[str | None, int]:
    words = [w.lower() for w in _WORD.findall(text)]
    counts = Counter(words)
    best, best_score = None, 0
    for pack in packs:
        if pack not in _LATIN_HINTS:
            continue
        score = sum(counts[w] for w in _LATIN_WORDS[pack])
        score += 2 * sum(text.lower().count(ch) for ch in _LATIN_HINTS[pack][1])
        if score > best_score:
            best, best_score = pack, score
    return best, best_score


def detect_language(image: Image.Image, tesseract_cmd: str | None = None) -> tuple[str, str] | None:
    """(Tesseract pack, translation code) of the main non-English language on screen, or None."""
    packs = [p for p in installed_packs(tesseract_cmd) if p in TESS_TO_ISO and p != "eng"]
    if not packs:
        return None
    gray = ImageOps.grayscale(image)
    # Scale down big screens: detection only needs enough text, not every word.
    if gray.width > 1600:
        gray = gray.resize((1600, round(gray.height * 1600 / gray.width)))
    jobs = []
    for script, group in SCRIPT_GROUPS.items():
        have = [p for p in group if p in packs]
        if have:
            # With English alongside: a pack on its own reads Latin letters as look-alikes
            # from its own alphabet ("Home" in Cyrillic), which would count as that language.
            jobs.append((script, have, "+".join(have + ["eng"])))
    latin = [p for p in packs if p not in _NON_LATIN]
    if latin:
        # English too, so English words are read as English and don't count for anything else.
        jobs.append(("latin", latin, "+".join(latin[:8] + ["eng"])))

    def read(job):
        script, have, langs = job
        passes = []
        # Light text on dark backgrounds reads better inverted: read both ways.
        for img in (gray, ImageOps.invert(gray)):
            try:
                passes.append(_words(run_tesseract(img, langs, 11, tesseract_cmd)))
            except OcrError:
                passes.append([])
        return script, have, passes

    with ThreadPoolExecutor(max_workers=min(3, len(jobs))) as pool:
        results = list(pool.map(read, jobs))

    # 1) Another alphabet wins if a real share of the words on screen are written in it.
    #    (A few stray look-alikes, like "Home" read as Cyrillic "Ноте", don't count.)
    best = None
    for script, have, passes in results:
        if script == "latin":
            continue
        for words in passes:
            own = [w for w in words if script_share(w, script) >= 0.6]
            share = len(own) / max(1, len(words))
            if len(own) >= 5 and share >= 0.15 and (best is None or len(own) > best[0]):
                best = (len(own), script, have, " ".join(own))
    if best:
        _, script, have, text = best
        pack = _pick_in_group(script, have, text)
        return pack, TESS_TO_ISO[pack]
    # 2) Otherwise a Latin-alphabet language, by its common words and letters.
    for script, have, passes in results:
        if script == "latin":
            scored = [_pick_latin(" ".join(words), have) for words in passes]
            pack, score = max(scored, key=lambda r: r[1])
            if pack and score >= 4:
                return pack, TESS_TO_ISO[pack]
    return None


def language_name(pack: str) -> str:
    names = {"rus": "Russian", "ukr": "Ukrainian", "bul": "Bulgarian", "jpn": "Japanese", "chi_sim": "Chinese",
             "chi_tra": "Chinese (Traditional)", "kor": "Korean", "ara": "Arabic", "fas": "Persian", "heb": "Hebrew",
             "hin": "Hindi", "tha": "Thai", "ell": "Greek", "spa": "Spanish", "fra": "French", "deu": "German",
             "ita": "Italian", "por": "Portuguese", "nld": "Dutch", "pol": "Polish", "tur": "Turkish",
             "swe": "Swedish", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "ces": "Czech",
             "ron": "Romanian", "hun": "Hungarian", "vie": "Vietnamese", "ind": "Indonesian", "cat": "Catalan"}
    return names.get(pack, pack)


assert all(SCRIPT_OF.get(TESS_TO_ISO[p]) for g in ("cyrillic", "hangul", "arabic") for p in SCRIPT_GROUPS[g]
           if p in TESS_TO_ISO and p != "fas")
