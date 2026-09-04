"""Entry point for the Teams status meme display tray app.

    python pc_app/main.py --dry-run     # print the serial lines instead of sending them
    python pc_app/main.py               # run in the system tray
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # allow both `python pc_app/main.py` and `python -m pc_app.main`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "pc_app"

from pc_app.config import Config, config_dir, config_path
from pc_app.i18n import DISPLAY_MODES, ORIENTATIONS, normalise
from pc_app.presence import Status, teams_is_running
from pc_app.presence import PresenceEngine
from pc_app.serial_link import HAVE_PYSERIAL, SerialLink
from pc_app.teams_log import TeamsLogWatcher

log = logging.getLogger("teams_meme")

VERSION = "1.2.0"


class Worker:
    """Polls the Teams logs and keeps the board in sync with the result."""

    def __init__(self, config: Config, dry_run: bool = False):
        self.config = config
        self.watcher = TeamsLogWatcher(config.log_dir, cloud_context=config.cloud_context)
        self.engine = PresenceEngine(debounce_seconds=config.debounce_seconds)
        self.link = SerialLink(port=config.port, baud=config.baud, dry_run=dry_run)
        self.dry_run = dry_run

        self._stop = threading.Event()
        self._was_connected = False
        self._last_sent_status: Status | None = None
        self._last_status_sent_at = 0.0
        self._last_clock = ""
        #: Set by the tray so the next tick pushes a NEXT command.
        self._pending_commands: list[str] = []
        self._lock = threading.Lock()
        #: Called with the published Status whenever it changes, so the tray icon can follow.
        self.on_status_change = lambda status: None

    # -- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        state = self.watcher.prime()
        if not state.log_found:
            log.warning(
                "no Teams logs in %s -- is the new Teams client installed? "
                "Status will stay UNKNOWN until it appears.",
                self.watcher.log_dir,
            )
        else:
            log.info(
                "primed: availability=%s in_call=%s accounts=%s",
                state.availability,
                state.in_call,
                list(state.accounts) or "n/a",
            )
        if teams_is_running() is False:
            log.warning("Teams does not appear to be running; presence may be stale")
        self.engine.observe(state.availability, state.in_call, state.log_found)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    # One bad tick (a locked log file, a yanked USB cable) must not kill the app.
                    log.exception("tick failed")
                self._stop.wait(self.config.poll_seconds)
        finally:
            self.link.close()

    # -- tray hooks ----------------------------------------------------------------------

    def queue_command(self, line: str) -> None:
        with self._lock:
            self._pending_commands.append(line)

    def set_override(self, status: Status | None) -> None:
        self.engine.set_override(status)
        log.info("manual override %s", status or "cleared")

    def reconnect(self) -> None:
        log.info("reconnecting on request")
        self.link.close()
        self._was_connected = False

    # -- the work ------------------------------------------------------------------------

    def tick(self) -> None:
        state = self.watcher.poll()
        previous = self.engine.status
        status = self.engine.observe(state.availability, state.in_call, state.log_found)
        if status != previous:
            self.on_status_change(status)

        connected = self.link.ensure_connected()
        if connected and not self._was_connected:
            # Fresh link (or a reconnect): push settings and force a status resend. The board
            # persists these in NVS and ignores any that already match, so resending is cheap.
            self.link.send(f"BRIGHT:{self.config.brightness}")
            self.link.send(f"ROTATE:{self.config.rotate_seconds}")
            self.link.send(f"LANG:{normalise(self.config.language)}")
            self.link.send(f"ORIENT:{self.config.orientation}")
            self.link.send(f"MODE:{self.config.display_mode}")
            self.link.send(f"TRANSITION:{self.config.transition_ms}")
            self._last_sent_status = None
            self._last_clock = ""
        self._was_connected = connected
        if not connected:
            return

        for line in self._drain_commands():
            self.link.send(line)

        now = time.monotonic()
        stale = now - self._last_status_sent_at >= self.config.heartbeat_seconds
        if status != self._last_sent_status or stale:
            if self.link.send(f"STATUS:{status}"):
                self._last_sent_status = status
                self._last_status_sent_at = now

        if self.config.send_clock:
            clock = time.strftime("%H:%M")
            if clock != self._last_clock and self.link.send(f"TIME:{clock}"):
                self._last_clock = clock

        for line in self.link.read_lines():
            if line.startswith("LOG:"):
                log.debug("board: %s", line[4:])
            elif line.startswith("READY:"):
                log.info("board booted: %s", line[6:])
                self._last_sent_status = None  # it lost its state, so resend
            elif line != "PONG":
                log.debug("board: %s", line)

    def _drain_commands(self) -> list[str]:
        with self._lock:
            commands, self._pending_commands = self._pending_commands, []
        return commands


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Teams status meme display")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the serial lines that would be sent instead of opening a port",
    )
    parser.add_argument("--no-tray", action="store_true", help="run in the console, no tray icon")
    parser.add_argument("--port", help="COM port to use instead of auto-detecting")
    parser.add_argument("--log-dir", help="override the Teams log folder")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> Path | None:
    """Log to the console and to a rotating file.

    The file matters: the packaged .exe is a windowed app with no console at all, so without it
    a failure to start would be completely silent.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)

    # A windowed PyInstaller build has no console: sys.stderr is None there, and a StreamHandler
    # attached to it fails on every single log call.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    try:
        from logging.handlers import RotatingFileHandler

        path = config_dir() / "app.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=512_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        root.addHandler(handler)
        return path
    except OSError as exc:
        root.warning("could not open the log file: %s", exc)
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_path = setup_logging(args.verbose)

    config = Config.load()
    if args.port:
        config.port = args.port
    if args.log_dir:
        config.log_dir = args.log_dir

    if config.orientation not in ORIENTATIONS:
        log.warning("unknown orientation %r; using landscape", config.orientation)
        config.orientation = "landscape"
    if config.display_mode not in DISPLAY_MODES:
        log.warning("unknown display mode %r; using image", config.display_mode)
        config.display_mode = "image"
    if normalise(config.language) != config.language:
        log.warning("unknown language %r; using en", config.language)
        config.language = normalise(config.language)

    log.info("teams-status %s (config: %s)", VERSION, config_path())
    if log_path:
        log.info("logging to %s", log_path)
    if not HAVE_PYSERIAL and not args.dry_run:
        log.error("pyserial is not installed -- run: pip install -r pc_app/requirements.txt")
        return 2

    worker = Worker(config, dry_run=args.dry_run)
    worker.start()

    if args.dry_run or args.no_tray:
        log.info("running headless; Ctrl-C to stop")
        try:
            worker.run()
        except KeyboardInterrupt:
            log.info("stopping")
        return 0

    from pc_app.tray import run_tray

    thread = threading.Thread(target=worker.run, name="worker", daemon=True)
    thread.start()
    run_tray(worker, config)
    worker.stop()
    thread.join(timeout=3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
