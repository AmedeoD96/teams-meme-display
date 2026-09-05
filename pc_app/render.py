"""Renders what the panel will show, on the PC, with no hardware attached.

Two callers: `tools/build_memes.py --preview` writes these to `preview/`, and the GUI shows one
live while you edit a phrase. The geometry constants mirror the kCaption*/kTextMode* constants in
`firmware/src/display.h`, so a caption that wraps to four lines here will be truncated there too.

This is a layout preview, not a pixel contract: the device draws TFT_eSPI bitmap fonts and this
draws a desktop TrueType font, so a line may break one word differently. The mascot is drawn from
the same parameter table the firmware uses (`pc_app/mascot_faces.py`) but with Pillow primitives,
so it is a likeness rather than a copy.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pc_app.mascot_faces import Face, face_for
from pc_app.text import wrap_caption as _wrap

# Caption band, kept in sync with the kCaption* constants in firmware/src/display.h.
CAPTION_PAD_X = 6
CAPTION_PAD_Y = 6

# The band picks the largest font the caption fits in. The device draws TFT_eSPI bitmap fonts 4
# and 2; these are the desktop point sizes that stand in for them at the same line heights.
CAPTION_FONT_BIG = 20
CAPTION_LINE_H_BIG = 26
CAPTION_LINES_BIG = 3
CAPTION_FONT_SMALL = 14
CAPTION_LINE_H_SMALL = 16
CAPTION_LINES_SMALL = 4

#: The small font is the one allowed the most lines, so it bounds the array.
CAPTION_MAX_LINES = CAPTION_LINES_SMALL
#: Vertical space the caption band can take, so scene art can stay clear of it. The big font at
#: its line limit is taller than the small font at its own.
CAPTION_RESERVE = CAPTION_LINES_BIG * CAPTION_LINE_H_BIG + 2 * CAPTION_PAD_Y

# Text-mode layout, mirroring layoutTextMode()/drawTextScene() in firmware/src/display.cpp.
TEXT_PAD_X = 12
#: The presence badge that heads the screen, in place of the status name.
TEXT_BADGE_CY = 26
TEXT_BADGE_R = 16
TEXT_RULE_Y = 50
TEXT_TOP = 58
TEXT_LINE_H = 26
TEXT_MAX_LINES = 8

# Mascot palette. Teams-flavoured purples, but this is an original character rather than the
# Microsoft Teams logo, which is a trademark and is deliberately not reproduced here.
MASCOT_BODY = (0x62, 0x64, 0xA7)
MASCOT_HIGHLIGHT = (0x7B, 0x83, 0xEB)
MASCOT_SHADOW = (0x4B, 0x53, 0xBC)
MASCOT_PUPIL = (0x2B, 0x2C, 0x50)
MASCOT_BLUSH = (0xE8, 0x7C, 0x9E)
WHITE = (255, 255, 255)

#: Largest sprite the firmware will allocate. Kept well under the space available: the caption is
#: what people actually read, so the character gives way to it rather than the other way round.
MASCOT_MAX_SIZE = 130
MASCOT_MARGIN = 20
#: How much of the status colour survives in the mascot backdrop. The full colour would clash
#: with the purple body and swallow the presence badge, which is the one element that has to stay
#: readable when a tone has the face saying something other than the truth.
MASCOT_BACKDROP = 0.22

STATUS_LABELS = {
    "en": {
        "available": "AVAILABLE", "busy": "BUSY", "in_meeting": "IN A MEETING",
        "dnd": "DO NOT DISTURB", "away": "AWAY", "brb": "BE RIGHT BACK",
        "offline": "OFFLINE", "unknown": "UNKNOWN", "disconnected": "NO PC",
    },
    "it": {
        "available": "DISPONIBILE", "busy": "OCCUPATO", "in_meeting": "IN RIUNIONE",
        "dnd": "NON DISTURBARE", "away": "ASSENTE", "brb": "TORNO SUBITO",
        "offline": "NON IN LINEA", "unknown": "SCONOSCIUTO", "disconnected": "NESSUN PC",
    },
}


def status_colour(status: str) -> tuple[int, int, int]:
    """Fallback background, matching the themes in firmware/src/status.cpp."""
    return {
        "available": (0x2E, 0xCC, 0x71),
        "busy": (0xE7, 0x4C, 0x3C),
        "in_meeting": (0x8E, 0x44, 0xAD),
        "dnd": (0xB0, 0x3A, 0x2E),
        "away": (0xF3, 0x9C, 0x12),
        "brb": (0xE6, 0x7E, 0x22),
        "offline": (0x7F, 0x8C, 0x8D),
        "unknown": (0x56, 0x65, 0x73),
        "disconnected": (0x34, 0x49, 0x5E),
    }[status]


def dim(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Scale a colour towards black. Mirrors dimColour() in firmware/src/mascot.cpp."""
    return tuple(max(0, min(255, round(channel * factor))) for channel in rgb)  # type: ignore[return-value]


def contrast_on(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever reads better on *rgb*. Mirrors contrastOn() in display.cpp."""
    luma = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) // 1000
    return (0, 0, 0) if luma > 140 else (255, 255, 255)


def wrap_caption(text: str, measure, width: int, max_lines: int = CAPTION_MAX_LINES) -> list[str]:
    """Wrap for the caption band. See pc_app.text.wrap_caption for the algorithm."""
    return _wrap(text, measure, width, max_lines, CAPTION_PAD_X)


def preview_font(size: int = 14):
    """A font approximating the device fonts (TFT_eSPI font 2 ~14px, font 4 ~20px)."""
    for candidate in ("C:\\Windows\\Fonts\\consola.ttf", "C:\\Windows\\Fonts\\arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


#: Kept under the old private name for callers that predate this module.
_preview_font = preview_font


# -- caption band ----------------------------------------------------------------------------


def layout_caption(caption: str, width: int) -> tuple[list[str], int, int, bool]:
    """How the band will lay this caption out: lines, font size, line height, and whether it is
    truncated.

    Mirrors layoutCaptionBand() in firmware/src/display.cpp -- try the big font first and drop to
    the small one only when the caption would not fit its line budget, so a short phrase is drawn
    large and a long one stays whole.
    """
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    if not caption.strip():
        return [], CAPTION_FONT_SMALL, CAPTION_LINE_H_SMALL, False

    big = preview_font(CAPTION_FONT_BIG)
    probe = wrap_caption(
        caption, lambda text: scratch.textlength(text, font=big), width, CAPTION_MAX_LINES
    )
    if 0 < len(probe) <= CAPTION_LINES_BIG:
        return probe, CAPTION_FONT_BIG, CAPTION_LINE_H_BIG, False

    small = preview_font(CAPTION_FONT_SMALL)

    def measure(text: str) -> float:
        return scratch.textlength(text, font=small)

    lines = wrap_caption(caption, measure, width, CAPTION_LINES_SMALL)
    whole = wrap_caption(caption, measure, width, 99)
    return lines, CAPTION_FONT_SMALL, CAPTION_LINE_H_SMALL, len(whole) > len(lines)


def draw_caption_band(frame: Image.Image, caption: str) -> int:
    """Paint the bottom caption band onto *frame*. Returns the number of lines drawn."""
    width, height = frame.size
    draw = ImageDraw.Draw(frame)
    lines, size, line_h, _ = layout_caption(caption, width)
    if not lines:
        return 0
    font = preview_font(size)

    # Solid, not translucent: the device fills the band with TFT_BLACK.
    band_h = len(lines) * line_h + 2 * CAPTION_PAD_Y
    top = height - band_h
    draw.rectangle((0, top, width, height), fill=(0, 0, 0))

    y = top + CAPTION_PAD_Y
    for line in lines:
        draw.text((CAPTION_PAD_X, y), line, font=font, fill=WHITE)
        y += line_h
    return len(lines)


def caption_line_count(caption: str, width: int) -> int:
    """How many band lines *caption* needs, in whichever font it ends up using."""
    return len(layout_caption(caption, width)[0])


# -- image mode ------------------------------------------------------------------------------


def render_image_frame(base: Image.Image, caption: str) -> Image.Image | None:
    """Compose meme + caption band exactly as the device will."""
    frame = base.copy().convert("RGB")
    if not draw_caption_band(frame, caption):
        return None
    return frame


# -- text mode -------------------------------------------------------------------------------


def draw_status_badge(
    draw: ImageDraw.ImageDraw, status: str, cx: int, cy: int, r: int, disc, glyph
) -> None:
    """The Teams-style presence badge: a filled disc with the glyph knocked out of it.

    Inverted relative to Teams and to the tray icon, and it has to be -- in text mode the
    background is already the status colour, so a status-coloured disc would be invisible. The
    disc takes the caption's colour and the glyph is punched out in the background colour. The
    shapes are the same vocabulary as make_icon() in pc_app/tray.py, scaled to *r*.

    Mirrors drawStatusBadge() in firmware/src/display.cpp.
    """
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=disc)
    thick = max(2, round(r * 0.21))

    if status == "available":  # tick
        draw.line(
            [(cx - r * 0.43, cy + r * 0.04), (cx - r * 0.11, cy + r * 0.39),
             (cx + r * 0.46, cy - r * 0.36)],
            fill=glyph, width=thick, joint="curve",
        )
    elif status == "dnd":  # minus
        bar = max(2, round(r * 0.14))
        draw.rounded_rectangle(
            (cx - r * 0.5, cy - bar, cx + r * 0.5, cy + bar), radius=2, fill=glyph
        )
    elif status == "in_meeting":  # play triangle
        draw.polygon(
            [(cx - r * 0.21, cy - r * 0.43), (cx - r * 0.21, cy + r * 0.43), (cx + r * 0.5, cy)],
            fill=glyph,
        )
    elif status == "busy":  # solid dot
        d = r * 0.36
        draw.ellipse((cx - d, cy - d, cx + d, cy + d), fill=glyph)
    elif status in ("away", "brb"):  # clock
        face = r * 0.5
        hand = max(2, round(r * 0.12))
        draw.ellipse((cx - face, cy - face, cx + face, cy + face), outline=glyph, width=hand)
        draw.line((cx, cy, cx, cy - face * 0.75), fill=glyph, width=hand)
        draw.line((cx, cy, cx + face * 0.6, cy), fill=glyph, width=hand)
    elif status == "offline":  # cross
        d = r * 0.36
        draw.line((cx - d, cy - d, cx + d, cy + d), fill=glyph, width=thick)
        draw.line((cx + d, cy - d, cx - d, cy + d), fill=glyph, width=thick)
    else:  # unknown / disconnected: a hollow ring
        d = r * 0.43
        draw.ellipse(
            (cx - d, cy - d, cx + d, cy + d), outline=glyph, width=max(2, round(r * 0.18))
        )


def render_text_frame(status: str, caption: str, size: tuple[int, int]) -> Image.Image:
    """The caption alone on the status colour, no image involved.

    Mirrors drawTextScene()/layoutTextMode() in firmware/src/display.cpp.
    """
    width, height = size
    background = status_colour(status)
    foreground = contrast_on(background)

    frame = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(frame)
    font = preview_font(20)

    # The badge replaces the status name: the colour already says which status this is.
    draw_status_badge(
        draw, status, width // 2, TEXT_BADGE_CY, TEXT_BADGE_R, foreground, background
    )
    draw.line((24, TEXT_RULE_Y, width - 24, TEXT_RULE_Y), fill=foreground)

    lines = wrap_caption(
        caption,
        lambda text: draw.textlength(text, font=font),
        width - 2 * TEXT_PAD_X + 2 * CAPTION_PAD_X,
        max_lines=TEXT_MAX_LINES,
    )
    top = TEXT_TOP + (height - TEXT_TOP - len(lines) * TEXT_LINE_H) // 2
    for index, line in enumerate(lines):
        draw.text(
            (width // 2, top + index * TEXT_LINE_H + TEXT_LINE_H // 2),
            line,
            font=font,
            fill=foreground,
            anchor="mm",
        )
    return frame


# -- mascot mode -----------------------------------------------------------------------------


def mascot_box(size: tuple[int, int]) -> tuple[int, int, int]:
    """Where the mascot sits: (left, top, side). Mirrors mascotBox() in firmware/src/mascot.cpp.

    The caption band is reserved first, so the character never sits under its own words.
    """
    width, height = size
    usable_h = height - CAPTION_RESERVE
    side = min(width, usable_h) - 2 * MASCOT_MARGIN
    side = max(60, min(side, MASCOT_MAX_SIZE))
    return (width - side) // 2, (usable_h - side) // 2, side


def _draw_mascot(
    draw: ImageDraw.ImageDraw, box: tuple[int, int, int], face: Face, status: str
) -> None:
    """Draw the character. Coordinates are fractions of the box side, so the firmware can use the
    same numbers at whatever size its sprite ends up being."""
    left, top, s = box

    def px(fx: float, fy: float) -> tuple[float, float]:
        return left + fx * s, top + fy * s

    radius = int(0.28 * s)
    draw.rounded_rectangle((left, top, left + s, top + s), radius=radius, fill=MASCOT_BODY)
    # A lighter band across the top reads as a light source without needing a gradient.
    draw.rounded_rectangle(
        (left + 0.06 * s, top + 0.05 * s, left + 0.94 * s, top + 0.36 * s),
        radius=int(0.18 * s),
        fill=MASCOT_HIGHLIGHT,
    )

    # The T mark. Evokes Teams without reproducing its logo.
    draw.rectangle((*px(0.30, 0.09), *px(0.70, 0.145)), fill=WHITE)
    draw.rectangle((*px(0.465, 0.09), *px(0.535, 0.25)), fill=WHITE)

    eye_rx = 0.085 * s
    eye_ry = max(0.012 * s, eye_rx * face.eye_open / 100)
    for fx in (0.34, 0.66):
        cx, cy = px(fx, 0.47)
        draw.ellipse((cx - eye_rx, cy - eye_ry, cx + eye_rx, cy + eye_ry), fill=WHITE)
        if face.eye_open > 12:
            pr = min(eye_rx * 0.55, eye_ry * 0.85)
            draw.ellipse((cx - pr, cy - pr, cx + pr, cy + pr), fill=MASCOT_PUPIL)

    # Brows. A negative tilt drops the *inner* ends towards the nose, which is what reads as
    # furrowed; a positive one lifts them, which reads as surprised. brow_asym flattens the left
    # brow so only the right one lifts -- the clearest "really?" cue available at this size.
    brow_w = 0.16 * s
    for index, fx in enumerate((0.34, 0.66)):
        cx, cy = px(fx, 0.33)
        tilt = -face.brow_tilt // 3 if (face.brow_asym and index == 0) else face.brow_tilt
        lift = tilt / 100 * 0.05 * s
        inner_y, outer_y = cy - lift, cy + lift
        # The left brow's inner end is the one on its right, and vice versa.
        left_y, right_y = (outer_y, inner_y) if index == 0 else (inner_y, outer_y)
        draw.line(
            (cx - brow_w / 2, left_y, cx + brow_w / 2, right_y),
            fill=MASCOT_SHADOW,
            width=max(2, int(0.03 * s)),
        )

    if face.blush:
        br = 0.06 * s
        for fx in (0.22, 0.78):
            cx, cy = px(fx, 0.60)
            draw.ellipse((cx - br, cy - br * 0.6, cx + br, cy + br * 0.6), fill=MASCOT_BLUSH)

    _draw_mouth(draw, box, face)
    _draw_badge(draw, box, status)


def _draw_mouth(draw: ImageDraw.ImageDraw, box: tuple[int, int, int], face: Face) -> None:
    left, top, s = box
    cx = left + 0.5 * s
    cy = top + 0.68 * s
    half_w = 0.13 * s
    curve = face.mouth_curve / 100 * 0.09 * s
    thickness = max(2, int(0.028 * s))

    if face.mouth_open > 0:
        # An open mouth is a filled ellipse; the curve nudges it up or down a little.
        oh = 0.02 * s + 0.05 * s * face.mouth_open / 100
        draw.ellipse(
            (cx - half_w * 0.8, cy - oh / 2 + curve / 3, cx + half_w * 0.8, cy + oh / 2 + curve / 3),
            fill=MASCOT_PUPIL,
        )
        return

    if abs(curve) < 1:
        draw.line((cx - half_w, cy, cx + half_w, cy), fill=MASCOT_PUPIL, width=thickness)
        return

    # An arc off the bottom or top of an ellipse, so a smile and a frown are the same code.
    if curve > 0:
        draw.arc(
            (cx - half_w, cy - curve, cx + half_w, cy + curve),
            start=0, end=180, fill=MASCOT_PUPIL, width=thickness,
        )
    else:
        draw.arc(
            (cx - half_w, cy + curve, cx + half_w, cy - curve),
            start=180, end=360, fill=MASCOT_PUPIL, width=thickness,
        )


def _draw_badge(draw: ImageDraw.ImageDraw, box: tuple[int, int, int], status: str) -> None:
    """A presence dot in the corner, echoing the one Teams puts on your avatar.

    This is what keeps the real status readable when the tone has the face saying otherwise.
    """
    left, top, s = box
    cx, cy = left + 0.87 * s, top + 0.87 * s
    r = 0.14 * s
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE)
    r *= 0.72
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=status_colour(status))


def render_mascot_frame(
    status: str, tone: str, caption: str, size: tuple[int, int]
) -> Image.Image:
    """The mascot on the status colour, with the caption in the band along the bottom."""
    frame = Image.new("RGB", size, dim(status_colour(status), MASCOT_BACKDROP))
    draw = ImageDraw.Draw(frame)
    _draw_mascot(draw, mascot_box(size), face_for(status, tone), status)
    draw_caption_band(frame, caption)
    return frame


# -- file-writing wrappers, used by tools/build_memes.py --------------------------------------


def _save(frame: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.save(destination, format="PNG")


def render_preview(base: Image.Image, caption: str, destination: Path) -> None:
    frame = render_image_frame(base, caption)
    if frame is not None:
        _save(frame, destination)


def render_text_preview(
    status: str, caption: str, size: tuple[int, int], destination: Path
) -> None:
    _save(render_text_frame(status, caption, size), destination)


def render_mascot_preview(
    status: str, tone: str, caption: str, size: tuple[int, int], destination: Path
) -> None:
    _save(render_mascot_frame(status, tone, caption, size), destination)
