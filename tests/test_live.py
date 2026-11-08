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


def _thumb(spots=()):
    t = np.full((240, 320), 230, np.uint8)
    for y, x in spots:
        t[y:y + 12, x:x + 30] = 20
    return t


def test_tracker_marks_only_changed_tiles_and_waits_for_quiet():
    tr = live.ChangeTracker()
    assert tr.update(_thumb()) is None  # first frame is the baseline
    changed = tr.update(_thumb([(100, 200)]))
    assert changed.any()
    rows, cols = np.nonzero(changed)
    assert set(rows) <= {6, 7} and set(cols) <= {12, 13, 14}
    assert not tr.ready().any()          # still changing right now
    tr.update(_thumb([(100, 200)]))
    assert (tr.ready() == changed).all()  # stopped changing: ready to re-read
    tr.clear(tr.ready())
    assert not tr.dirty.any()


def test_tracker_ignores_our_own_drawing():
    tr = live.ChangeTracker()
    tr.update(_thumb())
    drawn = np.zeros(tr.shape, dtype=bool)
    drawn[6:8, 12:15] = True
    tr.ignore_tiles(drawn)
    assert not tr.update(_thumb([(100, 200)])).any()   # our overlay appearing: ignored
    assert not tr.update(_thumb([(100, 200)])).any()
    assert tr.update(_thumb()).any()                     # later real changes count again


def test_an_animation_elsewhere_does_not_touch_other_tiles():
    tr = live.ChangeTracker()
    tr.update(_thumb())
    for i in range(6):
        changed = tr.update(_thumb([(220, 290)] if i % 2 == 0 else []))
        assert not changed[:10].any()  # the top of the screen is never affected
    assert live.band_rows(tr.dirty) == (12, 14)


def test_rects_to_tiles_and_band(app):
    shape = (15, 20)
    mask = live.rects_to_tiles([QRectF(100, 100, 50, 20)], shape, dpr=1.0)
    assert mask[2, 2] and mask[2, 3] and mask.sum() == 2  # 48px tiles
    assert live.band_rows(mask, extra=1) == (1, 3)
