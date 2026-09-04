"""Build the LittleFS payload for the ESP32: meme JPEGs, caption banks and per-status indexes.

    python tools/build_memes.py                 # build firmware/data/
    python tools/build_memes.py --preview       # also render what the screen will look like
    python tools/build_memes.py --clean         # drop the previous build first

Drop your own images into memes/<status>/ (png, jpg, webp, bmp, gif) and re-run. Sources are
never modified.

By default every image is built for BOTH screen orientations, so the board can be switched
between landscape and portrait at runtime. That costs twice the flash; use
--orientation landscape (or portrait) to build just the one you use and get the space back.

An image whose name ends in `.land.png` / `.port.png` (before the extension) is built for that
orientation only -- useful when a meme only crops well one way round.

Everything the firmware reads is plain text or JPEG -- deliberately no JSON, so the firmware
needs no JSON parser.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import unicodedata
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: pip install -r requirements-dev.txt")

REPO = Path(__file__).resolve().parent.parent
MEME_SRC = REPO / "memes"
CAPTION_SRC = REPO / "captions"
DATA_OUT = REPO / "firmware" / "data"
PREVIEW_OUT = REPO / "preview"

#: Statuses with their own folder. Must match docs/PROTOCOL.md and firmware/src/status.cpp.
STATUSES = (
    "available",
    "busy",
    "in_meeting",
    "dnd",
    "away",
    "brb",
    "offline",
    "unknown",
    "disconnected",
)

#: Caption languages. Must match kLanguages in firmware/src/status.cpp.
LANGUAGES = ("en", "it")

#: Screen orientations: on-device folder -> (width, height). Must match firmware/src/display.cpp.
ORIENTATIONS = {
    "land": (320, 240),
    "port": (240, 320),
}
#: Friendly names accepted on the command line and in the source-file suffix convention.
ORIENTATION_ALIASES = {
    "landscape": "land",
    "land": "land",
    "portrait": "port",
    "port": "port",
}

# Caption band, kept in sync with the kCaption* constants in firmware/src/display.h.
CAPTION_PAD_X = 6
CAPTION_PAD_Y = 6
CAPTION_LINE_H = 16
CAPTION_MAX_LINES = 3
#: Vertical space the caption band can take, so scene art can stay clear of it.
CAPTION_RESERVE = CAPTION_MAX_LINES * CAPTION_LINE_H + 2 * CAPTION_PAD_Y

# Text-mode layout, mirroring layoutTextMode()/drawTextScene() in firmware/src/display.cpp.
TEXT_PAD_X = 12
TEXT_LABEL_Y = 24
TEXT_RULE_Y = 44
TEXT_TOP = 52
TEXT_LINE_H = 26
TEXT_MAX_LINES = 8

SOURCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

#: Default 4MB ESP32 partition table gives ~1.5MB to the filesystem. Leave headroom for LittleFS
#: metadata and the caption files.
FS_BUDGET_BYTES = 1_400_000
DEFAULT_MAX_BYTES = 24_000
MIN_QUALITY = 40

#: The display font is ASCII only (TFT_eSPI font 2 carries 96 glyphs, 32-126), so accented
#: letters are folded to the apostrophe form Italian has always used on ASCII-only systems.
ACCENT_MAP = {
    "à": "a'", "è": "e'", "é": "e'", "ì": "i'", "ò": "o'", "ó": "o'", "ù": "u'",
    "À": "A'", "È": "E'", "É": "E'", "Ì": "I'", "Ò": "O'", "Ó": "O'", "Ù": "U'",
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...",
}


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


def to_display_ascii(text: str) -> tuple[str, list[str]]:
    """Fold text to what the display font can actually render.

    Returns the folded text plus any characters that had to be dropped, so the build can warn
    rather than silently printing question marks on the device.
    """
    out: list[str] = []
    lost: list[str] = []
    for char in text:
        if ord(char) < 128:
            out.append(char)
        elif char in ACCENT_MAP:
            out.append(ACCENT_MAP[char])
        else:
            # Last resort: strip the diacritic (é -> e) if that yields something printable.
            stripped = "".join(
                c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c)
            )
            if stripped and all(ord(c) < 128 for c in stripped):
                out.append(stripped)
            else:
                lost.append(char)
                out.append("?")
    return "".join(out), lost


def wrap_caption(text: str, measure, width: int, max_lines: int = CAPTION_MAX_LINES) -> list[str]:
    """Greedy word wrap by measured pixel width, mirroring wrapCaption() in display.cpp.

    Width rather than character count: TFT_eSPI font 2 is proportional, so counting characters
    would wrap in the wrong place. The preview font is not byte-identical to the device font, so
    a line may break one word differently -- close enough to judge layout, not a pixel contract.
    """
    max_width = width - 2 * CAPTION_PAD_X
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if measure(candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # A single word too wide for the line is hard-broken rather than left to overflow.
        while measure(word) > max_width and len(word) > 1:
            fit = len(word)
            while fit > 1 and measure(word[:fit]) > max_width:
                fit -= 1
            lines.append(word[:fit])
            word = word[fit:]
        current = word
    if current:
        lines.append(current)
    return lines[:max_lines]


def fit_image(image: Image.Image, mode: str, size: tuple[int, int]) -> Image.Image:
    """Resize to exactly *size*, either cropping to fill or letterboxing to fit."""
    target_w, target_h = size
    image = image.convert("RGB")
    src_w, src_h = image.size
    if src_w == 0 or src_h == 0:
        raise ValueError("image has zero size")

    if mode == "cover":
        scale = max(target_w / src_w, target_h / src_h)
        new = image.resize(
            (max(1, round(src_w * scale)), max(1, round(src_h * scale))), Image.LANCZOS
        )
        left = (new.width - target_w) // 2
        top = (new.height - target_h) // 2
        return new.crop((left, top, left + target_w, top + target_h))

    scale = min(target_w / src_w, target_h / src_h)
    new = image.resize((max(1, round(src_w * scale)), max(1, round(src_h * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", size, (0, 0, 0))
    canvas.paste(new, ((target_w - new.width) // 2, (target_h - new.height) // 2))
    return canvas


def encode_jpeg(image: Image.Image, destination: Path, max_bytes: int) -> int:
    """Write a baseline JPEG no larger than *max_bytes*, dropping quality until it fits.

    Baseline and 24-bit RGB are not optional: TJpg_Decoder on the ESP32 cannot decode
    progressive or greyscale JPEGs, and a bad file shows up as a blank screen rather than an
    error, so it is worth being strict here.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    quality = 85
    while True:
        image.save(
            destination,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=False,
            subsampling=2,  # 4:2:0
        )
        size = destination.stat().st_size
        if size <= max_bytes or quality <= MIN_QUALITY:
            return size
        quality -= 5


def _preview_font(size: int = 14):
    """A font approximating the device's TFT_eSPI fonts (font 2 ~14px, font 4 ~20px)."""
    for candidate in (r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def contrast_on(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever reads better on *rgb*. Mirrors contrastOn() in display.cpp."""
    luma = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) // 1000
    return (0, 0, 0) if luma > 140 else (255, 255, 255)


def render_text_preview(
    status: str, caption: str, size: tuple[int, int], label: str, destination: Path
) -> None:
    """Render text mode: the caption alone on the status colour, no image involved.

    Mirrors drawTextScene()/layoutTextMode() in firmware/src/display.cpp.
    """
    width, height = size
    background = status_colour(status)
    foreground = contrast_on(background)

    frame = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(frame)
    font = _preview_font(20)

    draw.text((width // 2, TEXT_LABEL_Y), label, font=font, fill=foreground, anchor="mm")
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

    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.save(destination, format="PNG")


def render_preview(base: Image.Image, caption: str, destination: Path) -> None:
    """Compose meme + caption band exactly as the device will, for eyeballing without hardware."""
    frame = base.copy().convert("RGB")
    width, height = frame.size
    draw = ImageDraw.Draw(frame)
    font = _preview_font()
    lines = wrap_caption(caption, lambda text: draw.textlength(text, font=font), width)
    if not lines:
        return

    # Solid, not translucent: the device fills the band with TFT_BLACK.
    band_h = len(lines) * CAPTION_LINE_H + 2 * CAPTION_PAD_Y
    top = height - band_h
    draw.rectangle((0, top, width, height), fill=(0, 0, 0))

    y = top + CAPTION_PAD_Y
    for line in lines:
        draw.text((CAPTION_PAD_X, y), line, font=font, fill=(255, 255, 255))
        y += CAPTION_LINE_H

    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.save(destination, format="PNG")


def source_orientation(path: Path) -> str | None:
    """The orientation a source file is restricted to, from a `name.land.png` style suffix.

    None means "build it for every orientation".
    """
    stem_suffix = path.stem.rsplit(".", 1)
    if len(stem_suffix) == 2:
        return ORIENTATION_ALIASES.get(stem_suffix[1].lower())
    return None


def source_images(status: str, orientation: str) -> list[Path]:
    """Sources for one status that apply to *orientation*."""
    folder = MEME_SRC / status
    if not folder.is_dir():
        return []
    usable = [
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
        and source_orientation(p) in (None, orientation)
    ]
    return sorted(usable, key=lambda p: p.name.lower())


def build(args: argparse.Namespace) -> int:
    orientations = (
        list(ORIENTATIONS)
        if args.orientation == "both"
        else [ORIENTATION_ALIASES[args.orientation]]
    )

    if args.clean:
        for path in (DATA_OUT / "memes", DATA_OUT / "captions", PREVIEW_OUT):
            if path.exists():
                shutil.rmtree(path)
                print(f"removed {path.relative_to(REPO)}")

    total = 0
    counts: dict[tuple[str, str], int] = {}
    problems: list[str] = []

    for orientation in orientations:
        size = ORIENTATIONS[orientation]
        for status in STATUSES:
            sources = source_images(status, orientation)
            out_dir = DATA_OUT / "memes" / orientation / status
            names: list[str] = []

            for index, source in enumerate(sources):
                try:
                    with Image.open(source) as raw:
                        raw.load()
                        fitted = fit_image(raw, args.fit, size)
                except Exception as exc:
                    problems.append(f"{source.relative_to(REPO)} [{orientation}]: {exc}")
                    continue

                name = f"{index:02d}.jpg"
                total += encode_jpeg(fitted, out_dir / name, args.max_bytes)
                names.append(name)

                if args.preview and args.preview_mode in ("image", "both"):
                    caption = _first_caption(status, args.preview_lang)
                    render_preview(
                        fitted,
                        caption,
                        PREVIEW_OUT / f"{orientation}_{status}_{index:02d}.png",
                    )

            counts[(orientation, status)] = len(names)
            if names:
                out_dir.mkdir(parents=True, exist_ok=True)
                # newline="\n" so Windows does not write CRLF: the device reads these byte for
                # byte and every stray \r is wasted flash.
                (out_dir / "index.txt").write_text(
                    "\n".join(names) + "\n", encoding="ascii", newline="\n"
                )

        # Text mode ignores memes entirely, so it gets one preview per status.
        if args.preview and args.preview_mode in ("text", "both"):
            for status in STATUSES:
                render_text_preview(
                    status,
                    _first_caption(status, args.preview_lang),
                    size,
                    STATUS_LABELS[args.preview_lang][status],
                    PREVIEW_OUT / f"{orientation}_text_{status}.png",
                )

        # Fallback previews so every status can be reviewed even with no memes at all.
        if args.preview and args.preview_mode in ("image", "both"):
            for status in STATUSES:
                if counts.get((orientation, status)):
                    continue
                base = Image.new("RGB", size, status_colour(status))
                render_preview(
                    base,
                    _first_caption(status, args.preview_lang),
                    PREVIEW_OUT / f"{orientation}_{status}_fallback.png",
                )

    caption_bytes, caption_problems = _copy_captions()
    total += caption_bytes
    problems.extend(caption_problems)

    _report(orientations, counts, total, problems, args)
    return 1 if problems or total > FS_BUDGET_BYTES else 0


def _first_caption(status: str, language: str) -> str:
    path = CAPTION_SRC / language / f"{status}.txt"
    if not path.exists():
        return status.replace("_", " ").upper()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return to_display_ascii(line.strip())[0]
    return status.replace("_", " ").upper()


def _copy_captions() -> tuple[int, list[str]]:
    """Copy every language's caption bank into the filesystem image, folded to display ASCII."""
    written = 0
    problems: list[str] = []
    for language in LANGUAGES:
        out_dir = DATA_OUT / "captions" / language
        out_dir.mkdir(parents=True, exist_ok=True)
        for status in STATUSES:
            source = CAPTION_SRC / language / f"{status}.txt"
            if not source.exists():
                problems.append(f"missing caption bank: captions/{language}/{status}.txt")
                continue
            lines: list[str] = []
            for raw in source.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                folded, lost = to_display_ascii(raw)
                if lost:
                    problems.append(
                        f"captions/{language}/{status}.txt: cannot display {''.join(sorted(set(lost)))!r}"
                    )
                lines.append(folded)
            target = out_dir / f"{status}.txt"
            target.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
            written += target.stat().st_size
    return written, problems


def _report(orientations, counts, total, problems, args) -> None:
    print()
    header = "status".ljust(14) + "".join(o.rjust(9) for o in orientations)
    print(header)
    print("-" * len(header))
    for status in STATUSES:
        row = status.ljust(14)
        for orientation in orientations:
            row += str(counts.get((orientation, status), 0)).rjust(9)
        if not any(counts.get((o, status), 0) for o in orientations):
            row += "   (built-in fallback scene)"
        print(row)

    pct = 100 * total / FS_BUDGET_BYTES
    print()
    print(f"payload: {total:,} bytes of a {FS_BUDGET_BYTES:,} byte budget ({pct:.1f}%)")
    if len(orientations) > 1:
        print("building both orientations; --orientation landscape halves this if space runs short")
    if total > FS_BUDGET_BYTES:
        print("OVER BUDGET -- remove some memes or lower --max-bytes", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
    if args.preview:
        print(f"previews ({args.preview_lang}) written to {PREVIEW_OUT.relative_to(REPO)}/")
    if not any(counts.values()):
        print("\nNo source images found. Drop images into memes/<status>/ and re-run;")
        print("until then the firmware draws its built-in fallback scene for every status.")
    if problems or total > FS_BUDGET_BYTES:
        # Do not invite a flash of a payload we know is incomplete or oversized.
        print("\nFix the problems above and re-run before flashing.", file=sys.stderr)
    else:
        print("\nNext: cd firmware && pio run -t uploadfs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--orientation",
        choices=("both", "landscape", "portrait"),
        default="both",
        help="which screen orientations to build for (default: both)",
    )
    parser.add_argument(
        "--fit",
        choices=("cover", "contain"),
        default="cover",
        help="cover crops to fill the screen (default); contain letterboxes",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"per-image size cap, quality is reduced to meet it (default {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument("--preview", action="store_true", help="render composed frames as PNGs")
    parser.add_argument(
        "--preview-lang",
        choices=LANGUAGES,
        default="en",
        help="caption language used in the previews (default: en)",
    )
    parser.add_argument(
        "--preview-mode",
        choices=("image", "text", "both"),
        default="both",
        help="which display mode(s) to preview (default: both)",
    )
    parser.add_argument("--clean", action="store_true", help="delete the previous build first")
    return build(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
