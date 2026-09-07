"""User settings, stored as JSON under %APPDATA%\\TeamsMemeDisplay\\config.json."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "TeamsMemeDisplay"


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    #: COM port to use, or None to auto-detect (see serial_link.find_port).
    port: str | None = None
    baud: int = 115200

    #: Override the Teams log folder. None means the documented default location.
    log_dir: str | None = None
    #: When several Teams accounts are signed in, only trust presence from the account whose
    #: cloud_context contains this substring. None means "first account that reports a real value".
    cloud_context: str | None = None

    #: How often to check the logs for new lines.
    poll_seconds: float = 1.0
    #: How long a new status must hold before it is published, to absorb Teams' bursts.
    debounce_seconds: float = 2.0
    #: Resend STATUS this often even when unchanged, to feed the firmware's watchdog.
    heartbeat_seconds: float = 5.0

    #: Passed to the firmware on connect.
    brightness: int = 80
    rotate_seconds: int = 30
    send_clock: bool = True
    #: Caption language on the device and in the tray menu: "en" or "it".
    language: str = "it"
    #: Screen orientation: "landscape" or "portrait". The board needs memes built for whichever
    #: one you pick (tools/build_memes.py builds both by default).
    orientation: str = "portrait"
    #: "mascot" draws the animated character; "image" shows a meme with a caption band; "text"
    #: shows the caption alone on the status colour, with no images involved.
    display_mode: str = "mascot"
    #: How the phrases are worded: "normal", "sarcastic" or "retriever". Independent of the real
    #: Teams status -- sarcasm mode stays discouraging while you are green. The device is told
    #: this only so the mascot can pull the matching face; the phrasing itself is chosen here.
    tone: str = "normal"
    #: Milliseconds a caption change is allowed to take. 0 switches instantly.
    transition_ms: int = 400

    # -- out-of-hours alert ----------------------------------------------------------------
    # A Teams notification arriving outside the working window below plays a GIF on the board
    # for a moment. See pc_app/work_hours.py for the window rules and docs/PROTOCOL.md for the
    # ALERT: command this ends up sending.

    #: Master switch. With this off the notification count is still parsed but nothing is sent.
    alert_enabled: bool = True
    #: The working window, "HH:MM". An end below the start wraps past midnight (22:00-06:00).
    work_start: str = "09:00"
    work_end: str = "18:00"
    #: Weekdays that count as working, Monday=0, matching datetime.weekday().
    work_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    #: How long the GIF plays before the board goes back to the status display.
    alert_seconds: float = 6.0
    #: Minimum gap between two alerts, so a burst of messages does not loop the GIF.
    alert_cooldown_seconds: float = 60.0

    start_with_windows: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            log.info("no config at %s, using defaults", path)
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt config should not stop the app from running.
            log.warning("could not read %s (%s); using defaults", path, exc)
            return cls()
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            log.warning("ignoring unknown config keys: %s", ", ".join(sorted(unknown)))
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        log.info("saved config to %s", path)
        return path
