"""Offline translation with Argos Translate / OPUS-MT models, run with CTranslate2.

Everything happens on this computer. The network is only used to download a
model package (by explicit request), and that request contains nothing but
the package name.

Packages are the public ``.argosmodel`` files: zip archives holding a
CTranslate2 model plus either a SentencePiece model or BPE codes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .translate import TranslationError, Translator

INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
ALLOWED_DOWNLOAD_HOSTS = {"raw.githubusercontent.com", "argos-net.com", "www.argos-net.com"}

# Tesseract language pack -> translation language code.
TESS_TO_ISO = {
    "eng": "en", "spa": "es", "fra": "fr", "deu": "de", "ita": "it", "por": "pt", "nld": "nl",
    "rus": "ru", "ukr": "uk", "pol": "pl", "ces": "cs", "slk": "sk", "ron": "ro", "hun": "hu",
    "bul": "bg", "ell": "el", "tur": "tr", "swe": "sv", "dan": "da", "nor": "nb", "fin": "fi",
    "est": "et", "lav": "lv", "lit": "lt", "slv": "sl", "cat": "ca", "eus": "eu", "glg": "gl",
    "ara": "ar", "heb": "he", "fas": "fa", "hin": "hi", "ben": "bn", "urd": "ur", "tha": "th",
    "vie": "vi", "ind": "id", "msa": "ms", "tgl": "tl", "jpn": "ja", "kor": "ko",
    "chi_sim": "zh", "chi_tra": "zt", "aze": "az", "sqi": "sq", "epo": "eo", "gle": "ga",
}
_NO_SPACE_TARGETS = {"ja", "zh", "zt", "th"}


def models_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "scan-screen" / "models"


def source_from_ocr(ocr_languages: str, target: str) -> str | None:
    """Pick the translation source language from the OCR languages setting."""
    for tess in ocr_languages.split("+"):
        iso = TESS_TO_ISO.get(tess)
        if iso and iso != target:
            return iso
    return None


# -- model packages ------------------------------------------------------------


@dataclass
class Package:
    from_code: str
    to_code: str
    path: Path


def installed_packages(root: Path | None = None) -> dict[tuple[str, str], Package]:
    root = root or models_dir()
    found: dict[tuple[str, str], Package] = {}
    if not root.exists():
        return found
    for meta in root.glob("*/metadata.json"):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            found[(m["from_code"], m["to_code"])] = Package(m["from_code"], m["to_code"], meta.parent)
        except (OSError, ValueError, KeyError):
            continue
    return found


def route(src: str, dst: str, available: set[tuple[str, str]]) -> list[tuple[str, str]] | None:
    """Direct pair, or via English (most models are X<->en)."""
    if (src, dst) in available:
        return [(src, dst)]
    if src != "en" and dst != "en" and (src, "en") in available and ("en", dst) in available:
        return [(src, "en"), ("en", dst)]
    return None


def fetch_index(get=None) -> list[dict]:
    import requests

    get = get or requests.get
    resp = get(INDEX_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def safe_extract(zip_path: Path, dest: Path) -> Path:
    """Extract a package, refusing anything that would land outside ``dest``."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest != target and dest not in target.parents:
                raise TranslationError(f"Refusing unsafe path in model package: {info.filename}")
            # Unix symlinks are stored with mode 0o120000 in the high bits.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise TranslationError(f"Refusing symlink in model package: {info.filename}")
        zf.extractall(dest)
    metas = list(dest.glob("*/metadata.json"))
    if len(metas) != 1:
        raise TranslationError("Model package has an unexpected layout.")
    return metas[0].parent


def install_pair(src: str, dst: str, progress: Callable[[int, int], None] | None = None,
                 root: Path | None = None) -> list[Package]:
    """Download what's needed to translate src -> dst (directly or via English)."""
    import requests
    from urllib.parse import urlparse

    root = root or models_dir()
    root.mkdir(parents=True, exist_ok=True)
    index = fetch_index()
    by_pair = {(p["from_code"], p["to_code"]): p for p in index}
    have = set(installed_packages(root))
    path = route(src, dst, set(by_pair) | have)
    if path is None:
        raise TranslationError(f"No offline model for {src} → {dst}.")
    added = []
    for pair in path:
        if pair in have:
            continue
        entry = by_pair[pair]
        url = entry["links"][0]
        u = urlparse(url)
        if u.scheme != "https" or u.hostname not in ALLOWED_DOWNLOAD_HOSTS:
            raise TranslationError(f"Refusing to download a model from {url}")
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            tmp = Path(tmp)
            archive = tmp / "pkg.zip"
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                done = 0
                with open(archive, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            pkg_dir = safe_extract(archive, tmp / "x")
            meta = json.loads((pkg_dir / "metadata.json").read_text(encoding="utf-8"))
            if (meta.get("from_code"), meta.get("to_code")) != pair:
                raise TranslationError("Downloaded model doesn't match the requested languages.")
            final = root / f"{pair[0]}_{pair[1]}"
            if final.exists():
                shutil.rmtree(final)
            pkg_dir.rename(final)
        added.append(Package(pair[0], pair[1], final))
    return added


def missing_download(src: str, dst: str, root: Path | None = None) -> bool:
    return route(src, dst, set(installed_packages(root))) is None


# -- translation -------------------------------------------------------------------


_DOUBLE_STOP = re.compile(r"(?<!\.)\.\.(?!\.)")
_TIME = re.compile(r"(\d): (\d\d)\b")
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")


class _Model:
    """One loaded language pair."""

    def __init__(self, pkg: Package):
        import ctranslate2

        self.src, self.dst = pkg.from_code, pkg.to_code
        threads = max(1, min(4, os.cpu_count() or 2))
        self.translator = ctranslate2.Translator(str(pkg.path / "model"), device="cpu",
                                                 compute_type="int8", inter_threads=1, intra_threads=threads)
        spm_path = pkg.path / "sentencepiece.model"
        if spm_path.exists():
            import sentencepiece

            self.sp = sentencepiece.SentencePieceProcessor(model_file=str(spm_path))
            self.bpe = None
        else:
            import codecs

            from sacremoses import MosesDetokenizer, MosesTokenizer
            from subword_nmt.apply_bpe import BPE

            self.sp = None
            with codecs.open(str(pkg.path / "bpe.model"), encoding="utf-8") as codes:
                self.bpe = BPE(codes)
            self.tok = MosesTokenizer(self.src)
            self.detok = MosesDetokenizer(self.dst)

    def _encode(self, text: str) -> list[str]:
        if self.sp:
            return self.sp.encode(text, out_type=str)
        return self.bpe.segment_tokens(self.tok.tokenize(text, escape=False))

    def _decode(self, tokens: list[str]) -> str:
        if self.sp:
            return self.sp.decode(tokens)
        joined = " ".join(tokens).replace("@@ ", "").replace("@@", "")
        text = self.detok.detokenize(joined.split())
        return _TIME.sub(r"\1:\2", text)  # the detokenizer turns "8:00" into "8: 00"

    def translate(self, texts: list[str]) -> list[str]:
        # Long paragraphs are split into sentences: the models are trained on sentences.
        pieces: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            for sentence in _SENTENCE_END.split(t.strip()) or [t]:
                if sentence:
                    pieces.append((i, sentence))
        if not pieces:
            return ["" for _ in texts]
        results = self.translator.translate_batch(
            [self._encode(s) for _, s in pieces], beam_size=2, max_decoding_length=256, max_batch_size=32)
        out: list[list[str]] = [[] for _ in texts]
        for (i, _), r in zip(pieces, results):
            out[i].append(self._decode(r.hypotheses[0]))
        sep = "" if self.dst in _NO_SPACE_TARGETS else " "
        # "a.m." at the end of a sentence comes back as "a.m.." - one full stop is enough.
        return [_DOUBLE_STOP.sub(".", sep.join(parts)) for parts in out]


class LocalTranslator(Translator):
    """Translates on this computer. Never touches the network."""

    name = "local"
    _cache: dict[tuple[str, str, str], _Model] = {}
    _lock = threading.Lock()

    def __init__(self, ocr_languages: str = "eng", root: Path | None = None):
        self.ocr_languages = ocr_languages
        self.root = root

    def resolve_source(self, source: str, target: str) -> str:
        if source and source != "auto":
            return source
        src = source_from_ocr(self.ocr_languages, target)
        if src is None:
            raise TranslationError("Offline mode can't auto-detect the language: choose the screen language "
                                   "in the tray menu (or set ocr_languages / source_language).")
        return src

    def _model(self, src: str, dst: str) -> _Model:
        key = (str(self.root), src, dst)
        with self._lock:
            if key not in self._cache:
                pkg = installed_packages(self.root)[(src, dst)]
                self._cache[key] = _Model(pkg)
            return self._cache[key]

    def preload(self, target: str, source: str = "auto") -> None:
        """Load the model(s) for this language pair ahead of time."""
        src = self.resolve_source(source, target)
        path = route(src, target, set(installed_packages(self.root)))
        for pair in path or []:
            self._model(*pair)

    def translate_batch(self, texts: list[str], target: str, source: str = "auto") -> list[str]:
        if not texts:
            return []
        src = self.resolve_source(source, target)
        if src == target:
            return list(texts)
        path = route(src, target, set(installed_packages(self.root)))
        if path is None:
            raise ModelMissing(src, target)
        for pair in path:
            texts = self._model(*pair).translate(texts)
        return texts


class ModelMissing(TranslationError):
    def __init__(self, src: str, dst: str):
        super().__init__(f"Offline model {src} → {dst} isn't installed yet.")
        self.src, self.dst = src, dst
