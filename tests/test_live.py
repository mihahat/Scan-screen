import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtCore")
from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from scan_screen import live  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _screen(text_x=None, caret=False):
    pix = QPixmap(900, 600)
    pix.fill(QColor(250, 250, 250))
    p = QPainter(pix)
    p.setPen(QColor(0, 0, 0))
    if text_x is not None:
        f = p.font()
        f.setPixelSize(20)
        p.setFont(f)
        p.drawText(text_x, 300, "Hola mundo")
    if caret:
        p.fillRect(600, 100, 1, 18, QColor(0, 0, 0))
    p.end()
    return pix


def test_detects_text_changes_but_not_a_blinking_caret(app):
    base = live.thumbnail(_screen(100))
    assert base.shape == (200, 300)
    assert not live.changed(base, live.thumbnail(_screen(100)))
    assert not live.changed(base, live.thumbnail(_screen(100, caret=True)))
    assert live.changed(base, live.thumbnail(_screen(400)))      # text moved (scroll)
    assert live.changed(base, live.thumbnail(_screen(None)))     # text disappeared
    assert live.changed(None, base)


def test_mask_hides_changes_under_the_overlay(app):
    a = live.thumbnail(_screen(100))
    b = live.thumbnail(_screen(None))
    mask = live.overlay_mask(a.shape, [QRectF(90, 270, 200, 50)], logical_width=900)
    assert mask.dtype == bool and mask.any()
    assert not live.changed(a, b, mask)
    assert live.changed(a, b, np.zeros_like(mask))
