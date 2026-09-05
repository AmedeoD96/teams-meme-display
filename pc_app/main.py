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
from pc_app.i18n import DISPLAY_MODES, ORIENTATIONS, TONES, normalise, normalise_tone
from pc_app.phrases import MAX_PHRASE_CHARS, PhraseBank
from pc_app.presence import Status, teams_is_running
from pc_app.presence import PresenceEngine
from pc_app.serial_link import HAVE_PYSERIAL, SerialLink
from pc_app.teams_log import TeamsLogWatcher
from pc_app.text import to_display_ascii

log = logging.getLogger("teams_meme")

VERSION = "1.3.0"


class Worker:
    """Polls the Teams logs and keeps the board in sync with the result."""

    def __init__(self, config: Config, dry_run: bool = False, phrases: PhraseBank | None = None):
        self.config = config
        self.watcher = TeamsLogWatcher(config.log_dir, cloud_context=config.cloud_context)
        self.engine = PresenceEngine(debounce_seconds=config.debounce_seconds)
        self.link = SerialLink(port=config.port, baud=config.baud, dry_run=dry_run)
        self.dry_run = dry_run
        #: Phrasing is chosen here rather than on the board, so an edit in the GUI reaches the
        #: screen on the next rotation tick without a rebuild. See pc_app/phrases.py.
        self.phrases = phrases if phrases is not None else PhraseBank.load()

        self._stop = threading.Event()
        self._was_connected = False
        self._last_sent_status: Status | None = None
        self._last_status_sent_at = 0.0
        self._last_caption_sent_at = 0.0
        self._last_caption = ""
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
        self.refresh_caption()

    def refresh_caption(self) -> None:
        """Ask for a new phrase on the next tick.

        Called whenever the thing a phrase depends on changes -- the status, the tone, the
        language -- and when the board reports a tap.
        """
        self._last_caption_sent_at = 0.0

    def set_tone(self, tone: str) -> None:
        self.config.tone = normalise_tone(tone)
        self.config.save()
        self.queue_command(f"TONE:{self.config.tone}")
        # The face follows from TONE:, but the words are chosen here, so they need a fresh pick.
        self.refresh_caption()
        log.info("tone %s", self.config.tone)

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
            self.refresh_caption()  # the old phrase was about the old status

        connected = self.link.ensure_connected()
        if connected and not self._was_connected:
            # Fresh link (or a reconnect): push settings and force a status resend. The board
            # persists these in NVS and ignores any that already match, so resending is cheap.
            self.link.send(f"BRIGHT:{self.config.brightness}")
            self.link.send(f"ROTATE:{self.config.rotate_seconds}")
            self.link.send(f"LANG:{normalise(self.config.language)}")
            self.link.send(f"ORIENT:{self.config.orientation}")
            self.link.send(f"MODE:{self.config.display_mode}")
            self.link.send(f"TONE:{normalise_tone(self.config.tone)}")
            self.link.send(f"TRANSITION:{self.config.transition_ms}")
            self._last_sent_status = None
            self._last_clock = ""
            self.refresh_caption()
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

        # The board rotates memes on its own timer, but the PC owns the words -- it is the side
        # that holds the phrase bank and knows the tone. A rotate interval of 0 disables the
        # timer without disabling the resend that a status, tone or language change asks for.
        rotate = self.config.rotate_seconds
        if self._last_caption_sent_at == 0.0 or (
            rotate > 0 and now - self._last_caption_sent_at >= rotate
        ):
            self._send_caption(status, now)

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
                self.refresh_caption()
            elif line.startswith("EVT:"):
                event = line[4:]
                # A tap on the panel. The board picks its own meme; the words come from here.
                if event == "NEXT":
                    self.refresh_caption()
                log.debug("board event: %s", event)
            elif line != "PONG":
                log.debug("board: %s", line)

    def _send_caption(self, status: Status, now: float) -> None:
        """Pick a phrase for the current status and tone, and put it on the wire."""
        phrase = self.phrases.pick(status, self.config.tone, self.config.language)
        folded, lost = to_display_ascii(phrase[:MAX_PHRASE_CHARS])
        if lost:
            # Not fatal -- to_display_ascii already substituted -- but worth saying once.
            log.debug("phrase %r contains characters the display cannot draw: %s",
                      phrase, "".join(sorted(set(lost))))
        if self.link.send(f"CAPTION:{folded}"):
            self._last_caption = folded
            self._last_caption_sent_at = now

    def show_caption(self, text: str) -> None:
        """Put one specific phrase on the screen now. Used by the GUI's preview button.

        Queued rather than sent directly: the serial port belongs to the worker thread.
        """
        folded, _ = to_display_ascii(text[:MAX_PHRASE_CHARS])
        self.queue_command(f"CAPTION:{folded}")
        # Let it stand for a full rotation interval instead of being replaced on the next tick.
        self._last_caption = folded
        self._last_caption_sent_at = time.monotonic()

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
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="tray icon only, without the settings window",
    )
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
    if config.tone not in TONES:
        log.warning("unknown tone %r; using normal", config.tone)
        config.tone = normalise_tone(config.tone)
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

    thread = threading.Thread(target=worker.run, name="worker", daemon=True)
    thread.start()
    try:
        run_shell(worker, config, want_gui=not args.no_gui)
    finally:
        worker.stop()
        thread.join(timeout=3.0)
    return 0


def run_shell(worker: Worker, config: Config, want_gui: bool = True) -> None:
    """Run the tray icon, with the settings window if we can have one.

    tkinter ships with CPython on Windows but can be absent from a trimmed install, and it is the
    only thing the window needs that the tray does not -- so losing it costs the settings window,
    not the app.
    """
    if want_gui:
        try:
            from pc_app.gui import run_app
        except ImportError as exc:
            log.warning("no settings window (%s); falling back to the tray menu alone", exc)
        else:
            run_app(worker, config)
            return

    from pc_app.tray import run_tray

    run_tray(worker, config)


if __name__ == "__main__":
    raise SystemExit(main())
