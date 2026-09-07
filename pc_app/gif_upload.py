"""Prepare a GIF for the board, and push it there over the serial link.

Two halves, both needed by the packaged .exe -- which is why this lives under pc_app/ rather than
tools/, alongside the other code the asset pipeline and the tray app share.

**Preparation.** The user's file is always re-encoded, never sent as-is. The reason is specific:
AnimatedGIF on the device keeps no canvas, so a GIF with *local* colour palettes or inter-frame
disposal renders wrong -- the decoder is handed one scanline at a time and has nothing to
composite against. Re-encoding to full frames sharing one global palette removes that whole class
of artefact, and shrinks the file enough to be worth sending over a 115200 line at the same time.

**Upload.** Base64 over the existing line protocol rather than raw binary, so the firmware's line
framing is untouched and the wire stays human-readable. See docs/PROTOCOL.md.
"""

from __future__ import annotations

import base64
import logging
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    from PIL import Image, ImageSequence
except ImportError:  # pragma: no cover - the GUI degrades to "no upload" without Pillow
    Image = None  # type: ignore[assignment]
    ImageSequence = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Largest canvas the board will be sent. Square, so one asset can be centred in either
#: orientation without a per-orientation build. Fits inside both 240x320 and 320x240.
MAX_SIZE = (240, 240)

#: Ceiling on frames. A long GIF costs upload seconds and buys nothing: the alert is on screen
#: for a few seconds and then gone.
MAX_FRAMES = 40

#: Must match kMaxGifBytes in firmware/src/alert.h.
MAX_BYTES = 300 * 1024

#: Palette sizes tried in order when the encode comes out too big. 256 first, because a smaller
#: palette is a visible cost and most GIFs fit at full colour once they have been resized.
PALETTE_STEPS = (256, 128, 64, 32)

#: Raw bytes per chunk. 96 bytes -> 128 base64 characters; with the "GIFDATA:" prefix that is
#: 136, comfortably inside the firmware's 160-character line limit (kMaxLine in serial_link.cpp).
CHUNK_BYTES = 96

#: Inter-frame delay for a source that does not declare one.
DEFAULT_FRAME_MS = 100


@dataclass(frozen=True)
class GifStats:
    """What the re-encoder produced, for the GUI to report and the tests to assert on."""

    width: int
    height: int
    frames: int
    colours: int
    size_bytes: int
    dropped_frames: int


class GifTooBig(ValueError):
    """The GIF could not be squeezed under the byte cap without becoming pointless."""


def _read_frames(source: Path) -> tuple[list, list[int]]:
    """Every frame of *source* as a full RGB image, plus its duration in milliseconds.

    Seeking a GIF in Pillow composites each frame onto the running canvas, so converting after
    the seek is what turns a delta-encoded source into the full frames the device needs.
    """
    frames = []
    durations: list[int] = []
    with Image.open(source) as raw:
        for frame in ImageSequence.Iterator(raw):
            durations.append(int(frame.info.get("duration") or DEFAULT_FRAME_MS))
            frames.append(frame.convert("RGB"))
    if not frames:
        raise ValueError("no frames in the image")
    return frames, durations


def _thin(frames: list, durations: list[int], limit: int) -> tuple[list, list[int]]:
    """Keep at most *limit* frames, spread evenly, folding dropped time into the survivors.

    Dropping every other frame would halve the playback speed; adding a dropped frame's duration
    to the one that replaces it keeps the animation running at its original pace.
    """
    if len(frames) <= limit:
        return frames, durations

    keep = [round(i * (len(frames) - 1) / (limit - 1)) for i in range(limit)] if limit > 1 else [0]
    kept_frames = []
    kept_durations: list[int] = []
    for position, index in enumerate(keep):
        end = keep[position + 1] if position + 1 < len(keep) else len(frames)
        kept_frames.append(frames[index])
        kept_durations.append(sum(durations[index:end]) or DEFAULT_FRAME_MS)
    return kept_frames, kept_durations


def _fit(frame, size: tuple[int, int]):
    """Scale down to fit inside *size*, preserving aspect. Never scales up."""
    scale = min(size[0] / frame.width, size[1] / frame.height, 1.0)
    if scale >= 1.0:
        return frame
    width = max(1, round(frame.width * scale))
    height = max(1, round(frame.height * scale))
    return frame.resize((width, height), Image.LANCZOS)


def shared_palette(frames: list, colours: int):
    """A single palette chosen across *every* frame, as a quantised image to map others onto.

    Deriving it from the first frame alone is the obvious shortcut and it is wrong: a colour that
    only appears later -- a red badge that blinks on, a character who turns round -- would have no
    entry, and every pixel of it would snap to the nearest colour that happens to be in frame one.
    Tiling the frames first means the palette sees the whole animation.
    """
    width, height = frames[0].size
    tile = Image.new("RGB", (width, height * len(frames)))
    for index, frame in enumerate(frames):
        tile.paste(frame, (0, index * height))
    return tile.quantize(colors=colours, method=Image.MEDIANCUT)


def _quantise(frames: list, colours: int) -> tuple[list, bytes]:
    """Put every frame on one shared palette, and return that palette alongside them.

    Mapping the frames is only half of it. Pillow decides per frame whether to write a *local*
    colour table, and it writes one for every frame after the first unless save() is handed an
    explicit `palette` -- even when the frames already share one. A local table is the single
    thing the device cannot render, so the palette has to come back out of here and be passed on.
    """
    base = shared_palette(frames, colours)
    mapped = [frame.quantize(palette=base, dither=Image.FLOYDSTEINBERG) for frame in frames]
    # Padded to a full 768 bytes: getpalette() stops at the colours actually used.
    raw = bytes(base.getpalette() or b"")
    return mapped, raw.ljust(768, b"\x00")


def _save(frames: list, durations: list[int], destination: Path, palette: bytes) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        destination,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        # Naming the palette is what stops Pillow writing a local colour table per frame.
        palette=palette,
        # Disposal 1 ("leave the frame in place") is the only method that needs no canvas on the
        # decoder side. Every frame is already full, so there is nothing to restore between them.
        disposal=1,
        optimize=False,
    )
    return destination.stat().st_size


def prepare_gif(
    source: Path | str,
    destination: Path | str,
    max_size: tuple[int, int] = MAX_SIZE,
    max_frames: int = MAX_FRAMES,
    max_bytes: int = MAX_BYTES,
) -> GifStats:
    """Re-encode *source* into something the board can actually play.

    Shed frames first and colours only after that: a shorter loop still reads as the same
    animation, while a banded palette reads as a broken picture.
    """
    if Image is None:  # pragma: no cover
        raise RuntimeError("Pillow is required to prepare a GIF: pip install pillow")

    source = Path(source)
    destination = Path(destination)

    original, durations = _read_frames(source)
    frames = [_fit(frame, max_size) for frame in original]
    frames, durations = _thin(frames, durations, max_frames)
    dropped = len(original) - len(frames)

    for colours in PALETTE_STEPS:
        for limit in (len(frames), max(2, len(frames) // 2), 2):
            attempt, attempt_durations = _thin(frames, durations, limit)
            mapped, palette = _quantise(attempt, colours)
            size = _save(mapped, attempt_durations, destination, palette)
            if size <= max_bytes:
                stats = GifStats(
                    width=attempt[0].width,
                    height=attempt[0].height,
                    frames=len(attempt),
                    colours=colours,
                    size_bytes=size,
                    dropped_frames=dropped + (len(frames) - len(attempt)),
                )
                log.info(
                    "prepared %s: %dx%d, %d frames, %d colours, %d bytes",
                    source.name, stats.width, stats.height, stats.frames, colours, size,
                )
                return stats

    destination.unlink(missing_ok=True)
    raise GifTooBig(
        f"{source.name} will not fit in {max_bytes:,} bytes even at "
        f"{PALETTE_STEPS[-1]} colours and 2 frames"
    )


# -- the wire ------------------------------------------------------------------------------


def crc32(data: bytes) -> int:
    """The checksum the firmware verifies before it commits an upload."""
    return zlib.crc32(data) & 0xFFFFFFFF


def upload_lines(data: bytes, chunk_bytes: int = CHUNK_BYTES) -> Iterator[str]:
    """The complete command sequence for one upload, ready to be written line by line."""
    yield f"GIFBEGIN:{len(data)}:{crc32(data)}"
    for start in range(0, len(data), chunk_bytes):
        yield "GIFDATA:" + base64.b64encode(data[start:start + chunk_bytes]).decode("ascii")
    yield "GIFEND"


def chunk_count(size_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> int:
    """How many GIFDATA lines an upload of *size_bytes* takes. For the progress bar."""
    return (size_bytes + chunk_bytes - 1) // chunk_bytes


def estimate_seconds(size_bytes: int, baud: int = 115200) -> float:
    """Rough wall-clock for an upload, so the GUI can warn before starting a slow one."""
    # 4 base64 characters per 3 bytes, plus the prefix and the newline, at 10 bits per character.
    lines = chunk_count(size_bytes)
    on_the_wire = lines * (len("GIFDATA:") + 1) + (size_bytes * 4 + 2) // 3
    return on_the_wire * 10 / baud


class UploadFailed(RuntimeError):
    """The board refused or could not finish the transfer. The old GIF is still intact."""


#: Chunks sent before pausing for an acknowledgement. Must match kUploadAckEvery in
#: firmware/src/alert.h: the window exists so the transfer stays inside the board's serial RX
#: buffer while LittleFS is being written, and a PC that ran ahead would overflow it.
ACK_EVERY = 16

#: How long to wait for one acknowledgement before giving up on the transfer.
ACK_TIMEOUT = 15.0


def _await_ack(link, deadline_after: float, sleep) -> str:
    """Block until the board acknowledges, and return the line it sent.

    Any other traffic -- LOG:, EVT:NEXT, a stray PONG -- is dropped on the floor. The worker
    would normally read those, but for the seconds an upload takes this loop owns the port.
    """
    import time as _time

    deadline = _time.monotonic() + deadline_after
    while _time.monotonic() < deadline:
        for line in link.read_lines():
            if line.startswith("EVT:GIFERR"):
                raise UploadFailed(line.partition(":GIFERR")[2].lstrip(":") or "refused")
            if line.startswith(("EVT:GIFACK", "EVT:GIFOK")):
                return line
            log.debug("board (during upload): %s", line)
        sleep(0.01)
    raise UploadFailed("the board stopped acknowledging")


def send_gif(link, data: bytes, on_progress=None, ack_every: int = ACK_EVERY) -> None:
    """Push *data* to the board over *link*, raising UploadFailed if it does not land.

    Must run on whichever thread owns the serial port -- the worker's, in this app.

    *on_progress* is called with (bytes_sent, total) as the window advances, never more often
    than once per window, so a GUI can drive a progress bar without being flooded.
    """
    import time as _time

    if not data:
        raise UploadFailed("nothing to send")
    if len(data) > MAX_BYTES:
        raise UploadFailed(f"{len(data):,} bytes is over the {MAX_BYTES:,} byte limit")

    lines = list(upload_lines(data))
    begin, chunks, end = lines[0], lines[1:-1], lines[-1]

    if not link.send(begin):
        raise UploadFailed("could not reach the board")
    _await_ack(link, ACK_TIMEOUT, _time.sleep)

    sent = 0
    for start in range(0, len(chunks), ack_every):
        window = chunks[start:start + ack_every]
        for line in window:
            if not link.send(line):
                raise UploadFailed("the link dropped mid-transfer")
        sent = min(len(data), (start + len(window)) * CHUNK_BYTES)
        # A full window is always acknowledged; a short final one is picked up by GIFEND.
        if len(window) == ack_every:
            _await_ack(link, ACK_TIMEOUT, _time.sleep)
        if on_progress is not None:
            on_progress(sent, len(data))

    if not link.send(end):
        raise UploadFailed("the link dropped before the transfer was committed")
    result = _await_ack(link, ACK_TIMEOUT, _time.sleep)
    if not result.startswith("EVT:GIFOK"):
        raise UploadFailed(f"unexpected reply {result!r}")
    if on_progress is not None:
        on_progress(len(data), len(data))
