"""The working day, and whether a moment falls inside it.

Pure functions with no serial and no tkinter, because the interesting cases here are all about
the calendar rather than the hardware: a window that wraps past midnight, a weekday set that
excludes the weekend, the exact minute a window opens and closes, and the lunch gap between the
morning and the afternoon.

The day is two windows rather than one, so the break between them -- lunch, typically -- falls
outside working hours and earns the alert like any other evening. The afternoon block is
optional: leave it unset and the day is a single continuous window, which is what this module
did before.

"Outside work hours" is what the alert keys off, so this deliberately answers the *inside*
question and lets the caller negate it -- there is only one rule to get right that way.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time

log = logging.getLogger(__name__)

#: Monday=0, matching datetime.weekday(). The default working week.
DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)

DEFAULT_MORNING_START = "9:00 AM"
DEFAULT_MORNING_END = "1:00 PM"
DEFAULT_AFTERNOON_START = "2:00 PM"
DEFAULT_AFTERNOON_END = "6:00 PM"

#: One clock time, in either of the two forms a config might hold: "9:00 AM" is what the settings
#: window writes now, "09:00" is what it wrote before, and both have to keep loading. The minutes
#: are optional so "9am" works, and the meridiem is optional so "13:00" does.
_CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{1,2}))?\s*(?:([ap])\.?\s*m\.?)?\s*$", re.IGNORECASE)


def parse_clock(raw: object) -> time | None:
    """Parse one clock time, or None if it cannot be read as one.

    This is the strict form. The callers below decide what an unreadable value should mean: a
    fallback for the morning, which must always resolve to something, or nothing at all for the
    afternoon, which is allowed to be absent.
    """
    if raw is None:
        return None
    match = _CLOCK_RE.match(str(raw))
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem is not None:
        # With AM/PM the hour is a 12-hour one, so "13:00 PM" is not a time.
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_hhmm(raw: str | None, fallback: str) -> time:
    """Parse a clock time, falling back to *fallback* rather than raising.

    A hand-edited config with "half nine" in it should not stop the app from starting, so a bad
    value is logged once and the default used.
    """
    for candidate, is_fallback in ((raw, False), (fallback, True)):
        if candidate is None or not str(candidate).strip():
            continue
        parsed = parse_clock(candidate)
        if parsed is not None:
            return parsed
        if not is_fallback:
            log.warning("could not read %r as a time; using %s", candidate, fallback)
    # Both the value and the fallback were unusable, which means a bad constant in this module.
    return time(0, 0)


def parse_optional(raw: object) -> time | None:
    """Parse a clock time that is allowed to be absent.

    Blank means "no afternoon block", which is a real answer rather than a mistake; only a value
    that is present and unreadable is worth a warning.
    """
    if raw is None or not str(raw).strip():
        return None
    parsed = parse_clock(raw)
    if parsed is None:
        log.warning("could not read %r as a time; ignoring it", raw)
    return parsed


def format_clock(value: time) -> str:
    """Render a time the way the settings window shows it: "9:00 AM", "12:00 PM", "1:05 PM".

    Built by hand rather than with %I, which zero-pads to "09:00 AM" on Windows.
    """
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def normalise_days(raw: object) -> frozenset[int]:
    """Coerce a configured weekday list into a set of 0-6, dropping anything out of range."""
    if raw is None:
        return frozenset(DEFAULT_WORK_DAYS)
    try:
        days = {int(day) for day in raw}  # type: ignore[union-attr]
    except (TypeError, ValueError):
        log.warning("could not read %r as a list of weekdays; using Mon-Fri", raw)
        return frozenset(DEFAULT_WORK_DAYS)
    valid = {day for day in days if 0 <= day <= 6}
    if valid != days:
        log.warning("ignoring out-of-range weekdays in %r", raw)
    return frozenset(valid)


@dataclass(frozen=True)
class Window:
    """A resolved working window: which days, and which minutes of them."""

    start: time
    end: time
    days: frozenset[int]

    @property
    def wraps(self) -> bool:
        """True when the window runs past midnight, e.g. 22:00-06:00."""
        return self.start > self.end

    @classmethod
    def from_config(cls, config) -> "Window":
        """Build the morning window from the alert fields on a Config."""
        return cls(
            start=parse_hhmm(getattr(config, "work_start", None), DEFAULT_MORNING_START),
            end=parse_hhmm(getattr(config, "work_end", None), DEFAULT_MORNING_END),
            days=normalise_days(getattr(config, "work_days", None)),
        )

    def contains(self, moment: datetime) -> bool:
        """Whether *moment* falls inside the working window.

        A window that wraps past midnight is attributed to the day it *started* on: with
        22:00-06:00 on a Monday, 23:00 Monday and 02:00 Tuesday are both inside it, but 02:00
        Monday is not -- that belongs to Sunday's window, which is only open if Sunday is a
        working day.
        """
        clock = moment.time()
        today = moment.weekday()

        if not self.wraps:
            return today in self.days and self.start <= clock < self.end

        if clock >= self.start:
            # The evening half: this is today's window opening.
            return today in self.days
        if clock < self.end:
            # The small-hours half: this belongs to yesterday's window.
            return (today - 1) % 7 in self.days
        return False


@dataclass(frozen=True)
class Schedule:
    """A whole working day: the morning window, and the afternoon one if there is one.

    Composed rather than merged, so every calendar rule -- the midnight wrap, the weekday set,
    the half-open boundaries -- stays in Window and is only written once.
    """

    windows: tuple[Window, ...]

    @classmethod
    def from_config(cls, config) -> "Schedule":
        """Build the day from the alert fields on a Config.

        Both blocks share the one weekday set: nobody works Monday mornings and Tuesday
        afternoons, and a second row of seven checkboxes would earn nothing.
        """
        days = normalise_days(getattr(config, "work_days", None))
        windows = [
            Window(
                start=parse_hhmm(getattr(config, "work_start", None), DEFAULT_MORNING_START),
                end=parse_hhmm(getattr(config, "work_end", None), DEFAULT_MORNING_END),
                days=days,
            )
        ]
        start = parse_optional(getattr(config, "afternoon_start", None))
        end = parse_optional(getattr(config, "afternoon_end", None))
        # An afternoon that is blank, half-filled or empty (start == end) is no afternoon at all,
        # which leaves the single continuous window this module used to have.
        if start is not None and end is not None and start != end:
            windows.append(Window(start=start, end=end, days=days))
        return cls(tuple(windows))

    @property
    def morning(self) -> Window:
        return self.windows[0]

    @property
    def afternoon(self) -> Window | None:
        return self.windows[1] if len(self.windows) > 1 else None

    @property
    def days(self) -> frozenset[int]:
        return self.windows[0].days

    def contains(self, moment: datetime) -> bool:
        """Whether *moment* falls inside any of the day's windows."""
        return any(window.contains(moment) for window in self.windows)

    def gap(self) -> tuple[time, time] | None:
        """The break between the two blocks -- lunch -- or None if there is not one.

        Only reported for the ordinary case: an afternoon that starts after the morning ends,
        with neither block wrapping past midnight. Anything else is a schedule the user built on
        purpose, and naming a "gap" in it would say something untrue.
        """
        afternoon = self.afternoon
        if afternoon is None or self.morning.wraps or afternoon.wraps:
            return None
        if self.morning.end < afternoon.start:
            return self.morning.end, afternoon.start
        return None


def is_within(moment: datetime, config) -> bool:
    """Whether *moment* is inside the working hours configured on *config*."""
    return Schedule.from_config(config).contains(moment)
