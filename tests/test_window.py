import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtCore")
from PyQt6.QtWidgets import QApplication  # noqa: E402

import scan_screen.app as appmod  # noqa: E402
from scan_screen import privacy  # noqa: E402
from scan_screen.config import Config  # noqa: E402


@pytest.fixture
def ctl(monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    monkeypatch.setattr(appmod, "save_config", lambda *a, **k: None)
    c = appmod.ScanScreen(app, Config(ocr_languages="spa", target_language="de"))
    monkeypatch.setattr(c, "_setup_hotkeys", lambda: None)
    quits = []
    monkeypatch.setattr(app, "quit", lambda: quits.append(True))
    c.quits = quits
    c.start()
    yield c
    privacy.disable()


def test_window_shows_settings_and_privacy(ctl):
    w = ctl.window
    assert w.isVisible()
    assert w.cmb_src.currentText() == "Spanish"
    assert w.cmb_dst.currentText() == "German"
    assert w.cmb_engine.currentData() == "local"
    assert "Offline" in w.badge.text()
    assert w.btn_screen.text().startswith("Translate screen")


def test_window_changes_settings_and_button_toggles(ctl):
    w = ctl.window
    w.cmb_dst.setCurrentIndex(w.cmb_dst.findData("fr"))
    w.cmb_dst.activated.emit(w.cmb_dst.currentIndex())
    assert ctl.cfg.target_language == "fr"
    w.chk_live.setChecked(False)
    assert ctl.cfg.live_mode is False
    ctl.live = True
    ctl._refresh_ui()
    assert w.btn_screen.text().startswith("Stop translating")
    ctl.status.emit("Live: 3 translations")
    assert w.status.text() == "Live: 3 translations"


def test_quit_button_and_close_quit_the_app(ctl):
    ctl.window.btn_quit.click()
    assert ctl.quits
    ctl.quits.clear()
    ctl.window.close()
    assert ctl.quits


def test_show_in_screenshots_setting(ctl):
    assert ctl.cfg.hide_from_screenshots is True
    assert not ctl.window.chk_shots.isChecked()
    ctl.set_show_in_screenshots(True)
    assert ctl.cfg.hide_from_screenshots is False
    assert ctl.window.chk_shots.isChecked()
