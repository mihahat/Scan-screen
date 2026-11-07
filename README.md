# Scan-screen

Press a shortcut and the text on your screen is replaced with its translation, in place: same position, colour and size, with the original erased from the background. It keeps up as the screen changes, and works in apps, games, images, videos, PDFs, anything you can see.

**Private by default:** reading the text and translating it both happen on your computer. Nothing from your screen is sent anywhere, and offline mode blocks the app's own internet access to guarantee it. [Details below.](#privacy)

## Download

Get the latest build from the [Releases page](https://github.com/mihahat/scan-screen/releases), or from the newest run on the [Actions page](https://github.com/mihahat/scan-screen/actions) (open a run and scroll to **Artifacts**; you need to be signed in to GitHub).

| System | File | Notes |
|---|---|---|
| **Windows 10/11** | `ScanScreen-windows-x64.zip` | Unzip anywhere and run `ScanScreen.exe`. Tesseract is included. Windows SmartScreen may warn that the app is unrecognised (it isn't code-signed): choose **More info → Run anyway**. |
| **macOS (Apple silicon)** | `ScanScreen-macos-arm64.zip` | Run `brew install tesseract tesseract-lang` first. The app isn't notarised: right-click it and choose **Open** the first time. Allow **Screen Recording** and **Accessibility** when asked. |
| **Linux (x64, X11)** | `ScanScreen-linux-x64.tar.gz` | Run `sudo apt install tesseract-ocr` plus language packs, e.g. `tesseract-ocr-spa`. Extract and run `ScanScreen/ScanScreen`. |

The first time you translate a language pair offline, the app offers to download its translation model (about 100–300 MB, once). See [Offline models](#offline-models).

## Use

Scan-screen runs in the system tray (the menu bar on macOS).

- **Ctrl+Alt+T** translates the **whole screen** your mouse is on and keeps it up to date while you scroll, type or watch. Press it again to stop. In live mode your clicks go through to your apps as normal.
- **Ctrl+Alt+R** translates just a **region**: drag a box around the text. Click or Esc to close it.
- **Ctrl+C** while a region translation is showing copies the translated text.

The **tray menu** lets you choose the **screen language**, the language to **translate into**, the **translation engine**, and whether to **keep updating** as the screen changes. Text already in your target language is left as it is.

### How live updating works

About twice a second the app takes a small, low-resolution look at the screen and compares it with the last one. When something changes (you scroll, a new message arrives), it hides the old translation so it never sits over the wrong text, waits for the screen to settle, and reads and translates it again. Text it has already translated is remembered for the session, so only new text is translated. A full re-read takes roughly 1–3 seconds depending on your screen size and computer. On Windows the translation is invisible to screenshots and screen sharing, so the app can watch the screen underneath it directly.

## Privacy

| What | Where it goes |
|---|---|
| Screenshots | **Nowhere.** Kept in memory only while being read, then discarded. Never saved to disk, never sent. |
| Recognised text, offline engine (default) | **Nowhere.** Translated on your computer. The app blocks all of its own outgoing internet connections, so nothing can be sent even by mistake. |
| Recognised text, Google or DeepL engine | Sent to that company to be translated. You must choose this in the tray menu and confirm a warning first. Repeated text is sent only once per session. |
| Translation models | Downloaded once from the Argos Translate project when you approve it. The request contains only the model's name. Model archives are checked so they can't write outside the models folder. |
| Settings | A local JSON file. No accounts, no telemetry, no update checks. |

Offline mode is enforced in code (`scan_screen/privacy.py`): internet sockets are refused while it's active, and the tests check this. Local connections the desktop needs, such as the X11 display, still work. The only exception is a model download you approve, which is allowed for that download alone.

Versions 0.1.x used Google as the default engine. On upgrade, settings that still had that old default are switched to offline automatically.

## Offline models

Offline translation uses open [Argos Translate](https://github.com/argosopentech/argos-translate) / OPUS-MT models, run with CTranslate2. Quality is good for most European languages and decent for others, though below Google or DeepL. Most models translate to or from English. Other pairs (e.g. Spanish → German) go through English automatically, using two models.

The tray app offers to download models when needed. You can also install them ahead of time:

```bash
scan-screen --install-model es:en
```

Models are stored in `%LOCALAPPDATA%\scan-screen\models` (Windows), `~/Library/Application Support/scan-screen/models` (macOS) or `~/.local/share/scan-screen/models` (Linux). Delete a folder there to remove a model.

## Run from source

1. **Python 3.9+**: https://www.python.org/downloads/
2. **Tesseract OCR**
   - Windows: the installer from [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki). Tick the extra languages you need.
   - macOS: `brew install tesseract tesseract-lang`
   - Debian/Ubuntu: `sudo apt install tesseract-ocr` plus language packs, e.g. `tesseract-ocr-jpn`
3. **Scan-screen**

   ```bash
   git clone https://github.com/mihahat/scan-screen
   cd scan-screen
   python -m pip install .
   scan-screen
   ```

One-off overrides from the command line:

```bash
scan-screen --to de             # translate into German this time
scan-screen --ocr jpn           # read Japanese text
scan-screen --engine deepl      # use an online engine this time
scan-screen --once              # translate the screen once, exit when dismissed
scan-screen --once region       # same, but pick a region first
```

## Settings

Most settings are in the tray menu. The rest are in the config file: run `scan-screen --config-path` or use **Open config file** in the tray menu, and restart after editing.

| Setting | Default | Meaning |
|---|---|---|
| `hotkey` | `<ctrl>+<alt>+t` | Translate the whole screen / stop ([pynput format](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)), e.g. `<cmd>+<shift>+t` |
| `region_hotkey` | `<ctrl>+<alt>+r` | Pick a region to translate. `""` disables it |
| `ocr_languages` | `eng` | Tesseract language(s) of the text on screen: `spa`, `jpn`, `chi_sim`, `eng+fra` … |
| `source_language` | `auto` | Language of the text. `auto` takes it from `ocr_languages` (offline) or detects it (online) |
| `target_language` | `en` | Language to translate into: `en`, `de`, `es`, `ja` … |
| `engine` | `local` | `local` (offline, private), `google` or `deepl` (online, sends text) |
| `deepl_api_key` | | Your DeepL key. The `DEEPL_API_KEY` environment variable also works |
| `live_mode` | `true` | Keep the whole-screen translation updated as the screen changes |
| `live_interval_ms` | `600` | How often live mode checks for changes |
| `click_through` | `false` | For one-off translations: let clicks go through to the apps below |
| `tesseract_cmd` | | Full path to `tesseract` if it isn't found automatically |
| `min_confidence` | `40` | Drop words Tesseract is less sure of (0–100). Raise it if you see garbage |
| `font_family` | | Font for translations, e.g. `"Noto Sans"` |

> **Important:** the screen language must match the *text on screen*, and that Tesseract language pack must be installed (the Windows build includes the languages in the tray menu).

## Platform notes

- **macOS**: allow the app under *System Settings → Privacy & Security* in both **Screen Recording** and **Accessibility** (for the hotkey).
- **Linux on Wayland**: apps can't grab global hotkeys or the screen directly. Use an X11 session, or bind a desktop shortcut to `scan-screen --once`.
- **Several monitors**: translation runs on the monitor your mouse is on.

## How it works

1. The screen is captured and read with Tesseract. A whole screen gets three passes (normal layout, sparse text for isolated labels, and inverted for light-on-dark areas), so menus, buttons and dark headers aren't missed.
2. Paragraphs are translated in a batch. Anything that comes back unchanged is skipped.
3. For each paragraph the background colour and the original text colour are measured. The old letters are erased: flat backgrounds are filled with their exact colour, and gradients or photos are filled in smoothly from the surrounding pixels.
4. The translation is drawn in the original's colour at a matching size, shrinking to fit if it's longer.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

To build the app yourself: `pip install -e ".[build]"`, `python packaging/make_icon.py`, then `pyinstaller packaging/scan-screen.spec`. The app is created in `dist/`. Set `TESSERACT_DIR` to a Tesseract folder to bundle it. The GitHub Actions workflow in `.github/workflows/build.yml` builds and self-tests all three platforms, and publishes a release when you push a `v*` tag.

Code in `scan_screen/`: `ocr.py` reads text and positions; `translate.py` and `translate_local.py` hold the engines; `privacy.py` enforces offline mode; `render.py` erases the original text; `live.py` detects screen changes; `overlay.py` draws the windows; `app.py` wires it all together.
