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

PROBE_TIMEOUT = 2.0
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0)


class SerialLink:
    """A resilient line-oriented link. Every send is best-effort: a dropped board must never
    take the tray app down, it should just reconnect when the board comes back."""

    def __init__(self, port: str | None = None, baud: int = 115200, dry_run: bool = False):
        self.configured_port = port
        self.baud = baud
        self.dry_run = dry_run
        self.port: str | None = None
        self._serial = None
        self._rx = ""
        self._failures = 0
        self._next_attempt = 0.0
        # Port discovery opens and probes every COM port in turn, which takes seconds. It runs on
        # its own thread so a missing board never stalls presence updates in the caller's loop.
        self._discovery: threading.Thread | None = None
        self._discovered: str | None = None
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
            self._serial = serial.Serial(port, self.baud, timeout=0.1, write_timeout=2.0)
        except Exception as exc:
            log.warning("could not open %s: %s", port, exc)
            self._schedule_retry()
            return False

        self.port = port
        self._failures = 0
        self._rx = ""
        log.info("connected to %s at %d baud", port, self.baud)
        return True

    def _poll_discovery(self) -> str | None:
        """Return a discovered port, starting or reaping the scan thread as needed."""
        with self._lock:
            if self._discovery is not None and not self._discovery.is_alive():
                self._discovery = None
                found, self._discovered = self._discovered, None
                if found is None:
                    self._schedule_retry()
                return found
            if self._discovery is not None:
                return None  # scan still running

        if time.monotonic() < self._next_attempt:
            return None

        def scan() -> None:
            result = find_port(self.baud)
            with self._lock:
                self._discovered = result

        with self._lock:
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
        parts = self._rx.split("\n")
        self._rx = parts.pop()
        for part in parts:
            line = part.strip()
            if line:
                yield line


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


def probe(port: str, baud: int = 115200, timeout: float = PROBE_TIMEOUT) -> bool:
    """Open *port*, send PING, and report whether the board answered.

    The handshake matters: several unrelated devices show up as COM ports, and writing status
    lines into somebody's serial console would be rude at best.
    """
    if not HAVE_PYSERIAL:
        return False
    try:
        with serial.Serial(port, baud, timeout=0.2, write_timeout=2.0) as conn:
            # The ESP32 resets when the port opens; give it a moment before it can hear us.
            time.sleep(0.3)
            conn.reset_input_buffer()
            conn.write(b"PING\n")
            deadline = time.monotonic() + timeout
            buffer = ""
            while time.monotonic() < deadline:
                buffer += conn.read(64).decode("ascii", errors="replace")
                if "PONG" in buffer or "READY:" in buffer:
                    return True
            return False
    except Exception as exc:
        log.debug("probe of %s failed: %s", port, exc)
        return False


def find_port(baud: int = 115200) -> str | None:
    """First port that answers our handshake, or None."""
    for port in candidate_ports():
        log.debug("probing %s", port)
        if probe(port, baud):
            log.info("found board on %s", port)
            return port
    return None
