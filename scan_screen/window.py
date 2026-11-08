"""The main control window: start/stop, languages, engine, privacy status, quit."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

if TYPE_CHECKING:
    from .app import ScanScreen

STYLE = """
QWidget#root { background: #f5f6f8; }
QLabel { color: #1d2129; font-size: 13px; }
QLabel#title { font-size: 20px; font-weight: 600; }
QLabel#subtitle { color: #5b6270; font-size: 12px; }
QLabel#badge { border-radius: 11px; padding: 3px 10px; font-size: 12px; font-weight: 600; }
QLabel#badge[mode="offline"] { background: #e3f6e8; color: #17633a; }
QLabel#badge[mode="online"] { background: #fff1db; color: #8a4b00; }
QLabel#fieldLabel { color: #5b6270; font-size: 12px; }
QLabel#status { color: #3b4250; font-size: 12px; }
QLabel#privacyNote { color: #5b6270; font-size: 11px; }
QFrame#card { background: white; border: 1px solid #e2e5ea; border-radius: 10px; }
QPushButton { border-radius: 8px; padding: 9px 14px; font-size: 13px; }
QPushButton#primary { background: #2878e6; color: white; border: none; font-size: 15px; font-weight: 600; padding: 12px; }
QPushButton#primary:hover { background: #1f68cc; }
QPushButton#primary[active="true"] { background: #d64545; }
QPushButton#primary[active="true"]:hover { background: #bb3535; }
QPushButton#secondary { background: white; color: #1d2129; border: 1px solid #cfd4dc; }
QPushButton#secondary:hover { background: #eef1f5; }
QPushButton#quit { background: white; color: #b42318; border: 1px solid #f0c4c0; }
QPushButton#quit:hover { background: #fdecea; }
QComboBox { background: white; border: 1px solid #cfd4dc; border-radius: 6px; padding: 5px 8px; min-height: 20px; color: #1d2129; }
QComboBox QAbstractItemView { background: white; color: #1d2129; selection-background-color: #dce9fb; selection-color: #1d2129; }
QCheckBox { color: #1d2129; font-size: 13px; spacing: 8px; }
"""


def _pretty_hotkey(hotkey: str) -> str:
    return "+".join(part.strip("<>").capitalize() for part in hotkey.split("+")) if hotkey else ""


class ControlWindow(QWidget):
    """Shown at launch so nobody has to hunt for a tray icon."""

    def __init__(self, ctl: ScanScreen, icon: QIcon, languages: list[tuple[str, str, str]], engines: dict[str, str]):
        super().__init__()
        self.ctl = ctl
        self.setObjectName("root")
        self.setWindowTitle("Scan-screen")
        self.setWindowIcon(icon)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(400)
        self._syncing = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(14)

        # Header: name and privacy badge.
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("Scan-screen")
        title.setObjectName("title")
        sub = QLabel("Translates the text on your screen, in place")
        sub.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(sub)
        head.addLayout(titles)
        head.addStretch()
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        head.addWidget(self.badge, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        # Main actions.
        self.btn_screen = QPushButton()
        self.btn_screen.setObjectName("primary")
        self.btn_screen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_screen.clicked.connect(self._on_screen)
        outer.addWidget(self.btn_screen)
        self.btn_region = QPushButton()
        self.btn_region.setObjectName("secondary")
        self.btn_region.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_region.clicked.connect(self._on_region)
        outer.addWidget(self.btn_region)

        # Settings card.
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        def label(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setObjectName("fieldLabel")
            return lab

        self.cmb_src = QComboBox()
        self.cmb_dst = QComboBox()
        for name, tess, iso in languages:
            self.cmb_src.addItem(name, tess)
            self.cmb_dst.addItem(name, iso)
        self.cmb_engine = QComboBox()
        for key, text in engines.items():
            self.cmb_engine.addItem(text, key)
        for c in (self.cmb_src, self.cmb_dst, self.cmb_engine):
            c.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        grid.addWidget(label("Text on screen is in"), 0, 0)
        grid.addWidget(self.cmb_src, 0, 1)
        grid.addWidget(label("Translate into"), 1, 0)
        grid.addWidget(self.cmb_dst, 1, 1)
        grid.addWidget(label("Translator"), 2, 0)
        grid.addWidget(self.cmb_engine, 2, 1)
        self.chk_live = QCheckBox("Keep updating as the screen changes")
        grid.addWidget(self.chk_live, 3, 0, 1, 2)
        self.chk_shots = QCheckBox("Show translations in screenshots and screen sharing")
        self.chk_shots.setToolTip(
            "Off: screenshots you take show the original text, and live mode can check the text "
            "underneath without any flicker.\nOn: screenshots show the translation, and live mode "
            "briefly lifts it every few seconds to check underneath.")
        grid.addWidget(self.chk_shots, 4, 0, 1, 2)
        # Only Windows can keep a window out of screenshots; elsewhere they always include it.
        self.chk_shots.setVisible(sys.platform == "win32")
        grid.setColumnStretch(1, 1)
        outer.addWidget(card)

        self.cmb_src.activated.connect(lambda _: self._syncing or ctl.set_source(self.cmb_src.currentData()))
        self.cmb_dst.activated.connect(lambda _: self._syncing or ctl.set_target(self.cmb_dst.currentData()))
        self.cmb_engine.activated.connect(lambda _: self._syncing or ctl.set_engine(self.cmb_engine.currentData()))
        self.chk_live.toggled.connect(lambda on: self._syncing or ctl.set_live_mode(on))
        self.chk_shots.toggled.connect(lambda on: self._syncing or ctl.set_show_in_screenshots(on))

        self.privacy_note = QLabel()
        self.privacy_note.setObjectName("privacyNote")
        self.privacy_note.setWordWrap(True)
        outer.addWidget(self.privacy_note)

        self.status = QLabel("Ready")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        # Bottom row: hide / quit.
        bottom = QHBoxLayout()
        self.btn_hide = QPushButton("Hide window")
        self.btn_hide.setObjectName("secondary")
        self.btn_hide.setToolTip("Keep Scan-screen running in the background; the hotkeys keep working")
        self.btn_hide.clicked.connect(self.hide_to_background)
        bottom.addWidget(self.btn_hide)
        bottom.addStretch()
        self.btn_quit = QPushButton("Quit Scan-screen")
        self.btn_quit.setObjectName("quit")
        self.btn_quit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_quit.clicked.connect(ctl.quit)
        bottom.addWidget(self.btn_quit)
        outer.addLayout(bottom)

        ctl.changed.connect(self.refresh)
        ctl.status.connect(self.set_status)
        self.refresh()

    # -- reacting to the controller ----------------------------------------------

    def refresh(self) -> None:
        ctl, cfg = self.ctl, self.ctl.cfg
        self._syncing = True
        try:
            active = ctl.live or ctl.overlay is not None
            hk = _pretty_hotkey(cfg.hotkey)
            self.btn_screen.setText(f"Stop translating   ({hk})" if active else f"Translate screen   ({hk})")
            self.btn_screen.setProperty("active", "true" if active else "false")
            self.btn_region.setText(f"Translate a region   ({_pretty_hotkey(cfg.region_hotkey)})")
            self._select(self.cmb_src, cfg.ocr_languages.split("+")[0])
            self._select(self.cmb_dst, cfg.target_language)
            self._select(self.cmb_engine, cfg.engine)
            self.chk_live.setChecked(cfg.live_mode)
            self.chk_shots.setChecked(not cfg.hide_from_screenshots)
            offline = cfg.engine == "local"
            self.badge.setText("Offline · private" if offline else "Online")
            self.badge.setProperty("mode", "offline" if offline else "online")
            self.privacy_note.setText(
                "Everything stays on this computer: reading and translating both happen here, "
                "and the app's internet access is blocked." if offline else
                f"Screenshots stay on this computer, but the recognised text is sent to {cfg.engine.capitalize()} "
                "to be translated.")
            self.btn_hide.setToolTip(
                "Keep running in the background (find it again in the system tray, or press the hotkey)"
                if ctl.tray is not None else "Minimise; the hotkeys keep working")
        finally:
            self._syncing = False
        # Re-apply the stylesheet so property-based colours update.
        for w in (self.btn_screen, self.badge):
            w.style().unpolish(w)
            w.style().polish(w)

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    # -- actions --------------------------------------------------------------------

    def _on_screen(self) -> None:
        self.ctl.translate_screen()  # minimises this window first, so it isn't translated too

    def _on_region(self) -> None:
        self.ctl.translate_region()

    def hide_to_background(self) -> None:
        if self.ctl.tray is not None:
            self.hide()
            self.ctl.tray.showMessage("Scan-screen is still running",
                                      "Press the hotkey to translate, or click the tray icon to open this window.")
        else:
            self.showMinimized()

    def bring_back(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e: QCloseEvent) -> None:
        # The window's X button quits the app, like a normal program.
        e.accept()
        self.ctl.quit()
