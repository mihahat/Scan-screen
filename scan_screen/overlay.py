"""Windows: the region picker, the in-place translation overlay and a status toast."""

from __future__ import annotations

import statistics
import sys
import time
from dataclasses import dataclass

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QImage, QKeySequence, QPainter, QPen, QPixmap, QScreen
from PyQt6.QtWidgets import QWidget

def exclude_from_capture(widget: QWidget, exclude: bool = True) -> bool:
    """Hide a window from (or show it in) screenshots and screen sharing (Windows 10 2004+).

    Hidden, live mode can watch the screen underneath the translations, but
    screenshots you take show the original text. Returns False where the OS
    doesn't support it.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
        mode = WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), mode))
    except Exception:  # noqa: BLE001
        return False


MOTION_DAMPING = 0.85   # predict a little less than the measured speed
MOTION_TIMEOUT_S = 0.2  # stop predicting if no new measurement arrives within this time

_OVERLAY_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
)


class RegionSelector(QWidget):
    """Full-screen frozen screenshot; drag a rectangle to choose what to translate."""

    # Selected rectangle in global logical coordinates, and the cropped full-resolution image.
    selected = pyqtSignal(QRect, QPixmap)
    cancelled = pyqtSignal()

    def __init__(self, screen: QScreen, shot: QPixmap):
        super().__init__(None, _OVERLAY_FLAGS)
        self.screen_geo = screen.geometry()
        self.shot = shot
        self.origin: QPoint | None = None
        self.current: QPoint | None = None
        self.setGeometry(self.screen_geo)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def start(self) -> None:
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _rect(self) -> QRect:
        if self.origin is None or self.current is None:
            return QRect()
        return QRect(self.origin, self.current).normalized()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.shot)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        r = self._rect()
        if not r.isEmpty():
            # Show the selected area undimmed.
            dpr = self.shot.devicePixelRatio()
            src = QRect(round(r.x() * dpr), round(r.y() * dpr), round(r.width() * dpr), round(r.height() * dpr))
            p.drawPixmap(r, self.shot, src)
            p.setPen(QPen(QColor(80, 170, 255), 2))
            p.drawRect(r)
        else:
            p.setPen(QColor(255, 255, 255))
            f = p.font()
            f.setPointSize(14)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, 40, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "Drag around the text to translate  ·  Esc to cancel")

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.origin = self.current = e.position().toPoint()
            self.update()
        elif e.button() == Qt.MouseButton.RightButton:
            self._cancel()

    def mouseMoveEvent(self, e) -> None:
        if self.origin is not None:
            self.current = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton or self.origin is None:
            return
        self.current = e.position().toPoint()
        r = self._rect()
        if r.width() < 8 or r.height() < 8:
            self.origin = self.current = None
            self.update()
            return
        dpr = self.shot.devicePixelRatio()
        crop = self.shot.copy(QRect(round(r.x() * dpr), round(r.y() * dpr), round(r.width() * dpr), round(r.height() * dpr)))
        crop.setDevicePixelRatio(dpr)
        self.close()
        self.selected.emit(r.translated(self.screen_geo.topLeft()), crop)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self._cancel()

    def _cancel(self) -> None:
        self.close()
        self.cancelled.emit()



_DESCENDERS = set("gjpqyQ,;()[]{}|@$")


def em_per_box_height(text: str) -> float:
    """Font size relative to the height of Tesseract's box around a line.

    The box only spans the ink: about 1 em when letters hang below the
    baseline (g, p, y), but only the cap height (~0.73 em) when none do.
    """
    return 1.0 if any(ch in _DESCENDERS for ch in text) else 1.3


def pil_to_qimage(img) -> QImage:
    img = img.convert("RGB")
    data = img.tobytes()
    return QImage(data, img.width, img.height, img.width * 3, QImage.Format.Format_RGB888).copy()


@dataclass
class _Placed:
    image: QImage
    image_rect: QRectF   # where the cleaned background goes (logical px)
    text_rect: QRect     # where the translation is written
    font: QFont
    text: str
    color: QColor
    fill: QColor
    align: Qt.AlignmentFlag
    ink_rect: QRect      # area the drawn text actually covers
    overflows: bool      # text needed more room than the original had
    offset: float = 0.0  # moved down by this much since layout (scrolling); kept exact, not rounded
    patch: object = None  # the render.Patch it was made from (original text position, translation)
    dpr: float = 1.0


class ReplaceOverlay(QWidget):
    """Covers the captured area and shows it with the text swapped for its translation.

    Only the patches are drawn; everywhere else the window is transparent, so
    the real screen shows through. Click or Esc dismisses (unless the overlay is
    click-through, then use the hotkey again); Ctrl+C copies the translation.
    """

    dismissed = pyqtSignal()
    MIN_FONT_PX = 8

    def __init__(self, area: QRect, click_through: bool = False, font_family: str = ""):
        flags = _OVERLAY_FLAGS
        if click_through:
            flags |= Qt.WindowType.WindowTransparentForInput
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setGeometry(area)
        self.click_through = click_through
        self.font_family = font_family
        self.placed: list[_Placed] = []
        self.flash: str | None = None
        self._sizes: list[int] = []  # font sizes in use, so similar text gets identical sizes
        # Areas that are scrolling right now: (x0, x1, y0, y1, speed px/s, measured at, max ahead).
        # Between checks of the screen, translations there glide on at that speed (redrawn
        # ~60 times a second) instead of jumping once per check.
        self._motions: list[tuple] = []
        self._anim = QTimer(self)
        self._anim.setInterval(15)
        self._anim.timeout.connect(self._animate)

    def set_patches(self, patches, dpr: float) -> None:
        self.placed = [self._layout(p, dpr) for p in patches]
        self.update()

    def add_patches(self, patches, dpr: float) -> list[QRectF]:
        """Add translations; returns the areas that now look different."""
        new = [self._layout(p, dpr) for p in patches]
        self.placed += new
        self.update()
        return [self.item_rect(i) for i in new]

    def remove_where(self, predicate) -> list[QRectF]:
        """Remove translations for which predicate(area) is true; returns their areas."""
        keep, gone = [], []
        for item in self.placed:
            (gone if predicate(self.item_rect(item)) else keep).append(item)
        if gone:
            self.placed = keep
            self.update()
        return [self.item_rect(i) for i in gone]

    @staticmethod
    def phys_rect(item: _Placed) -> tuple[int, int, int, int]:
        """Where the original text under this translation is now, in physical pixels (x, y, w, h)."""
        p = item.patch
        return p.x, p.y + round(item.offset * item.dpr), p.image.width, p.image.height

    def replace_item(self, old: _Placed, patch, dpr: float) -> tuple[QRectF, QRectF]:
        """Redraw one translation from a new patch (e.g. in a link's hover colours)."""
        new = self._layout(patch, dpr)
        before = self.item_rect(old)
        self.placed = [new if i is old else i for i in self.placed]
        self.update()
        return before, self.item_rect(new)

    def take_all(self) -> list[_Placed]:
        """Remove every translation but hand them back (see restore)."""
        items, self.placed = self.placed, []
        self.update()
        return items

    def restore(self, items: list[_Placed]) -> list[QRectF]:
        self.placed += items
        self.update()
        return [self.item_rect(i) for i in items]

    @staticmethod
    def item_rect(item: _Placed) -> QRectF:
        r = QRectF(item.image_rect)
        if item.overflows:
            r = r.united(QRectF(item.ink_rect.adjusted(-2, -2, 2, 2)))
        return r.translated(0, item.offset)

    def set_motion(self, x0: float, x1: float, y0: float, y1: float, speed: float, measured_at: float,
                   max_ahead: float) -> None:
        """Translations in this area are moving at ``speed`` logical px per second."""
        self._motions = [m for m in self._motions if not (abs(m[0] - x0) < 60 and abs(m[1] - x1) < 60)]
        self._motions.append((x0, x1, y0, y1, speed, measured_at, max_ahead))
        if not self._anim.isActive():
            self._anim.start()

    def stop_motion(self) -> None:
        if self._motions:
            self._motions = []
            self.update()

    def _ahead(self, r: QRectF) -> float:
        """How far ahead of its last measured position a translation should be drawn right now."""
        if not self._motions:
            return 0.0
        cx, cy = r.center().x(), r.center().y()
        now = time.monotonic()
        for x0, x1, y0, y1, speed, t0, limit in self._motions:
            if x0 <= cx <= x1 and y0 - limit <= cy <= y1 + limit:
                # Damped a little, so it rarely runs ahead of the real text.
                ahead = speed * (now - t0) * MOTION_DAMPING
                return max(-limit, min(limit, ahead))
        return 0.0

    def _animate(self) -> None:
        now = time.monotonic()
        self._motions = [m for m in self._motions if now - m[5] < MOTION_TIMEOUT_S]
        if not self._motions:
            self._anim.stop()
        self.update()

    def shift_where(self, predicate, dy: float) -> tuple[list[QRectF], list[QRectF], list[_Placed]]:
        """Move matching translations down by dy logical px (negative = up).

        Returns their areas before and after (so callers can ignore the redraw) and the items.
        """
        before, after, items = [], [], []
        for item in self.placed:
            r = self.item_rect(item)
            if predicate(r):
                before.append(r)
                item.offset += dy
                after.append(self.item_rect(item))
                items.append(item)
        if before:
            self.update()
        return before, after, items

    def remove_items(self, items: list[_Placed]) -> list[QRectF]:
        gone = [i for i in self.placed if any(i is x for x in items)]
        if gone:
            self.placed = [i for i in self.placed if not any(i is x for x in gone)]
            self.update()
        return [self.item_rect(i) for i in gone]

    def replace_where(self, predicate, patches, dpr: float) -> tuple[list[QRectF], list[QRectF]]:
        """Swap translations for new ones in a single repaint, so nothing flashes.

        Removes translations for which predicate(area) is true, or that overlap a new one.
        """
        new = [self._layout(p, dpr) for p in patches]
        new_rects = [self.item_rect(i) for i in new]
        gone = [i for i in self.placed
                if predicate(self.item_rect(i)) or any(self.item_rect(i).intersects(r) for r in new_rects)]
        removed = [self.item_rect(i) for i in gone]
        self.placed = [i for i in self.placed if not any(i is g for g in gone)] + new
        self.update()
        return removed, new_rects

    def _layout(self, patch, dpr: float) -> _Placed:
        b = patch.block
        image = pil_to_qimage(patch.image)
        image_rect = QRectF(patch.x / dpr, patch.y / dpr, image.width() / dpr, image.height() / dpr)
        box = QRect(round(b.x / dpr), round(b.y / dpr), max(1, round(b.w / dpr)), max(1, round(b.h / dpr)))
        multi_line = len(b.lines) > 1
        align = Qt.AlignmentFlag.AlignLeft | (Qt.AlignmentFlag.AlignTop if multi_line else Qt.AlignmentFlag.AlignVCenter)
        size = self._matched_size(b, dpr)
        font, text_rect, overflows = self._fit(patch.translation, box, size, max(1, len(b.lines)))
        ink = QFontMetrics(font).boundingRect(text_rect, int(Qt.TextFlag.TextWordWrap | align), patch.translation)
        item = _Placed(image, image_rect, text_rect, font, patch.translation,
                       QColor(*patch.text_color), QColor(*patch.bg_color), align, ink, overflows)
        item.patch = patch
        item.dpr = dpr
        return item

    def _base_font(self) -> QFont:
        return QFont(self.font_family) if self.font_family else QFont()

    def _matched_size(self, b, dpr: float) -> int:
        """Font size (px) that makes our font look as big as the original text.

        Measured from the original's width: lay its own words out in our font and
        compare with how wide they were on screen. Box heights alone vary with the
        letters a line happens to contain (g, y, p hang lower), which made sizes jump.
        """
        guess = b.line_height / dpr * em_per_box_height(b.text)
        probe = self._base_font()
        probe.setPixelSize(100)
        fm = QFontMetrics(probe)
        ratios = []
        for (_, _, lw, _), text in zip(b.lines, b.line_texts or []):
            advance = fm.horizontalAdvance(text)
            if advance > 0 and len(text.strip()) >= 4:
                ratios.append(lw / dpr / advance * 100)
        size = statistics.median(ratios) if ratios else guess
        # Don't trust a width that disagrees wildly with the height (misread text).
        size = min(max(size, guess * 0.7), guess * 1.4)
        return self._snap(size)

    def _snap(self, size: float) -> int:
        """Text that was the same size on screen gets exactly the same size here."""
        for known in self._sizes:
            if abs(size - known) <= known * 0.08:
                return known
        px = max(self.MIN_FONT_PX, round(size))
        self._sizes.append(px)
        return px

    def _fit(self, text: str, box: QRect, size: int, n_lines: int = 1) -> tuple[QFont, QRect, bool]:
        """Lay the translation out at the original's size, shrinking at most a little.

        A longer translation first gets more room (sideways for a one-line label,
        wrapping for a paragraph), and only then a slightly smaller font, never below
        85% of the original. Also reports whether it now covers more than the
        original did (that extra area then needs a background).
        """
        flags = int(Qt.TextFlag.TextWordWrap)
        font = self._base_font()
        if n_lines == 1:
            # One-line labels may run on to the right rather than shrink.
            room = min(round(box.width() * 1.6), max(box.width(), self.width() - box.x() - 4))
        else:
            room = round(box.width() * 1.1)
        room = max(room, box.width() + 4)
        smallest = max(self.MIN_FONT_PX, round(size * 0.85))
        br = None
        for px in sorted({size, round(size * 0.92), smallest}, reverse=True):
            if px < smallest:
                continue
            font.setPixelSize(px)
            fm = QFontMetrics(font)
            br = fm.boundingRect(QRect(box.x(), box.y(), room, 100_000), flags, text)
            lines = max(1, round(br.height() / fm.lineSpacing()))
            if lines <= n_lines:
                rect = QRect(box.x(), box.y(), max(box.width(), br.width() + 2), max(box.height(), br.height()))
                return QFont(font), rect, br.width() > box.width() * 1.05 + 2
        # Still too long at the smallest size: keep that size and let it wrap onto extra lines.
        return QFont(font), QRect(box.x(), box.y(), room, br.height()), True

    def translated_text(self) -> str:
        return "\n\n".join(p.text for p in self.placed)

    def covered_rects(self) -> list[QRectF]:
        """Everything this overlay paints on (logical px, relative to the overlay)."""
        rects = [item.image_rect for item in self.placed]
        rects += [QRectF(item.ink_rect.adjusted(-2, -2, 2, 2)) for item in self.placed if item.overflows]
        return rects

    def show_over_screen(self) -> None:
        self.show()
        self.raise_()
        if not self.click_through:
            self.activateWindow()
            self.setFocus()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Where each translation goes right now: its measured position, plus how far it
        # has glided on since then if its area is scrolling.
        shift = [item.offset + self._ahead(self.item_rect(item)) for item in self.placed]
        for item, dy in zip(self.placed, shift):
            # A translation longer than the original spills past the cleaned patch;
            # give the spill a background (the patch is drawn over it right after).
            if item.overflows:
                p.fillRect(QRectF(item.ink_rect.adjusted(-2, -2, 2, 2)).translated(0, dy), item.fill)
        for item, dy in zip(self.placed, shift):
            p.drawImage(item.image_rect.translated(0, dy), item.image)
        for item, dy in zip(self.placed, shift):
            p.setPen(item.color)
            p.setFont(item.font)
            p.drawText(QRectF(item.text_rect).translated(0, dy), int(Qt.TextFlag.TextWordWrap | item.align), item.text)
        if self.flash:
            f = self._base_font()
            f.setPixelSize(14)
            label = QFontMetrics(f).boundingRect(self.flash).adjusted(-10, -6, 10, 6)
            label.moveCenter(self.rect().center())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(18, 18, 20, 235))
            p.drawRoundedRect(label, 6, 6)
            p.setPen(QColor(240, 240, 240))
            p.setFont(f)
            p.drawText(label, Qt.AlignmentFlag.AlignCenter, self.flash)

    def mousePressEvent(self, _e) -> None:
        self.close()

    def keyPressEvent(self, e) -> None:
        if e.matches(QKeySequence.StandardKey.Copy):
            QGuiApplication.clipboard().setText(self.translated_text())
            self.flash = "Copied"
            self.update()
            QTimer.singleShot(600, self._clear_flash)
        elif e.key() == Qt.Key.Key_Escape:
            self.close()

    def _clear_flash(self) -> None:
        self.flash = None
        self.update()

    def closeEvent(self, e) -> None:
        self.dismissed.emit()
        super().closeEvent(e)


class StatusToast(QWidget):
    """Small message pill at the top of a screen ("Translating…", errors)."""

    def __init__(self, screen_geo: QRect, text: str, auto_close_ms: int = 0):
        super().__init__(None, _OVERLAY_FLAGS | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.text = text
        f = QFont()
        f.setPixelSize(14)
        self.setFont(f)
        w = min(screen_geo.width() - 40, QFontMetrics(f).horizontalAdvance(text) + 32)
        self.setGeometry(screen_geo.x() + (screen_geo.width() - w) // 2, screen_geo.y() + 24, w, 34)
        self.show()
        if auto_close_ms:
            QTimer.singleShot(auto_close_ms, self.close)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(18, 18, 20, 230))
        p.drawRoundedRect(self.rect(), 17, 17)
        p.setPen(QColor(240, 240, 240))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text)
