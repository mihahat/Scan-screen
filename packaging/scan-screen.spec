# PyInstaller build definition. Run from the repository root:
#   python packaging/make_icon.py
#   pyinstaller packaging/scan-screen.spec
# Set TESSERACT_DIR to a Tesseract install folder (with tessdata/) to bundle it.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
datas, binaries, hiddenimports = [], [], []
for pkg in ("ctranslate2", "sacremoses", "subword_nmt", "sentencepiece"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pynput picks its keyboard backend at runtime; include the one for this OS.
backend = {"win32": "_win32", "darwin": "_darwin"}.get(sys.platform, "_xorg")
hiddenimports += [f"pynput.keyboard.{backend}", f"pynput.mouse.{backend}"]

tess = os.environ.get("TESSERACT_DIR")
if tess:
    datas.append((tess, "tesseract"))

icon = ROOT / "packaging" / "icon.png"

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Nothing that phones home or isn't needed.
    excludes=["tkinter", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtNetwork",
              "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtMultimedia", "PyQt6.QtBluetooth", "PyQt6.QtPositioning",
              "torch", "matplotlib", "IPython", "subword_nmt.tests", "cv2"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScanScreen",
    console=False,  # a tray app: no console window
    icon=str(icon) if icon.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ScanScreen")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ScanScreen.app",
        icon=str(icon) if icon.exists() else None,
        bundle_identifier="io.github.mihahat.scanscreen",
        info_plist={
            "LSUIElement": True,  # menu-bar app, no Dock icon
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "0.3.0",
        },
    )
