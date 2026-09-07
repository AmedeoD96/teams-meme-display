"""Tests for the WiFi transport: discovery, the token handshake, framing and failover.

The board is simulated the way test_gif_upload simulates it -- closely enough that a disagreement
between the two sides shows up here rather than on the desk. FakeSocket below assembles lines the
way firmware/src/net_link.cpp does, and hangs up on a client whose first line is not the token,
which is the one rule the network side has that the cable does not.
"""

from __future__ import annotations

import time

import pytest

from pc_app import net_link
from pc_app.net_link import BeaconListener, NetworkLink, handshake, parse_beacon
from pc_app.tests.test_gif_upload import FakeBoard
from pc_app.transport import AutoLink

TOKEN = "0123456789abcdef"


class FakeSocket:
    """The board's end of a TCP connection.

    *board* is an optional test_gif_upload.FakeBoard, which lets one test drive a real GIF upload
    through this transport and prove the ack window survives the change of wire.
    """

    def __init__(self, token: str = TOKEN, board: FakeBoard | None = None, deaf: bool = False):
        self.token = token
        self.board = board
        self.deaf = deaf  # answers nothing at all, like a port held open by something else
        self.sent = bytearray()
        self.closed = False
        self.authenticated = False
        self.timeout = None
        self._line = bytearray()
        self._out = bytearray()

    # -- the socket surface net_link uses ------------------------------------------------

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError("sending on a closed socket")
        self.sent += data
        for byte in data:
            if byte != 0x0A:
                self._line.append(byte)
                continue
            line = bytes(self._line).decode("ascii").strip()
            self._line.clear()
            self._handle(line)

    def recv(self, size: int) -> bytes:
        if self._out:
            chunk, self._out = bytes(self._out[:size]), self._out[size:]
            return chunk
        if self.closed:
            return b""  # an orderly close, which is what a bad token gets
        time.sleep(0.005)
        raise TimeoutError("nothing to read")

    def settimeout(self, value) -> None:
        self.timeout = value

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return 1

    # -- the board's half ----------------------------------------------------------------

    def readable(self) -> bool:
        return bool(self._out) or self.closed

    def say(self, text: str) -> None:
        """Push a line at the PC, as the board would."""
        self._out += (text + "\n").encode("ascii")

    def _handle(self, line: str) -> None:
        if self.deaf:
            return
        if not self.authenticated:
            if line != f"AUTH:{self.token}":
                self.say("LOG:bad token")
                self.closed = True
                return
            self.authenticated = True
            self.say("READY:1.4.0")
            return
        if line == "PING":
            self.say("PONG")
            return
        if self.board is not None:
            self.board.send(line)
            for reply in self.board.read_lines():
                self.say(reply)


@pytest.fixture
def wired(monkeypatch):
    """Point net_link at FakeSockets instead of the network, and make waiting cheap."""
    made: list[FakeSocket] = []

    def create_connection(_address, timeout=None):
        if not made:
            made.append(FakeSocket())
        return made[0]

    monkeypatch.setattr(net_link.socket, "create_connection", create_connection)
    monkeypatch.setattr(
        net_link.select, "select",
        lambda readers, _w, _x, _t: ([s for s in readers if s.readable()], [], []),
    )
    monkeypatch.setattr(net_link, "HANDSHAKE_TIMEOUT", 0.3)
    return made


def connected_link(wired, board: FakeBoard | None = None) -> tuple[NetworkLink, FakeSocket]:
    """A NetworkLink that has already shaken hands, and the socket behind it."""
    sock = FakeSocket(board=board)
    wired.append(sock)
    link = NetworkLink(host="10.0.0.7", token=TOKEN)
    assert link.ensure_connected() is False  # the attempt runs on its own thread
    link._connecting.join(2.0)
    assert link.ensure_connected() is True
    return link, sock


# -- the beacon ----------------------------------------------------------------------------


def test_a_beacon_says_where_the_board_is():
    assert parse_beacon(b"TEAMSMEME:1.4.0:192.168.1.42:3141\n") == ("192.168.1.42", 3141, "1.4.0")


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"hello\n",
        b"TEAMSMEME:1.4.0:192.168.1.42\n",  # no port
        b"TEAMSMEME:1.4.0:192.168.1.42:kittens\n",
        b"TEAMSMEME:1.4.0:192.168.1.42:0\n",
        b"TEAMSMEME:1.4.0::3141\n",  # no address
        b"TEAMSMEME:1.4.0:192.168.1.42:70000\n",
        "TEAMSMEME:1.4.0:192.168.1.42:3141\n".encode("utf-16"),
    ],
)
def test_anything_else_on_the_port_is_ignored(payload):
    """UDP 3142 is a broadcast address: whatever else the network shouts must not be taken for
    a board."""
    assert parse_beacon(payload) is None


def test_a_stale_beacon_is_not_an_address():
    listener = BeaconListener()
    listener._latest = ("192.168.1.42", 3141)
    listener._seen_at = time.monotonic() - 120
    assert listener.latest() is None
    listener._seen_at = time.monotonic()
    assert listener.latest() == ("192.168.1.42", 3141)


def test_a_busy_discovery_port_is_survivable(monkeypatch):
    """Something else on 3142 costs discovery, not the app: a typed-in address still works."""
    def refuse(*_args, **_kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(net_link.socket, "socket", refuse)
    listener = BeaconListener()
    assert listener.start() is False
    assert "3142" in listener.last_error
    assert listener.start() is False  # and it does not keep retrying every tick


# -- the handshake -------------------------------------------------------------------------


def test_the_token_goes_first_with_nothing_in_front_of_it(wired):
    """The mirror image of the serial probe, which leads with a newline to clear the board's
    buffer. Here a leading newline would be an empty first line -- and the board hangs up on a
    client whose first line is not the token."""
    sock = FakeSocket()
    assert handshake(sock, TOKEN) is None
    assert bytes(sock.sent).startswith(f"AUTH:{TOKEN}\n".encode("ascii"))


def test_a_wrong_token_is_reported_as_such(wired):
    sock = FakeSocket(token="something-else")
    assert handshake(sock, TOKEN) == "refused our token"


def test_a_silent_board_gives_up_rather_than_hanging(wired):
    sock = FakeSocket(deaf=True)
    started = time.monotonic()
    assert handshake(sock, TOKEN) == "no answer"
    assert time.monotonic() - started < 2.0


# -- connecting ----------------------------------------------------------------------------


def test_without_a_token_we_do_not_even_knock():
    """The board would hang up on us, and the reason has to reach the settings window."""
    link = NetworkLink(host="10.0.0.7", token=None)
    assert link.ensure_connected() is False
    assert "token" in link.last_error


def test_a_connected_link_names_the_address(wired):
    link, _sock = connected_link(wired)
    assert link.port == "10.0.0.7:3141"
    assert link.connected and link.last_error is None


def test_an_unreachable_board_leaves_a_reason(wired, monkeypatch):
    monkeypatch.setattr(
        net_link.socket, "create_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("host unreachable")),
    )
    link = NetworkLink(host="10.0.0.7", token=TOKEN)
    link.ensure_connected()
    link._connecting.join(2.0)

    assert link.ensure_connected() is False
    assert "10.0.0.7:3141" in link.last_error
    assert link._next_attempt > 0  # and it backs off rather than spinning


def test_an_answer_from_before_the_rescan_is_discarded(wired):
    """Reconnect has to mean "look again now" -- the same rule the serial side follows."""
    link, _sock = connected_link(wired)
    link.force_rescan()
    assert link.connected is False

    wired.clear()
    wired.append(FakeSocket())
    assert link.ensure_connected() is False
    link._connecting.join(2.0)
    assert link.ensure_connected() is True


def test_the_configured_address_wins_over_the_beacon():
    link = NetworkLink(host="10.0.0.7", token=TOKEN)
    link.beacons._latest = ("192.168.1.42", 3141)
    link.beacons._seen_at = time.monotonic()
    assert link.target() == ("10.0.0.7", 3141)


def test_with_no_address_configured_the_beacon_is_the_address():
    link = NetworkLink(token=TOKEN)
    link.beacons._failed = True  # do not open a real socket in a test
    link.beacons._latest = ("192.168.1.42", 3141)
    link.beacons._seen_at = time.monotonic()
    assert link.target() == ("192.168.1.42", 3141)


# -- traffic -------------------------------------------------------------------------------


def test_a_line_reaches_the_board_terminated(wired):
    link, sock = connected_link(wired)
    sock.sent.clear()

    assert link.send("STATUS:DND") is True
    assert bytes(sock.sent) == b"STATUS:DND\n"


def test_a_line_split_across_packets_is_only_yielded_once_whole(wired):
    link, sock = connected_link(wired)
    list(link.read_lines())  # drain the greeting

    sock._out += b"EVT:WI"
    assert list(link.read_lines()) == []
    sock._out += b"FI:online:192.168.1.42:-55\nLOG:hal"
    assert list(link.read_lines()) == ["EVT:WIFI:online:192.168.1.42:-55"]
    sock._out += b"f a line\n"
    assert list(link.read_lines()) == ["LOG:half a line"]


def test_a_board_that_hangs_up_drops_the_link(wired):
    link, sock = connected_link(wired)
    sock.closed = True

    assert list(link.read_lines()) == []
    assert link.connected is False
    assert "closed by the board" in link.last_error


def test_a_failed_send_drops_the_link(wired):
    link, sock = connected_link(wired)
    sock.closed = True

    assert link.send("STATUS:DND") is False
    assert link.connected is False


def test_a_gif_upload_survives_the_change_of_wire(wired):
    """send_gif only ever touches send() and read_lines(), and this is the claim worth pinning:
    the ack window that exists for the serial buffer works unchanged over TCP."""
    from pc_app.gif_upload import send_gif

    board = FakeBoard()
    link, _sock = connected_link(wired, board=board)
    data = bytes(range(256)) * 40

    send_gif(link, data)

    assert board.committed == data


# -- choosing a transport ------------------------------------------------------------------


class StubLink:
    def __init__(self, kind: str, works: bool):
        self.kind = kind
        self.works = works
        self.port = f"{kind}-port" if works else None
        self.last_error = None if works else f"no {kind} board"
        self.dry_run = False
        self.sent: list[str] = []
        self.rescans = 0

    @property
    def connected(self) -> bool:
        return self.works

    def ensure_connected(self) -> bool:
        return self.works

    def send(self, line: str) -> bool:
        self.sent.append(line)
        return True

    def read_lines(self):
        return iter(())

    def close(self) -> None:
        self.works = False

    def force_rescan(self) -> None:
        self.rescans += 1


def test_the_cable_wins_when_both_are_there():
    """USB is the one that cannot be wrong about which board it reached."""
    auto = AutoLink(StubLink("serial", True), StubLink("network", True))
    assert auto.ensure_connected() is True
    assert auto.kind == "serial"


def test_wifi_takes_over_when_the_cable_comes_out():
    serial, network = StubLink("serial", True), StubLink("network", True)
    auto = AutoLink(serial, network)
    auto.ensure_connected()

    serial.works = False
    assert auto.ensure_connected() is True
    assert (auto.kind, auto.port) == ("network", "network-port")


def test_the_cable_takes_back_over_when_it_returns():
    """It is the only way to change the board's WiFi, so a plugged-in cable has to win -- the
    settings window refuses to send a password over anything else."""
    serial, network = StubLink("serial", False), StubLink("network", True)
    auto = AutoLink(serial, network)
    auto.ensure_connected()
    assert auto.kind == "network"

    serial.works = True
    serial.port = "serial-port"

    assert auto.ensure_connected() is True
    assert auto.kind == "serial"
    assert network.connected is False  # and the board's one client slot is given back


def test_with_neither_working_both_reasons_are_reported():
    auto = AutoLink(StubLink("serial", False), StubLink("network", False))
    assert auto.ensure_connected() is False
    assert "no serial board" in auto.last_error and "no network board" in auto.last_error


def test_a_rescan_asks_both_to_look_again():
    serial, network = StubLink("serial", True), StubLink("network", True)
    auto = AutoLink(serial, network)
    auto.ensure_connected()

    auto.force_rescan()

    assert (serial.rescans, network.rescans, auto.kind) == (1, 1, None)


def test_sending_with_nothing_attached_is_not_an_error():
    auto = AutoLink(StubLink("serial", False), StubLink("network", False))
    assert auto.send("STATUS:DND") is False
    assert list(auto.read_lines()) == []
