import os
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from scan_screen.langdetect import _pick_in_group, _pick_latin, detect_language, installed_packs

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")

UI = ["Home", "Settings", "Mark As Read", "Log in", "Search", "Members — 3,215", "Not trained", "PDF",
      "General", "Voice chat", "Friends", "Direct Messages", "Online — 12", "Help", "Rules"]


_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _screen(lines, dark=False):
    # A font with Cyrillic letters (Pillow's built-in one has none).
    if not os.path.exists(_DEJAVU):
        pytest.skip("DejaVu Sans font not available")
    font = ImageFont.truetype(_DEJAVU, 18)
    img = Image.new("RGB", (1920, 1080), (49, 51, 56) if dark else (250, 250, 250))
    d = ImageDraw.Draw(img)
    fg = (215, 215, 215) if dark else (25, 25, 25)
    for i, t in enumerate(UI):
        d.text((30, 30 + i * 40), t, fill=fg, font=font)
    for i, t in enumerate(lines):
        d.text((400, 60 + i * 44), t, fill=fg, font=font)
    return img


def _need(*packs):
    missing = [p for p in packs if p not in installed_packs()]
    if missing:
        pytest.skip(f"tesseract packs not installed: {missing}")


def test_detects_russian_among_english_ui():
    _need("rus")
    ru = ["Пытаемся поиграть с другом, оба скачали фикс, но ничего нету.", "А спейсвар есть?",
          "Все решил, выключил защитник виндовс и переустановил фикс.", "Мой домашний питомец"]
    assert detect_language(_screen(ru, dark=True)) == ("rus", "ru")


def test_detects_spanish_and_ignores_english():
    _need("spa")
    es = ["El museo abre a las diez de la mañana.", "¿Dónde está la estación de tren?",
          "Me gusta leer libros en el parque.", "La tienda cierra pronto, pero no hay problema."]
    assert detect_language(_screen(es)) == ("spa", "es")
    en = ["This is what I do during the week.", "My room is small, but I've got everything I need."]
    assert detect_language(_screen(en)) is None
    assert detect_language(_screen(en + ["Привет"])) is None  # one stray word isn't a language


def test_picking_within_an_alphabet():
    assert _pick_in_group("cyrillic", ["rus", "ukr"], "Привіт, як справи? Їжак і є ґанок") == "ukr"
    assert _pick_in_group("cyrillic", ["rus", "ukr"], "Привет, как дела?") == "rus"
    assert _pick_latin("el perro y la casa de los niños", ["spa", "por", "ita"])[0] == "spa"
    assert _pick_latin("o cão e a casa não é", ["spa", "por", "ita"])[0] == "por"
