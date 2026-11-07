import numpy as np
from PIL import Image, ImageDraw, ImageFont

from scan_screen.ocr import TextBlock
from scan_screen.render import is_unchanged, make_patches, needs_translation


def _text_image(bg, fg, text="Bienvenido"):
    img = Image.new("RGB", (300, 80), bg)
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), text, fill=fg, font=ImageFont.load_default(size=32))
    x0, y0, x1, y1 = draw.textbbox((20, 20), text, font=ImageFont.load_default(size=32))
    block = TextBlock(text, x0, y0, x1 - x0, y1 - y0, y1 - y0, [(x0, y0, x1 - x0, y1 - y0)])
    return img, block


def test_erases_text_and_finds_colors():
    img, block = _text_image((250, 245, 230), (30, 40, 160))
    (patch,) = make_patches(img, [block], ["Welcome"])
    arr = np.asarray(patch.image).astype(int)
    # Every pixel of the cleaned patch is close to the background: the glyphs are gone.
    assert np.abs(arr - np.array([250, 245, 230])).max() < 25
    assert np.abs(np.array(patch.text_color) - np.array([30, 40, 160])).max() < 25
    assert patch.bg_color == (250, 245, 230)
    assert (patch.x, patch.y) == (block.x - 6, block.y - 6)


def test_dark_mode_text():
    img, block = _text_image((24, 26, 30), (235, 235, 235))
    (patch,) = make_patches(img, [block], ["Welcome"])
    assert np.asarray(patch.image).max() < 60
    assert min(patch.text_color) > 200


def test_gradient_background_is_inpainted():
    grad = np.tile(np.linspace(60, 220, 300, dtype=np.uint8)[None, :, None], (80, 1, 3))
    img, block = _text_image((0, 0, 0), (255, 255, 255))
    img = Image.fromarray(grad)
    ImageDraw.Draw(img).text((20, 20), "Bienvenido", fill=(255, 255, 255), font=ImageFont.load_default(size=32))
    (patch,) = make_patches(img, [block], ["Welcome"])
    cleaned = np.asarray(patch.image).astype(int)
    original = grad[patch.y:patch.y + cleaned.shape[0], patch.x:patch.x + cleaned.shape[1]].astype(int)
    # Close to the underlying gradient, not a flat median colour.
    assert np.abs(cleaned - original).mean() < 12


def test_skips_untranslated_and_noise():
    img, block = _text_image((255, 255, 255), (0, 0, 0), "Settings")
    assert make_patches(img, [block], ["Settings"]) == []
    assert make_patches(img, [block], [""]) == []
    assert is_unchanged("Hello, world!", "hello world")
    assert not needs_translation("12:45")
    assert not needs_translation("x")
    assert needs_translation("Hola")
