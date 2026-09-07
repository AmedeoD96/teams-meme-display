"""Tests for GIF preparation and the upload protocol.

The board is simulated closely enough to catch the mistakes that matter: FakeBoard below decodes
base64, accumulates the CRC exactly as firmware/src/alert.cpp does, and acknowledges on the same
window. If the two sides ever stop agreeing about chunk sizes, line limits or the checksum, these
tests fail rather than the hardware.
"""

from __future__ import annotations

import base64
import zlib
from pathlib import Path

import pytest

from pc_app import gif_upload
from pc_app.gif_upload import (
    CHUNK_BYTES,
    MAX_SIZE,
    GifTooBig,
    UploadFailed,
    chunk_count,
    prepare_gif,
    send_gif,
    shared_palette,
    upload_lines,
)

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

#: Mirrors kMaxLine in firmware/src/serial_link.h. A GIFDATA line longer than this is discarded
#: by the firmware, silently, and the transfer would fail its checksum with no clue why.
FIRMWARE_MAX_LINE = 160


# -- fixtures ------------------------------------------------------------------------------


def make_gif(path: Path, frames: int = 8, size=(400, 300), late_colour=False) -> Path:
    """A multi-frame test GIF. With *late_colour*, a colour that only appears after frame 0."""
    images = []
    for index in range(frames):
        frame = Image.new("RGB", size, (20, 30, 40))
        draw = ImageDraw.Draw(frame)
        # Wrapped, and tinted per frame, so no two frames are identical: Pillow merges duplicate
        # consecutive frames on save, which would quietly shorten the fixture.
        shift = (index * 7) % max(1, size[0] - 40)
        tint = (240, 200 - index % 40, 60 + index % 40)
        draw.ellipse((shift, shift, shift + 120, shift + 120), fill=tint)
        if late_colour and index >= frames // 2:
            # Pure magenta, nowhere in the first frame.
            draw.rectangle((10, 10, 60, 60), fill=(255, 0, 255))
        images.append(frame)
    images[0].save(
        path, format="GIF", save_all=True, append_images=images[1:], duration=80, loop=0
    )
    return path


@pytest.fixture
def source(tmp_path) -> Path:
    return make_gif(tmp_path / "source.gif")


# -- preparation ---------------------------------------------------------------------------


def test_prepare_fits_the_board(source, tmp_path):
    stats = prepare_gif(source, tmp_path / "out.gif")
    assert stats.width <= MAX_SIZE[0] and stats.height <= MAX_SIZE[1]
    assert stats.size_bytes == (tmp_path / "out.gif").stat().st_size


def test_prepare_preserves_the_aspect_ratio(source, tmp_path):
    stats = prepare_gif(source, tmp_path / "out.gif")
    # 400x300 scaled to fit a 240 square is 240x180.
    assert (stats.width, stats.height) == (240, 180)


def test_prepare_never_scales_a_small_gif_up(tmp_path):
    small = make_gif(tmp_path / "small.gif", size=(64, 48))
    stats = prepare_gif(small, tmp_path / "out.gif")
    assert (stats.width, stats.height) == (64, 48)


def test_prepare_caps_the_frame_count(tmp_path):
    long_gif = make_gif(tmp_path / "long.gif", frames=90, size=(120, 120))
    stats = prepare_gif(long_gif, tmp_path / "out.gif", max_frames=10)
    assert stats.frames == 10
    assert stats.dropped_frames == 80


def read_gif_blocks(path: Path) -> tuple[bool, list[tuple[bool, int]]]:
    """Parse *path* far enough to answer the two questions the device cares about.

    Returns (has_global_palette, [(frame_has_local_palette, disposal_method), ...]). Written out
    rather than taken from Pillow because Pillow reports what it would *render*, and what matters
    here is what is actually in the bytes the board will be handed.
    """
    data = path.read_bytes()
    assert data[:6] in (b"GIF87a", b"GIF89a")
    packed = data[10]
    has_global = bool(packed & 0x80)
    pos = 13 + (3 * 2 ** ((packed & 0x07) + 1) if has_global else 0)

    frames: list[tuple[bool, int]] = []
    disposal = 0
    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B:  # trailer
            break
        if marker == 0x21:  # extension
            label = data[pos + 1]
            pos += 2
            if label == 0xF9:  # graphic control
                disposal = (data[pos + 1] >> 2) & 0x07
            while data[pos]:  # sub-blocks, terminated by a zero length
                pos += data[pos] + 1
            pos += 1
        elif marker == 0x2C:  # image descriptor
            frames.append((bool(data[pos + 9] & 0x80), disposal))
            pos += 10
            if data[pos - 1] & 0x80:  # skip a local colour table if there is one
                pos += 3 * 2 ** ((data[pos - 1] & 0x07) + 1)
            pos += 1  # LZW minimum code size
            while data[pos]:
                pos += data[pos] + 1
            pos += 1
        else:
            break
    return has_global, frames


def test_prepare_writes_one_global_palette_and_no_local_ones(source, tmp_path):
    """The device keeps no canvas, so a local palette is the one thing it cannot render."""
    out = tmp_path / "out.gif"
    prepare_gif(source, out)
    has_global, frames = read_gif_blocks(out)

    assert has_global, "no global palette: every frame would need a local one"
    assert frames, "no frames parsed"
    assert not any(local for local, _disposal in frames), "a frame carries its own palette"


def test_every_frame_asks_to_be_left_in_place(source, tmp_path):
    """Disposal 1 is the only method that needs no canvas.

    Pillow still crops each frame to the region that changed, and that is fine: with disposal 1
    the panel itself acts as the canvas, and drawLine() in firmware/src/alert.cpp honours the
    frame offset it is given. Anything that asked for a *restore* would not work.
    """
    out = tmp_path / "out.gif"
    prepare_gif(source, out)
    _has_global, frames = read_gif_blocks(out)

    assert all(disposal in (0, 1) for _local, disposal in frames)


def test_the_first_frame_covers_the_whole_canvas(source, tmp_path):
    """Playback loops back to frame 0, so it has to be able to stand on its own."""
    out = tmp_path / "out.gif"
    prepare_gif(source, out)
    with Image.open(out) as result:
        result.seek(0)
        assert result.tile[0][1] == (0, 0, result.width, result.height)


def test_a_colour_that_appears_late_survives(tmp_path):
    """Regression: a palette built from frame 0 alone loses anything introduced later."""
    late = make_gif(tmp_path / "late.gif", frames=8, size=(120, 120), late_colour=True)
    out = tmp_path / "out.gif"
    prepare_gif(late, out)

    with Image.open(out) as result:
        result.seek(result.n_frames - 1)
        colours = result.convert("RGB").getcolors(maxcolors=1 << 16) or []
    # The magenta block should still be magenta-ish, not snapped to the yellow or the background.
    magenta = [rgb for _count, rgb in colours if rgb[0] > 180 and rgb[2] > 180 and rgb[1] < 90]
    assert magenta, "the late-appearing colour was quantised away"


def test_shared_palette_sees_every_frame():
    plain = Image.new("RGB", (8, 8), (0, 0, 0))
    magenta = Image.new("RGB", (8, 8), (255, 0, 255))
    palette = shared_palette([plain, magenta], 16)
    entries = palette.getpalette()[: 16 * 3]
    triples = list(zip(entries[::3], entries[1::3], entries[2::3]))
    assert (255, 0, 255) in triples


def test_an_impossible_gif_is_refused(source, tmp_path):
    with pytest.raises(GifTooBig):
        prepare_gif(source, tmp_path / "out.gif", max_bytes=64)
    assert not (tmp_path / "out.gif").exists()


# -- the wire format -----------------------------------------------------------------------


def test_every_line_fits_the_firmware_limit():
    data = bytes(range(256)) * 40
    for line in upload_lines(data):
        assert len(line) <= FIRMWARE_MAX_LINE, line[:40]


def test_the_framing_is_begin_chunks_end():
    data = b"x" * (CHUNK_BYTES * 3 + 7)
    lines = list(upload_lines(data))
    assert lines[0] == f"GIFBEGIN:{len(data)}:{zlib.crc32(data) & 0xFFFFFFFF}"
    assert lines[-1] == "GIFEND"
    assert len(lines) - 2 == chunk_count(len(data)) == 4
    assert all(line.startswith("GIFDATA:") for line in lines[1:-1])


def test_the_chunks_reassemble_to_the_original():
    data = bytes(range(251)) * 13
    payload = b"".join(
        base64.b64decode(line[len("GIFDATA:"):]) for line in list(upload_lines(data))[1:-1]
    )
    assert payload == data


# -- a simulated board ---------------------------------------------------------------------


class FakeBoard:
    """Enough of firmware/src/alert.cpp to prove the two sides agree."""

    def __init__(self, ack_every: int = gif_upload.ACK_EVERY, max_bytes: int = gif_upload.MAX_BYTES):
        self.ack_every = ack_every
        self.max_bytes = max_bytes
        self.committed: bytes | None = None
        self.temp = bytearray()
        self.uploading = False
        self.expected = 0
        self.expected_crc = 0
        self.chunks_since_ack = 0
        self._out: list[str] = []

    # -- the SerialLink surface send_gif actually uses
    def send(self, line: str) -> bool:
        assert len(line) <= FIRMWARE_MAX_LINE, f"line too long for the firmware: {len(line)}"
        command, _, value = line.partition(":")
        getattr(self, f"_on_{command.lower()}", self._ignore)(value)
        return True

    def read_lines(self):
        out, self._out = self._out, []
        return out

    def _ignore(self, _value: str) -> None:
        pass

    def _fail(self, reason: str) -> None:
        self.temp = bytearray()
        self.uploading = False
        self._out.append(f"EVT:GIFERR:{reason}")

    def _on_gifbegin(self, value: str) -> None:
        size, _, crc = value.partition(":")
        if not 0 < int(size) <= self.max_bytes:
            self._out.append(f"EVT:GIFERR:size {size}")
            return
        self.expected, self.expected_crc = int(size), int(crc)
        self.temp = bytearray()
        self.chunks_since_ack = 0
        self.uploading = True
        self._out.append("EVT:GIFACK:0")

    def _on_gifdata(self, value: str) -> None:
        if not self.uploading:
            return
        self.temp += base64.b64decode(value)
        if len(self.temp) > self.expected:
            self._fail("too long")
            return
        self.chunks_since_ack += 1
        if self.chunks_since_ack >= self.ack_every:
            self.chunks_since_ack = 0
            self._out.append(f"EVT:GIFACK:{len(self.temp)}")

    def _on_gifend(self, _value: str) -> None:
        if not self.uploading:
            self._out.append("EVT:GIFERR:no upload in progress")
            return
        self.uploading = False
        if len(self.temp) != self.expected:
            self._fail("short")
            return
        if zlib.crc32(self.temp) & 0xFFFFFFFF != self.expected_crc:
            self._fail("checksum")
            return
        # Committed only here, which is what leaves the old GIF intact on any failure above.
        self.committed = bytes(self.temp)
        self._out.append(f"EVT:GIFOK:{len(self.committed)}")


def test_a_gif_arrives_intact(source, tmp_path):
    prepare_gif(source, tmp_path / "out.gif")
    data = (tmp_path / "out.gif").read_bytes()
    board = FakeBoard()

    send_gif(board, data)

    assert board.committed == data


@pytest.mark.parametrize("size", [1, CHUNK_BYTES - 1, CHUNK_BYTES, CHUNK_BYTES + 1,
                                  CHUNK_BYTES * gif_upload.ACK_EVERY,
                                  CHUNK_BYTES * gif_upload.ACK_EVERY + 1])
def test_window_boundaries_do_not_deadlock(size):
    """An exact multiple of the ack window is the case where a missed ack would hang."""
    data = bytes(range(256)) * (size // 256 + 1)
    data = data[:size]
    board = FakeBoard()
    send_gif(board, data)
    assert board.committed == data


def test_progress_reaches_the_total(source, tmp_path):
    prepare_gif(source, tmp_path / "out.gif")
    data = (tmp_path / "out.gif").read_bytes()
    seen: list[tuple[int, int]] = []

    send_gif(FakeBoard(), data, on_progress=lambda sent, total: seen.append((sent, total)))

    assert seen[-1] == (len(data), len(data))
    assert all(sent <= total for sent, total in seen)
    assert seen == sorted(seen)


def test_a_refused_upload_raises_and_leaves_the_old_gif(tmp_path):
    board = FakeBoard(max_bytes=100)
    with pytest.raises(UploadFailed):
        send_gif(board, b"y" * 5000)
    assert board.committed is None


def test_a_corrupted_transfer_is_caught_by_the_checksum():
    board = FakeBoard()
    original_send = board.send

    def flip(line: str) -> bool:
        # Corrupt one chunk in a way that preserves its length, so only the CRC can catch it.
        if line.startswith("GIFDATA:") and board.chunks_since_ack == 3:
            raw = bytearray(base64.b64decode(line[8:]))
            raw[0] ^= 0xFF
            line = "GIFDATA:" + base64.b64encode(bytes(raw)).decode()
        return original_send(line)

    board.send = flip
    with pytest.raises(UploadFailed, match="checksum"):
        send_gif(board, bytes(range(256)) * 20)
    assert board.committed is None


def test_an_oversized_gif_is_rejected_before_anything_is_sent():
    board = FakeBoard()
    with pytest.raises(UploadFailed, match="over the"):
        send_gif(board, b"z" * (gif_upload.MAX_BYTES + 1))
    assert not board.uploading


def test_a_silent_board_times_out_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(gif_upload, "ACK_TIMEOUT", 0.05)

    class Deaf(FakeBoard):
        def read_lines(self):
            return []

    with pytest.raises(UploadFailed, match="stopped acknowledging"):
        send_gif(Deaf(), b"q" * 500)
