"""Layout tests for the settings window.

There is one bug this file exists to catch. The Alerts tab is a grid whose row numbers used to be
written in by hand, so every control added to it meant renumbering everything below -- and twice
that renumbering quietly dropped two widgets into the same cell, where Tk draws them on top of
each other rather than complaining. The rows are handed out by a running counter now, and this
asserts the property that counter is there to guarantee.

Skipped wherever Tk cannot open a display, which is the normal state of a headless CI box.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from pc_app.config import Config
from pc_app.main import Worker
from pc_app.phrases import PhraseBank

tk = pytest.importorskip("tkinter", reason="no tkinter in this interpreter")


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """One real settings window, shared by every test here.

    Module-scoped deliberately. Tk does not take kindly to a root being created and destroyed
    repeatedly in one process -- the second one fails with a "tk wasn't installed properly" that
    has nothing to do with the installation -- and these tests only read the layout, so one
    window serves them all.
    """
    gui = pytest.importorskip("pc_app.gui", reason="tkinter present but unusable")
    config = Config(log_dir=str(tmp_path_factory.mktemp("logs")))
    config.save = lambda path=None: None  # never touch the real config folder
    worker = Worker(config, dry_run=True, phrases=PhraseBank())
    try:
        window = gui.App(worker, config)
    except tk.TclError as exc:  # no display
        pytest.skip(f"cannot open a Tk display: {exc}")
    window.root.update_idletasks()
    yield window
    window.root.destroy()


def occupancy(holder) -> dict[tuple[int, int], list[str]]:
    """Which widgets sit in each (row, column) of *holder*, columnspan included."""
    cells: dict[tuple[int, int], list[str]] = defaultdict(list)
    for widget in holder.grid_slaves():
        info = widget.grid_info()
        row, column = int(info["row"]), int(info["column"])
        for offset in range(int(info["columnspan"])):
            cells[(row, column + offset)].append(str(widget))
    return cells


def holder_of(tab):
    """The single frame each tab packs its controls into."""
    return tab.winfo_children()[0]


@pytest.mark.parametrize("tab_name", ["_alerts", "_device"])
def test_no_two_widgets_share_a_grid_cell(app, tab_name):
    cells = occupancy(holder_of(getattr(app, tab_name)))
    clashes = {where: names for where, names in cells.items() if len(names) > 1}
    assert not clashes, f"widgets drawn on top of each other at {sorted(clashes)}"


def test_the_alerts_tab_rows_are_contiguous(app):
    """A gap means a row was skipped, which is the other half of a bad renumber."""
    rows = {int(w.grid_info()["row"]) for w in holder_of(app._alerts).grid_slaves()}
    assert rows == set(range(min(rows), max(rows) + 1))


def test_the_hours_note_sits_below_the_two_time_rows(app):
    """The note explains the blocks above it, so it has to come after them."""
    holder = holder_of(app._alerts)
    labelled = {
        str(w.cget("text")): int(w.grid_info()["row"])
        for w in holder.grid_slaves()
        if w.winfo_class() == "TLabel" and str(w.cget("text")) in ("Morning", "Afternoon")
    }
    note_row = int(app._hours_note.grid_info()["row"])
    assert labelled["Morning"] < labelled["Afternoon"] < note_row
