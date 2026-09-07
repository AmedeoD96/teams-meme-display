"""Tests for the working-hours window.

The cases that matter are all calendar edges: the exact minute the window opens and closes, a
window that runs past midnight, and which *day* such a window belongs to.
"""

from __future__ import annotations

from datetime import datetime, time

import pytest

from pc_app.config import Config
from pc_app.work_hours import (
    DEFAULT_WORK_DAYS,
    Window,
    is_within,
    normalise_days,
    parse_hhmm,
)

WEEKDAYS = frozenset(DEFAULT_WORK_DAYS)
EVERY_DAY = frozenset(range(7))

# 2026-09-07 is a Monday, so the whole week can be addressed as day 7..13.
MONDAY = 7
SATURDAY = 12
SUNDAY = 13


def at(day: int, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 9, day, hour, minute)


# -- parsing ------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [("09:00", time(9, 0)), ("9:5", time(9, 5)), ("00:00", time(0, 0)), ("23:59", time(23, 59))],
)
def test_parse_hhmm_reads_valid_times(raw, expected):
    assert parse_hhmm(raw, "09:00") == expected


@pytest.mark.parametrize("raw", ["9am", "", None, "25:00", "half nine"])
def test_parse_hhmm_falls_back_rather_than_raising(raw):
    # A hand-edited config should never stop the app from starting.
    assert parse_hhmm(raw, "08:30") == time(8, 30)


def test_normalise_days_drops_out_of_range_and_defaults_on_junk():
    assert normalise_days([0, 2, 4]) == frozenset({0, 2, 4})
    assert normalise_days([0, 9, -1]) == frozenset({0})
    assert normalise_days(None) == WEEKDAYS
    assert normalise_days("nonsense") == WEEKDAYS


# -- an ordinary daytime window ------------------------------------------------------------

@pytest.fixture
def nine_to_six() -> Window:
    return Window(start=time(9, 0), end=time(18, 0), days=WEEKDAYS)


def test_daytime_window_does_not_wrap(nine_to_six):
    assert not nine_to_six.wraps


@pytest.mark.parametrize("hhmm", ["09:00", "09:01", "12:30", "17:59"])
def test_inside_the_working_day(nine_to_six, hhmm):
    assert nine_to_six.contains(at(MONDAY, hhmm))


@pytest.mark.parametrize("hhmm", ["08:59", "18:00", "18:01", "23:59", "00:00"])
def test_outside_the_working_day(nine_to_six, hhmm):
    # 09:00 is inside and 18:00 is already outside: the window is half-open, so an alert at
    # exactly closing time counts as after hours.
    assert not nine_to_six.contains(at(MONDAY, hhmm))


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_the_weekend_is_outside_whatever_the_clock_says(nine_to_six, day):
    assert not nine_to_six.contains(at(day, "12:00"))


# -- a window that wraps past midnight -----------------------------------------------------

@pytest.fixture
def night_shift() -> Window:
    return Window(start=time(22, 0), end=time(6, 0), days=WEEKDAYS)


def test_night_shift_wraps(night_shift):
    assert night_shift.wraps


@pytest.mark.parametrize("hhmm", ["22:00", "23:30"])
def test_the_evening_half_belongs_to_the_day_it_starts_on(night_shift, hhmm):
    assert night_shift.contains(at(MONDAY, hhmm))


@pytest.mark.parametrize("hhmm", ["00:00", "05:59"])
def test_the_small_hours_belong_to_the_previous_day(night_shift, hhmm):
    # Tuesday 02:00 is inside Monday's window.
    assert night_shift.contains(at(MONDAY + 1, hhmm))


def test_the_small_hours_of_monday_belong_to_sunday(night_shift):
    # Sunday is not a working day, so Monday 02:00 is outside -- it is Sunday's window, and
    # Sunday's window never opened.
    assert not night_shift.contains(at(MONDAY, "02:00"))


def test_the_small_hours_of_monday_are_inside_when_sunday_works():
    window = Window(start=time(22, 0), end=time(6, 0), days=EVERY_DAY)
    assert window.contains(at(MONDAY, "02:00"))


@pytest.mark.parametrize("hhmm", ["06:00", "12:00", "21:59"])
def test_the_middle_of_the_day_is_outside_a_night_shift(night_shift, hhmm):
    assert not night_shift.contains(at(MONDAY, hhmm))


def test_saturday_morning_is_inside_a_friday_night_shift(night_shift):
    # Friday is a working day, so its window runs into Saturday morning.
    assert night_shift.contains(at(SATURDAY, "03:00"))
    assert not night_shift.contains(at(SATURDAY, "23:00"))


# -- the Config bridge ---------------------------------------------------------------------

def test_is_within_reads_the_config_defaults():
    config = Config()
    assert is_within(at(MONDAY, "10:00"), config)
    assert not is_within(at(MONDAY, "20:00"), config)
    assert not is_within(at(SATURDAY, "10:00"), config)


def test_a_broken_config_falls_back_instead_of_raising():
    config = Config(work_start="whenever", work_end="18:00", work_days="nope")
    # Unusable values resolve to the documented defaults rather than blowing up a tick.
    assert is_within(at(MONDAY, "10:00"), config)
