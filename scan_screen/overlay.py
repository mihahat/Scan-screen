"""Windows: the region picker, the in-place translation overlay and a status toast."""

from __future__ import annotations

import sys
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
        start_px = max(self.MIN_FONT_PX, round(b.line_height / dpr * em_per_box_height(b.text)))
        font, text_rect, overflows = self._fit(patch.translation, box, start_px, max(1, len(b.lines)))
        ink = QFontMetrics(font).boundingRect(text_rect, int(Qt.TextFlag.TextWordWrap | align), patch.translation)
        return _Placed(image, image_rect, text_rect, font, patch.translation,
                       QColor(*patch.text_color), QColor(*patch.bg_color), align, ink, overflows)

    def _base_font(self) -> QFont:
        return QFont(self.font_family) if self.font_family else QFont()

    def _fit(self, text: str, box: QRect, start_px: int, n_lines: int = 1) -> tuple[QFont, QRect, bool]:
        """Largest size that fits the original box; if none does, grow the box.

        Also reports whether the text overflows, i.e. wraps onto more lines than
        the original had (then the extra area needs a background).
        """
        flags = int(Qt.TextFlag.TextWordWrap)
        font = self._base_font()
        # Allow a little extra width so short UI labels don't wrap needlessly.
        roomy = box.adjusted(0, 0, max(4, box.width() // 10), 0)
        for size in range(start_px, self.MIN_FONT_PX - 1, -1):
            font.setPixelSize(size)
            br = QFontMetrics(font).boundingRect(roomy, flags, text)
            if br.width() <= roomy.width() and br.height() <= max(box.height(), size * 1.3):
                lines = round(br.height() / QFontMetrics(font).lineSpacing())
                return QFont(font), roomy, lines > n_lines
        font.setPixelSize(self.MIN_FONT_PX)
        tall = QRect(box.x(), box.y(), roomy.width(), 100_000)
        br = QFontMetrics(font).boundingRect(tall, flags, text)
        return QFont(font), QRect(box.x(), box.y(), roomy.width(), br.height()), True

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
        for item in self.placed:
            # A translation longer than the original spills past the cleaned patch;
            # give the spill a background (the patch is drawn over it right after).
            if item.overflows:
                p.fillRect(QRectF(item.ink_rect.adjusted(-2, -2, 2, 2)).translated(0, item.offset), item.fill)
        for item in self.placed:
            p.drawImage(item.image_rect.translated(0, item.offset), item.image)
        for item in self.placed:
            p.setPen(item.color)
            p.setFont(item.font)
            p.drawText(QRectF(item.text_rect).translated(0, item.offset), int(Qt.TextFlag.TextWordWrap | item.align), item.text)
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
