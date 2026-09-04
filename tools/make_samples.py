"""Generate the bundled placeholder memes, one per status per orientation.

These exist so the project looks finished before you add your own images, and so there is
something to flash on day one. They are drawn from primitives here rather than shipped as binary
art, which keeps them unambiguously ours to distribute -- unlike most real memes.

    python tools/make_samples.py

Writes memes/<status>/00_sample.land.png and 00_sample.port.png. The `.land` / `.port` suffix is
build_memes.py's convention for "this image is only for that orientation"; your own images go in
the same folders, and without a suffix they are built for both.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
MEME_DIR = REPO / "memes"

#: Matches ORIENTATIONS in build_memes.py.
SIZES = {"land": (320, 240), "port": (240, 320)}

#: Height the caption band can occupy; scene art stays above it. Matches CAPTION_RESERVE.
CAPTION_RESERVE = 60

BG = (0x1B, 0x24, 0x30)
DESK = (0x2C, 0x3A, 0x4B)
BEZEL = (0x11, 0x18, 0x20)
MUG = (0xE0, 0xE6, 0xED)

#: Per status: screen tint, eye style, mouth style, and a prop standing on the desk.
FACES = {
    "available":    ((0x2E, 0xCC, 0x71), "open",  "smile", "mug"),
    "busy":         ((0xE7, 0x4C, 0x3C), "focus", "flat",  "papers"),
    "in_meeting":   ((0x8E, 0x44, 0xAD), "open",  "talk",  "mic"),
    "dnd":          ((0xB0, 0x3A, 0x2E), "angry", "frown", "papers"),
    "away":         ((0xF3, 0x9C, 0x12), "shut",  "flat",  "zzz"),
    "brb":          ((0xE6, 0x7E, 0x22), "shut",  "smile", "mug"),
    "offline":      ((0x7F, 0x8C, 0x8D), "off",   "off",   "mug"),
    "unknown":      ((0x56, 0x65, 0x73), "shrug", "wavy",  "mug"),
    "disconnected": ((0x34, 0x49, 0x5E), "off",   "off",   "cable"),
}


@dataclass(frozen=True)
class Layout:
    """Geometry derived from the frame size, so one drawing works in both orientations."""

    w: int
    h: int
    safe: int          # nothing meaningful is drawn below this; the caption band covers it
    mon: tuple[int, int, int, int]     # monitor bezel
    screen: tuple[int, int, int, int]  # inner screen
    desk_y: int
    eye_y: int
    eye_dx: int        # horizontal offset of each eye from centre
    unit: int          # scales feature sizes with the screen

    @property
    def cx(self) -> int:
        return self.w // 2


def layout_for(w: int, h: int) -> Layout:
    safe = h - CAPTION_RESERVE

    # The monitor takes most of the width and the upper part of the safe area, leaving room for
    # a desk with a prop on it underneath.
    mon_w = int(w * 0.76)
    mon_h = int(safe * 0.66)
    mon_x = (w - mon_w) // 2
    mon_y = int(safe * 0.05)
    mon = (mon_x, mon_y, mon_x + mon_w, mon_y + mon_h)

    inset = max(4, int(mon_w * 0.035))
    screen = (mon[0] + inset, mon[1] + inset, mon[2] - inset, mon[3] - inset)

    desk_y = mon[3] + int(safe * 0.09)
    screen_h = screen[3] - screen[1]
    eye_y = screen[1] + int(screen_h * 0.38)
    unit = max(6, int(min(screen[2] - screen[0], screen_h) * 0.115))
    eye_dx = int((screen[2] - screen[0]) * 0.20)

    return Layout(w, h, safe, mon, screen, desk_y, eye_y, eye_dx, unit)


def _dim(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in colour)  # type: ignore[return-value]


def draw_scene(status: str, size: tuple[int, int]) -> Image.Image:
    tint, eyes, mouth, prop = FACES[status]
    w, h = size
    lay = layout_for(w, h)
    image = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(image)

    # Desk. It runs to the bottom of the frame, but the caption band hides everything below
    # lay.safe, so nothing meaningful is drawn down there.
    d.rectangle((0, lay.desk_y, w, h), fill=DESK)
    d.line((0, lay.desk_y, w, lay.desk_y), fill=_dim(DESK, 1.5), width=2)

    # Monitor: bezel, screen, title bar, stand.
    d.rounded_rectangle(lay.mon, radius=max(4, lay.unit // 2), fill=BEZEL)
    d.rectangle(lay.screen, fill=_dim(tint, 0.32))
    bar_h = max(8, lay.unit)
    d.rectangle((lay.screen[0], lay.screen[1], lay.screen[2], lay.screen[1] + bar_h),
                fill=_dim(tint, 0.55))
    dot = max(3, bar_h // 3)
    for i in range(3):
        x = lay.screen[0] + dot * 2 + i * dot * 3
        y = lay.screen[1] + (bar_h - dot) // 2
        d.ellipse((x, y, x + dot, y + dot), fill=_dim(tint, 1.4))

    stand_w = int(lay.w * 0.09)
    d.rectangle((lay.cx - stand_w // 2, lay.mon[3], lay.cx + stand_w // 2, lay.desk_y), fill=BEZEL)
    base_w = int(lay.w * 0.28)
    d.rectangle((lay.cx - base_w // 2, lay.desk_y - 4, lay.cx + base_w // 2, lay.desk_y + 2),
                fill=_dim(BEZEL, 1.4))

    _draw_eyes(d, eyes, tint, lay)
    _draw_mouth(d, mouth, tint, lay)
    _draw_prop(d, prop, tint, lay)
    return image


def _draw_eyes(d: ImageDraw.ImageDraw, style: str, tint, lay: Layout) -> None:
    bright = _dim(tint, 1.7)
    r = lay.unit
    y = lay.eye_y
    left, right = lay.cx - lay.eye_dx, lay.cx + lay.eye_dx

    if style == "open":
        for x in (left, right):
            d.ellipse((x - r, y - r, x + r, y + r), fill=bright)
            d.ellipse((x - r // 3, y - r // 3, x + r // 2, y + r // 2), fill=BEZEL)
    elif style == "focus":
        for x in (left, right):
            d.ellipse((x - r, y - r * 2 // 3, x + r, y + r * 2 // 3), fill=bright)
            d.ellipse((x - r // 4, y - r // 4, x + r // 2, y + r // 2), fill=BEZEL)
    elif style == "angry":
        for x, tilt in ((left, 1), (right, -1)):
            d.ellipse((x - r, y - r, x + r, y + r), fill=bright)
            d.ellipse((x - r // 4, y - r // 5, x + r // 2, y + r // 2), fill=BEZEL)
            brow = r // 3
            d.line((x - r - 2, y - r - brow - tilt * brow, x + r + 2, y - r - brow + tilt * brow),
                   fill=BEZEL, width=max(4, r // 2))
    elif style == "shut":
        for x in (left, right):
            d.arc((x - r, y - r, x + r, y + r), start=200, end=340, fill=bright,
                  width=max(3, r // 3))
    elif style == "shrug":
        for x in (left, right):
            d.ellipse((x - r, y - r, x + r, y + r), outline=bright, width=max(3, r // 4))
            d.line((x - r // 2, y - r // 3, x + r // 2, y + r // 2), fill=bright,
                   width=max(3, r // 4))
    else:  # off
        dim = _dim(tint, 1.1)
        for x in (left, right):
            d.line((x - r, y - r, x + r, y + r), fill=dim, width=max(4, r // 3))
            d.line((x + r, y - r, x - r, y + r), fill=dim, width=max(4, r // 3))


def _draw_mouth(d: ImageDraw.ImageDraw, style: str, tint, lay: Layout) -> None:
    bright = _dim(tint, 1.7)
    r = lay.unit
    y = lay.eye_y + int(r * 2.6)
    half = int(lay.eye_dx * 0.9)
    box = (lay.cx - half, y - r, lay.cx + half, y + r)
    width = max(4, r // 2)

    if style == "smile":
        d.arc(box, start=0, end=180, fill=bright, width=width)
    elif style == "flat":
        d.line((lay.cx - half, y, lay.cx + half, y), fill=bright, width=width)
    elif style == "frown":
        d.arc((box[0], y, box[2], y + r * 2), start=180, end=360, fill=bright, width=width)
    elif style == "talk":
        d.ellipse((lay.cx - half // 2, y - r, lay.cx + half // 2, y + r), fill=bright)
        d.ellipse((lay.cx - half // 4, y - r // 2, lay.cx + half // 4, y + r // 2), fill=BEZEL)
    elif style == "wavy":
        step = (half * 2) // 5
        points = [(lay.cx - half + i * step, y + (r // 2 if i % 2 else -r // 2)) for i in range(6)]
        d.line(points, fill=bright, width=max(3, r // 3))


def _draw_prop(d: ImageDraw.ImageDraw, prop: str, tint, lay: Layout) -> None:
    """Props stand on the desk between desk_y and safe, clear of the caption band."""
    bright = _dim(tint, 1.6)
    base = lay.safe
    tall = max(18, int((lay.safe - lay.desk_y) * 0.85))
    left = int(lay.w * 0.07)
    right = int(lay.w * 0.93)

    if prop == "mug":
        wide = int(tall * 1.05)
        d.rounded_rectangle((left, base - tall, left + wide, base), radius=4, fill=MUG)
        d.arc((left + wide - 6, base - tall + 4, left + wide + 12, base - 6),
              start=270, end=90, fill=MUG, width=max(3, tall // 7))
        d.rectangle((left + 4, base - tall + 4, left + wide - 4, base - tall + 4 + tall // 6),
                    fill=(0x6F, 0x4E, 0x37))
    elif prop == "papers":
        wide = int(tall * 1.5)
        for offset in range(3):
            x = right - wide - offset * 6
            top = base - tall - offset * 5
            d.rectangle((x, top, x + wide, base - offset * 5), fill=(0xEC, 0xF0, 0xF1))
            d.line((x + 4, top + tall // 4, x + wide - 4, top + tall // 4), fill=DESK, width=2)
    elif prop == "mic":
        wide = max(12, tall // 2)
        cx = right - wide
        d.rounded_rectangle((cx - wide // 2, base - tall, cx + wide // 2, base - tall // 3),
                            radius=wide // 2, fill=bright)
        d.arc((cx - wide, base - tall * 3 // 4, cx + wide, base - 2),
              start=0, end=180, fill=bright, width=max(3, wide // 4))
    elif prop == "zzz":
        size = max(7, tall // 3)
        for index in range(3):
            x = right - int(lay.w * 0.30) + index * int(size * 1.7)
            y = base - size - index * int(size * 1.3)
            d.line((x, y, x + size, y), fill=bright, width=3)
            d.line((x + size, y, x, y + size), fill=bright, width=3)
            d.line((x, y + size, x + size, y + size), fill=bright, width=3)
    elif prop == "cable":
        # An unplugged cable: two ends, a gap, and a couple of sparks across it.
        y = base - tall // 2
        span = int(lay.w * 0.34)
        x0 = right - span
        plug = max(8, span // 8)
        thick = max(4, tall // 6)
        d.line((x0 - plug, y, x0 + plug, y), fill=bright, width=thick)
        d.rounded_rectangle((x0 + plug, y - thick, x0 + plug * 2, y + thick), radius=3, fill=MUG)
        d.rounded_rectangle((right - plug * 2, y - thick, right - plug, y + thick),
                            radius=3, fill=MUG)
        d.line((right - plug, y, right + plug, y), fill=bright, width=thick)
        mid = (x0 + plug * 2 + right - plug * 2) // 2
        for offset in (-thick, 0, thick):
            d.line((mid - plug // 2, y + offset, mid + plug // 2, y + offset), fill=MUG, width=2)


def main() -> int:
    for status in FACES:
        folder = MEME_DIR / status
        folder.mkdir(parents=True, exist_ok=True)
        for orientation, size in SIZES.items():
            target = folder / f"00_sample.{orientation}.png"
            draw_scene(status, size).save(target, format="PNG")
        print(f"wrote {(folder / '00_sample.*.png').relative_to(REPO)}")
    print("\nNext: python tools/build_memes.py --preview")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
