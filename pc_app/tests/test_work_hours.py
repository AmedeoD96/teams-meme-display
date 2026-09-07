"""Tests for the working day.

The cases that matter are all calendar edges: the exact minute a window opens and closes, a
window that runs past midnight, which *day* such a window belongs to, and the gap between the
morning and the afternoon -- lunch -- which is outside working hours and so earns the alert.
"""

from __future__ import annotations

from datetime import datetime, time

import pytest

from pc_app.config import Config
from pc_app.work_hours import (
    DEFAULT_WORK_DAYS,
    Schedule,
    Window,
    format_clock,
    is_within,
    normalise_days,
    parse_clock,
    parse_hhmm,
    parse_optional,
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
    [
        # The 24-hour forms, which is what a config written before the AM/PM change holds.
        ("09:00", time(9, 0)),
        ("9:5", time(9, 5)),
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
        ("13:00", time(13, 0)),
        # The AM/PM forms the settings window writes now.
        ("9:00 AM", time(9, 0)),
        ("9am", time(9, 0)),
        ("9:00 a.m.", time(9, 0)),
        ("1:00 PM", time(13, 0)),
        ("1 pm", time(13, 0)),
        # Noon and midnight, where a naive hour % 12 gets it backwards.
        ("12:00 AM", time(0, 0)),
        ("12:00 PM", time(12, 0)),
        ("12:30 AM", time(0, 30)),
    ],
)
def test_parse_hhmm_reads_valid_times(raw, expected):
    assert parse_hhmm(raw, "09:00") == expected


@pytest.mark.parametrize("raw", ["", None, "25:00", "13:00 PM", "0:00 PM", "half nine"])
def test_parse_hhmm_falls_back_rather_than_raising(raw):
    # A hand-edited config should never stop the app from starting. "13:00 PM" is the trap worth
    # naming: it looks like a time, but a meridiem makes the hour a 12-hour one.
    assert parse_hhmm(raw, "08:30") == time(8, 30)


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_parse_optional_reads_a_blank_as_absent(raw):
    # Which is what "no afternoon block" looks like in the config.
    assert parse_optional(raw) is None


def test_parse_optional_reads_a_time_and_refuses_junk():
    assert parse_optional("2:00 PM") == time(14, 0)
    assert parse_optional("half two") is None


@pytest.mark.parametrize(
    "value, expected",
    [
        (time(9, 0), "9:00 AM"),
        (time(13, 5), "1:05 PM"),
        (time(12, 0), "12:00 PM"),
        (time(0, 0), "12:00 AM"),
        (time(23, 59), "11:59 PM"),
    ],
)
def test_format_clock_is_what_the_settings_window_shows(value, expected):
    assert format_clock(value) == expected
    # And what it shows must read back as the same time, or Apply would drift the value.
    assert parse_clock(expected) == value


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


# -- the split day -------------------------------------------------------------------------

@pytest.fixture
def split_day() -> Schedule:
    """9:00 AM to 1:00 PM and 2:00 PM to 6:00 PM, Mon-Fri: the defaults."""
    return Schedule.from_config(Config())


@pytest.mark.parametrize("hhmm", ["09:00", "11:00", "12:59", "14:00", "16:00", "17:59"])
def test_both_blocks_are_inside(split_day, hhmm):
    assert split_day.contains(at(MONDAY, hhmm))


@pytest.mark.parametrize("hhmm", ["13:00", "13:30", "13:59"])
def test_lunch_is_outside(split_day, hhmm):
    # The whole point of having two blocks: a message at 1:30 PM earns the GIF.
    assert not split_day.contains(at(MONDAY, hhmm))


@pytest.mark.parametrize("hhmm", ["08:59", "18:00", "22:30"])
def test_either_end_of_the_day_is_still_outside(split_day, hhmm):
    assert not split_day.contains(at(MONDAY, hhmm))


def test_the_weekend_is_outside_both_blocks(split_day):
    assert not split_day.contains(at(SATURDAY, "11:00"))
    assert not split_day.contains(at(SATURDAY, "16:00"))


def test_the_gap_is_reported_for_the_note(split_day):
    assert split_day.gap() == (time(13, 0), time(14, 0))


@pytest.mark.parametrize(
    "afternoon_start, afternoon_end",
    [(None, None), ("", ""), ("2:00 PM", None), (None, "6:00 PM"), ("2:00 PM", "2:00 PM")],
)
def test_a_missing_afternoon_leaves_one_continuous_window(afternoon_start, afternoon_end):
    """A blank, half-filled or empty afternoon is no afternoon, and the day is one window."""
    config = Config(
        work_start="9:00 AM",
        work_end="6:00 PM",
        afternoon_start=afternoon_start,
        afternoon_end=afternoon_end,
    )
    schedule = Schedule.from_config(config)
    assert len(schedule.windows) == 1
    assert schedule.afternoon is None
    assert schedule.gap() is None
    # Which is exactly the behaviour a config written before the split had.
    assert schedule.contains(at(MONDAY, "13:30"))


def test_no_gap_is_claimed_when_the_blocks_do_not_leave_one():
    # Back-to-back blocks, and an afternoon that opens before the morning shuts. Neither leaves
    # a lunch break, and naming one would say something untrue.
    touching = Schedule.from_config(
        Config(work_start="9:00 AM", work_end="1:00 PM",
               afternoon_start="1:00 PM", afternoon_end="6:00 PM")
    )
    overlapping = Schedule.from_config(
        Config(work_start="9:00 AM", work_end="3:00 PM",
               afternoon_start="2:00 PM", afternoon_end="6:00 PM")
    )
    assert touching.gap() is None
    assert overlapping.gap() is None
    assert touching.contains(at(MONDAY, "13:00"))


def test_an_overnight_block_still_works_alongside_a_morning():
    # A morning shift and a night shift on the same day, which each Window handles on its own.
    config = Config(
        work_start="9:00 AM", work_end="1:00 PM",
        afternoon_start="10:00 PM", afternoon_end="6:00 AM",
    )
    schedule = Schedule.from_config(config)
    assert schedule.contains(at(MONDAY, "10:00"))
    assert schedule.contains(at(MONDAY, "23:00"))
    assert schedule.contains(at(MONDAY + 1, "02:00"))  # Monday night runs into Tuesday
    assert not schedule.contains(at(MONDAY, "16:00"))
    # An overnight block has no lunch gap worth naming.
    assert schedule.gap() is None


# -- the Config bridge ---------------------------------------------------------------------

def test_is_within_reads_the_config_defaults():
    config = Config()
    assert is_within(at(MONDAY, "10:00"), config)
    assert is_within(at(MONDAY, "15:00"), config)
    assert not is_within(at(MONDAY, "13:30"), config)
    assert not is_within(at(MONDAY, "20:00"), config)
    assert not is_within(at(SATURDAY, "10:00"), config)


def test_a_broken_config_falls_back_instead_of_raising():
    config = Config(work_start="whenever", work_end="1:00 PM", work_days="nope")
    # Unusable values resolve to the documented defaults rather than blowing up a tick.
    assert is_within(at(MONDAY, "10:00"), config)


def test_an_unreadable_afternoon_is_dropped_rather_than_raising():
    config = Config(work_start="9:00 AM", work_end="6:00 PM", afternoon_end="half six")
    schedule = Schedule.from_config(config)
    assert schedule.afternoon is None
    assert schedule.contains(at(MONDAY, "13:30"))
