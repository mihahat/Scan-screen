"""Application wiring: tray icon, global hotkeys, capture → OCR → translate → overlay."""

from __future__ import annotations

import argparse
import sys
import threading
import traceback

from PIL import Image
from PyQt6.QtCore import QObject, QRect, QRunnable, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QCursor, QDesktopServices, QFont, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PyQt6 import sip
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import __version__, live, privacy
from .config import Config, config_path, load_config, save_config
from .ocr import extract_blocks
from .overlay import RegionSelector, ReplaceOverlay, StatusToast, exclude_from_capture
from .render import make_patches, needs_translation
from .translate import ONLINE_ENGINES, CachingTranslator, Translator, TranslationError, get_translator

# Shown in the tray menus: (name, Tesseract pack, translation code).
LANGUAGES = [
    ("English", "eng", "en"), ("Spanish", "spa", "es"), ("French", "fra", "fr"), ("German", "deu", "de"),
    ("Italian", "ita", "it"), ("Portuguese", "por", "pt"), ("Dutch", "nld", "nl"), ("Polish", "pol", "pl"),
    ("Russian", "rus", "ru"), ("Ukrainian", "ukr", "uk"), ("Turkish", "tur", "tr"), ("Arabic", "ara", "ar"),
    ("Hindi", "hin", "hi"), ("Japanese", "jpn", "ja"), ("Korean", "kor", "ko"), ("Chinese (Simplified)", "chi_sim", "zh"),
]
ENGINE_LABELS = {"local": "Offline — private, nothing leaves this computer",
                 "google": "Google Translate — online, sends text",
                 "deepl": "DeepL — online, sends text"}


def qimage_to_pil(img: QImage) -> Image.Image:
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(ptr), "raw", "RGBA", img.bytesPerLine(), 1).convert("RGB")


class _JobSignals(QObject):
    done = pyqtSignal(list)  # list[render.Patch]
    failed = pyqtSignal(object)  # Exception


class TranslateJob(QRunnable):
    """OCR, translation and text removal, run off the UI thread."""

    def __init__(self, image: Image.Image, cfg: Config, translator: Translator):
        super().__init__()
        self.image = image
        self.cfg = cfg
        self.translator = translator
        self.signals = _JobSignals()

    def run(self) -> None:
        try:
            blocks = extract_blocks(self.image, self.cfg.ocr_languages, self.cfg.min_confidence, self.cfg.resolved_tesseract_cmd())
            blocks = [b for b in blocks if needs_translation(b.text)]
            if not blocks:
                self.signals.done.emit([])
                return
            translations = self.translator.translate_batch([b.text for b in blocks], self.cfg.target_language, self.cfg.source_language)
            self.signals.done.emit(make_patches(self.image, blocks, translations))
        except TranslationError as e:
            self.signals.failed.emit(e)
        except Exception as e:  # noqa: BLE001 - shown to the user instead of crashing the tray app
            name = type(e).__name__
            if name == "TesseractNotFoundError":
                e = TranslationError("Tesseract isn't installed or isn't on PATH (see README).")
            elif name == "TesseractError" and "Failed loading language" in str(e):
                e = TranslationError(f"Tesseract language pack missing for '{self.cfg.ocr_languages}'.")
            else:
                traceback.print_exc()
            self.signals.failed.emit(e)
        finally:
            self.image = None  # drop the screenshot as soon as we're done with it


class _DownloadSignals(QObject):
    progress = pyqtSignal(int, int)
    done = pyqtSignal()
    failed = pyqtSignal(str)


class DownloadJob(QRunnable):
    """Downloads an offline model. The only network access offline mode allows."""

    def __init__(self, src: str, dst: str):
        super().__init__()
        self.src, self.dst = src, dst
        self.signals = _DownloadSignals()

    def run(self) -> None:
        from .translate_local import install_pair

        try:
            with privacy.allow_network():
                install_pair(self.src, self.dst, progress=self.signals.progress.emit)
            self.signals.done.emit()
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(f"Model download failed: {e}")


class _HotkeyBridge(QObject):
    # pynput calls back on its own thread; signals hop back to the Qt thread.
    screen = pyqtSignal()
    region = pyqtSignal()


class ScanScreen(QObject):
    def __init__(self, app: QApplication, cfg: Config, once: str | None = None):
        super().__init__()
        self.app = app
        self.cfg = cfg
        self.once = once  # None, "screen" or "region"
        self.pool = QThreadPool.globalInstance()
        self.selector: RegionSelector | None = None
        self.overlay: ReplaceOverlay | None = None
        self.toast: StatusToast | None = None
        self.last_region: QRect | None = None
        self.busy = False
        self.tray: QSystemTrayIcon | None = None
        self.hotkey_listener = None
        self._jobs: list[QRunnable] = []
        self._translator: Translator | None = None
        self._translator_lock = threading.Lock()
        self._pending_retry = None
        # Live mode
        self.live = False
        self.live_state = "idle"  # settling -> translating -> showing -> settling ...
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._live_tick)
        self.live_screen = None
        self.ref_thumb = None
        self.prev_thumb = None
        self.stable_ticks = 0
        self.apply_privacy()

    # -- privacy ---------------------------------------------------------------

    def apply_privacy(self) -> None:
        if self.cfg.engine in ONLINE_ENGINES:
            privacy.disable()
        else:
            privacy.enforce_offline()

    def translator(self) -> Translator:
        with self._translator_lock:
            if self._translator is None:
                self._translator = CachingTranslator(get_translator(self.cfg))
            return self._translator

    def _settings_changed(self) -> None:
        save_config(self.cfg)
        with self._translator_lock:
            self._translator = None  # rebuild with the new settings (and an empty cache)
        self.apply_privacy()
        self._update_tray()
        if self.live:
            self._live_restart()

    # -- setup -------------------------------------------------------------------

    def start(self) -> None:
        if self.once:
            QTimer.singleShot(0, self.translate_screen if self.once == "screen" else self.translate_region)
            return
        self._setup_tray()
        self._setup_hotkeys()
        if self.tray is None and self.hotkey_listener is None:
            print("No system tray and no global hotkey available. Bind your OS shortcut to "
                  "`scan-screen --once` instead (see README).", file=sys.stderr)
            self.app.quit()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(_make_icon(), self.app)
        menu = QMenu()
        self._act_screen = menu.addAction("", self.translate_screen)
        menu.addAction(f"Translate region  ({self.cfg.region_hotkey})", self.translate_region)
        menu.addAction("Translate last region again", self.translate_last)
        menu.addSeparator()

        self._act_live = menu.addAction("Keep updating as the screen changes")
        self._act_live.setCheckable(True)
        self._act_live.toggled.connect(self._set_live_mode)

        src_menu = menu.addMenu("Screen language")
        dst_menu = menu.addMenu("Translate into")
        self._src_group = QActionGroup(src_menu)
        self._dst_group = QActionGroup(dst_menu)
        for name, tess, iso in LANGUAGES:
            a = src_menu.addAction(name)
            a.setCheckable(True)
            a.setData(tess)
            self._src_group.addAction(a)
            b = dst_menu.addAction(name)
            b.setCheckable(True)
            b.setData(iso)
            self._dst_group.addAction(b)
        self._src_group.triggered.connect(lambda a: self._set_source(a.data()))
        self._dst_group.triggered.connect(lambda a: self._set_target(a.data()))

        engine_menu = menu.addMenu("Translation engine")
        self._engine_group = QActionGroup(engine_menu)
        for key, label in ENGINE_LABELS.items():
            a = engine_menu.addAction(label)
            a.setCheckable(True)
            a.setData(key)
            self._engine_group.addAction(a)
        self._engine_group.triggered.connect(lambda a: self._set_engine(a.data()))

        menu.addSeparator()
        menu.addAction("Open config file", self.open_config)
        menu.addAction("Quit", self.app.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger and self.translate_screen())
        self._menu = menu  # keep a reference
        self.tray = tray
        self._update_tray()
        tray.show()

    def _update_tray(self) -> None:
        if self.tray is None:
            return
        label = "Stop translating" if self.live else "Translate screen"
        self._act_screen.setText(f"{label}  ({self.cfg.hotkey})")
        self._act_live.blockSignals(True)
        self._act_live.setChecked(self.cfg.live_mode)
        self._act_live.blockSignals(False)
        for group, value in [(self._src_group, self.cfg.ocr_languages.split("+")[0]),
                             (self._dst_group, self.cfg.target_language),
                             (self._engine_group, self.cfg.engine)]:
            for a in group.actions():
                a.setChecked(a.data() == value)
        mode = "offline (private)" if self.cfg.engine == "local" else f"ONLINE: text is sent to {self.cfg.engine}"
        self.tray.setToolTip(f"Scan-screen — {self.cfg.hotkey} to translate — {mode}")

    def _set_live_mode(self, on: bool) -> None:
        self.cfg.live_mode = on
        if not on and self.live:
            self.stop_live()
        self._settings_changed()

    def _set_source(self, tess: str) -> None:
        self.cfg.ocr_languages = tess
        self.cfg.source_language = "auto"
        self._settings_changed()

    def _set_target(self, iso: str) -> None:
        self.cfg.target_language = iso
        self._settings_changed()

    def _set_engine(self, engine: str) -> None:
        if engine in ONLINE_ENGINES and self.cfg.engine not in ONLINE_ENGINES:
            answer = QMessageBox.question(
                None, "Use an online translator?",
                f"With {engine.capitalize()}, the text recognised on your screen is sent to "
                f"{engine.capitalize()}'s servers to be translated. Screenshots are never sent.\n\n"
                "Offline mode keeps everything on this computer. Switch to the online engine anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                self._update_tray()
                return
        if engine == "deepl" and not self.cfg.resolved_deepl_key():
            self._notify("Add your DeepL API key to the config file first (deepl_api_key).")
            self._update_tray()
            return
        self.cfg.engine = engine
        self._settings_changed()

    def _setup_hotkeys(self) -> None:
        try:
            from pynput import keyboard

            self._bridge = _HotkeyBridge()
            self._bridge.screen.connect(self.translate_screen)
            self._bridge.region.connect(self.translate_region)
            keys = {self.cfg.hotkey: self._bridge.screen.emit}
            if self.cfg.region_hotkey:
                keys[self.cfg.region_hotkey] = self._bridge.region.emit
            listener = keyboard.GlobalHotKeys(keys)
            listener.daemon = True
            listener.start()
            self.hotkey_listener = listener
            mode = "offline" if self.cfg.engine == "local" else f"online ({self.cfg.engine})"
            print(f"Scan-screen {__version__} running, {mode}. {self.cfg.hotkey}: translate screen, "
                  f"{self.cfg.region_hotkey}: translate a region.")
        except Exception as e:  # noqa: BLE001 - e.g. Wayland, no X display, bad hotkey string
            print(f"Global hotkey unavailable ({type(e).__name__}: {e}). Use the tray icon or `scan-screen --once`.",
                  file=sys.stderr)

    def open_config(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_path())))

    # -- one-shot flow ---------------------------------------------------------------

    def _close_overlay(self) -> bool:
        """Close the current overlay; True if there was one."""
        if self.overlay is None:
            return False
        self.overlay.dismissed.disconnect()
        self.overlay.close()
        self.overlay = None
        return True

    def _toast(self, screen_geo: QRect, text: str, auto_close_ms: int = 0) -> None:
        self._clear_toast()
        self.toast = StatusToast(screen_geo, text, auto_close_ms)

    def _clear_toast(self) -> None:
        # Toasts that time out delete themselves; only close one that still exists.
        if self.toast is not None and not sip.isdeleted(self.toast):
            self.toast.close()
        self.toast = None

    def translate_screen(self) -> None:
        """Translate the screen under the mouse; pressing it again stops/hides it."""
        if self.live:
            self.stop_live()
            return
        if self.busy:
            return
        if self._close_overlay():
            return  # toggle off a one-shot translation
        if self.cfg.live_mode and not self.once:
            self.start_live()
            return
        self.busy = True
        # Give the compositor a moment to remove our own windows before capturing.
        QTimer.singleShot(120, self._capture_screen)

    def _capture_screen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        shot = screen.grabWindow(0)
        if not self._check_shot(shot):
            return
        self._run(screen.geometry(), shot, screen.geometry())

    def translate_region(self) -> None:
        if self.busy:
            return
        if self.live:
            self.stop_live()
        self.busy = True
        self._close_overlay()
        QTimer.singleShot(120, self._open_selector)

    def _open_selector(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        shot = screen.grabWindow(0)
        if not self._check_shot(shot):
            return
        self.selector = RegionSelector(screen, shot)
        geo = screen.geometry()
        self.selector.selected.connect(lambda region, crop: self._on_selected(region, crop, geo))
        self.selector.cancelled.connect(self._on_cancelled)
        self.selector.start()

    def translate_last(self) -> None:
        if self.last_region is None:
            self.translate_region()
            return
        if self.busy:
            return
        if self.live:
            self.stop_live()
        self.busy = True
        self._close_overlay()
        QTimer.singleShot(120, self._capture_last)

    def _capture_last(self) -> None:
        r = self.last_region
        screen = QGuiApplication.screenAt(r.center()) or QGuiApplication.primaryScreen()
        local = r.translated(-screen.geometry().topLeft())
        shot = screen.grabWindow(0)
        if not self._check_shot(shot):
            return
        dpr = shot.devicePixelRatio()
        crop = shot.copy(QRect(round(local.x() * dpr), round(local.y() * dpr), round(local.width() * dpr), round(local.height() * dpr)))
        crop.setDevicePixelRatio(dpr)
        self._run(r, crop, screen.geometry())

    def _check_shot(self, shot: QPixmap) -> bool:
        if shot.isNull() or shot.width() == 0:
            self.busy = False
            self._notify("Couldn't capture the screen. On macOS allow Screen Recording; on Wayland see README.")
            return False
        return True

    def _on_cancelled(self) -> None:
        self.selector = None
        self.busy = False
        if self.once:
            self.app.quit()

    def _on_selected(self, region: QRect, crop: QPixmap, screen_geo: QRect) -> None:
        self.selector = None
        self.last_region = region
        self._run(region, crop, screen_geo)

    def _start_job(self, shot: QPixmap, on_done, on_failed) -> None:
        job = TranslateJob(qimage_to_pil(shot.toImage()), self.cfg, self.translator())
        job.signals.done.connect(lambda patches: (self._finish_job(job), on_done(patches)))
        job.signals.failed.connect(lambda err: (self._finish_job(job), on_failed(err)))
        # Keep a Python reference until the job reports back, so its signals stay alive.
        self._jobs.append(job)
        job.setAutoDelete(False)
        self.busy = True
        self.pool.start(job)

    def _run(self, area: QRect, shot: QPixmap, screen_geo: QRect) -> None:
        """One-shot: translate ``shot``, which shows ``area`` (global logical coords)."""
        self._toast(screen_geo, "Translating…")
        dpr = shot.devicePixelRatio()
        retry = lambda: self._run(area, shot, screen_geo)  # noqa: E731
        self._start_job(shot, lambda patches: self._on_done(area, screen_geo, patches, dpr),
                        lambda err: self._on_failed(screen_geo, err, retry))

    def _on_done(self, area: QRect, screen_geo: QRect, patches: list, dpr: float) -> None:
        self._clear_toast()
        if not patches:
            self._toast(screen_geo, "Nothing to translate here", auto_close_ms=1500)
            if self.once:
                QTimer.singleShot(1600, self.app.quit)
            return
        overlay = ReplaceOverlay(area, click_through=self.cfg.click_through, font_family=self.cfg.font_family)
        overlay.set_patches(patches, dpr)
        overlay.dismissed.connect(self._on_dismissed)
        self.overlay = overlay
        overlay.show_over_screen()

    def _on_failed(self, screen_geo: QRect, err: Exception, retry=None) -> None:
        from .translate_local import ModelMissing

        self._clear_toast()
        if isinstance(err, ModelMissing):
            self._offer_download(err, screen_geo, retry)
            return
        msg = str(err) if isinstance(err, TranslationError) else f"{type(err).__name__}: {err}"
        print(f"Error: {msg}", file=sys.stderr)
        self._toast(screen_geo, msg, auto_close_ms=4000)
        if self.once:
            QTimer.singleShot(4100, self.app.quit)

    def _finish_job(self, job: QRunnable) -> None:
        self.busy = False
        if job in self._jobs:
            self._jobs.remove(job)

    def _on_dismissed(self) -> None:
        self.overlay = None
        if self.once:
            self.app.quit()

    # -- offline model download ------------------------------------------------------

    def _offer_download(self, err, screen_geo: QRect, retry) -> None:
        answer = QMessageBox.question(
            None, "Download offline translation model?",
            f"To translate {err.src} → {err.dst} offline, Scan-screen needs a translation model "
            "(about 100–300 MB, once per language pair, from the Argos Translate project).\n\n"
            "Only the model file is downloaded. Nothing from your screen is sent. Download now?")
        if answer != QMessageBox.StandardButton.Yes:
            if self.once:
                self.app.quit()
            return
        job = DownloadJob(err.src, err.dst)
        job.signals.progress.connect(lambda done, total: self._toast(
            screen_geo, f"Downloading model {err.src} → {err.dst}: " +
            (f"{done * 100 // total}%" if total else f"{done >> 20} MB")))
        job.signals.done.connect(lambda: (self._finish_job(job), self._clear_toast(), retry and retry()))
        job.signals.failed.connect(lambda msg: (self._finish_job(job), self._toast(screen_geo, msg, 5000)))
        self._jobs.append(job)
        job.setAutoDelete(False)
        self.busy = True
        self._toast(screen_geo, f"Downloading model {err.src} → {err.dst}…")
        self.pool.start(job)

    # -- live mode -------------------------------------------------------------------

    def start_live(self) -> None:
        self.live_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = self.live_screen.geometry()
        self.overlay = ReplaceOverlay(geo, click_through=True, font_family=self.cfg.font_family)
        self.overlay.dismissed.connect(self._on_dismissed)
        # Where the OS supports it (Windows 10 2004+), screenshots don't see our overlay at
        # all, so change detection doesn't have to mask it out.
        self.overlay_excluded = exclude_from_capture(self.overlay)
        self.live = True
        self.ref_thumb = self.prev_thumb = None
        self.stable_ticks = 0
        self._update_tray()
        self._toast(geo, "Live translation on — press the hotkey again to stop", auto_close_ms=1200)
        # First translation right away (after the toast is gone), then watch for changes.
        self.live_state = "settling"
        QTimer.singleShot(1300, self._live_translate_now)
        self.live_timer.start(max(200, self.cfg.live_interval_ms))

    def stop_live(self) -> None:
        self.live = False
        self.live_state = "idle"
        self.live_timer.stop()
        self._close_overlay()
        self._clear_toast()
        self._update_tray()

    def _live_restart(self) -> None:
        if self.overlay is not None:
            self.overlay.hide()
        self.ref_thumb = self.prev_thumb = None
        self.live_state = "settling"

    def _grab_live(self) -> QPixmap | None:
        shot = self.live_screen.grabWindow(0)
        return None if shot.isNull() or shot.width() == 0 else shot

    def _live_tick(self) -> None:
        if not self.live or self.busy or self.live_state == "translating":
            return
        shot = self._grab_live()
        if shot is None:
            return
        thumb = live.thumbnail(shot)
        if self.live_state == "showing":
            mask = None
            if not self.overlay_excluded and self.overlay is not None and self.overlay.isVisible():
                mask = live.overlay_mask(thumb.shape, self.overlay.covered_rects(), self.overlay.width())
            if live.changed(thumb, self.ref_thumb, mask):
                # The screen moved under the translation: hide it until things settle.
                self.overlay.hide()
                self.live_state = "settling"
                self.prev_thumb = None
                self.stable_ticks = 0
        elif self.live_state == "settling":
            if self.prev_thumb is not None and not live.changed(thumb, self.prev_thumb):
                self.stable_ticks += 1
            else:
                self.stable_ticks = 0
            self.prev_thumb = thumb
            if self.stable_ticks >= 1:
                self._live_translate(shot, thumb)

    def _live_translate_now(self) -> None:
        if not self.live or self.busy:
            return
        shot = self._grab_live()
        if shot is not None:
            self._live_translate(shot, live.thumbnail(shot))

    def _live_translate(self, shot: QPixmap, thumb) -> None:
        self.live_state = "translating"
        self.ref_thumb = thumb
        dpr = shot.devicePixelRatio()
        geo = self.live_screen.geometry()

        def done(patches):
            if not self.live:
                return
            self.overlay.set_patches(patches, dpr)
            if patches:
                self.overlay.show_over_screen()
            self.live_state = "showing"

        def failed(err):
            self.stop_live()
            self._on_failed(geo, err, retry=self.translate_screen)

        self._start_job(shot, done, failed)

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


def _cli_install(pair: str) -> int:
    from .translate_local import install_pair

    try:
        src, dst = pair.split(":")
    except ValueError:
        print("Use --install-model FROM:TO, e.g. --install-model es:en", file=sys.stderr)
        return 2

    def progress(done, total):
        pct = f"{done * 100 // total:3d}%" if total else f"{done >> 20} MB"
        print(f"\rDownloading {src} → {dst}: {pct}", end="", flush=True)

    try:
        added = install_pair(src, dst, progress=progress)
    except Exception as e:  # noqa: BLE001
        print(f"\nFailed: {e}", file=sys.stderr)
        return 1
    print(f"\nInstalled: {', '.join(f'{p.from_code}→{p.to_code}' for p in added) or 'already installed'}")
    return 0


def _selftest(out_path: str) -> int:
    """Check a packaged build works: OCR with the bundled Tesseract, text removal, offline guard.

    Writes a JSON report (windowed builds have no console to print to).
    """
    import json

    from PIL import ImageDraw, ImageFont

    from .render import make_patches

    report = {"version": __version__, "ok": False}
    try:
        cfg = load_config()
        privacy.enforce_offline()
        img = Image.new("RGB", (900, 200), (255, 255, 255))
        try:
            font = ImageFont.load_default(size=40)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
        ImageDraw.Draw(img).text((30, 60), "Scan screen self test", fill=(0, 0, 0), font=font)
        report["tesseract"] = cfg.resolved_tesseract_cmd() or "PATH"
        blocks = extract_blocks(img, "eng", 40, cfg.resolved_tesseract_cmd())
        report["text"] = " ".join(b.text for b in blocks)
        report["patches"] = len(make_patches(img, blocks, ["x" for _ in blocks]))
        try:
            import socket

            socket.create_connection(("example.com", 443), timeout=3)
            report["network_blocked"] = False
        except privacy.NetworkBlocked:
            report["network_blocked"] = True
        import ctranslate2  # noqa: F401  (offline engine is bundled)
        import sacremoses  # noqa: F401
        import subword_nmt.apply_bpe  # noqa: F401

        report["ok"] = "self test" in report["text"].lower() and report["network_blocked"] and report["patches"] > 0
    except Exception as e:  # noqa: BLE001
        report["error"] = f"{type(e).__name__}: {e}"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scan-screen", description="Translate the text on your screen in place.")
    parser.add_argument("--once", nargs="?", const="screen", choices=["screen", "region"],
                        help="translate the screen (or a region you pick) once, then exit when dismissed; "
                             "bind this to an OS keyboard shortcut if the built-in hotkeys don't work")
    parser.add_argument("--to", metavar="LANG", help="target language for this run, e.g. de, ja, es")
    parser.add_argument("--ocr", metavar="LANGS", help="Tesseract languages for this run, e.g. jpn or eng+fra")
    parser.add_argument("--engine", choices=["local", "google", "deepl"], help="translation engine for this run")
    parser.add_argument("--install-model", metavar="FROM:TO", help="download an offline model, e.g. es:en, and exit")
    parser.add_argument("--config-path", action="store_true", help="print the config file path and exit")
    parser.add_argument("--selftest", metavar="REPORT.json", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"scan-screen {__version__}")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest(args.selftest)
    if args.install_model:
        return _cli_install(args.install_model)
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
