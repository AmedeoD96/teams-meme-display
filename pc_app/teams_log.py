"""Discover, tail and parse the new Microsoft Teams client logs.

Teams writes its presence into rotating log files under

    %LOCALAPPDATA%\\Packages\\MSTeams_8wekyb3d8bbwe\\LocalCache\\Microsoft\\MSTeams\\Logs

This module knows only about *raw* Teams vocabulary ("Available", "Busy", "PresenceUnknown", ...)
and whether a call is up. Mapping that onto our own status enum is presence.py's job.

None of this is documented by Microsoft; the formats below were confirmed against real logs.
If Teams changes them, this is the file to fix -- see tests/test_teams_log.py.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_LOG_DIR = r"%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache\Microsoft\MSTeams\Logs"

# Presence, preferred form. Carries the account's cloud context, so it is the only one that can be
# attributed to a specific account when several are signed in:
#   UserPresenceAction: {cloud_context: https://teams.microsoft.com, availability: Available}
USER_PRESENCE_ACTION = re.compile(
    r"UserPresenceAction:\s*\{cloud_context:\s*([^,]+),\s*availability:\s*(\w+)\s*\}"
)

# Presence, fallback form. Appears once per signed-in account on a single line, e.g.
#   State Event: UserDataGlobalState total number of users: 2
#   { availability: PresenceUnknown, unread notification count: 0 }
#   { availability: Available, unread notification count: 0 }
# so it must be scanned with findall and PresenceUnknown entries discarded.
AVAILABILITY_BLOCK = re.compile(r"\{ availability: (\w+), unread notification count: (\d+) \}")

# Call state, from the SlimCore media stack log. Teams never logs "in a meeting" as an
# availability, so this is the only signal that distinguishes BUSY from IN_MEETING.
CALL_START = re.compile(
    r"SlimCoreModule::WindowContentProtectionProvider: SetWindowContentProtection"
)
CALL_END = re.compile(r"SlimCoreModule::WindowContentProtectionProvider: UnregisterCall")

MAIN_LOG_RE = re.compile(r"^MSTeams_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2}\.\d+)\.log$")
SLIMCORE_LOG_RE = re.compile(
    r"^MSTeamsNM_SlimCore_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2}\.\d+)\.log$"
)

PRESENCE_UNKNOWN = "PresenceUnknown"

#: Cap on the backward scan performed when we first attach to a log file. Main logs run to a few
#: hundred KB; this only guards against a pathological one.
PRIME_SCAN_BYTES = 8 * 1024 * 1024

#: How many main logs to walk back through when recovering the current presence at startup.
#: Teams rotates its log on every launch, and when it is running with the web client suspended it
#: writes no presence at all -- so the newest file is frequently empty of presence and the real
#: last-known value lives in an earlier one.
PRIME_MAX_FILES = 6

#: Do not recover presence from a log older than this. A value from last week is worse than
#: honestly reporting UNKNOWN until Teams logs something current.
PRIME_MAX_AGE_DAYS = 2


def expand_log_dir(raw: str | None = None) -> Path:
    """Expand a configured log folder, tolerating both %VAR% and ~ forms."""
    return Path(os.path.expandvars(os.path.expanduser(raw or DEFAULT_LOG_DIR)))


def same_path(a: Path, b: Path) -> bool:
    """Compare two paths for identity, tolerating separator and case differences."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def recent_logs(log_dir: Path, pattern: re.Pattern[str], limit: int | None = None) -> list[Path]:
    """Logs matching *pattern*, newest first, ranked by the timestamp *in the filename*.

    Filename order is used rather than mtime: during rotation Teams can touch an older file, and
    an mtime-based pick then flips between two files and replays stale lines.
    """
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return []

    ranked: list[tuple[tuple[str, str], str]] = []
    for entry in entries:
        match = pattern.match(entry.name)
        if match:
            ranked.append(((match.group(1), match.group(2)), entry.name))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if limit is not None:
        ranked = ranked[:limit]
    # Rebuild from log_dir rather than returning the iterdir() entry so the result is joined the
    # same way callers join their own paths. Some Python builds (msys2/MinGW, where os.sep is "/")
    # produce mixed-separator paths from iterdir() that do not compare equal to a "/" join.
    return [log_dir / name for _key, name in ranked]


def newest_log(log_dir: Path, pattern: re.Pattern[str]) -> Path | None:
    """Newest log matching *pattern*, or None if there is none."""
    logs = recent_logs(log_dir, pattern, limit=1)
    return logs[0] if logs else None


def log_age_days(path: Path, pattern: re.Pattern[str], today: date | None = None) -> float:
    """Age of a log in days, taken from the date in its filename."""
    match = pattern.match(path.name)
    if not match:
        return float("inf")
    try:
        stamp = datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return float("inf")
    return ((today or date.today()) - stamp).days


class Tail:
    """Byte-offset tail of a single file, tolerant of rotation and truncation."""

    def __init__(self, path: Path, start_at_end: bool = True):
        self.path = path
        self.offset = 0
        self._partial = ""
        if start_at_end:
            try:
                self.offset = path.stat().st_size
            except OSError:
                self.offset = 0

    def read_new_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []

        if size < self.offset:
            # File was truncated or replaced under the same name; start over.
            log.debug("%s shrank (%d < %d), restarting tail", self.path.name, size, self.offset)
            self.offset = 0
            self._partial = ""
        if size == self.offset:
            return []

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except OSError as exc:
            log.debug("could not read %s: %s", self.path, exc)
            return []

        chunk = self._partial + chunk
        self._partial = ""
        lines = chunk.split("\n")
        # A trailing fragment means Teams is mid-write; hold it until the rest arrives.
        if not chunk.endswith("\n"):
            self._partial = lines.pop()
        return [line for line in lines if line]


@dataclass
class LogState:
    """What the logs currently say. A None availability means "nothing found yet"."""

    availability: str | None = None
    in_call: bool = False
    log_found: bool = False
    unread: int = 0
    accounts: dict[str, str] = field(default_factory=dict)


def parse_availability(line: str, cloud_context: str | None = None) -> str | None:
    """Extract an availability from one log line, or None if it carries no presence.

    PresenceUnknown is only returned when it is the *only* thing on the line -- with several
    accounts signed in, the signed-out ones report PresenceUnknown next to the real value.
    """
    contexts = {ctx.strip(): value for ctx, value in USER_PRESENCE_ACTION.findall(line)}
    if contexts:
        if cloud_context:
            for ctx, value in contexts.items():
                if cloud_context in ctx:
                    return value
        for value in contexts.values():
            if value != PRESENCE_UNKNOWN:
                return value
        return next(iter(contexts.values()))

    blocks = [value for value, _unread in AVAILABILITY_BLOCK.findall(line)]
    if not blocks:
        return None
    for value in blocks:
        if value != PRESENCE_UNKNOWN:
            return value
    return blocks[0]


def _read_tail_bytes(path: Path, limit: int) -> list[str]:
    """Read at most the last *limit* bytes of *path* as lines."""
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if size > limit:
                handle.seek(size - limit)
                handle.readline()  # discard the partial line we landed in
            return [line for line in handle.read().split("\n") if line]
    except OSError as exc:
        log.warning("could not scan %s: %s", path, exc)
        return []


class TeamsLogWatcher:
    """Follows the newest main + SlimCore logs and reports the latest presence and call state."""

    def __init__(self, log_dir: Path | str | None = None, cloud_context: str | None = None):
        self.log_dir = log_dir if isinstance(log_dir, Path) else expand_log_dir(log_dir)
        self.cloud_context = cloud_context
        self.state = LogState()
        self._main_tail: Tail | None = None
        self._slim_tail: Tail | None = None

    # -- file rotation -------------------------------------------------------------------

    def _rotate(self, tail: Tail | None, pattern: re.Pattern[str]) -> tuple[Tail | None, bool]:
        """Return the tail to use for *pattern* plus whether it is a newly opened file."""
        newest = newest_log(self.log_dir, pattern)
        if newest is None:
            return None, False
        if tail is not None and same_path(tail.path, newest):
            return tail, False
        if tail is not None:
            log.info("log rotated: %s -> %s", tail.path.name, newest.name)
        # A newly opened file is read from the beginning so nothing written before we noticed the
        # rotation is missed.
        return Tail(newest, start_at_end=False), True

    # -- scanning ------------------------------------------------------------------------

    def _scan_lines(self, lines: list[str]) -> None:
        for line in lines:
            availability = parse_availability(line, self.cloud_context)
            if availability is not None:
                self.state.availability = availability
            for ctx, value in USER_PRESENCE_ACTION.findall(line):
                self.state.accounts[ctx.strip()] = value
            blocks = AVAILABILITY_BLOCK.findall(line)
            if blocks:
                self.state.unread = max(int(unread) for _value, unread in blocks)

    def _scan_call_lines(self, lines: list[str]) -> None:
        for line in lines:
            if CALL_START.search(line):
                self.state.in_call = True
            elif CALL_END.search(line):
                self.state.in_call = False

    def prime(self) -> LogState:
        """Recover the current state by scanning existing logs, before tailing for changes.

        Without this the app would report UNKNOWN until the user next changed their status.

        Presence is searched backwards across several logs, because Teams rotates its log on
        every launch and writes no presence at all while running with its web client suspended --
        so the newest file is often presence-free while the real last-known value sits in an
        earlier one. Call state is deliberately *not* recovered that way: a call cannot outlive
        the Teams restart that rotated the log, so anything older than the current log is stale.
        """
        mains = recent_logs(self.log_dir, MAIN_LOG_RE, limit=PRIME_MAX_FILES)
        slim = newest_log(self.log_dir, SLIMCORE_LOG_RE)
        self.state.log_found = bool(mains)

        for index, path in enumerate(mains):
            age = log_age_days(path, MAIN_LOG_RE)
            if age > PRIME_MAX_AGE_DAYS:
                log.info("stopping prime scan at %s (%.0f days old)", path.name, age)
                break
            self._scan_lines(_read_tail_bytes(path, PRIME_SCAN_BYTES))
            if self.state.availability is not None:
                log.info(
                    "primed from %s -> availability=%s%s",
                    path.name,
                    self.state.availability,
                    "" if index == 0 else f" (fell back {index} log(s))",
                )
                break
        else:
            if mains:
                log.warning(
                    "no presence found in the last %d Teams log(s); "
                    "status stays UNKNOWN until Teams logs one",
                    len(mains),
                )

        if mains:
            self._main_tail = Tail(mains[0], start_at_end=True)
        if slim is not None:
            self._scan_call_lines(_read_tail_bytes(slim, PRIME_SCAN_BYTES))
            self._slim_tail = Tail(slim, start_at_end=True)
            log.info("primed from %s -> in_call=%s", slim.name, self.state.in_call)
        return self.state

    def poll(self) -> LogState:
        """Consume anything new in both logs and return the updated state."""
        self._main_tail, _main_is_new = self._rotate(self._main_tail, MAIN_LOG_RE)
        self._slim_tail, slim_is_new = self._rotate(self._slim_tail, SLIMCORE_LOG_RE)
        self.state.log_found = self._main_tail is not None

        if self._main_tail is not None:
            self._scan_lines(self._main_tail.read_new_lines())
        if self._slim_tail is not None:
            lines = self._slim_tail.read_new_lines()
            has_markers = any(CALL_START.search(l) or CALL_END.search(l) for l in lines)
            # A freshly rotated SlimCore log with no call markers says nothing about the call;
            # keep what we already knew rather than silently dropping to "not in call".
            if has_markers or not slim_is_new:
                self._scan_call_lines(lines)
        return self.state
