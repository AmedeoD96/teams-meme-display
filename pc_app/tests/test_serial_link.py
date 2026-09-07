"""Tests for port detection and reconnection.

The cases that matter are the ones that left a board plugged in at boot undiscoverable: a stray
byte in the board's line buffer, a first PING that goes missing, a port held by another program,
and a Reconnect button that has to mean "look again now".
"""

from __future__ import annotations

import pytest

from pc_app import serial_link
from pc_app.serial_link import BUSY, SerialLink


class FakeBoard:
    """The board's half of the link, modelled on firmware/src/serial_link.cpp: bytes accumulate
    and are acted on only when a newline arrives.

    *junk* is what is already sitting in that buffer when the port opens -- the cold-boot case.
    *swallow_lines* drops that many complete lines on the floor, standing in for a board that is
    not listening yet.
    """

    def __init__(self, junk: bytes = b"", reply: bytes = b"PONG\n", swallow_lines: int = 0):
        self._line = bytearray(junk)
        self._out = bytearray()
        self._reply = reply
        self._swallow = swallow_lines

    def feed(self, data: bytes) -> None:
        for byte in data:
            if byte == 0x0A:
                line = bytes(self._line).strip()
                self._line.clear()
                if self._swallow:
                    self._swallow -= 1
                    continue
                if line == b"PING":
                    self._out += self._reply
            else:
                self._line.append(byte)

    def read(self, size: int) -> bytes:
        chunk, self._out = bytes(self._out[:size]), self._out[size:]
        return chunk

    def drop_pending(self) -> None:
        self._out.clear()


class FakePort:
    def __init__(self, board: FakeBoard | None, error: Exception | None):
        self._board = board
        self._error = error
        self.opened = False
        self.dtr = None
        self.rts = None
        self.lines_at_open: tuple = ()
        self.writes: list[bytes] = []

    # pyserial's own API, as much of it as open_serial() and probe() use.
    def open(self) -> None:
        if self._error is not None:
            raise self._error
        self.opened = True
        self.lines_at_open = (self.dtr, self.rts)

    def close(self) -> None:
        self.opened = False

    def __enter__(self) -> "FakePort":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        if self._board is not None:
            self._board.feed(data)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        return self._board.read(size) if self._board is not None else b""

    def reset_input_buffer(self) -> None:
        if self._board is not None:
            self._board.drop_pending()

    @property
    def in_waiting(self) -> int:
        return 0


class FakeSerialModule:
    def __init__(self, board: FakeBoard | None = None, error: Exception | None = None):
        self._board = board
        self._error = error
        self.last: FakePort | None = None

    def Serial(self) -> FakePort:  # noqa: N802 - pyserial spells it this way
        self.last = FakePort(self._board, self._error)
        return self.last


class Info:
    def __init__(self, device: str, vid: int | None, pid: int | None):
        self.device, self.vid, self.pid = device, vid, pid


class FakeListPorts:
    def __init__(self, *infos: Info):
        self._infos = infos

    def comports(self) -> tuple[Info, ...]:
        return self._infos


CH340 = serial_link.CH340_VID_PID
OTHER = (0x0403, 0x6001)


@pytest.fixture
def fast(monkeypatch):
    """No real waiting: the probe's settle sleep goes away and its window shrinks."""
    monkeypatch.setattr(serial_link, "HAVE_PYSERIAL", True)
    monkeypatch.setattr(serial_link.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(serial_link, "PING_INTERVAL", 0.02)


def attach(monkeypatch, board=None, error=None) -> FakeSerialModule:
    module = FakeSerialModule(board, error)
    monkeypatch.setattr(serial_link, "serial", module)
    return module


# -- probe -------------------------------------------------------------------------------


def test_probe_recovers_from_a_partial_line(fast, monkeypatch):
    """The bug: one stray byte in the board's buffer made <junk>PING an unrecognised line, and
    nothing short of unplugging the board cleared it."""
    module = attach(monkeypatch, FakeBoard(junk=b"\x00"))
    assert serial_link.probe("COM4", timeout=0.3) == "ok"
    # The newline that closes off the junk has to come first, in the very first write.
    assert module.last.writes[0].startswith(b"\n")


def test_probe_repeats_the_ping(fast, monkeypatch):
    attach(monkeypatch, FakeBoard(swallow_lines=2))  # the empty line and the first PING
    assert serial_link.probe("COM4", timeout=0.5) == "ok"


def test_probe_accepts_ready(fast, monkeypatch):
    attach(monkeypatch, FakeBoard(reply=b"READY:1.3.0\n"))
    assert serial_link.probe("COM4", timeout=0.3) == "ok"


def test_probe_reports_a_busy_port(fast, monkeypatch):
    attach(monkeypatch, error=PermissionError("Access is denied"))
    assert serial_link.probe("COM4", timeout=0.3) == BUSY


def test_probe_sees_through_a_wrapped_busy_error(fast, monkeypatch):
    """What Windows plus pyserial actually raise: a SerialException carrying a repr of the
    PermissionError, which no `except PermissionError` will ever catch."""
    wrapped = Exception(
        "could not open port 'COM4': PermissionError(13, 'Access is denied.', None, 5)"
    )
    attach(monkeypatch, error=wrapped)
    assert serial_link.probe("COM4", timeout=0.3) == BUSY


def test_probe_gives_up_on_a_silent_port(fast, monkeypatch):
    attach(monkeypatch, FakeBoard(swallow_lines=99))
    assert serial_link.probe("COM4", timeout=0.2) == "no answer"


def test_open_serial_does_not_pulse_the_reset_lines(fast, monkeypatch):
    module = attach(monkeypatch, FakeBoard())
    serial_link.open_serial("COM4", 115200, timeout=0.1, write_timeout=2.0)
    # Both had to be chosen before the port opened, or the board would already have rebooted.
    assert module.last.lines_at_open == (False, False)


# -- find_port ---------------------------------------------------------------------------


def use_ports(monkeypatch, *infos: Info) -> None:
    monkeypatch.setattr(serial_link, "HAVE_PYSERIAL", True)
    monkeypatch.setattr(serial_link, "list_ports", FakeListPorts(*infos))


def answers(monkeypatch, **by_port: str) -> None:
    monkeypatch.setattr(
        serial_link, "probe", lambda port, baud=115200: by_port.get(port, "no answer")
    )


def test_find_port_takes_the_port_that_answers(monkeypatch):
    use_ports(monkeypatch, Info("COM3", *OTHER), Info("COM4", *CH340))
    answers(monkeypatch, COM4="ok")
    assert serial_link.find_port() == ("COM4", None)


def test_find_port_explains_a_busy_port(monkeypatch):
    use_ports(monkeypatch, Info("COM4", *CH340))
    answers(monkeypatch, COM4=BUSY)
    port, reason = serial_link.find_port()
    assert port is None and reason == f"COM4 {BUSY}"


def test_find_port_falls_back_to_a_lone_ch340(monkeypatch):
    """Nothing answered, but the board is right there. Somebody with only the .exe cannot fix a
    wedged parser, so the port is taken anyway rather than left on DISCONNECTED forever."""
    use_ports(monkeypatch, Info("COM3", *OTHER), Info("COM4", *CH340))
    answers(monkeypatch)
    assert serial_link.find_port() == ("COM4", None)


def test_find_port_never_adopts_a_stranger(monkeypatch):
    use_ports(monkeypatch, Info("COM3", *OTHER))
    answers(monkeypatch)
    port, reason = serial_link.find_port()
    assert port is None and "COM3" in reason


def test_find_port_will_not_guess_between_two_ch340s(monkeypatch):
    use_ports(monkeypatch, Info("COM4", *CH340), Info("COM7", *CH340))
    answers(monkeypatch)
    assert serial_link.find_port()[0] is None


# -- reconnecting ------------------------------------------------------------------------


def test_force_rescan_clears_the_backoff():
    link = SerialLink()
    link._failures = 4
    link._schedule_retry()
    assert link._next_attempt > 0

    link.force_rescan()

    assert (link._failures, link._next_attempt, link.last_error) == (0, 0.0, None)


def test_a_scan_that_finished_before_the_rescan_is_discarded(monkeypatch):
    """Reconnect has to mean "look again now" -- not "believe the answer you already had"."""
    found = ["COM4", "COM9"]
    monkeypatch.setattr(
        serial_link, "find_port", lambda baud=115200, verbose=False: (found.pop(0), None)
    )
    link = SerialLink()

    assert link._poll_discovery() is None  # starts a scan
    link._discovery.join(2.0)  # which has now answered COM4
    link.force_rescan()  # ...but the user asked for a fresh look

    assert link._poll_discovery() is None  # COM4 dropped, another scan starts
    link._discovery.join(2.0)
    assert link._poll_discovery() == "COM9"


def test_a_failed_scan_leaves_a_reason_behind(monkeypatch):
    monkeypatch.setattr(
        serial_link, "find_port", lambda baud=115200, verbose=False: (None, "no COM ports found")
    )
    link = SerialLink()
    assert link._poll_discovery() is None
    link._discovery.join(2.0)

    assert link._poll_discovery() is None
    assert link.last_error == "no COM ports found"


def test_a_port_that_will_not_open_is_reported(fast, monkeypatch):
    attach(monkeypatch, error=OSError("the parameter is incorrect"))
    link = SerialLink(port="COM4")

    assert link.ensure_connected() is False
    assert "COM4" in link.last_error


def test_connecting_clears_the_reason(fast, monkeypatch):
    attach(monkeypatch, FakeBoard())
    link = SerialLink(port="COM4")
    link.last_error = "stale"

    assert link.ensure_connected() is True
    assert (link.port, link.last_error) == ("COM4", None)
