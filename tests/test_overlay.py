import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PyQt6.QtCore")
from PyQt6.QtCore import QRect  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from scan_screen.ocr import TextBlock  # noqa: E402
from scan_screen.overlay import TranslationOverlay  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_overlay_places_and_grows(app):
    region = QRect(100, 100, 300, 80)
    ov = TranslationOverlay(region)
    blocks = [TextBlock("Hola", 0, 0, 120, 40, 40), TextBlock("Adiós", 0, 80, 120, 40, 40)]
    long_text = "This translation is much, much longer than the short original text it replaces " * 3
    ov.show_translations(blocks, ["Hello", long_text], dpr=2.0)
    assert len(ov.placed) == 2
    short = ov.placed[0]
    assert short.rect.width() == 60 + 2 * ov.PAD  # 120px physical / dpr 2, plus padding
    # The long translation needs more room than the original box: the window grows downwards.
    assert ov.height() > region.height()
    assert "Hello" in ov.translated_text()
    ov.grab()  # paints without error
    ov.close()


def test_overlay_message(app):
    ov = TranslationOverlay(QRect(0, 0, 200, 60))
    ov.show_message("No text found")
    ov.grab()
    ov.close()
