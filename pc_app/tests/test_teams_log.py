"""Tests for the Teams log parser.

The fixture lines below are hand-written to match the shapes observed in real logs on a machine
with two Teams accounts signed in. Real log excerpts are deliberately NOT committed -- they carry
account and tenant context.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from pc_app.presence import PresenceEngine, Status, map_availability
from pc_app.teams_log import (
    MAIN_LOG_RE,
    SLIMCORE_LOG_RE,
    Tail,
    TeamsLogWatcher,
    newest_log,
    parse_availability,
)

# -- fixture lines -----------------------------------------------------------------------

PRESENCE_ACTION = (
    "Fri Sep 04 2026 18:23:29 Inf  UserPresenceAction: "
    "{cloud_context: https://teams.microsoft.com, availability: Available}"
)
PRESENCE_ACTION_BUSY = (
    "Fri Sep 04 2026 18:24:01 Inf  UserPresenceAction: "
    "{cloud_context: https://teams.microsoft.com, availability: Busy}"
)
PRESENCE_ACTION_DND = (
    "Fri Sep 04 2026 18:25:01 Inf  UserPresenceAction: "
    "{cloud_context: https://teams.microsoft.com, availability: DoNotDisturb}"
)
# Two accounts: the signed-out one reports PresenceUnknown alongside the real value.
MULTI_ACCOUNT = (
    "Fri Sep 04 2026 18:23:30 Inf  State Event: UserDataGlobalState total number of users: 2 "
    "{ availability: PresenceUnknown, unread notification count: 0 } "
    "{ availability: Busy, unread notification count: 3 }"
)
ALL_UNKNOWN = (
    "Fri Sep 04 2026 18:23:31 Inf  State Event: UserDataGlobalState total number of users: 2 "
    "{ availability: PresenceUnknown, unread notification count: 0 } "
    "{ availability: PresenceUnknown, unread notification count: 0 }"
)
NOISE = "Fri Sep 04 2026 18:23:32 Inf  ecs: fetched config, enableNewChatCreation: true"

CALL_ON = (
    "Fri Sep 04 2026 18:30:00 Inf  SlimCoreModule::WindowContentProtectionProvider: "
    "SetWindowContentProtection hwnd=0x1234 enabled=1"
)
CALL_OFF = (
    "Fri Sep 04 2026 18:45:00 Inf  SlimCoreModule::WindowContentProtectionProvider: "
    "UnregisterCall callId=abc-123"
)


# -- parse_availability ------------------------------------------------------------------


def test_presence_action_is_parsed():
    assert parse_availability(PRESENCE_ACTION) == "Available"
    assert parse_availability(PRESENCE_ACTION_DND) == "DoNotDisturb"


def test_multi_account_line_skips_presence_unknown():
    """The whole point of the fallback regex: do not let a signed-out account win."""
    assert parse_availability(MULTI_ACCOUNT) == "Busy"


def test_all_unknown_line_reports_unknown():
    assert parse_availability(ALL_UNKNOWN) == "PresenceUnknown"


def test_unrelated_line_yields_none():
    assert parse_availability(NOISE) is None
    assert parse_availability("") is None


def test_cloud_context_filter_selects_the_right_account():
    line = (
        "UserPresenceAction: {cloud_context: https://teams.microsoft.com, availability: Available} "
        "UserPresenceAction: {cloud_context: https://gov.teams.microsoft.us, availability: Busy}"
    )
    assert parse_availability(line, cloud_context="gov.teams.microsoft.us") == "Busy"
    assert parse_availability(line, cloud_context="teams.microsoft.com") == "Available"
    # With no filter the first non-unknown value wins.
    assert parse_availability(line) == "Available"


def test_presence_action_wins_over_availability_block():
    line = PRESENCE_ACTION_BUSY + " " + MULTI_ACCOUNT
    assert parse_availability(line) == "Busy"


# -- filename ranking --------------------------------------------------------------------


def test_newest_log_ranks_by_filename_not_mtime(tmp_path):
    older = tmp_path / "MSTeams_2026-07-01_21-53-47.00.log"
    newer = tmp_path / "MSTeams_2026-07-03_18-23-29.00.log"
    newer.write_text("newer", encoding="utf-8")
    older.write_text("older", encoding="utf-8")
    # Touch the older file last so an mtime-based implementation would pick the wrong one.
    import os
    import time

    os.utime(older, (time.time() + 60, time.time() + 60))

    assert newest_log(tmp_path, MAIN_LOG_RE) == newer


def test_newest_log_ignores_sibling_log_families(tmp_path):
    (tmp_path / "MSTeamsBackgroundEcs_2026-09-04_21-54-10.469.log").write_text("x", encoding="utf-8")
    (tmp_path / "MSTeamsUpdate_2026-09-04_21-54-10.4.log").write_text("x", encoding="utf-8")
    (tmp_path / "MSTeamsNM_SlimCore_2026-09-04_21-54-10.07.log").write_text("x", encoding="utf-8")
    wanted = tmp_path / "MSTeams_2026-09-04_18-23-29.00.log"
    wanted.write_text("x", encoding="utf-8")

    assert newest_log(tmp_path, MAIN_LOG_RE) == wanted


def test_slimcore_pattern_does_not_match_main_log(tmp_path):
    assert SLIMCORE_LOG_RE.match("MSTeams_2026-09-04_18-23-29.00.log") is None
    assert SLIMCORE_LOG_RE.match("MSTeamsNM_SlimCore_2026-09-04_18-23-30.07.log") is not None
    assert MAIN_LOG_RE.match("MSTeamsNM_SlimCore_2026-09-04_18-23-30.07.log") is None


def test_newest_log_on_missing_directory(tmp_path):
    assert newest_log(tmp_path / "nope", MAIN_LOG_RE) is None


# -- Tail --------------------------------------------------------------------------------


def test_tail_reads_only_new_lines(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    tail = Tail(path, start_at_end=True)
    assert tail.read_new_lines() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("three\n")
    assert tail.read_new_lines() == ["three"]
    assert tail.read_new_lines() == []


def test_tail_holds_partial_line_until_complete(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("", encoding="utf-8")
    tail = Tail(path, start_at_end=True)

    with path.open("a", encoding="utf-8") as handle:
        handle.write("half a li")
    assert tail.read_new_lines() == []  # not emitted yet

    with path.open("a", encoding="utf-8") as handle:
        handle.write("ne\n")
    assert tail.read_new_lines() == ["half a line"]


def test_tail_restarts_when_file_is_truncated(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("a\nb\nc\n", encoding="utf-8")
    tail = Tail(path, start_at_end=True)
    path.write_text("fresh\n", encoding="utf-8")
    assert tail.read_new_lines() == ["fresh"]


# -- watcher end to end ------------------------------------------------------------------


def _today() -> str:
    """Logs must look recent or the prime scan will correctly refuse to trust them."""
    return date.today().strftime("%Y-%m-%d")


def _write(path, *lines):
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def test_prime_recovers_current_status_without_waiting_for_a_change(tmp_path):
    main = tmp_path / f"MSTeams_{_today()}_10-00-00.00.log"
    _write(main, NOISE, PRESENCE_ACTION, NOISE, PRESENCE_ACTION_DND, NOISE)

    watcher = TeamsLogWatcher(log_dir=tmp_path)
    state = watcher.prime()

    assert state.availability == "DoNotDisturb"  # last one wins
    assert state.log_found is True


def test_prime_reports_no_log_when_folder_is_empty(tmp_path):
    watcher = TeamsLogWatcher(log_dir=tmp_path)
    state = watcher.prime()
    assert state.log_found is False
    assert state.availability is None


def test_prime_falls_back_to_an_earlier_log_when_the_newest_has_no_presence(tmp_path):
    """Teams rotates its log on every launch and logs no presence while its web client is
    suspended, so the newest file is routinely presence-free. Observed on a live machine."""
    today = _today()
    older = tmp_path / f"MSTeams_{today}_22-13-38.01.log"
    newest = tmp_path / f"MSTeams_{today}_22-16-50.02.log"
    _write(older, NOISE, PRESENCE_ACTION)
    _write(newest, NOISE, NOISE)  # startup noise only

    watcher = TeamsLogWatcher(log_dir=tmp_path)
    state = watcher.prime()

    assert state.availability == "Available"
    # ...but we still follow the newest file for changes.
    assert watcher._main_tail.path.name == newest.name


def test_prime_prefers_the_newest_log_that_has_presence(tmp_path):
    today = _today()
    _write(tmp_path / f"MSTeams_{today}_10-00-00.00.log", PRESENCE_ACTION)
    _write(tmp_path / f"MSTeams_{today}_11-00-00.01.log", PRESENCE_ACTION_DND)
    _write(tmp_path / f"MSTeams_{today}_12-00-00.02.log", NOISE)

    assert TeamsLogWatcher(log_dir=tmp_path).prime().availability == "DoNotDisturb"


def test_prime_will_not_resurrect_stale_presence(tmp_path):
    """A value from last week is worse than honestly reporting UNKNOWN."""
    ancient = date.today() - timedelta(days=30)
    _write(tmp_path / f"MSTeams_{ancient:%Y-%m-%d}_10-00-00.00.log", PRESENCE_ACTION)
    _write(tmp_path / f"MSTeams_{_today()}_12-00-00.02.log", NOISE)

    assert TeamsLogWatcher(log_dir=tmp_path).prime().availability is None


def test_prime_does_not_recover_call_state_from_an_older_log(tmp_path):
    """A call cannot outlive the Teams restart that rotated the log."""
    today = _today()
    _write(tmp_path / f"MSTeams_{today}_10-00-00.00.log", PRESENCE_ACTION_BUSY)
    _write(tmp_path / f"MSTeamsNM_SlimCore_{today}_10-00-01.07.log", CALL_ON)
    _write(tmp_path / f"MSTeamsNM_SlimCore_{today}_11-00-01.08.log", NOISE)

    assert TeamsLogWatcher(log_dir=tmp_path).prime().in_call is False


def test_poll_follows_new_lines(tmp_path):
    main = tmp_path / f"MSTeams_{_today()}_10-00-00.00.log"
    _write(main, PRESENCE_ACTION)
    watcher = TeamsLogWatcher(log_dir=tmp_path)
    assert watcher.prime().availability == "Available"

    _write(main, PRESENCE_ACTION_BUSY)
    assert watcher.poll().availability == "Busy"


def test_poll_picks_up_rotation_and_reads_the_new_file_from_the_start(tmp_path):
    old = tmp_path / f"MSTeams_{_today()}_10-00-00.00.log"
    _write(old, PRESENCE_ACTION)
    watcher = TeamsLogWatcher(log_dir=tmp_path)
    watcher.prime()

    # Teams rotates; the line we care about is written to the new file before we notice.
    new = tmp_path / f"MSTeams_{_today()}_12-00-00.00.log"
    _write(new, NOISE, PRESENCE_ACTION_DND)

    state = watcher.poll()
    assert watcher._main_tail.path == new
    assert state.availability == "DoNotDisturb"


def test_call_markers_drive_in_call_flag(tmp_path):
    main = tmp_path / f"MSTeams_{_today()}_10-00-00.00.log"
    slim = tmp_path / f"MSTeamsNM_SlimCore_{_today()}_10-00-01.07.log"
    _write(main, PRESENCE_ACTION_BUSY)
    _write(slim, NOISE)
    watcher = TeamsLogWatcher(log_dir=tmp_path)
    watcher.prime()
    assert watcher.state.in_call is False

    _write(slim, CALL_ON)
    assert watcher.poll().in_call is True

    _write(slim, CALL_OFF)
    assert watcher.poll().in_call is False


def test_slimcore_rotation_without_markers_keeps_call_state(tmp_path):
    """A fresh SlimCore log says nothing about the call; it must not cancel IN_MEETING."""
    main = tmp_path / f"MSTeams_{_today()}_10-00-00.00.log"
    slim = tmp_path / f"MSTeamsNM_SlimCore_{_today()}_10-00-01.07.log"
    _write(main, PRESENCE_ACTION_BUSY)
    _write(slim, CALL_ON)
    watcher = TeamsLogWatcher(log_dir=tmp_path)
    watcher.prime()
    assert watcher.state.in_call is True

    new_slim = tmp_path / f"MSTeamsNM_SlimCore_{_today()}_11-00-00.08.log"
    _write(new_slim, NOISE, NOISE)

    assert watcher.poll().in_call is True


def test_watcher_tracks_each_account_separately(tmp_path):
    main = tmp_path / f"MSTeams_{_today()}_10-00-00.00.log"
    _write(
        main,
        "UserPresenceAction: {cloud_context: https://teams.microsoft.com, availability: Available}",
        "UserPresenceAction: {cloud_context: https://gov.teams.microsoft.us, availability: Busy}",
    )
    watcher = TeamsLogWatcher(log_dir=tmp_path)
    watcher.prime()
    assert watcher.state.accounts == {
        "https://teams.microsoft.com": "Available",
        "https://gov.teams.microsoft.us": "Busy",
    }


def test_unread_count_is_captured(tmp_path):
    main = tmp_path / f"MSTeams_{_today()}_10-00-00.00.log"
    _write(main, MULTI_ACCOUNT)
    watcher = TeamsLogWatcher(log_dir=tmp_path)
    watcher.prime()
    assert watcher.state.unread == 3


# -- mapping and debounce ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Available", Status.AVAILABLE),
        ("AvailableIdle", Status.AVAILABLE),
        ("Busy", Status.BUSY),
        ("BusyIdle", Status.BUSY),
        ("DoNotDisturb", Status.DND),
        ("Away", Status.AWAY),
        ("BeRightBack", Status.BRB),
        ("Offline", Status.OFFLINE),
        ("PresenceUnknown", Status.UNKNOWN),
        (None, Status.UNKNOWN),
        ("SomethingMicrosoftAddedIn2027", Status.UNKNOWN),
    ],
)
def test_availability_mapping(raw, expected):
    assert map_availability(raw) is expected


def test_busy_plus_call_becomes_in_meeting():
    assert map_availability("Busy", in_call=True) is Status.IN_MEETING
    assert map_availability("BusyIdle", in_call=True) is Status.IN_MEETING
    # Ad-hoc calls can start while still green.
    assert map_availability("Available", in_call=True) is Status.IN_MEETING


def test_dnd_is_not_upgraded_by_a_call():
    """Presenting shows as DoNotDisturb; the user asked not to be disturbed, so respect that."""
    assert map_availability("DoNotDisturb", in_call=True) is Status.DND
    assert map_availability("Away", in_call=True) is Status.AWAY


def test_debounce_holds_a_new_status_until_it_settles():
    engine = PresenceEngine(debounce_seconds=2.0, initial=Status.AVAILABLE)

    assert engine.observe("Busy", now=100.0) is Status.AVAILABLE  # pending
    assert engine.observe("Busy", now=101.0) is Status.AVAILABLE  # still pending
    assert engine.observe("Busy", now=102.0) is Status.BUSY  # settled


def test_debounce_discards_a_transient_flap():
    engine = PresenceEngine(debounce_seconds=2.0, initial=Status.AVAILABLE)

    assert engine.observe("PresenceUnknown", now=100.0) is Status.AVAILABLE
    assert engine.observe("Available", now=100.5) is Status.AVAILABLE
    assert engine.observe("Available", now=105.0) is Status.AVAILABLE
    # The blip never got published.


def test_missing_log_folder_reports_unknown():
    engine = PresenceEngine(debounce_seconds=0.0, initial=Status.AVAILABLE)
    assert engine.observe("Available", log_found=False, now=1.0) is Status.UNKNOWN


def test_override_pins_the_status_and_releases_cleanly():
    engine = PresenceEngine(debounce_seconds=0.0, initial=Status.AVAILABLE)
    engine.set_override(Status.IN_MEETING)
    assert engine.observe("Available", now=1.0) is Status.IN_MEETING
    assert engine.observe("Offline", now=2.0) is Status.IN_MEETING

    engine.set_override(None)
    assert engine.observe("Offline", now=3.0) is Status.OFFLINE
