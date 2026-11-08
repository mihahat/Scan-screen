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


def bundled_tesseract() -> str | None:
    """Tesseract shipped inside the packaged app (see packaging/), if any."""
    if not getattr(sys, "frozen", False):
        return None
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    exe = base / "tesseract" / ("tesseract.exe" if sys.platform == "win32" else "tesseract")
    if exe.exists():
        os.environ.setdefault("TESSDATA_PREFIX", str(base / "tesseract" / "tessdata"))
        return str(exe)
    return None


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    # Global shortcuts in pynput syntax, e.g. "<ctrl>+<alt>+t" or "<cmd>+<shift>+t".
    # hotkey translates the whole screen (press again to stop); region_hotkey lets you pick an area.
    hotkey: str = "<ctrl>+<alt>+t"
    region_hotkey: str = "<ctrl>+<alt>+r"
    # Tesseract language packs to read, joined with "+", e.g. "eng", "jpn", "eng+fra".
    ocr_languages: str = "eng"
    # Language codes for translation (ISO 639-1, e.g. "en", "de", "ja").
    # "auto": offline mode takes it from ocr_languages; online engines detect it.
    source_language: str = "auto"
    target_language: str = "en"
    # "local"  - offline, on this computer. Nothing is sent anywhere (default).
    # "google" - sends the recognised TEXT to Google Translate (free, unofficial endpoint).
    # "deepl"  - sends the recognised TEXT to DeepL (needs deepl_api_key or DEEPL_API_KEY).
    # Screenshots themselves never leave the computer with any engine.
    engine: str = "local"
    deepl_api_key: str = ""
    # Full path to the tesseract executable if it isn't on PATH.
    tesseract_cmd: str = ""
    # Words Tesseract is less confident about than this (0-100) are dropped.
    min_confidence: int = 40
    # Keep the whole-screen translation up to date as the screen changes.
    # Clicks then pass through to your apps; stop it with the hotkey or tray menu.
    live_mode: bool = True
    # How often live mode checks the screen for changes, in milliseconds.
    live_interval_ms: int = 300
    # Windows only: keep live translations out of screenshots and screen sharing.
    # Live mode then sees the real text underneath without lifting them; but
    # screenshots you take show the original text, not the translation.
    hide_from_screenshots: bool = True
    # Only where screenshots include the translations (e.g. macOS, Linux): how often
    # to lift them for a moment to check whether the text underneath changed.
    live_peek_ms: int = 10000
    # One-shot / region mode: let clicks pass through to the apps underneath
    # (dismiss with the hotkey instead of a click).
    click_through: bool = False
    # Font for translations; empty uses the system UI font.
    font_family: str = ""
    # Format version of this file; used to migrate old settings safely.
    config_version: int = 3

    def resolved_deepl_key(self) -> str:
        return os.environ.get("DEEPL_API_KEY") or self.deepl_api_key

    def resolved_tesseract_cmd(self) -> str | None:
        if self.tesseract_cmd:
            return self.tesseract_cmd
        bundled = bundled_tesseract()
        if bundled:
            return bundled
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
    migrated = False
    if data.get("config_version", 1) < 2:
        # Version 1 wrote the online Google engine as the default. Switch those
        # files to offline; anyone who wants an online engine can choose it again.
        if data.get("engine", "google") == "google":
            data["engine"] = "local"
        data["config_version"] = 2
        migrated = True
    if data.get("config_version", 1) < 3:
        # Version 2 saved slower live-mode defaults; move unchanged ones to the new defaults.
        if data.get("live_interval_ms") == 600:
            data["live_interval_ms"] = 300
        if data.get("live_peek_ms") == 3000:
            data["live_peek_ms"] = 10000
        data["config_version"] = 3
        migrated = True
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    if migrated:
        save_config(cfg, path)
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")
