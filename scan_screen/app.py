"""Application wiring: tray icon, global hotkey, capture → OCR → translate → overlay."""

from __future__ import annotations

import argparse
import sys
import traceback

from PIL import Image
from PyQt6.QtCore import Qt, QObject, QRect, QRunnable, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCursor, QDesktopServices, QFont, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import __version__
from .config import Config, config_path, load_config
from .ocr import TextBlock, extract_blocks
from .overlay import RegionSelector, TranslationOverlay
from .translate import TranslationError, get_translator


def qimage_to_pil(img: QImage) -> Image.Image:
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(ptr), "raw", "RGBA", img.bytesPerLine(), 1).convert("RGB")


class _JobSignals(QObject):
    done = pyqtSignal(list, list)
    failed = pyqtSignal(str)


class TranslateJob(QRunnable):
    """OCR + translation, run off the UI thread."""

    def __init__(self, image: Image.Image, cfg: Config):
        super().__init__()
        self.image = image
        self.cfg = cfg
        self.signals = _JobSignals()

    def run(self) -> None:
        try:
            blocks = extract_blocks(self.image, self.cfg.ocr_languages, self.cfg.min_confidence, self.cfg.resolved_tesseract_cmd())
            if not blocks:
                self.signals.done.emit([], [])
                return
            translator = get_translator(self.cfg)
            translations = translator.translate_batch([b.text for b in blocks], self.cfg.target_language, self.cfg.source_language)
            self.signals.done.emit(blocks, translations)
        except TranslationError as e:
            self.signals.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - shown to the user instead of crashing the tray app
            name = type(e).__name__
            if name == "TesseractNotFoundError":
                msg = "Tesseract isn't installed or isn't on PATH (see README)."
            elif name == "TesseractError" and "Failed loading language" in str(e):
                msg = f"Tesseract language pack missing for '{self.cfg.ocr_languages}'."
            else:
                traceback.print_exc()
                msg = f"{name}: {e}"
            self.signals.failed.emit(msg)


class _HotkeyBridge(QObject):
    # pynput calls back on its own thread; a signal hops back to the Qt thread.
    pressed = pyqtSignal()


class ScanScreen(QObject):
    def __init__(self, app: QApplication, cfg: Config, once: bool = False):
        super().__init__()
        self.app = app
        self.cfg = cfg
        self.once = once
        self.pool = QThreadPool.globalInstance()
        self.selector: RegionSelector | None = None
        self.overlay: TranslationOverlay | None = None
        self.last_region: QRect | None = None
        self.busy = False
        self.tray: QSystemTrayIcon | None = None
        self.hotkey_listener = None
        self._jobs: list[TranslateJob] = []

    # -- setup ---------------------------------------------------------------

    def start(self) -> None:
        if self.once:
            QTimer.singleShot(0, self.translate_region)
            return
        self._setup_tray()
        self._setup_hotkey()
        if self.tray is None and self.hotkey_listener is None:
            print("No system tray and no global hotkey available. Bind your OS shortcut to "
                  "`scan-screen --once` instead (see README).", file=sys.stderr)
            self.app.quit()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(_make_icon(), self.app)
        tray.setToolTip(f"Scan-screen — {self.cfg.hotkey} to translate")
        menu = QMenu()
        for label, slot in [
            (f"Translate region  ({self.cfg.hotkey})", self.translate_region),
            ("Translate last region again", self.translate_last),
            (None, None),
            ("Open config file", self.open_config),
            ("Quit", self.app.quit),
        ]:
            if label is None:
                menu.addSeparator()
                continue
            act = QAction(label, menu)
            act.triggered.connect(slot)
            menu.addAction(act)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger and self.translate_region())
        tray.show()
        self._menu = menu  # keep a reference
        self.tray = tray

    def _setup_hotkey(self) -> None:
        try:
            from pynput import keyboard

            self._bridge = _HotkeyBridge()
            self._bridge.pressed.connect(self.translate_region)
            listener = keyboard.GlobalHotKeys({self.cfg.hotkey: self._bridge.pressed.emit})
            listener.daemon = True
            listener.start()
            self.hotkey_listener = listener
            print(f"Scan-screen {__version__} running. Press {self.cfg.hotkey} to translate a region.")
        except Exception as e:  # noqa: BLE001 - e.g. Wayland, no X display, bad hotkey string
            print(f"Global hotkey unavailable ({type(e).__name__}: {e}). Use the tray icon or `scan-screen --once`.",
                  file=sys.stderr)

    def open_config(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_path())))

    # -- flow ----------------------------------------------------------------

    def _close_overlay(self) -> None:
        if self.overlay is not None:
            self.overlay.dismissed.disconnect()
            self.overlay.close()
            self.overlay = None

    def translate_region(self) -> None:
        if self.busy:
            return
        self.busy = True
        self._close_overlay()
        # Give the compositor a moment to remove our own windows before capturing.
        QTimer.singleShot(120, self._open_selector)

    def _open_selector(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        shot = screen.grabWindow(0)
        if shot.isNull() or shot.width() == 0:
            self.busy = False
            self._notify("Couldn't capture the screen. On macOS allow Screen Recording; on Wayland see README.")
            return
        self.selector = RegionSelector(screen, shot)
        self.selector.selected.connect(self._on_selected)
        self.selector.cancelled.connect(self._on_cancelled)
        self.selector.start()

    def translate_last(self) -> None:
        if self.last_region is None:
            self.translate_region()
            return
        if self.busy:
            return
        self.busy = True
        self._close_overlay()
        QTimer.singleShot(120, self._capture_last)

    def _capture_last(self) -> None:
        r = self.last_region
        screen = QGuiApplication.screenAt(r.center()) or QGuiApplication.primaryScreen()
        local = r.translated(-screen.geometry().topLeft())
        shot = screen.grabWindow(0)
        dpr = shot.devicePixelRatio()
        crop = shot.copy(QRect(round(local.x() * dpr), round(local.y() * dpr), round(local.width() * dpr), round(local.height() * dpr)))
        crop.setDevicePixelRatio(dpr)
        self._on_selected(r, crop)

    def _on_cancelled(self) -> None:
        self.selector = None
        self.busy = False
        if self.once:
            self.app.quit()

    def _on_selected(self, region: QRect, crop: QPixmap) -> None:
        self.selector = None
        self.last_region = region
        overlay = TranslationOverlay(region, self.cfg.overlay_opacity, self.cfg.font_family)
        overlay.dismissed.connect(self._on_dismissed)
        self.overlay = overlay
        overlay.show_message("Translating…")

        job = TranslateJob(qimage_to_pil(crop.toImage()), self.cfg)
        dpr = crop.devicePixelRatio()
        job.signals.done.connect(lambda blocks, texts: self._on_done(job, overlay, blocks, texts, dpr))
        job.signals.failed.connect(lambda msg: self._on_failed(job, overlay, msg))
        # Keep a Python reference until the job reports back, so its signals stay alive.
        self._jobs.append(job)
        job.setAutoDelete(False)
        self.pool.start(job)

    def _on_done(self, job: TranslateJob, overlay: TranslationOverlay, blocks: list[TextBlock], texts: list[str], dpr: float) -> None:
        self._finish_job(job)
        if overlay is not self.overlay:
            return  # dismissed while we were working
        if not blocks:
            overlay.show_message("No text found", auto_close_ms=1500)
        else:
            overlay.show_translations(blocks, texts, dpr)

    def _on_failed(self, job: TranslateJob, overlay: TranslationOverlay, msg: str) -> None:
        self._finish_job(job)
        print(f"Error: {msg}", file=sys.stderr)
        if overlay is self.overlay:
            overlay.show_message(msg, auto_close_ms=4000)

    def _finish_job(self, job: TranslateJob) -> None:
        self.busy = False
        if job in self._jobs:
            self._jobs.remove(job)

    def _on_dismissed(self) -> None:
        self.overlay = None
        self.busy = False
        if self.once:
            self.app.quit()

    def _notify(self, msg: str) -> None:
        print(msg, file=sys.stderr)
        if self.tray is not None:
            self.tray.showMessage("Scan-screen", msg)
        elif self.once:
            self.app.quit()


def _make_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(40, 120, 230))
    p.setPen(QColor(0, 0, 0, 0))
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setPen(QColor(255, 255, 255))
    f = QFont()
    f.setPixelSize(34)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "Aa")
    p.end()
    return QIcon(pix)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scan-screen", description="Translate text anywhere on your screen.")
    parser.add_argument("--once", action="store_true", help="select one region now, translate it, then exit "
                        "(bind this to an OS keyboard shortcut if the built-in hotkey doesn't work)")
    parser.add_argument("--to", metavar="LANG", help="target language for this run, e.g. de, ja, es")
    parser.add_argument("--ocr", metavar="LANGS", help="Tesseract languages for this run, e.g. jpn or eng+fra")
    parser.add_argument("--engine", choices=["google", "deepl"], help="translation engine for this run")
    parser.add_argument("--config-path", action="store_true", help="print the config file path and exit")
    parser.add_argument("--version", action="version", version=f"scan-screen {__version__}")
    args = parser.parse_args(argv)

    if args.config_path:
        load_config()  # creates it on first run
        print(config_path())
        return 0

    cfg = load_config()
    if args.to:
        cfg.target_language = args.to
    if args.ocr:
        cfg.ocr_languages = args.ocr
    if args.engine:
        cfg.engine = args.engine

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Scan-screen")
    app.setQuitOnLastWindowClosed(False)
    controller = ScanScreen(app, cfg, once=args.once)
    controller.start()
    return app.exec()
