"""Generate assets/icon.ico for the packaged .exe.

Drawn from primitives for the same reason as the placeholder memes: no binary art to license.

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "assets" / "icon.ico"

#: Windows picks the closest size from the .ico, so ship the whole usual set.
SIZES = (16, 24, 32, 48, 64, 128, 256)

BEZEL = (0x11, 0x18, 0x20)
GREEN = (0x2E, 0xCC, 0x71)


def draw(size: int) -> Image.Image:
    """A little monitor with a smiling face -- the app in one glyph."""
    # Drawn at 8x and downsampled, so the curves stay clean at 16px.
    scale = 8
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    def px(value: float) -> float:
        return value * s / 64.0  # author against a 64x64 grid

    d.rounded_rectangle((px(4), px(8), px(60), px(46)), radius=px(6), fill=BEZEL)
    d.rounded_rectangle((px(9), px(13), px(55), px(41)), radius=px(3), fill=GREEN)
    d.rectangle((px(28), px(46), px(36), px(53)), fill=BEZEL)
    d.rounded_rectangle((px(18), px(53), px(46), px(57)), radius=px(2), fill=BEZEL)

    eye_y = px(23)
    for eye_x in (px(22), px(42)):
        d.ellipse((eye_x - px(4), eye_y - px(4), eye_x + px(4), eye_y + px(4)), fill=BEZEL)
    d.arc((px(21), px(24), px(43), px(38)), start=0, end=180, fill=BEZEL, width=int(px(3.5)))

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(size) for size in SIZES]
    frames[-1].save(TARGET, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"wrote {TARGET.relative_to(REPO)} ({TARGET.stat().st_size:,} bytes, sizes {SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
