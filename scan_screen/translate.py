"""Translation engines. Add a new one by subclassing Translator."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import requests

from .config import Config

TIMEOUT = 15


class TranslationError(RuntimeError):
    pass


class Translator(ABC):
    name = "base"

    @abstractmethod
    def translate_batch(self, texts: list[str], target: str, source: str = "auto") -> list[str]:
        """Translate each text; the result has the same length and order."""


class GoogleFreeTranslator(Translator):
    """Google Translate's public web endpoint. Free and keyless, but unofficial:
    it is rate limited and may change without notice."""

    name = "google"
    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    MAX_CHARS = 4500  # the endpoint rejects much longer queries

    def _one(self, text: str, target: str, source: str) -> str:
        for attempt in range(2):
            try:
                resp = self.session.post(
                    self.URL,
                    params={"client": "gtx", "sl": source, "tl": target, "dt": "t"},
                    data={"q": text},
                    timeout=TIMEOUT,
                )
                if resp.status_code == 429 and attempt == 0:
                    time.sleep(1.5)
                    continue
                if resp.status_code == 429:
                    raise TranslationError("Google Translate is rate limiting you; wait a moment or switch to DeepL.")
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.RequestException, ValueError) as e:
                raise TranslationError(f"Google Translate request failed: {e}") from e
        try:
            return "".join(seg[0] for seg in data[0] if seg and seg[0])
        except (TypeError, IndexError, KeyError) as e:
            raise TranslationError("Unexpected response from Google Translate") from e

    def translate_batch(self, texts: list[str], target: str, source: str = "auto") -> list[str]:
        """One request per chunk of paragraphs, one paragraph per line.

        Batching avoids the rate limit that parallel requests trip. If the
        line structure doesn't survive translation, fall back to one request
        per paragraph so text never lands on the wrong box.
        """
        results: list[str] = []
        for chunk in _chunks([t.replace("\n", " ") for t in texts], self.MAX_CHARS):
            lines = self._one("\n".join(chunk), target, source).split("\n")
            lines = [l.strip() for l in lines]
            if len(lines) == len(chunk):
                results.extend(lines)
            else:
                results.extend(self._one(t, target, source) for t in chunk)
        return results


def _chunks(texts: list[str], max_chars: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    size = 0
    for t in texts:
        if chunks and size + len(t) + 1 <= max_chars:
            chunks[-1].append(t)
            size += len(t) + 1
        else:
            chunks.append([t])
            size = len(t)
    return chunks


class DeepLTranslator(Translator):
    name = "deepl"

    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise TranslationError("DeepL needs an API key: set deepl_api_key in the config or DEEPL_API_KEY.")
        self.api_key = api_key
        self.session = session or requests.Session()
        # Free-plan keys end in ":fx" and use a different host.
        host = "api-free.deepl.com" if api_key.endswith(":fx") else "api.deepl.com"
        self.url = f"https://{host}/v2/translate"

    @staticmethod
    def _target_code(lang: str) -> str:
        # DeepL wants a regional variant for English and Portuguese targets.
        return {"en": "EN-US", "pt": "PT-BR"}.get(lang.lower(), lang.upper())

    def translate_batch(self, texts: list[str], target: str, source: str = "auto") -> list[str]:
        if not texts:
            return []
        body: dict = {"text": texts, "target_lang": self._target_code(target)}
        if source and source != "auto":
            body["source_lang"] = source.upper()
        try:
            resp = self.session.post(
                self.url,
                json=body,
                headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise TranslationError(f"DeepL request failed: {e}") from e
        if resp.status_code == 403:
            raise TranslationError("DeepL rejected the API key.")
        if resp.status_code == 456:
            raise TranslationError("DeepL quota used up for this month.")
        if not resp.ok:
            raise TranslationError(f"DeepL error {resp.status_code}: {resp.text[:200]}")
        return [t["text"] for t in resp.json()["translations"]]


def get_translator(cfg: Config) -> Translator:
    if cfg.engine == "google":
        return GoogleFreeTranslator()
    if cfg.engine == "deepl":
        return DeepLTranslator(cfg.resolved_deepl_key())
    raise TranslationError(f"Unknown engine {cfg.engine!r}; use 'google' or 'deepl'.")
