"""Build the screenshots used in README.md.

    python tools/make_docs_images.py

Composes the frames that build_memes.py --preview renders into labelled figures under
docs/images/. Unlike preview/, those are committed, so the README shows something even before
anyone clones the repo.

These are renders of what the firmware draws, not photographs of the panel -- same layout, same
colours, same caption wrapping, but a desktop font stands in for the TFT_eSPI one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
PREVIEW = REPO / "preview"
OUT = REPO / "docs" / "images"

BG = (0x14, 0x16, 0x1A)
LABEL = (0x9A, 0xA4, 0xB2)
BEZEL = (0x30, 0x35, 0x3D)

PAD = 18
BEZEL_W = 6
LABEL_H = 26


def font(size: int):
    for candidate in (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def framed(image: Image.Image) -> Image.Image:
    """Wrap a screen render in a bezel so it reads as a display rather than a flat picture."""
    w, h = image.size
    out = Image.new("RGB", (w + BEZEL_W * 2, h + BEZEL_W * 2), BG)
    ImageDraw.Draw(out).rounded_rectangle(
        (0, 0, out.width - 1, out.height - 1), radius=8, fill=BEZEL
    )
    out.paste(image, (BEZEL_W, BEZEL_W))
    return out


def figure(sources: list[tuple[Path, str]], destination: Path) -> None:
    """A row of framed screens, each with a caption underneath."""
    frames = [(framed(Image.open(path)), label) for path, label in sources]
    cell_w = max(f.width for f, _ in frames)
    cell_h = max(f.height for f, _ in frames)

    width = PAD + len(frames) * (cell_w + PAD)
    height = PAD + cell_h + LABEL_H + PAD // 2
    sheet = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(sheet)
    label_font = font(15)

    for index, (frame, label) in enumerate(frames):
        x = PAD + index * (cell_w + PAD) + (cell_w - frame.width) // 2
        sheet.paste(frame, (x, PAD))
        draw.text(
            (PAD + index * (cell_w + PAD) + cell_w // 2, PAD + cell_h + LABEL_H // 2),
            label,
            font=label_font,
            fill=LABEL,
            anchor="mm",
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, format="PNG", optimize=True)
    kb = destination.stat().st_size / 1024
    print(f"wrote {destination.relative_to(REPO)}  ({sheet.width}x{sheet.height}, {kb:.0f} KB)")


def build_previews() -> None:
    """Regenerate preview/ so the figures reflect whatever memes are currently in memes/."""
    # English first, its frames set aside under an en__ prefix; Italian second so the plainly
    # named frames are the Italian ones the main figures use. Each run overwrites the last.
    for language in ("en", "it"):
        subprocess.check_call(
            [sys.executable, str(REPO / "tools" / "build_memes.py"),
             "--preview", "--preview-lang", language],
            stdout=subprocess.DEVNULL,
        )
        if language == "en":
            for source in sorted(PREVIEW.glob("*.png")):
                if source.name.startswith(("_", "en__")):
                    continue
                source.replace(source.with_name(f"en__{source.name}"))


def build_mascot_previews() -> None:
    """One set of mascot frames per tone, so the tone figure can put them side by side.

    Same trick as build_previews uses for languages: render, then move the frames aside under a
    prefix before the next run overwrites them.
    """
    for tone in ("normal", "sarcastic", "retriever"):
        subprocess.check_call(
            [sys.executable, str(REPO / "tools" / "build_memes.py"),
             "--preview", "--preview-mode", "mascot", "--preview-lang", "it",
             "--preview-tone", tone],
            stdout=subprocess.DEVNULL,
        )
        for source in sorted(PREVIEW.glob("*_mascot_*.png")):
            if not source.name.startswith(("land_", "port_")):
                continue  # already carries a language or tone prefix
            source.replace(source.with_name(f"{tone}__{source.name}"))


def main() -> int:
    build_previews()
    build_mascot_previews()

    # Default look: portrait, Italian, image mode.
    figure(
        [
            (PREVIEW / "port_in_meeting_00.png", "In riunione"),
            (PREVIEW / "port_dnd_00.png", "Non disturbare"),
            (PREVIEW / "port_available_00.png", "Disponibile"),
        ],
        OUT / "portrait-image-mode.png",
    )

    # Mascot mode: the animated character, drawn by the firmware rather than flashed as art.
    figure(
        [
            (PREVIEW / "normal__port_mascot_in_meeting.png", "In riunione"),
            (PREVIEW / "normal__port_mascot_dnd.png", "Non disturbare"),
            (PREVIEW / "normal__port_mascot_available.png", "Disponibile"),
        ],
        OUT / "portrait-mascot-mode.png",
    )

    # The tones: one status, three registers. The badge stays green throughout, which is the
    # whole point -- the face and the words change, the truth does not.
    figure(
        [
            (PREVIEW / "normal__port_mascot_available.png", "Normale"),
            (PREVIEW / "sarcastic__port_mascot_available.png", "Sarcasmo"),
            (PREVIEW / "retriever__port_mascot_available.png", "Golden Retriever"),
        ],
        OUT / "tones.png",
    )

    # Text-only mode: no images, background is the status colour.
    figure(
        [
            (PREVIEW / "port_text_in_meeting.png", "In riunione"),
            (PREVIEW / "port_text_dnd.png", "Non disturbare"),
            (PREVIEW / "port_text_available.png", "Disponibile"),
        ],
        OUT / "portrait-text-mode.png",
    )

    # Landscape, the other orientation.
    figure(
        [
            (PREVIEW / "land_busy_00.png", "Occupato"),
            (PREVIEW / "land_away_00.png", "Assente"),
        ],
        OUT / "landscape-image-mode.png",
    )

    # Both languages, so the option is visible rather than just described.
    figure(
        [
            (PREVIEW / "en__port_text_in_meeting.png", "English"),
            (PREVIEW / "port_text_in_meeting.png", "Italiano"),
        ],
        OUT / "languages.png",
    )

    print("\nFigures are renders of what the firmware draws, not photos of the panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
