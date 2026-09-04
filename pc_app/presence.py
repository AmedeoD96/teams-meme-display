"""Turn raw Teams log vocabulary into the status enum defined in docs/PROTOCOL.md.

The enum here and firmware/src/status.cpp must agree; docs/PROTOCOL.md is the contract.
"""

from __future__ import annotations

import logging
import time
from enum import Enum

log = logging.getLogger(__name__)


class Status(str, Enum):
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    IN_MEETING = "IN_MEETING"
    DND = "DND"
    AWAY = "AWAY"
    BRB = "BRB"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"
    #: Firmware-only; the PC never sends this. Present so the tray can display it.
    DISCONNECTED = "DISCONNECTED"

    def __str__(self) -> str:
        return self.value


#: Raw Teams availability -> our status. Idle variants collapse onto their base status: Teams
#: reports e.g. AvailableIdle when the machine has been untouched but the user is still green.
AVAILABILITY_MAP: dict[str, Status] = {
    "available": Status.AVAILABLE,
    "availableidle": Status.AVAILABLE,
    "busy": Status.BUSY,
    "busyidle": Status.BUSY,
    "donotdisturb": Status.DND,
    "away": Status.AWAY,
    "berightback": Status.BRB,
    "offline": Status.OFFLINE,
    "presenceunknown": Status.UNKNOWN,
}

#: Statuses that become IN_MEETING when a call is up. Teams reports plain Busy during a meeting,
#: so the call markers from the SlimCore log are what separate the two.
CALL_UPGRADABLE = (Status.BUSY, Status.AVAILABLE)

#: Tray icon / display accent per status. Kept in sync with THEME in firmware/src/status.cpp.
STATUS_COLOR: dict[Status, str] = {
    Status.AVAILABLE: "#2ECC71",
    Status.BUSY: "#E74C3C",
    Status.IN_MEETING: "#8E44AD",
    Status.DND: "#B03A2E",
    Status.AWAY: "#F39C12",
    Status.BRB: "#E67E22",
    Status.OFFLINE: "#7F8C8D",
    Status.UNKNOWN: "#566573",
    Status.DISCONNECTED: "#34495E",
}

# Human-readable labels live in i18n.py, which has them in every supported language.

def map_availability(availability: str | None, in_call: bool = False) -> Status:
    """Map one raw availability plus the call flag onto a status."""
    if availability is None:
        return Status.UNKNOWN
    status = AVAILABILITY_MAP.get(availability.strip().lower())
    if status is None:
        # An unrecognised value is more likely a Teams change than a real state, so say so
        # loudly once rather than silently showing the wrong meme.
        log.warning("unrecognised Teams availability %r; treating as UNKNOWN", availability)
        return Status.UNKNOWN
    if in_call and status in CALL_UPGRADABLE:
        return Status.IN_MEETING
    return status


class PresenceEngine:
    """Debounces raw log observations into a stable status.

    Teams emits bursts of presence lines around a change (and briefly reports PresenceUnknown
    while reconnecting). Committing every one of those would make the display flap, so a new
    status must hold for *debounce_seconds* before it is published.
    """

    def __init__(self, debounce_seconds: float = 2.0, initial: Status = Status.UNKNOWN):
        self.debounce_seconds = debounce_seconds
        self.status = initial
        self._pending: Status | None = None
        self._pending_since = 0.0
        self._override: Status | None = None

    @property
    def override(self) -> Status | None:
        return self._override

    def set_override(self, status: Status | None) -> None:
        """Force a status regardless of Teams, or pass None to resume following Teams."""
        self._override = status
        if status is not None:
            self.status = status

    def observe(
        self,
        availability: str | None,
        in_call: bool = False,
        log_found: bool = True,
        now: float | None = None,
    ) -> Status:
        """Feed one observation in; returns the currently published status."""
        if self._override is not None:
            self.status = self._override
            return self.status

        now = time.monotonic() if now is None else now
        candidate = map_availability(availability, in_call) if log_found else Status.UNKNOWN

        if candidate == self.status:
            self._pending = None
            return self.status

        if candidate != self._pending:
            self._pending = candidate
            self._pending_since = now

        if now - self._pending_since >= self.debounce_seconds:
            log.info("status %s -> %s", self.status, candidate)
            self.status = candidate
            self._pending = None
        return self.status


def teams_is_running() -> bool | None:
    """Whether the new Teams client is running.

    Returns None when psutil is not installed, so callers can tell "no" apart from "cannot
    tell" -- we never want a missing optional dependency to force the display to UNKNOWN.
    """
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in ("ms-teams.exe", "msteams.exe"):
                return True
    except Exception as exc:  # psutil raises a family of OS-specific errors here
        log.debug("process scan failed: %s", exc)
        return None
    return False
