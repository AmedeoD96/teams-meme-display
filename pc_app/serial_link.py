"""Serial transport to the ESP32, including port auto-detection and reconnection.

The wire format is documented in docs/PROTOCOL.md.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterator

log = logging.getLogger(__name__)

try:  # pyserial is not needed for --dry-run, so tolerate its absence.
    import serial
    from serial.tools import list_ports

    HAVE_PYSERIAL = True
except ImportError:  # pragma: no cover - exercised only on machines without pyserial
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]
    HAVE_PYSERIAL = False

#: CH340 USB-serial bridge, as fitted to the ESP32-2432S028R.
CH340_VID_PID = (0x1A86, 0x7523)

PROBE_TIMEOUT = 3.0
#: How often the probe repeats its PING while it waits. See probe() for why it repeats at all.
PING_INTERVAL = 0.4
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0)

#: probe() returns this verbatim and find_port() tests for it, so keep the two in step.
BUSY = "in use by another program"


class SerialLink:
    """A resilient line-oriented link. Every send is best-effort: a dropped board must never
    take the tray app down, it should just reconnect when the board comes back."""

    #: Which way this reaches the board. The settings window asks, because WiFi provisioning is
    #: only accepted over the cable.
    kind = "serial"

    def __init__(self, port: str | None = None, baud: int = 115200, dry_run: bool = False):
        self.configured_port = port
        self.baud = baud
        self.dry_run = dry_run
        self.port: str | None = None
        #: Why there is no port, in words a user can act on. Shown by the tray and the settings
        #: window: without it a failed scan is completely silent, which is what made this hard to
        #: diagnose in the first place.
        self.last_error: str | None = None
        self._serial = None
        self._rx = ""
        self._failures = 0
        self._next_attempt = 0.0
        # Port discovery opens and probes every COM port in turn, which takes seconds. It runs on
        # its own thread so a missing board never stalls presence updates in the caller's loop.
        self._discovery: threading.Thread | None = None
        self._discovered: tuple[int, str | None, str | None] | None = None
        #: Bumped by force_rescan(). A scan carries the generation it started under, so an answer
        #: from before the user asked to reconnect is dropped rather than believed.
        self._scan_generation = 0
        #: The first scan of a run, and every forced one, logs each port at INFO. The rest stay at
        #: debug: this repeats every ten seconds and would otherwise fill the rotating log.
        self._verbose_scan = True
        self._lock = threading.Lock()

    # -- connection ----------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.dry_run or self._serial is not None

    def ensure_connected(self) -> bool:
        """Connect if possible, without ever blocking for long. Returns whether the link is usable.

        Discovery is asynchronous, so this returns False while a scan is still running; the caller
        simply tries again on its next tick.
        """
        if self.dry_run or self._serial is not None:
            return True

        port = self.configured_port
        if port is None:
            port = self._poll_discovery()
            if port is None:
                return False

        try:
            self._serial = open_serial(port, self.baud, timeout=0.1, write_timeout=2.0)
        except Exception as exc:
            log.warning("could not open %s: %s", port, exc)
            self.last_error = (
                f"{port} {BUSY}" if port_is_busy(exc) else f"could not open {port}: {exc}"
            )
            self._schedule_retry()
            return False

        self.port = port
        self.last_error = None
        self._failures = 0
        self._rx = ""
        log.info("connected to %s at %d baud", port, self.baud)
        return True

    def force_rescan(self) -> None:
        """Drop the link and look again immediately, whatever the backoff had decided.

        This is what the Reconnect button asks for. Without it the button did nothing at all in
        the case that matters -- an app that had never connected -- because there was no port to
        close and the retry timer, ten seconds by the fourth failure, was left exactly as it was.

        Runs on the worker thread, which owns the port. See Worker.reconnect() in pc_app/main.py.
        """
        self.close()
        with self._lock:
            self._failures = 0
            self._next_attempt = 0.0
            self._discovered = None
            # A scan may be in flight. It cannot be cancelled, so let it finish and be ignored.
            self._scan_generation += 1
            self._verbose_scan = True
        self.last_error = None

    def _poll_discovery(self) -> str | None:
        """Return a discovered port, starting or reaping the scan thread as needed."""
        with self._lock:
            if self._discovery is not None:
                if self._discovery.is_alive():
                    return None  # scan still running
                self._discovery = None
                result, self._discovered = self._discovered, None
                if result is not None and result[0] == self._scan_generation:
                    _, found, error = result
                    self.last_error = error
                    if found is not None:
                        return found
                    self._schedule_retry()
                    return None
                # Stale answer, or a thread that died without leaving one: scan again below.

            if time.monotonic() < self._next_attempt:
                return None

            generation = self._scan_generation
            verbose = self._verbose_scan
            self._verbose_scan = False

            def scan() -> None:
                found, error = find_port(self.baud, verbose=verbose)
                with self._lock:
                    self._discovered = (generation, found, error)

            self._discovery = threading.Thread(target=scan, name="port-scan", daemon=True)
            self._discovery.start()
            return None

    def _schedule_retry(self) -> None:
        delay = RECONNECT_BACKOFF[min(self._failures, len(RECONNECT_BACKOFF) - 1)]
        self._failures += 1
        self._next_attempt = time.monotonic() + delay

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
        self.port = None

    def _drop(self, exc: Exception) -> None:
        log.warning("serial link to %s lost: %s", self.port, exc)
        self.last_error = f"lost {self.port}: {exc}"
        self.close()
        self._schedule_retry()

    # -- traffic -------------------------------------------------------------------------

    def send(self, line: str) -> bool:
        """Send one line. Returns whether it went out."""
        if self.dry_run:
            try:
                print(f"TX {line}", flush=True)
            except OSError:
                # No usable stdout (piped and closed, or a windowed build); the log still has it.
                log.info("TX %s", line)
            return True
        if not self.ensure_connected():
            return False
        try:
            self._serial.write((line + "\n").encode("ascii", errors="replace"))
            return True
        except Exception as exc:
            self._drop(exc)
            return False

    def read_lines(self) -> Iterator[str]:
        """Yield whatever complete lines the board has sent since the last call."""
        if self.dry_run or self._serial is None:
            return
        try:
            waiting = self._serial.in_waiting
            if not waiting:
                return
            self._rx += self._serial.read(waiting).decode("ascii", errors="replace")
        except Exception as exc:
            self._drop(exc)
            return
        lines, self._rx = split_lines(self._rx)
        yield from lines


def split_lines(buffer: str) -> tuple[list[str], str]:
    """Complete lines out of *buffer*, and whatever is left of a line that has not arrived yet.

    Shared with pc_app/net_link.py: both transports frame the same way, because the board does.
    """
    parts = buffer.split("\n")
    remainder = parts.pop()
    return [line for line in (part.strip() for part in parts) if line], remainder


def open_serial(port: str, baud: int, timeout: float, write_timeout: float):
    """Open *port* without pulsing DTR and RTS.

    Board revisions that carry the ESP32 auto-reset circuit reboot when those lines are asserted,
    which is exactly what opening a port normally does -- so a scan would reset the board once per
    probe and then have to outwait its boot. Choosing the line states means building the port
    unopened, which is the only way pyserial offers.
    """
    conn = serial.Serial()
    conn.port = port
    conn.baudrate = baud
    conn.timeout = timeout
    conn.write_timeout = write_timeout
    conn.dtr = False
    conn.rts = False
    conn.open()
    return conn


def port_is_busy(exc: Exception) -> bool:
    """Whether *exc* means another program is holding the port.

    Worth a function: pyserial wraps the Windows error in a SerialException, so the type alone
    never matches -- what arrives is a SerialException whose text contains a repr of the
    PermissionError. The class name in that repr is the one part of it Windows does not localise.
    """
    if isinstance(exc, PermissionError):
        return True
    if isinstance(getattr(exc, "__cause__", None) or getattr(exc, "__context__", None),
                  PermissionError):
        return True
    return "permissionerror" in str(exc).lower()


def candidate_ports() -> list[str]:
    """COM ports worth probing, CH340 devices first."""
    if not HAVE_PYSERIAL:
        return []
    ch340: list[str] = []
    others: list[str] = []
    for info in list_ports.comports():
        if (info.vid, info.pid) == CH340_VID_PID:
            ch340.append(info.device)
        else:
            others.append(info.device)
    return ch340 + others


def ch340_ports() -> list[str]:
    """Just the ports whose VID:PID says our own USB-serial bridge is on the end."""
    if not HAVE_PYSERIAL:
        return []
    return [i.device for i in list_ports.comports() if (i.vid, i.pid) == CH340_VID_PID]


def probe(port: str, baud: int = 115200, timeout: float = PROBE_TIMEOUT) -> str:
    """Open *port*, ask whether our board is on the end of it, and say what happened.

    Returns "ok", or a short reason written to be read by a person -- it reaches the log and the
    settings window.

    The handshake matters: several unrelated devices show up as COM ports, and writing status
    lines into somebody's serial console would be rude at best.

    PING is repeated, and preceded by a bare newline, because the board assembles lines byte by
    byte and acts on them only when the newline arrives. One stray byte already sitting in its
    buffer -- routine on the first open of a CH340 port after the PC cold-boots -- turns a single
    PING into an unrecognised line, and no amount of retrying at this end would have fixed it.
    That is what left a board plugged in at boot undiscoverable until the cable was pulled.
    """
    if not HAVE_PYSERIAL:
        return "pyserial is not installed"
    try:
        with open_serial(port, baud, timeout=0.2, write_timeout=2.0) as conn:
            # Revisions that do reset when the port opens need a moment before they can hear us.
            time.sleep(0.3)
            conn.reset_input_buffer()
            deadline = time.monotonic() + timeout
            next_ping = 0.0
            buffer = ""
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_ping:
                    # The newline first: it closes off whatever partial line the board is holding,
                    # so the PING behind it arrives as a line of its own.
                    conn.write(b"\nPING\n")
                    next_ping = now + PING_INTERVAL
                buffer += conn.read(64).decode("ascii", errors="replace")
                if "PONG" in buffer or "READY:" in buffer:
                    return "ok"
            return "no answer"
    except Exception as exc:
        if port_is_busy(exc):
            return BUSY
        return str(exc) or exc.__class__.__name__


def find_port(baud: int = 115200, verbose: bool = False) -> tuple[str | None, str | None]:
    """First port that answers our handshake, and if none does, why not.

    Returns (port, reason); exactly one of the two is set.
    """
    if not HAVE_PYSERIAL:
        return None, "pyserial is not installed"

    say = log.info if verbose else log.debug
    ports = candidate_ports()
    if not ports:
        return None, "no COM ports found"

    say("probing %s", ", ".join(ports))
    busy: list[str] = []
    for port in ports:
        result = probe(port, baud)
        if result == "ok":
            log.info("found board on %s", port)
            return port, None
        if result == BUSY:
            log.warning("%s is %s", port, BUSY)
            busy.append(port)
        else:
            say("no board on %s: %s", port, result)

    # Nothing answered. If exactly one CH340 is attached it is almost certainly our board with its
    # line parser wedged, and somebody running the packaged .exe can neither see that nor fix it.
    # Take the port anyway -- gated on our own VID:PID, so no unrelated device is ever written to
    # -- rather than leave a display stuck on DISCONNECTED until the cable is pulled.
    ours = [p for p in ch340_ports() if p not in busy]
    if len(ours) == 1:
        log.warning("no answer from %s, but it is the only CH340 attached -- using it anyway",
                    ours[0])
        return ours[0], None

    if busy:
        return None, f"{', '.join(busy)} {BUSY}"
    return None, f"no board answered on {', '.join(ports)}"
