import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtCore")
from PIL import Image  # noqa: E402
from PyQt6.QtCore import QRect  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from scan_screen.ocr import TextBlock  # noqa: E402
from scan_screen.overlay import ReplaceOverlay, StatusToast  # noqa: E402
from scan_screen.render import Patch  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _patch(text, block):
    img = Image.new("RGB", (block.w + 8, block.h + 8), (255, 255, 255))
    return Patch(block.x - 4, block.y - 4, img, block, text, (10, 10, 10), (255, 255, 255))


def test_overlay_places_patches_and_text(app):
    ov = ReplaceOverlay(QRect(0, 0, 400, 300))
    short = TextBlock("Hola", 20, 20, 120, 40, 40, [(20, 20, 120, 40)])
    tight = TextBlock("Adiós", 20, 200, 60, 20, 20, [(20, 200, 60, 20)])
    long_text = "This translation is much, much longer than the short original text it replaces " * 3
    ov.set_patches([_patch("Hello", short), _patch(long_text, tight)], dpr=2.0)
    assert len(ov.placed) == 2
    first = ov.placed[0]
    assert first.image_rect.x() == 8 and first.image_rect.width() == 64  # physical px / dpr 2
    assert first.text_rect.x() == 10 and first.font.pixelSize() == 26   # 40px box / dpr 2 * 1.3 (no descenders)
    # The long translation can't fit the tiny original box: it gets a taller one.
    assert ov.placed[1].text_rect.height() > 10
    assert "Hello" in ov.translated_text()
    ov.grab()  # paints without error
    ov.close()


def test_click_through_overlay_and_toast(app):
    ov = ReplaceOverlay(QRect(0, 0, 200, 60), click_through=True)
    ov.set_patches([], dpr=1.0)
    ov.grab()
    ov.close()
    toast = StatusToast(QRect(0, 0, 800, 600), "Translating…")
    assert toast.width() < 800
    toast.grab()
    toast.close()
