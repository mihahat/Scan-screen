"""The two windows: a region picker and the translation overlay."""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence, QPainter, QPen, QPixmap, QScreen
from PyQt6.QtWidgets import QWidget

from .ocr import TextBlock

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


@dataclass
class _Placed:
    rect: QRect
    font: QFont
    text: str


class TranslationOverlay(QWidget):
    """Draws translated text over where the original text was.

    Click or press Esc to dismiss; Ctrl+C copies the translation.
    """

    dismissed = pyqtSignal()
    PAD = 3

    def __init__(self, region: QRect, opacity: float = 0.96, font_family: str = ""):
        super().__init__(None, _OVERLAY_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.region = region
        self.bg = QColor(18, 18, 20, round(255 * max(0.3, min(opacity, 1.0))))
        self.font_family = font_family
        self.placed: list[_Placed] = []
        self.message: str | None = None
        self.setGeometry(region)

    # -- content -----------------------------------------------------------

    def show_message(self, text: str, auto_close_ms: int = 0) -> None:
        self.message = text
        self.placed = []
        self.setGeometry(self.region)
        self.update()
        self._show()
        if auto_close_ms:
            QTimer.singleShot(auto_close_ms, self.close)

    def show_translations(self, blocks: list[TextBlock], translations: list[str], dpr: float) -> None:
        self.message = None
        self.placed = []
        bottom = self.region.height()
        for block, text in zip(blocks, translations):
            rect = QRect(round(block.x / dpr), round(block.y / dpr), round(block.w / dpr), round(block.h / dpr))
            rect = rect.adjusted(-self.PAD, -self.PAD, self.PAD, self.PAD)
            font, rect = self._fit(text, rect, start_px=max(9, round(block.line_height / dpr * 0.8)))
            self.placed.append(_Placed(rect, font, text))
            bottom = max(bottom, rect.bottom() + 1)
        # Translations can be longer than the original; grow downwards if needed.
        self.setGeometry(self.region.x(), self.region.y(), self.region.width(), bottom)
        self.update()
        self._show()

    def translated_text(self) -> str:
        return "\n\n".join(p.text for p in self.placed)

    def _fit(self, text: str, rect: QRect, start_px: int) -> tuple[QFont, QRect]:
        """Largest font size (down to 9px) at which the text fits the box."""
        flags = int(Qt.TextFlag.TextWordWrap)
        inner = rect.adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        font = QFont(self.font_family) if self.font_family else QFont()
        for size in range(start_px, 8, -1):
            font.setPixelSize(size)
            br = QFontMetrics(font).boundingRect(inner, flags, text)
            if br.height() <= inner.height() and br.width() <= inner.width():
                return QFont(font), rect
        # Still too long at the minimum size: keep the width, make the box taller.
        font.setPixelSize(9)
        tall = QRect(inner.x(), inner.y(), max(inner.width(), 40), 100_000)
        br = QFontMetrics(font).boundingRect(tall, flags, text)
        return QFont(font), QRect(rect.x(), rect.y(), max(rect.width(), 40 + 2 * self.PAD), br.height() + 2 * self.PAD)

    # -- window plumbing -----------------------------------------------------

    def _show(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if self.message is not None:
            p.setPen(QPen(QColor(80, 170, 255), 2))
            p.setBrush(QColor(18, 18, 20, 70))
            p.drawRect(self.rect().adjusted(1, 1, -1, -1))
            f = QFont(self.font_family) if self.font_family else QFont()
            f.setPixelSize(14)
            fm = QFontMetrics(f)
            label = fm.boundingRect(self.message).adjusted(-10, -6, 10, 6)
            label.moveCenter(self.rect().center())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.bg)
            p.drawRoundedRect(label, 6, 6)
            p.setPen(QColor(240, 240, 240))
            p.setFont(f)
            p.drawText(label, Qt.AlignmentFlag.AlignCenter, self.message)
            return
        for item in self.placed:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.bg)
            p.drawRoundedRect(item.rect, 4, 4)
            p.setPen(QColor(245, 245, 245))
            p.setFont(item.font)
            p.drawText(item.rect.adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD),
                       int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), item.text)

    def mousePressEvent(self, _e) -> None:
        self.close()

    def keyPressEvent(self, e) -> None:
        if e.matches(QKeySequence.StandardKey.Copy):
            QGuiApplication.clipboard().setText(self.translated_text())
            self.show_message_flash("Copied")
        elif e.key() == Qt.Key.Key_Escape:
            self.close()

    def show_message_flash(self, text: str) -> None:
        """Briefly show a message without discarding the translations."""
        self.message = text
        self.update()
        QTimer.singleShot(600, self._clear_message)

    def _clear_message(self) -> None:
        self.message = None
        self.update()

    def closeEvent(self, e) -> None:
        self.dismissed.emit()
        super().closeEvent(e)
