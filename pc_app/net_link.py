"""TCP transport to the ESP32 over WiFi, including discovery and reconnection.

The wire format is the same one the cable carries -- see docs/PROTOCOL.md. This module exists so
the board can sit on a powerbank instead of a USB cable: it presents exactly the surface
`SerialLink` does, so `Worker` cannot tell the two apart.

Two things are different from the serial side, both of them consequences of anyone else being
able to reach a TCP port:

* the board hangs up on a client whose first line is not `AUTH:<token>`, and
* it says where it is with a UDP beacon rather than being found by probing.
"""

from __future__ import annotations

import logging
import select
import socket
import threading
import time
from typing import Iterator

# The two transports share their reconnection manners deliberately: the same backoff, the same
# "a scan that finished before the rescan is discarded" rule, the same line framing. A board that
# comes and goes should behave the same way whichever way it is attached.
from pc_app.serial_link import RECONNECT_BACKOFF, split_lines

log = logging.getLogger(__name__)

#: Must match kPort and kBeaconPort in firmware/src/net_link.h.
DEFAULT_PORT = 3141
BEACON_PORT = 3142

BEACON_PREFIX = "TEAMSMEME:"
#: A beacon older than this says where the board used to be. The firmware sends one every 3s.
BEACON_MAX_AGE = 30.0

CONNECT_TIMEOUT = 3.0
#: How long to wait for the board's READY: after sending the token.
HANDSHAKE_TIMEOUT = 3.0


def parse_beacon(payload: bytes) -> tuple[str, int, str] | None:
    """`(host, port, version)` from one discovery datagram, or None if it is not ours.

    Kept a plain function of bytes so the format can be tested without a socket anywhere near it.
    """
    try:
        text = payload.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not text.startswith(BEACON_PREFIX):
        return None
    parts = text[len(BEACON_PREFIX):].split(":")
    if len(parts) != 3:
        return None
    version, host, port = (part.strip() for part in parts)
    if not host or not version:
        return None
    try:
        number = int(port)
    except ValueError:
        return None
    if not 0 < number < 65536:
        return None
    return host, number, version


class BeaconListener:
    """Keeps the last discovery beacon the board broadcast.

    One socket and one thread for the life of the app: the board announces itself every few
    seconds whether or not anyone is connected, so by the time the link wants an address there is
    usually already one waiting.
    """

    def __init__(self, port: int = BEACON_PORT):
        self.port = port
        self.last_error: str | None = None
        self._latest: tuple[str, int] | None = None
        self._seen_at = 0.0
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._failed = False
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Begin listening. Returns whether the socket came up; safe to call repeatedly."""
        with self._lock:
            if self._thread is not None or self._failed:
                return self._socket is not None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", self.port))
                sock.settimeout(0.5)
            except OSError as exc:
                # Another copy of the app, or something else on 3142. Not fatal: an address typed
                # into the settings window still works, so this is recorded and not raised.
                self._failed = True
                self.last_error = f"cannot listen for the board on UDP {self.port}: {exc}"
                log.warning("%s", self.last_error)
                return False
            self._socket = sock
            self._thread = threading.Thread(target=self._run, name="beacon", daemon=True)
            self._thread.start()
            log.info("listening for the board on UDP %d", self.port)
            return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload, sender = self._socket.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                return  # the socket was closed under us by stop()
            found = parse_beacon(payload)
            if found is None:
                continue
            host, port, version = found
            # The sender's own address wins over the one it wrote in the packet: behind a router
            # doing NAT they can differ, and only one of them is a place we can connect to.
            if sender[0] and sender[0] != host:
                log.debug("beacon says %s but came from %s; using the sender", host, sender[0])
                host = sender[0]
            with self._lock:
                previous = self._latest
                self._latest = (host, port)
                self._seen_at = time.monotonic()
            if previous != (host, port):
                log.info("board announced itself at %s:%d (firmware %s)", host, port, version)

    def latest(self, max_age: float = BEACON_MAX_AGE) -> tuple[str, int] | None:
        with self._lock:
            if self._latest is None or time.monotonic() - self._seen_at > max_age:
                return None
            return self._latest

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None


class NetworkLink:
    """A resilient line-oriented link over TCP. Same surface as SerialLink, and the same promise:
    a board that goes away must never take the tray app down."""

    kind = "network"

    def __init__(
        self,
        host: str | None = None,
        tcp_port: int = DEFAULT_PORT,
        token: str | None = None,
        discovery_port: int = BEACON_PORT,
        dry_run: bool = False,
    ):
        self.configured_host = host
        self.tcp_port = tcp_port
        self.token = token
        self.dry_run = dry_run
        #: "<host>:<port>" once connected, so the tray and the settings window can print it
        #: exactly as they print a COM port.
        self.port: str | None = None
        #: Why there is no link, in words a user can act on. Same job as SerialLink.last_error.
        self.last_error: str | None = None
        self.beacons = BeaconListener(discovery_port)
        self._socket: socket.socket | None = None
        self._rx = ""
        self._failures = 0
        self._next_attempt = 0.0
        # Connecting means a DNS lookup, a TCP handshake and waiting for the board's answer --
        # seconds, in the bad cases. It runs on its own thread for the same reason port discovery
        # does: presence updates must not stall behind a board that is switched off.
        self._connecting: threading.Thread | None = None
        self._connected: tuple[int, socket.socket | None, str | None, str | None] | None = None
        #: Bumped by force_rescan(), so an answer from before the user asked to reconnect is
        #: dropped rather than believed.
        self._generation = 0
        self._lock = threading.Lock()

    # -- connection ----------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.dry_run or self._socket is not None

    def configure(self, host: str | None = None, token: str | None = None) -> None:
        """Adopt an address or a token learned since we started -- both arrive over USB, from
        `EVT:WIFI:` and `EVT:TOKEN:`. Anything already in flight is given up."""
        changed = False
        if host is not None and host != self.configured_host:
            self.configured_host = host
            changed = True
        if token is not None and token != self.token:
            self.token = token
            changed = True
        if changed:
            self.force_rescan()

    def ensure_connected(self) -> bool:
        """Connect if possible, without ever blocking for long.

        Returns False while the connection attempt is still running; the caller tries again on its
        next tick.
        """
        if self.dry_run or self._socket is not None:
            return True
        if not self.token:
            # Nothing to prove ourselves with. The board would hang up on us, so do not even
            # knock -- and say why, because it is fixable from the settings window.
            self.last_error = "no device token yet - plug the board in over USB once"
            return False
        return self._poll_connect()

    def force_rescan(self) -> None:
        """Drop the link and try again immediately, whatever the backoff had decided."""
        self.close()
        with self._lock:
            self._failures = 0
            self._next_attempt = 0.0
            self._connected = None
            # An attempt may be in flight. It cannot be cancelled, so let it finish and be ignored.
            self._generation += 1
        self.last_error = None

    def target(self) -> tuple[str, int] | None:
        """Where to knock: what you configured, or wherever the board last said it was."""
        if self.configured_host:
            return self.configured_host, self.tcp_port
        self.beacons.start()
        return self.beacons.latest()

    def _poll_connect(self) -> bool:
        with self._lock:
            if self._connecting is not None:
                if self._connecting.is_alive():
                    return False
                self._connecting = None
                result, self._connected = self._connected, None
                if result is not None and result[0] == self._generation:
                    _, sock, address, error = result
                    self.last_error = error
                    if sock is not None:
                        self._socket = sock
                        self.port = address
                        self._failures = 0
                        self._rx = ""
                        log.info("connected to the board at %s", address)
                        return True
                    self._schedule_retry()
                    return False
                if result is not None and result[1] is not None:
                    result[1].close()  # a stale answer still owns a socket
                # Stale, or a thread that died without leaving an answer: try again below.

            if time.monotonic() < self._next_attempt:
                return False

            where = self.target()
            if where is None:
                self.last_error = (
                    self.beacons.last_error
                    or "no board on the network yet - it announces itself every few seconds"
                )
                return False

            generation = self._generation
            host, port = where
            token = self.token

            def attempt() -> None:
                sock, error = connect(host, port, token)
                with self._lock:
                    self._connected = (generation, sock, f"{host}:{port}", error)

            self._connecting = threading.Thread(target=attempt, name="board-connect", daemon=True)
            self._connecting.start()
            return False

    def _schedule_retry(self) -> None:
        delay = RECONNECT_BACKOFF[min(self._failures, len(RECONNECT_BACKOFF) - 1)]
        self._failures += 1
        self._next_attempt = time.monotonic() + delay

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        self.port = None

    def _drop(self, reason: str) -> None:
        log.warning("link to %s lost: %s", self.port, reason)
        self.last_error = f"lost {self.port}: {reason}"
        self.close()
        self._schedule_retry()

    # -- traffic -------------------------------------------------------------------------

    def send(self, line: str) -> bool:
        """Send one line. Returns whether it went out."""
        if self.dry_run:
            try:
                print(f"TX {line}", flush=True)
            except OSError:
                log.info("TX %s", line)
            return True
        if not self.ensure_connected():
            return False
        try:
            self._socket.sendall((line + "\n").encode("ascii", errors="replace"))
            return True
        except OSError as exc:
            self._drop(str(exc))
            return False

    def read_lines(self) -> Iterator[str]:
        """Yield whatever complete lines the board has sent since the last call."""
        if self.dry_run or self._socket is None:
            return
        while True:
            try:
                ready, _, _ = select.select([self._socket], [], [], 0)
                if not ready:
                    break
                chunk = self._socket.recv(4096)
            except OSError as exc:
                self._drop(str(exc))
                return
            if not chunk:
                # An orderly close from the other end: the board rebooted, or a second client
                # took our slot.
                self._drop("closed by the board")
                return
            self._rx += chunk.decode("ascii", errors="replace")
        lines, self._rx = split_lines(self._rx)
        yield from lines


def connect(host: str, port: int, token: str) -> tuple[socket.socket | None, str | None]:
    """Open a socket to the board and prove who we are. Returns (socket, reason); one of the two."""
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        return None, f"cannot reach {host}:{port}: {exc}"

    reason = handshake(sock, token)
    if reason is not None:
        sock.close()
        return None, f"{host}:{port}: {reason}"
    # Writes block for at most this long from here on; reads are selected on, never waited for.
    sock.settimeout(2.0)
    return sock, None


def handshake(sock: socket.socket, token: str) -> str | None:
    """Send the token and wait for the board's greeting. Returns None on success, else why not.

    `AUTH:<token>` goes first with nothing in front of it. That is the opposite of the serial
    probe, which leads with a newline to clear the board's line buffer: a fresh TCP connection
    cannot have junk in it, and the board hangs up on a client whose first line is not the token.
    """
    try:
        sock.settimeout(HANDSHAKE_TIMEOUT)
        sock.sendall(f"AUTH:{token}\nPING\n".encode("ascii", errors="replace"))
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        buffer = ""
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(256)
            except TimeoutError:
                continue  # nothing yet; the deadline above is what ends this
            if not chunk:
                # The board hung up, which is exactly what it does to a wrong token.
                return "refused our token"
            buffer += chunk.decode("ascii", errors="replace")
            if "READY:" in buffer or "PONG" in buffer:
                return None
            if "bad token" in buffer:
                return "refused our token"
        return "no answer"
    except OSError as exc:
        return f"handshake failed: {exc}"
