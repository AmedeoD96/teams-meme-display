"""The working-hours window, and whether a moment falls inside it.

Pure functions with no serial and no tkinter, because the interesting cases here are all about
the calendar rather than the hardware: a window that wraps past midnight, a weekday set that
excludes the weekend, and the exact minute the window opens and closes.

"Outside work hours" is what the alert keys off, so this deliberately answers the *inside*
question and lets the caller negate it -- there is only one rule to get right that way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time

log = logging.getLogger(__name__)

#: Monday=0, matching datetime.weekday(). The default working week.
DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)

DEFAULT_START = "09:00"
DEFAULT_END = "18:00"


def parse_hhmm(raw: str | None, fallback: str) -> time:
    """Parse "HH:MM", falling back to *fallback* rather than raising.

    A hand-edited config with "9am" in it should not stop the app from starting, so a bad value
    is logged once and the default used.
    """
    for candidate, is_fallback in ((raw, False), (fallback, True)):
        if not candidate:
            continue
        try:
            hour, _, minute = str(candidate).partition(":")
            return time(int(hour), int(minute))
        except ValueError:
            if not is_fallback:
                log.warning("could not read %r as HH:MM; using %s", candidate, fallback)
    # Both the value and the fallback were unusable, which means a bad constant in this module.
    return time(0, 0)


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
        """Build a window from the alert fields on a Config."""
        return cls(
            start=parse_hhmm(getattr(config, "work_start", None), DEFAULT_START),
            end=parse_hhmm(getattr(config, "work_end", None), DEFAULT_END),
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


def is_within(moment: datetime, config) -> bool:
    """Whether *moment* is inside the working hours configured on *config*."""
    return Window.from_config(config).contains(moment)
