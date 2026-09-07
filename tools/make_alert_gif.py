"""Generate the bundled out-of-hours alert GIF.

    python tools/make_alert_gif.py

Writes assets/alert.gif, which tools/build_memes.py then re-encodes into firmware/data/. Drawn
from primitives here rather than shipped as binary art, for the same reason as the placeholder
memes: it is then unambiguously ours to distribute.

The scene is a notification bell being rung at an hour when nobody should be reading it: it
swings from its crown, the clapper lags behind it, sound arcs pulse outwards, and a badge blinks
in the corner. Replace it with anything you like from the Alerts tab; this is only what a fresh
install starts with.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The palette is chosen the same way an uploaded GIF's is, so this asset cannot drift from what
# the real pipeline produces -- and so the badge, which only exists on half the frames, gets a
# palette entry of its own.
from pc_app.gif_upload import shared_palette  # noqa: E402

OUT = REPO / "assets" / "alert.gif"

#: Drawn oversized and downsampled, which is the cheap way to get smooth edges out of Pillow's
#: hard-edged primitives. MAX_SIZE in pc_app/gif_upload.py is the size that actually ships.
SUPERSAMPLE = 3
SIZE = 240
FRAMES = 16
FRAME_MS = 80

BG = (0x1B, 0x24, 0x30)
BELL = (0xF5, 0xC9, 0x42)
BELL_DARK = (0xC9, 0x9A, 0x1E)
CLAPPER = (0xA9, 0x7C, 0x12)
ARC = (0xF3, 0x9C, 0x12)
BADGE = (0xE7, 0x4C, 0x3C)
BADGE_GLYPH = (0xFF, 0xFF, 0xFF)


def draw_frame(index: int) -> Image.Image:
    """One frame of the loop. *index* runs 0..FRAMES-1."""
    s = SUPERSAMPLE
    n = SIZE * s
    image = Image.new("RGB", (n, n), BG)
    draw = ImageDraw.Draw(image)

    phase = 2 * math.pi * index / FRAMES
    # A sine, so the bell decelerates at the ends of its arc and reads as something with weight
    # being swung rather than something spinning.
    swing = math.sin(phase) * 13

    cx = n // 2
    pivot_y = int(n * 0.30)
    w = int(n * 0.165)

    # Sound arcs first, so the bell sits on top of them. They grow outwards over each half of the
    # loop and restart, which is what makes it read as ringing rather than wobbling.
    reach = (index % (FRAMES // 2)) / (FRAMES // 2)
    for ring in range(3):
        if ring > reach * 3:
            continue
        radius = int(n * (0.26 + 0.075 * ring))
        box = (cx - radius, pivot_y + w - radius, cx + radius, pivot_y + w + radius)
        for start_deg, end_deg in ((150, 205), (335, 30)):
            draw.arc(box, start=start_deg, end=end_deg, fill=ARC, width=max(3, n // 80))

    # The bell on its own layer, rotated about the crown so it swings from the top like a real
    # one rather than pivoting about its middle.
    bell = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bell)

    # Crown: the little loop it hangs from.
    crown = n // 40
    bd.ellipse((cx - crown, pivot_y - 2 * crown, cx + crown, pivot_y), fill=BELL_DARK)

    # Dome, then a flared skirt down to the lip.
    shoulder = pivot_y + w
    bd.pieslice((cx - w, pivot_y, cx + w, pivot_y + 2 * w), start=180, end=360, fill=BELL)
    flare = int(w * 1.34)
    lip_y = pivot_y + int(w * 2.05)
    bd.polygon(
        [(cx - w, shoulder), (cx - flare, lip_y), (cx + flare, lip_y), (cx + w, shoulder)],
        fill=BELL,
    )
    lip_h = n // 26
    bd.rounded_rectangle(
        (cx - flare, lip_y - lip_h // 2, cx + flare, lip_y + lip_h // 2),
        radius=lip_h // 2,
        fill=BELL_DARK,
    )
    bell = bell.rotate(swing, resample=Image.BICUBIC, center=(cx, pivot_y))
    image.paste(bell, (0, 0), bell)

    # The clapper swings wider than the bell and lags a quarter turn behind it, which is what
    # sells the whole thing as being struck.
    clapper_r = n // 22
    lag = math.sin(phase - math.pi / 4) * 22
    clapper_y = pivot_y + int(w * 2.35)
    reach_px = math.radians(lag) * (clapper_y - pivot_y)
    draw.ellipse(
        (cx + reach_px - clapper_r, clapper_y - clapper_r,
         cx + reach_px + clapper_r, clapper_y + clapper_r),
        fill=CLAPPER,
    )

    # A badge that blinks on the half of the loop where the bell is furthest from rest, so the
    # two beats read as one event rather than two.
    if math.cos(phase) < 0:
        br = n // 11
        bx, by = cx + int(w * 1.5), pivot_y - n // 30
        draw.ellipse((bx - br, by - br, bx + br, by + br), fill=BADGE)
        bar_w, bar_h = br // 4, br
        draw.rounded_rectangle(
            (bx - bar_w, by - bar_h + br // 5, bx + bar_w, by + br // 8),
            radius=bar_w,
            fill=BADGE_GLYPH,
        )
        dot = by + br // 3
        draw.ellipse((bx - bar_w, dot, bx + bar_w, dot + 2 * bar_w), fill=BADGE_GLYPH)

    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> int:
    frames = [draw_frame(i) for i in range(FRAMES)]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Saved as plain frames on a shared palette; build_memes.py re-encodes it through the same
    # pipeline an uploaded GIF goes through, so this only has to be readable.
    palette = shared_palette(frames, 64)
    quantised = [f.quantize(palette=palette) for f in frames]
    quantised[0].save(
        OUT,
        format="GIF",
        save_all=True,
        append_images=quantised[1:],
        duration=FRAME_MS,
        loop=0,
        disposal=1,
    )
    print(f"wrote {OUT.relative_to(REPO)} ({OUT.stat().st_size:,} bytes, {FRAMES} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
