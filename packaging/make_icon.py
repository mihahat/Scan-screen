"""Draw the app icon (packaging/icon.png); PyInstaller converts it per platform."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

size = 512
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((24, 24, size - 24, size - 24), radius=96, fill=(40, 120, 230))
try:
    font = ImageFont.load_default(size=260)
except TypeError:
    font = ImageFont.load_default()
d.text((size / 2, size / 2), "Aa", fill="white", font=font, anchor="mm")
out = Path(__file__).with_name("icon.png")
img.save(out)
print(out)
