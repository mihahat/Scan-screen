"""User configuration, stored as JSON in the platform's config directory."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "scan-screen"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    # Global shortcuts in pynput syntax, e.g. "<ctrl>+<alt>+t" or "<cmd>+<shift>+t".
    # hotkey translates the whole screen (press again to hide); region_hotkey lets you pick an area.
    hotkey: str = "<ctrl>+<alt>+t"
    region_hotkey: str = "<ctrl>+<alt>+r"
    # Tesseract language packs to read, joined with "+", e.g. "eng", "jpn", "eng+fra".
    ocr_languages: str = "eng"
    # Language codes for translation (ISO 639-1, e.g. "en", "de", "ja"). "auto" detects.
    source_language: str = "auto"
    target_language: str = "en"
    # "google" (free, no key) or "deepl" (needs deepl_api_key or DEEPL_API_KEY env var).
    engine: str = "google"
    deepl_api_key: str = ""
    # Full path to the tesseract executable if it isn't on PATH.
    tesseract_cmd: str = ""
    # Words Tesseract is less confident about than this (0-100) are dropped.
    min_confidence: int = 40
    # Let mouse clicks pass through the translated overlay to the apps underneath.
    # Then dismiss it with the hotkey instead of a click.
    click_through: bool = False
    # Font for translations; empty uses the system UI font.
    font_family: str = ""

    def resolved_deepl_key(self) -> str:
        return os.environ.get("DEEPL_API_KEY") or self.deepl_api_key

    def resolved_tesseract_cmd(self) -> str | None:
        if self.tesseract_cmd:
            return self.tesseract_cmd
        if shutil.which("tesseract"):
            return None  # pytesseract finds it on PATH
        candidates = []
        if sys.platform == "win32":
            candidates = [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
        elif sys.platform == "darwin":
            candidates = ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"]
        for c in candidates:
            if Path(c).exists():
                return c
        return None


def load_config(path: Path | None = None) -> Config:
    """Load the config, creating a default file on first run.

    Unknown keys are ignored so old config files keep working.
    """
    path = path or config_path()
    if not path.exists():
        cfg = Config()
        save_config(cfg, path)
        return cfg
    data = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: Config, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")
