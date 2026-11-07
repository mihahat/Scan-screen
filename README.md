# Scan-screen

Press a shortcut and the text on your screen is replaced with its translation, in place: same position, colour and size, with the original erased from the background. It works in apps, games, images, videos, PDFs, anything you can see.

It runs on Windows, macOS and Linux (X11). Text is read on your own computer with [Tesseract](https://github.com/tesseract-ocr/tesseract). Only the recognised text is sent to the translation service, never the screenshot.

## Install

1. **Python 3.9+**: https://www.python.org/downloads/
2. **Tesseract OCR**
   - Windows: the installer from [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki). Tick the extra languages you need during setup.
   - macOS: `brew install tesseract tesseract-lang`
   - Debian/Ubuntu: `sudo apt install tesseract-ocr` plus language packs, e.g. `tesseract-ocr-jpn`
3. **Scan-screen**

   ```bash
   git clone https://github.com/mihahat/scan-screen
   cd scan-screen
   python -m pip install .
   ```

## Use

```bash
scan-screen
```

A tray icon appears. Then:

- **Ctrl+Alt+T** translates the **whole screen** your mouse is on. Text already in your target language is left as it is. Press it again (or click, or Esc) to go back to the original.
- **Ctrl+Alt+R** translates just a **region**: drag a box around the text. Esc or right-click cancels.
- **Ctrl+C** while a translation is showing copies the translated text.

The translation is a snapshot: if the screen scrolls or changes, press the hotkey again. The tray menu also has **Translate last region again**, which re-reads the same spot. That's handy for game dialogue or subtitles.

One-off overrides from the command line:

```bash
scan-screen --to de             # translate into German this time
scan-screen --ocr jpn           # read Japanese text
scan-screen --engine deepl
scan-screen --once              # translate the screen once, exit when dismissed
scan-screen --once region       # same, but pick a region first
```

## Settings

Run `scan-screen --config-path` to find the config file, or use **Open config file** in the tray menu. Restart the app after editing it.

| Setting | Default | Meaning |
|---|---|---|
| `hotkey` | `<ctrl>+<alt>+t` | Translate the whole screen / hide it again ([pynput format](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys)), e.g. `<cmd>+<shift>+t` |
| `region_hotkey` | `<ctrl>+<alt>+r` | Pick a region to translate. Set to `""` to disable |
| `ocr_languages` | `eng` | Tesseract language(s) of the text on screen: `jpn`, `chi_sim`, `kor`, `deu`, `eng+fra` … |
| `source_language` | `auto` | Language of the text for the translator, or `auto` to detect |
| `target_language` | `en` | Language to translate into: `en`, `de`, `es`, `ja` … |
| `engine` | `google` | `google` (free, no key) or `deepl` |
| `deepl_api_key` | | Your DeepL key. The `DEEPL_API_KEY` environment variable also works |
| `tesseract_cmd` | | Full path to `tesseract` if it isn't found automatically |
| `min_confidence` | `40` | Drop words Tesseract is less sure of (0–100). Raise it if you see garbage |
| `click_through` | `false` | `true` lets clicks go through the translated screen to the apps below. Then hide it with the hotkey |
| `font_family` | | Font for translations, e.g. `"Noto Sans"` |

> **Important:** `ocr_languages` must match the *text on screen*, and you need that Tesseract language pack installed. With the default `eng`, Japanese text won't be read.

### Translation engines

- **google** (default): Google Translate's public web endpoint. It's free and needs no account, but it's unofficial: Google rate-limits it and may block it after heavy use. If you see *"rate limiting you"*, wait a bit or switch to DeepL.
- **deepl**: better quality and an official API. Get a free key (500,000 characters a month) at https://www.deepl.com/pro-api, then set `"engine": "deepl"` and `"deepl_api_key": "…"`.

New engines can be added by subclassing `Translator` in `scan_screen/translate.py` and registering it in `get_translator`.

## Platform notes

- **macOS**: the first time, allow your terminal (or Python) under *System Settings → Privacy & Security* in both **Screen Recording** (to capture the screen) and **Accessibility** (for the global hotkey).
- **Linux on Wayland**: apps can't grab global hotkeys or the screen directly. Log into an X11 session, or bind a desktop keyboard shortcut to `scan-screen --once` (screen capture may still be blocked by your compositor).
- **Several monitors**: the region picker opens on the monitor your mouse is on.

## How it works

1. The screen is captured and read with Tesseract. A whole screen gets three passes (normal layout, sparse text for isolated labels, and inverted for light-on-dark areas), so menus, buttons and dark headers aren't missed.
2. Paragraphs are translated in one request. Anything that comes back unchanged is skipped.
3. For each paragraph the background colour and the original text colour are measured. The old letters are erased: flat backgrounds are filled with their exact colour, and gradients or photos are filled in from their surroundings (OpenCV inpainting).
4. The translation is drawn in the original's colour at a matching size, shrinking to fit if it's longer.

## Tips for better results

- Select only the text, not the whole window. Less background means fewer misreads.
- Very small or very stylised text is read worst. Zooming in on the content helps a lot.
- Light text on dark backgrounds is handled automatically.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

The code lives in `scan_screen/`: `ocr.py` reads text and positions, `translate.py` holds the engines, `render.py` erases the original text, `overlay.py` draws the picker and the translated screen, and `app.py` wires them together with the tray and hotkeys.
