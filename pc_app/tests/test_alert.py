"""Tests for the out-of-hours alert decision.

The Worker is driven in dry-run mode, so no COM port is opened and every line it would send is
captured instead. What is asserted throughout is whether an ALERT: reached the wire -- that is
the whole observable behaviour of the feature.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pc_app.config import Config
from pc_app.main import Worker
from pc_app.phrases import PhraseBank

# 2026-09-07 is a Monday. Inside the default Mon-Fri day -- 9:00 AM to 1:00 PM and 2:00 PM to
# 6:00 PM -- and outside it, including the lunch gap between the two blocks.
DURING_HOURS = datetime(2026, 9, 7, 10, 30)
LUNCH = datetime(2026, 9, 7, 13, 30)
AFTER_HOURS = datetime(2026, 9, 7, 22, 30)
WEEKEND = datetime(2026, 9, 12, 10, 30)


@pytest.fixture
def worker(monkeypatch, tmp_path) -> Worker:
    """A Worker that talks to nothing and never touches the real Teams logs."""
    config = Config(log_dir=str(tmp_path))
    worker = Worker(config, dry_run=True, phrases=PhraseBank())
    worker.sent: list[str] = []
    monkeypatch.setattr(worker.link, "send", lambda line: worker.sent.append(line) or True)
    return worker


def at(worker: Worker, monkeypatch, moment: datetime) -> None:
    """Pin the wall clock the working-hours check reads."""
    import pc_app.main as main

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(main, "datetime", _Clock)


def alerts(worker: Worker) -> list[str]:
    return [line for line in worker.sent if line.startswith("ALERT:")]


def notify(worker: Worker, count: int = 1) -> None:
    """Pretend the log watcher saw *count* notifications arrive, then run one tick."""
    worker.watcher.state.unread_events += count
    worker.tick()


# -- the decision ---------------------------------------------------------------------------


def test_a_notification_after_hours_plays_the_gif(worker, monkeypatch):
    at(worker, monkeypatch, AFTER_HOURS)
    notify(worker)
    assert alerts(worker) == [f"ALERT:{int(worker.config.alert_seconds * 1000)}"]


def test_a_notification_during_hours_plays_nothing(worker, monkeypatch):
    at(worker, monkeypatch, DURING_HOURS)
    notify(worker)
    assert alerts(worker) == []


def test_lunch_counts_as_out_of_hours(worker, monkeypatch):
    """The gap between the morning and afternoon blocks earns the GIF like any evening does."""
    at(worker, monkeypatch, LUNCH)
    notify(worker)
    assert len(alerts(worker)) == 1


def test_the_weekend_counts_as_out_of_hours(worker, monkeypatch):
    at(worker, monkeypatch, WEEKEND)
    notify(worker)
    assert len(alerts(worker)) == 1


def test_no_notification_means_no_alert(worker, monkeypatch):
    at(worker, monkeypatch, AFTER_HOURS)
    worker.tick()
    worker.tick()
    assert alerts(worker) == []


def test_the_master_switch_suppresses_it(worker, monkeypatch):
    worker.config.alert_enabled = False
    at(worker, monkeypatch, AFTER_HOURS)
    notify(worker)
    assert alerts(worker) == []


# -- alert_always, which takes the clock out of the decision ----------------------------------


@pytest.mark.parametrize("moment", [DURING_HOURS, LUNCH, AFTER_HOURS, WEEKEND])
def test_alert_always_fires_whatever_the_clock_says(worker, monkeypatch, moment):
    worker.config.alert_always = True
    at(worker, monkeypatch, moment)
    notify(worker)
    assert len(alerts(worker)) == 1


def test_alert_always_is_off_by_default(worker, monkeypatch):
    assert worker.config.alert_always is False
    at(worker, monkeypatch, DURING_HOURS)
    notify(worker)
    assert alerts(worker) == []


def test_alert_always_still_respects_the_cooldown(worker, monkeypatch):
    """Ignoring the hours is not the same as ignoring the gap between alerts."""
    worker.config.alert_always = True
    at(worker, monkeypatch, DURING_HOURS)
    for _ in range(4):
        notify(worker)
    assert len(alerts(worker)) == 1

    worker._last_alert_at -= worker.config.alert_cooldown_seconds + 1
    notify(worker)
    assert len(alerts(worker)) == 2


def test_the_master_switch_still_wins_over_alert_always(worker, monkeypatch):
    worker.config.alert_enabled = False
    worker.config.alert_always = True
    at(worker, monkeypatch, DURING_HOURS)
    notify(worker)
    assert alerts(worker) == []


def test_alert_always_needs_a_notification(worker, monkeypatch):
    # It widens *when* an arrival counts, it does not invent arrivals.
    worker.config.alert_always = True
    at(worker, monkeypatch, DURING_HOURS)
    worker.tick()
    worker.tick()
    assert alerts(worker) == []


# -- what the Device tab reads ----------------------------------------------------------------


def test_the_board_gif_state_starts_unknown(worker):
    # There is no command to ask the board what it holds, so nothing is claimed until it says.
    assert worker._board_has_gif is None


def test_an_alert_that_plays_means_the_board_has_a_gif(worker):
    worker._on_alert_event("ALERT:6000")
    assert worker._board_has_gif is True


def test_a_refused_alert_says_whether_the_gif_is_missing(worker):
    worker._on_alert_event("ALERTERR:nogif")
    assert worker._board_has_gif is False
    # "uploading" says nothing about whether a file is there, so it must not overwrite the answer.
    worker._on_alert_event("ALERTERR:uploading")
    assert worker._board_has_gif is False


def test_a_fresh_link_forgets_what_the_last_board_held(worker, monkeypatch):
    worker._on_alert_event("ALERT:6000")
    assert worker._board_has_gif is True
    at(worker, monkeypatch, DURING_HOURS)
    worker._was_connected = False  # as if the cable had just been plugged into another board
    worker.tick()
    assert worker._board_has_gif is None


# -- the cooldown ---------------------------------------------------------------------------


def test_a_burst_of_messages_plays_the_gif_once(worker, monkeypatch):
    at(worker, monkeypatch, AFTER_HOURS)
    for _ in range(5):
        notify(worker)
    assert len(alerts(worker)) == 1


def test_the_cooldown_expires(worker, monkeypatch):
    at(worker, monkeypatch, AFTER_HOURS)
    notify(worker)
    # Pretend the cooldown has elapsed rather than sleeping through it.
    worker._last_alert_at -= worker.config.alert_cooldown_seconds + 1
    notify(worker)
    assert len(alerts(worker)) == 2


def test_the_first_alert_is_not_held_back_by_the_cooldown(worker, monkeypatch):
    """_last_alert_at starts at zero, which must not read as "we just alerted"."""
    at(worker, monkeypatch, AFTER_HOURS)
    assert worker._last_alert_at == 0.0
    notify(worker)
    assert len(alerts(worker)) == 1


# -- the counter ----------------------------------------------------------------------------


def test_notifications_during_hours_do_not_queue_up_for_later(worker, monkeypatch):
    """The counter advances whatever the clock says, so nothing is owed at 18:01."""
    at(worker, monkeypatch, DURING_HOURS)
    for _ in range(3):
        notify(worker)
    assert alerts(worker) == []

    at(worker, monkeypatch, AFTER_HOURS)
    worker.tick()  # a tick with no new arrival
    assert alerts(worker) == []


def test_an_alert_missed_while_disconnected_is_not_replayed(worker, monkeypatch):
    # Fail the connection outright rather than letting the real port scan run in a test.
    monkeypatch.setattr(worker.link, "ensure_connected", lambda: False)
    at(worker, monkeypatch, AFTER_HOURS)
    notify(worker)
    assert alerts(worker) == []

    monkeypatch.setattr(worker.link, "ensure_connected", lambda: True)
    worker.tick()
    assert alerts(worker) == []


# -- the test button ------------------------------------------------------------------------


def test_alert_now_ignores_the_clock(worker, monkeypatch):
    at(worker, monkeypatch, DURING_HOURS)
    worker.alert_now()
    worker.tick()
    assert len(alerts(worker)) == 1


def test_the_board_answer_reaches_the_settings_window(worker, monkeypatch):
    """The button's only honest feedback is what the board says back, so it has to arrive."""
    seen: list[tuple[bool, str]] = []
    worker.on_alert_result = lambda ok, detail: seen.append((ok, detail))
    replies = [["EVT:ALERT:6000"], ["EVT:ALERTERR:nogif"]]
    monkeypatch.setattr(worker.link, "read_lines", lambda: replies.pop(0) if replies else [])

    worker.tick()
    worker.tick()
    assert seen == [(True, "6000"), (False, "nogif")]


def test_an_unanswered_alert_costs_nothing(worker, monkeypatch):
    """No callback is the normal case -- the tray has none -- and must not break a tick."""
    monkeypatch.setattr(worker.link, "read_lines", lambda: ["EVT:ALERTERR:nogif"])
    worker.tick()


def test_the_duration_is_clamped_to_what_the_firmware_accepts(worker):
    worker.config.alert_seconds = 9999
    assert worker._alert_ms() == 60000
    worker.config.alert_seconds = 0
    assert worker._alert_ms() == 200


# -- end to end -----------------------------------------------------------------------------


def write_log(log_dir, *lines: str) -> None:
    """Append to today's Teams main log, creating it if need be."""
    from datetime import date

    path = log_dir / f"MSTeams_{date.today():%Y-%m-%d}_10-00-00.00.log"
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def notification_line(count: int) -> str:
    """The real shape, copied from a live MSTeams_*.log."""
    return (
        "2026-09-05T13:04:00.992179+02:00 0x00002140 <INFO> "
        "native_modules::UserDataCrossCloudModule: Received Action: UserNotificationAction: "
        f"{{cloud_context: https://teams.microsoft.com, unread notification count: {count} }}"
    )


def test_a_real_log_line_out_of_hours_reaches_the_wire(worker, monkeypatch, tmp_path):
    """The whole chain: Teams writes a line, and the board is told to play the GIF.

    Everything between is real -- the log tail, the regex, the edge detector, the working-hours
    window and the worker. Only the clock and the serial port are stood in for.
    """
    log_dir = Path(worker.watcher.log_dir)
    write_log(log_dir, "some startup noise", notification_line(0))
    worker.start()  # primes the baseline from what is already there

    at(worker, monkeypatch, AFTER_HOURS)
    write_log(log_dir, notification_line(1))
    worker.tick()

    assert alerts(worker) == [f"ALERT:{int(worker.config.alert_seconds * 1000)}"]


def test_the_same_line_during_hours_reaches_nothing(worker, monkeypatch):
    log_dir = Path(worker.watcher.log_dir)
    write_log(log_dir, notification_line(0))
    worker.start()

    at(worker, monkeypatch, DURING_HOURS)
    write_log(log_dir, notification_line(1))
    worker.tick()

    assert alerts(worker) == []
