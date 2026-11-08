import os
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from scan_screen import ocr
from scan_screen.langcheck import is_source_text, looks_english, ocr_languages_for


@pytest.mark.parametrize("text, expected", [
    ("Пытаемся поиграть с другом", True),
    ("А спейсвар есть?", True),
    ("Мой брат играет в Minecraft", True),   # Russian with an English name in it
    ("Mark As Read", False),
    ("Chopperkuni69", False),
    ("Level A2 17 texts", False),
    ("My dog, my best friend.", False),
])
def test_russian_screen_only_translates_russian(text, expected):
    assert is_source_text(text, "ru", "en") is expected


@pytest.mark.parametrize("text, expected", [
    ("El museo abre a las diez.", True),
    ("Mañana lloverá por la tarde.", True),
    ("Inicio", True),
    ("Login", False),
    ("Settings", False),
    ("This is what I do during the week.", False),
    ("Пытаемся поиграть", False),
])
def test_spanish_screen_leaves_english_and_other_scripts(text, expected):
    assert is_source_text(text, "es", "en") is expected


def test_unknown_source_translates_everything_and_english_detection():
    assert is_source_text("Hello there", None, "en")
    assert looks_english("Not trained")
    assert not looks_english("Mañana")
    assert ocr_languages_for("rus") == "rus+eng"
    assert ocr_languages_for("eng") == "eng"


def test_split_block_separates_names_from_messages():
    block = ocr.TextBlock("Пытаемся поиграть. Asenaris", 10, 10, 300, 60, 20,
                          lines=[(10, 10, 300, 22), (10, 40, 90, 22)],
                          line_texts=["Пытаемся поиграть.", "Asenaris"])
    parts = ocr.split_block(block, lambda t: is_source_text(t, "ru", "en"))
    assert [p.text for p in parts] == ["Пытаемся поиграть.", "Asenaris"]
    assert (parts[1].x, parts[1].y, parts[1].w, parts[1].h) == (10, 40, 90, 22)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_tesseract_runs_in_memory_and_keeps_thread_limit_to_itself():
    img = Image.new("RGB", (600, 120), "white")
    ImageDraw.Draw(img).text((20, 40), "Reading in memory", fill="black", font=ImageFont.load_default(size=32))
    data = ocr.run_tesseract(img, "eng", 7)
    assert "Reading" in " ".join(data["text"])
    assert os.environ.get("OMP_THREAD_LIMIT") is None   # the translator keeps all its threads
    assert ocr._TESS_ENV["OMP_THREAD_LIMIT"] == "1"
    with pytest.raises(ocr.OcrError, match="language pack"):
        ocr.run_tesseract(img, "xyz", 7)
