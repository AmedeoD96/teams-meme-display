"""System tray icon and menu.

Imported lazily by main.py so that --dry-run works without pystray/Pillow installed.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pystray
from PIL import Image, ImageDraw

from pc_app.config import Config, config_dir
from pc_app.i18n import (
    DISPLAY_MODES,
    LANGUAGE_NAMES,
    LANGUAGES,
    ORIENTATIONS,
    status_label,
    tr,
)
from pc_app.presence import STATUS_COLOR, Status

if TYPE_CHECKING:
    from pc_app.main import Worker

log = logging.getLogger(__name__)

ICON_SIZE = 64
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TeamsMemeDisplay"

#: Statuses offered in the manual override submenu, in menu order.
OVERRIDABLE = (
    Status.AVAILABLE,
    Status.BUSY,
    Status.IN_MEETING,
    Status.DND,
    Status.AWAY,
    Status.BRB,
    Status.OFFLINE,
)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def make_icon(status: Status) -> Image.Image:
    """A filled disc in the status colour, with a white glyph drawn from primitives.

    Primitives rather than text: the default PIL font has no dependable glyphs for the symbols
    we want, and bundling a font file for a 64px icon is not worth it.
    """
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colour = _hex_to_rgb(STATUS_COLOR[status])
    white = (255, 255, 255, 255)
    pad = 4
    draw.ellipse((pad, pad, ICON_SIZE - pad, ICON_SIZE - pad), fill=colour + (255,))

    mid = ICON_SIZE // 2
    if status is Status.AVAILABLE:  # tick
        draw.line((20, 33, 29, 43), fill=white, width=6)
        draw.line((29, 43, 45, 22), fill=white, width=6)
    elif status is Status.DND:  # minus
        draw.rounded_rectangle((18, mid - 4, 46, mid + 4), radius=4, fill=white)
    elif status is Status.IN_MEETING:  # play triangle
        draw.polygon([(26, 20), (26, 44), (46, 32)], fill=white)
    elif status is Status.BUSY:  # solid dot
        draw.ellipse((22, 22, 42, 42), fill=white)
    elif status in (Status.AWAY, Status.BRB):  # clock hands
        draw.ellipse((18, 18, 46, 46), outline=white, width=4)
        draw.line((mid, mid, mid, 24), fill=white, width=4)
        draw.line((mid, mid, 40, mid), fill=white, width=4)
    elif status is Status.OFFLINE:  # cross
        draw.line((22, 22, 42, 42), fill=white, width=6)
        draw.line((42, 22, 22, 42), fill=white, width=6)
    else:  # UNKNOWN / DISCONNECTED: a hollow ring
        draw.ellipse((20, 20, 44, 44), outline=white, width=5)
    return image


def _set_run_at_startup(enabled: bool) -> None:
    """Add or remove the HKCU Run entry. Per-user, so it needs no elevation."""
    try:
        import winreg
    except ImportError:
        log.warning("run-at-startup is Windows-only")
        return

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                # A PyInstaller build is its own launcher; only a source checkout needs the
                # interpreter and script spelled out separately.
                if getattr(sys, "frozen", False):
                    target = f'"{sys.executable}"'
                else:
                    target = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, target)
                log.info("start with Windows enabled: %s", target)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                    log.info("start with Windows disabled")
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log.warning("could not update the Run key: %s", exc)


def run_tray(worker: "Worker", config: Config) -> None:
    """Run the tray icon. Blocks until the user quits; must be called on the main thread."""

    def lang() -> str:
        return config.language

    def status_text(_item=None) -> str:
        status = worker.engine.status
        label = status_label(status, lang())
        if worker.engine.override is not None:
            label += f"  ({tr('forced', lang())})"
        port = worker.link.port
        return f"{label} - {port}" if port else f"{label} - {tr('not_connected', lang())}"

    def on_next(_icon, _item):
        worker.queue_command("NEXT")

    def on_reconnect(_icon, _item):
        worker.reconnect()

    def on_open_config(_icon, _item):
        path = config_dir()
        path.mkdir(parents=True, exist_ok=True)
        if not (path / "config.json").exists():
            config.save()
        # Explorer returns a non-zero exit code even on success, so do not check it.
        subprocess.Popen(["explorer", str(path)])

    def refresh():
        icon.icon = make_icon(worker.engine.status)
        icon.update_menu()

    def make_override_action(status: Status | None):
        def action(_icon, _item):
            worker.set_override(status)
            refresh()

        return action

    def is_override(status: Status | None):
        return lambda _item: worker.engine.override is status

    def make_language_action(code: str):
        def action(_icon, _item):
            config.language = code
            config.save()
            worker.queue_command(f"LANG:{code}")
            # The whole menu is in this language, so rebuild it too.
            refresh()

        return action

    def make_mode_action(name: str):
        def action(_icon, _item):
            config.display_mode = name
            config.save()
            worker.queue_command(f"MODE:{name}")
            refresh()

        return action

    def on_toggle_transition(_icon, _item):
        # A single toggle rather than a duration picker: the only interesting choice is whether
        # captions fade or snap.
        config.transition_ms = 0 if config.transition_ms else 400
        config.save()
        worker.queue_command(f"TRANSITION:{config.transition_ms}")

    def make_orientation_action(name: str):
        def action(_icon, _item):
            config.orientation = name
            config.save()
            worker.queue_command(f"ORIENT:{name}")
            refresh()

        return action

    def on_toggle_startup(_icon, _item):
        config.start_with_windows = not config.start_with_windows
        _set_run_at_startup(config.start_with_windows)
        config.save()

    def on_quit(icon, _item):
        icon.visible = False
        icon.stop()

    override_menu = pystray.Menu(
        pystray.MenuItem(
            lambda _item: tr("follow_teams", lang()),
            make_override_action(None),
            checked=is_override(None),
            radio=True,
        ),
        pystray.Menu.SEPARATOR,
        *[
            pystray.MenuItem(
                # A lambda so the label re-renders when the language changes.
                (lambda s: lambda _item: status_label(s, lang()))(status),
                make_override_action(status),
                checked=is_override(status),
                radio=True,
            )
            for status in OVERRIDABLE
        ],
    )

    language_menu = pystray.Menu(
        *[
            pystray.MenuItem(
                LANGUAGE_NAMES[code],
                make_language_action(code),
                checked=(lambda c: lambda _item: config.language == c)(code),
                radio=True,
            )
            for code in LANGUAGES
        ]
    )

    mode_menu = pystray.Menu(
        *[
            pystray.MenuItem(
                (lambda n: lambda _item: tr(n, lang()))(name),
                make_mode_action(name),
                checked=(lambda n: lambda _item: config.display_mode == n)(name),
                radio=True,
            )
            for name in DISPLAY_MODES
        ],
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda _item: tr("transition", lang()),
            on_toggle_transition,
            checked=lambda _item: bool(config.transition_ms),
        ),
    )

    orientation_menu = pystray.Menu(
        *[
            pystray.MenuItem(
                (lambda n: lambda _item: tr(n, lang()))(name),
                make_orientation_action(name),
                checked=(lambda n: lambda _item: config.orientation == n)(name),
                radio=True,
            )
            for name in ORIENTATIONS
        ]
    )

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _item: tr("next_meme", lang()), on_next),
        pystray.MenuItem(lambda _item: tr("force_status", lang()), override_menu),
        pystray.MenuItem(lambda _item: tr("display", lang()), mode_menu),
        pystray.MenuItem(lambda _item: tr("language", lang()), language_menu),
        pystray.MenuItem(lambda _item: tr("orientation", lang()), orientation_menu),
        pystray.MenuItem(lambda _item: tr("reconnect", lang()), on_reconnect),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _item: tr("open_config", lang()), on_open_config),
        pystray.MenuItem(
            lambda _item: tr("start_with_windows", lang()),
            on_toggle_startup,
            checked=lambda _item: config.start_with_windows,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _item: tr("quit", lang()), on_quit),
    )

    icon = pystray.Icon(
        "teams_meme_display",
        icon=make_icon(worker.engine.status),
        title=tr("tooltip", config.language),
        menu=menu,
    )

    def on_status_change(status: Status) -> None:
        icon.icon = make_icon(status)
        icon.title = f"Teams: {status_label(status, config.language)}"
        icon.update_menu()

    worker.on_status_change = on_status_change
    icon.run()
