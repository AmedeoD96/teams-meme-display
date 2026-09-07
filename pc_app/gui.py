"""The settings window: phrase editor, device settings, and a live preview of the panel.

Threading is the fiddly part. Tk insists on owning the thread that created its root, and pystray
wants a message loop of its own, so:

    main thread   Tk root and every widget call
    worker thread the existing Worker.run() loop, which owns the serial port
    tray thread   pystray's icon and menu

Tray callbacks therefore never touch a widget directly: they hand a callable to `App.post`, and
the Tk loop drains that queue from an `after` timer. Widget callbacks in the other direction only
mutate the config, the phrase bank, or the worker's command queue -- all of which are safe to
touch from another thread.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from pc_app import work_hours
from pc_app.config import Config, config_dir
from pc_app.gif_upload import MAX_BYTES as GIF_MAX_BYTES
from pc_app.gif_upload import estimate_seconds
from pc_app.i18n import (
    DISPLAY_MODES,
    LANGUAGE_NAMES,
    LANGUAGES,
    ORIENTATIONS,
    TONES,
    status_label,
    tr,
)
from pc_app.phrases import MAX_PHRASE_CHARS
from pc_app.presence import Status
from pc_app.render import (
    CAPTION_FONT_BIG,
    CAPTION_LINES_BIG,
    layout_caption,
    render_image_frame,
    render_mascot_frame,
    render_text_frame,
    status_colour,
)
from pc_app.text import to_display_ascii

log = logging.getLogger(__name__)

#: Statuses offered in the editor, in menu order. Mirrors OVERRIDABLE in tray.py but includes the
#: two the PC never sends, because their banks still need editing.
EDITABLE = tuple(Status)

#: The Device tab's health block, in the order the alert depends on them: the link carries it,
#: the log triggers it, and the GIF is what actually plays.
HEALTH_ROWS = (
    ("board", "Board"),
    ("log", "Teams log"),
    ("gif", "Alert GIF"),
)

#: Health colours: settled and working, settled and broken, and nothing known yet.
OK, BAD, UNKNOWN = "#060", "#a00", "#666"

#: Panel sizes, matching ORIENTATIONS in tools/build_memes.py.
PANEL_SIZE = {"landscape": (320, 240), "portrait": (240, 320)}
#: Shown at 1:1. A blown-up preview would dominate the window, and life size is the honest way
#: to judge whether a phrase is actually readable on a 2.8" panel.
PREVIEW_ZOOM = 1


class PreviewPane(ttk.Frame):
    """Renders one frame exactly as `tools/build_memes.py --preview` would."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self._label = ttk.Label(self, anchor="center")
        self._label.pack()
        self._caption = ttk.Label(self, foreground="#666")
        self._caption.pack(pady=(4, 0))
        self._photo: ImageTk.PhotoImage | None = None

    def render(self, config: Config, status: Status, phrase: str) -> None:
        size = PANEL_SIZE.get(config.orientation, PANEL_SIZE["portrait"])
        key = status.value.lower()
        folded, _ = to_display_ascii(phrase)

        if config.display_mode == "text":
            frame = render_text_frame(key, folded, size)
            note = "text mode"
        elif config.display_mode == "image":
            # No meme library on this side, so the status colour stands in for the picture -- the
            # same fallback the build tool previews when a status has no images.
            base = Image.new("RGB", size, status_colour(key))
            frame = render_image_frame(base, folded) or base
            note = "image mode (your meme goes behind the caption)"
        else:
            frame = render_mascot_frame(key, config.tone, folded, size)
            note = "mascot mode (the face animates on the device)"

        if PREVIEW_ZOOM != 1:
            # Nearest neighbour, so a scaled preview still shows whole device pixels.
            frame = frame.resize(
                (frame.width * PREVIEW_ZOOM, frame.height * PREVIEW_ZOOM), Image.NEAREST
            )
        self._photo = ImageTk.PhotoImage(frame)
        self._label.configure(image=self._photo)
        self._caption.configure(
            text=note + chr(10) + f"{size[0]}x{size[1]}, life size"
        )


class App:
    """Owns the Tk root, the tray thread, and the queue between them."""

    def __init__(self, worker, config: Config):
        self.worker = worker
        self.config = config
        self.phrases = worker.phrases

        self.root = tk.Tk()
        self.root.title("Teams status display")
        # Sized explicitly: the preview is a fixed 240x320 and the editor row below the phrase
        # list has no give, so leaving Tk to negotiate a natural size clips one or the other.
        self.root.geometry("1080x780")
        self.root.minsize(1000, 700)
        # Closing the window only hides it; the app lives in the tray until Quit.
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.withdraw()

        self._requests: queue.Queue = queue.Queue()
        self._icon = None
        self._tray_thread: threading.Thread | None = None
        self._building = False
        #: Bumped per press of the test button, so a late answer to an earlier press cannot
        #: overwrite what the current one is saying.
        self._alert_request = 0

        self._build()

        # Fires on the worker thread, so it is bounced through the pump like the upload callbacks.
        self.worker.on_alert_result = lambda ok, detail: self.post(
            lambda: self._on_alert_result(ok, detail)
        )

    # -- cross-thread plumbing -----------------------------------------------------------

    def post(self, callback) -> None:
        """Ask the Tk thread to run *callback*. Safe to call from the tray or worker thread."""
        self._requests.put(callback)

    def _pump(self) -> None:
        while True:
            try:
                callback = self._requests.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                # A failed menu action must not take the whole window down.
                log.exception("queued GUI action failed")
        self._refresh_connection()
        self.root.after(100, self._pump)

    # -- lifecycle -----------------------------------------------------------------------

    def run(self) -> None:
        from pc_app.tray import build_icon

        self._icon = build_icon(
            self.worker,
            self.config,
            on_settings=lambda: self.post(self.show),
            on_quit=lambda: self.post(self.root.quit),
        )
        self._tray_thread = threading.Thread(
            target=self._icon.run, name="tray", daemon=True
        )
        self._tray_thread.start()

        self._pump()
        self.root.mainloop()

        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    def show(self) -> None:
        self._reload_from_config()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()

    # -- construction --------------------------------------------------------------------

    def _build(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self._messages = ttk.Frame(notebook)
        self._look = ttk.Frame(notebook)
        self._alerts = ttk.Frame(notebook)
        self._device = ttk.Frame(notebook)
        notebook.add(self._messages, text="Messages")
        notebook.add(self._look, text="Look")
        notebook.add(self._alerts, text="Alerts")
        notebook.add(self._device, text="Device")

        self._build_messages(self._messages)
        self._build_look(self._look)
        self._build_alerts(self._alerts)
        self._build_device(self._device)
        self._reload_from_config()

    # -- Messages tab --------------------------------------------------------------------

    def _build_messages(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)

        chooser = ttk.Frame(parent)
        chooser.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(4, 8))

        self._edit_language = tk.StringVar()
        self._edit_tone = tk.StringVar()
        ttk.Label(chooser, text="Language").pack(side="left")
        language_box = ttk.Combobox(
            chooser,
            textvariable=self._edit_language,
            values=[LANGUAGE_NAMES[code] for code in LANGUAGES],
            state="readonly",
            width=12,
        )
        language_box.pack(side="left", padx=(6, 16))
        language_box.bind("<<ComboboxSelected>>", lambda _e: self._on_bank_changed())

        ttk.Label(chooser, text="Tone").pack(side="left")
        tone_box = ttk.Combobox(
            chooser,
            textvariable=self._edit_tone,
            values=[tr(name, "en") for name in TONES],
            state="readonly",
            width=18,
        )
        tone_box.pack(side="left", padx=(6, 16))
        tone_box.bind("<<ComboboxSelected>>", lambda _e: self._on_bank_changed())

        ttk.Label(
            chooser,
            text="Edits are live: the next phrase the device shows comes from this list.",
            foreground="#666",
        ).pack(side="left")

        # Status list, phrase list, preview.
        self._status_list = tk.Listbox(parent, exportselection=False, width=20)
        self._status_list.grid(row=1, column=0, sticky="ns")
        self._status_list.bind("<<ListboxSelect>>", lambda _e: self._on_status_selected())

        middle = ttk.Frame(parent)
        middle.grid(row=1, column=1, sticky="nsew", padx=8)
        middle.rowconfigure(0, weight=1)
        middle.columnconfigure(0, weight=1)

        self._phrase_list = tk.Listbox(middle, exportselection=False)
        self._phrase_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(middle, orient="vertical", command=self._phrase_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        wide = ttk.Scrollbar(middle, orient="horizontal", command=self._phrase_list.xview)
        wide.grid(row=1, column=0, sticky="ew")
        self._phrase_list.configure(yscrollcommand=scroll.set, xscrollcommand=wide.set)
        self._phrase_list.bind("<<ListboxSelect>>", lambda _e: self._on_phrase_selected())

        editor = ttk.Frame(middle)
        editor.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        editor.columnconfigure(0, weight=1)

        self._entry = ttk.Entry(editor)
        self._entry.grid(row=0, column=0, sticky="ew")
        self._entry.bind("<KeyRelease>", lambda _e: self._on_entry_changed())
        self._entry.bind("<Return>", lambda _e: self._on_add())

        self._warning = ttk.Label(editor, text="", foreground="#a00")
        self._warning.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Two rows rather than one: six buttons side by side are wider than this column is
        # guaranteed to be, and the last of them would fall off the edge.
        buttons = ttk.Frame(editor)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        actions = (
            ("Add", self._on_add),
            ("Update", self._on_update),
            ("Delete", self._on_delete),
            ("Move up", lambda: self._on_move(-1)),
            ("Move down", lambda: self._on_move(1)),
            ("Show on device", self._on_show_on_device),
        )
        for index, (text, command) in enumerate(actions):
            buttons.columnconfigure(index % 3, weight=1)
            ttk.Button(buttons, text=text, command=command).grid(
                row=index // 3, column=index % 3, sticky="ew", padx=(0, 6), pady=(0, 4)
            )

        self._messages_preview = PreviewPane(parent)
        self._messages_preview.grid(row=1, column=2, sticky="n", padx=(8, 0))

    # -- Look tab ------------------------------------------------------------------------

    def _build_look(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

        self._mode = tk.StringVar()
        self._tone = tk.StringVar()
        self._language = tk.StringVar()
        self._orientation = tk.StringVar()
        self._brightness = tk.IntVar()
        self._rotate = tk.IntVar()
        self._fade = tk.BooleanVar()
        self._clock = tk.BooleanVar()

        row = 0
        row = self._radio_group(controls, row, "Display", self._mode, DISPLAY_MODES,
                                lambda name: tr(name, "en"), self._on_mode_changed)
        row = self._radio_group(controls, row, "Tone", self._tone, TONES,
                                lambda name: tr(name, "en"), self._on_tone_changed)
        row = self._radio_group(controls, row, "Language", self._language, LANGUAGES,
                                lambda code: LANGUAGE_NAMES[code], self._on_language_changed)
        row = self._radio_group(controls, row, "Orientation", self._orientation, ORIENTATIONS,
                                lambda name: tr(name, "en"), self._on_orientation_changed)

        ttk.Label(controls, text="Brightness").grid(row=row, column=0, sticky="w", pady=(10, 0))
        ttk.Scale(
            controls, from_=0, to=100, orient="horizontal", variable=self._brightness,
            command=lambda _v: self._on_brightness_changed(), length=180,
        ).grid(row=row, column=1, sticky="w", pady=(10, 0))
        row += 1

        ttk.Label(controls, text="Rotate every (s)").grid(row=row, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(
            controls, from_=0, to=3600, textvariable=self._rotate, width=8,
            command=self._on_rotate_changed,
        ).grid(row=row, column=1, sticky="w", pady=(10, 0))
        row += 1

        ttk.Checkbutton(
            controls, text="Fade between phrases", variable=self._fade,
            command=self._on_fade_changed,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 0))
        row += 1
        ttk.Checkbutton(
            controls, text="Show the clock", variable=self._clock, command=self._on_clock_changed,
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Label(
            controls,
            text=("Rotation of 0 stops the phrase changing on its own;\n"
                  "it still changes when your status does."),
            foreground="#666",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))

        self._look_preview = PreviewPane(parent)
        self._look_preview.grid(row=0, column=1, sticky="n", pady=8)

    # -- Alerts tab ----------------------------------------------------------------------

    def _build_alerts(self, parent: ttk.Frame) -> None:
        holder = ttk.Frame(parent)
        holder.pack(anchor="nw", padx=12, pady=12, fill="x")

        self._alert_enabled = tk.BooleanVar()
        self._alert_always = tk.BooleanVar()
        self._work_start = tk.StringVar()
        self._work_end = tk.StringVar()
        self._afternoon_start = tk.StringVar()
        self._afternoon_end = tk.StringVar()
        self._work_days = [tk.BooleanVar() for _ in range(7)]
        # Whole seconds: the config stores floats, but nobody sets an alert to 6.5 seconds and
        # a spinbox showing "6.0" just looks like a bug.
        self._alert_seconds = tk.IntVar()
        self._alert_cooldown = tk.IntVar()

        # Keeps the longest label in column 0 ("Wait between alerts (s)") off the control beside
        # it, without padding every grid call individually.
        holder.columnconfigure(0, pad=12)

        # Rows are handed out by a running counter, the way _build_look does it. Hard-coded
        # indices had to be renumbered by hand every time a control was added here, and the last
        # renumber quietly dropped the note and the Morning row into the same cell.
        row = 0

        ttk.Checkbutton(
            holder,
            text="Play a GIF when a Teams notification arrives outside working hours",
            variable=self._alert_enabled,
            command=self._on_alert_enabled_changed,
        ).grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1

        ttk.Checkbutton(
            holder,
            text="Alert for every notification, even during working hours",
            variable=self._alert_always,
            command=self._on_alert_always_changed,
        ).grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1

        ttk.Label(
            holder,
            text=("Teams logs how many notifications are unread, not who sent them, so the\n"
                  "board can say that something arrived but never what it was, or who from."),
            foreground="#666",
            justify="left",
        ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(2, 12))
        row += 1

        # -- the two blocks of the day. The break between them is what makes lunch count as
        # out of hours, so they are two rows rather than one with a start and an end.
        for label, start, end in (
            ("Morning", self._work_start, self._work_end),
            ("Afternoon", self._afternoon_start, self._afternoon_end),
        ):
            ttk.Label(holder, text=label).grid(row=row, column=0, sticky="w", pady=(0, 2))
            times = ttk.Frame(holder)
            times.grid(row=row, column=1, columnspan=3, sticky="w", pady=(0, 2))
            for variable in (start, end):
                entry = ttk.Entry(times, textvariable=variable, width=10)
                # Enter or leaving the box applies, so a typed time is not silently left
                # uncommitted when the Apply button goes unnoticed.
                entry.bind("<Return>", lambda _event: self._on_hours_changed())
                entry.bind("<FocusOut>", lambda _event: self._on_hours_changed())
                entry.pack(side="left")
                if variable is start:
                    ttk.Label(times, text="to").pack(side="left", padx=6)
            row += 1

        applied = ttk.Frame(holder)
        applied.grid(row=row, column=1, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(applied, text="Apply", command=self._on_hours_changed).pack(side="left")
        ttk.Label(
            applied,
            text="Leave the afternoon blank for one continuous day",
            foreground="#666",
        ).pack(side="left", padx=(8, 0))
        row += 1

        self._hours_note = ttk.Label(holder, text="", foreground="#666", justify="left")
        self._hours_note.grid(row=row, column=1, columnspan=3, sticky="w", pady=(6, 0))
        row += 1

        ttk.Label(holder, text="Working days").grid(row=row, column=0, sticky="w", pady=(10, 0))
        days = ttk.Frame(holder)
        days.grid(row=row, column=1, columnspan=3, sticky="w", pady=(10, 0))
        for index, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            ttk.Checkbutton(
                days, text=name, variable=self._work_days[index], command=self._on_days_changed,
            ).pack(side="left", padx=(0, 8))
        row += 1

        # These two have no Apply of their own, so a typed value has to commit on Enter or on
        # leaving the box -- the spinbox `command` only fires for the little arrows.
        for label, variable, limit, step in (
            ("Show for (s)", self._alert_seconds, 60, 1),
            ("Wait between alerts (s)", self._alert_cooldown, 3600, 5),
        ):
            ttk.Label(holder, text=label).grid(row=row, column=0, sticky="w", pady=(10, 0))
            spin = ttk.Spinbox(
                holder, from_=0 if step == 5 else 1, to=limit, increment=step,
                textvariable=variable, width=8, command=self._on_alert_timing_changed,
            )
            spin.bind("<Return>", lambda _event: self._on_alert_timing_changed())
            spin.bind("<FocusOut>", lambda _event: self._on_alert_timing_changed())
            spin.grid(row=row, column=1, sticky="w", pady=(10, 0))
            row += 1

        # -- the GIF
        ttk.Separator(holder, orient="horizontal").grid(
            row=row, column=0, columnspan=4, sticky="ew", pady=16
        )
        row += 1

        ttk.Label(holder, text="The GIF").grid(row=row, column=0, sticky="w")
        gif_buttons = ttk.Frame(holder)
        gif_buttons.grid(row=row, column=1, columnspan=3, sticky="w")
        self._choose_gif = ttk.Button(
            gif_buttons, text="Choose GIF...", command=self._on_choose_gif
        )
        self._choose_gif.pack(side="left")
        ttk.Button(gif_buttons, text="Test alert now", command=self._on_test_alert).pack(
            side="left", padx=(6, 0)
        )
        row += 1

        self._gif_progress = ttk.Progressbar(holder, orient="horizontal", length=260, maximum=100)
        self._gif_progress.grid(row=row, column=1, columnspan=3, sticky="w", pady=(8, 0))
        row += 1
        self._gif_status = ttk.Label(holder, text="", foreground="#666")
        self._gif_status.grid(row=row, column=1, columnspan=3, sticky="w", pady=(4, 0))
        row += 1

        ttk.Label(
            holder,
            text=(
                "Your GIF is resized to 240x240, put on a single palette and trimmed to fit,\n"
                "then sent over USB. The board composites nothing -- it draws a scanline at a\n"
                "time and has no canvas -- so the file is re-encoded rather than passed through.\n"
                f"Expect up to about {estimate_seconds(GIF_MAX_BYTES):.0f} seconds. The\n"
                "bundled GIF stays flashed as the fallback for when no PC is attached."
            ),
            foreground="#666",
            justify="left",
        ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(16, 0))

    # -- Alerts tab handlers ---------------------------------------------------------------

    def _on_alert_enabled_changed(self) -> None:
        self.config.alert_enabled = bool(self._alert_enabled.get())
        if not self._building:
            self.config.save()

    def _on_alert_always_changed(self) -> None:
        self.config.alert_always = bool(self._alert_always.get())
        if not self._building:
            self.config.save()
        # The hours below stop meaning anything while this is on, and the note says so.
        self._refresh_hours_note()

    def _on_hours_changed(self) -> None:
        """Read the four entries, store what they were understood to mean, and say it back."""
        if self._building:
            return
        self.config.work_start = self._work_start.get().strip()
        self.config.work_end = self._work_end.get().strip()
        # A blank half of the afternoon is a missing afternoon, not an empty string to parse.
        self.config.afternoon_start = self._afternoon_start.get().strip() or None
        self.config.afternoon_end = self._afternoon_end.get().strip() or None

        # Store the canonical form rather than whatever was typed, so a hand-typed "13:00" is
        # saved and shown as "1:00 PM" and config.json agrees with the window.
        schedule = work_hours.Schedule.from_config(self.config)
        afternoon = schedule.afternoon
        self.config.work_start = work_hours.format_clock(schedule.morning.start)
        self.config.work_end = work_hours.format_clock(schedule.morning.end)
        self.config.afternoon_start = (
            work_hours.format_clock(afternoon.start) if afternoon is not None else None
        )
        self.config.afternoon_end = (
            work_hours.format_clock(afternoon.end) if afternoon is not None else None
        )
        self.config.save()
        self._show_hours()
        self._refresh_hours_note()

    def _on_days_changed(self) -> None:
        if self._building:
            return
        self.config.work_days = [i for i, var in enumerate(self._work_days) if var.get()]
        self.config.save()
        self._refresh_hours_note()

    def _on_alert_timing_changed(self) -> None:
        if self._building:
            return
        try:
            self.config.alert_seconds = float(self._alert_seconds.get())
            self.config.alert_cooldown_seconds = float(self._alert_cooldown.get())
        except (tk.TclError, ValueError):
            return
        self.config.save()

    def _show_hours(self) -> None:
        """Put the stored hours in the entries. Blank afternoon entries stay blank."""
        schedule = work_hours.Schedule.from_config(self.config)
        self._work_start.set(work_hours.format_clock(schedule.morning.start))
        self._work_end.set(work_hours.format_clock(schedule.morning.end))
        afternoon = schedule.afternoon
        self._afternoon_start.set(
            work_hours.format_clock(afternoon.start) if afternoon is not None else ""
        )
        self._afternoon_end.set(
            work_hours.format_clock(afternoon.end) if afternoon is not None else ""
        )

    def _refresh_hours_note(self) -> None:
        """Say back what the day was understood to mean, gap and wrap and all.

        Two cases are worth confirming out loud. The gap between the blocks is the whole point of
        having two of them -- a message at lunch gets the GIF -- and an overnight window belongs
        to the day it opens on, which nobody should have to guess from a pair of entry boxes.
        """
        if self.config.alert_always:
            self._hours_note.configure(
                text="every notification alerts, so none of the above is being read"
            )
            return

        schedule = work_hours.Schedule.from_config(self.config)
        if not schedule.days:
            self._hours_note.configure(
                text="no working days selected: every notification counts as out of hours"
            )
            return

        blocks = []
        for window in schedule.windows:
            block = (
                f"{work_hours.format_clock(window.start)} "
                f"to {work_hours.format_clock(window.end)}"
            )
            if window.wraps:
                block += " (overnight, counted from the day it starts)"
            blocks.append(block)
        note = ", ".join(blocks)

        gap = schedule.gap()
        if gap is not None:
            note += (
                f"\n{work_hours.format_clock(gap[0])} "
                f"to {work_hours.format_clock(gap[1])} counts as out of hours"
            )
        self._hours_note.configure(text=note)

    def _on_choose_gif(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Choose an alert GIF",
            filetypes=[("Animated GIF", "*.gif"), ("All files", "*.*")],
        )
        if not path:
            return
        source = Path(path)
        try:
            size = source.stat().st_size
        except OSError as exc:
            messagebox.showerror("Alert GIF", str(exc), parent=self.root)
            return
        # Only a hint: the re-encode usually brings a big source well under the cap on its own.
        if size > GIF_MAX_BYTES * 4:
            rough = estimate_seconds(GIF_MAX_BYTES)
            if not messagebox.askokcancel(
                "Alert GIF",
                f"{source.name} is {size / 1024:,.0f} KB. It will be shrunk to fit, which may "
                f"cost frames or colours, and sending it takes up to about {rough:.0f} seconds."
                "\n\nGo ahead?",
                parent=self.root,
            ):
                return

        self._choose_gif.configure(state="disabled")
        self._gif_progress.configure(value=0)
        self._gif_status.configure(text=f"Preparing {source.name}...", foreground="#666")
        # Both callbacks fire on the worker thread, so they are bounced back through the pump
        # rather than touching a widget from there.
        self.worker.upload_gif(
            source,
            on_progress=lambda sent, total: self.post(
                lambda: self._on_gif_progress(sent, total)
            ),
            on_done=lambda stats, error: self.post(
                lambda: self._on_gif_done(source, stats, error)
            ),
        )

    def _on_test_alert(self) -> None:
        """Play the GIF now, and say what the board made of it.

        The command itself cannot fail here -- it is queued for the worker thread -- so the only
        honest report comes from the board's own answer, or from its absence.
        """
        self._alert_request += 1
        request = self._alert_request
        self._gif_status.configure(text="Asking the board...", foreground="#666")
        self.worker.alert_now()
        # Firmware without EVT:ALERT (or no board at all) answers nothing, which would otherwise
        # look exactly like the button doing nothing.
        self.root.after(5000, lambda: self._on_alert_timeout(request))

    def _on_alert_timeout(self, request: int) -> None:
        if request != self._alert_request:
            return  # answered, or superseded by a later press
        self._gif_status.configure(
            text="No reply from the board -- is it connected and running the current firmware "
                 "(pio run -t upload)?",
            foreground="#a00",
        )

    def _on_alert_result(self, ok: bool, detail: str) -> None:
        # Retires whatever timeout is outstanding: the next press starts a new request anyway.
        self._alert_request += 1
        if ok:
            self._gif_status.configure(text="Playing on the board", foreground="#060")
            return
        if detail.startswith("nogif"):
            text = ("The board has no GIF yet -- choose one above, or flash the bundled one "
                    "(pio run -t uploadfs)")
        elif detail.startswith("uploading"):
            text = "An upload is in progress; try again when it finishes"
        else:
            text = f"The board could not play the GIF: {detail}"
        self._gif_status.configure(text=text, foreground="#a00")

    def _on_gif_progress(self, sent: int, total: int) -> None:
        self._gif_progress.configure(value=100 * sent / max(1, total))
        self._gif_status.configure(text=f"Sending... {sent // 1024} of {total // 1024} KB")

    def _on_gif_done(self, source, stats, error) -> None:
        self._choose_gif.configure(state="normal")
        if error is not None:
            self._gif_progress.configure(value=0)
            self._gif_status.configure(text=f"{source.name}: {error}", foreground="#a00")
            return
        self._gif_progress.configure(value=100)
        detail = f"{source.name}: {stats.width}x{stats.height}, {stats.frames} frames"
        if stats.dropped_frames:
            detail += f", {stats.dropped_frames} dropped"
        self._gif_status.configure(text=detail + " -- on the board", foreground="#060")

    def _radio_group(self, parent, row, title, variable, values, label, command):
        ttk.Label(parent, text=title).grid(row=row, column=0, sticky="nw", pady=(10, 0))
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="w", pady=(10, 0))
        for value in values:
            ttk.Radiobutton(
                holder, text=label(value), value=value, variable=variable, command=command,
            ).pack(side="left", padx=(0, 10))
        return row + 1

    # -- Device tab ----------------------------------------------------------------------

    def _build_device(self, parent: ttk.Frame) -> None:
        holder = ttk.Frame(parent)
        holder.pack(anchor="nw", padx=12, pady=12, fill="x")

        # Everything the alert depends on, in one block. These four fail independently -- a board
        # can be attached with no GIF on it, notifications can be read with Teams not running --
        # and chasing each one down its own tab is how a silent alert stays a mystery.
        health = ttk.Frame(holder)
        health.pack(anchor="w", fill="x")
        self._health: dict[str, ttk.Label] = {}
        self._health_text: dict[str, str] = {}
        for row, (key, label) in enumerate(HEALTH_ROWS):
            ttk.Label(health, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12))
            value = ttk.Label(health, text="")
            value.grid(row=row, column=1, sticky="w")
            self._health[key] = value
            self._health_text[key] = ""

        self._startup = tk.BooleanVar()
        ttk.Checkbutton(
            holder, text="Start with Windows", variable=self._startup,
            command=self._on_startup_changed,
        ).pack(anchor="w", pady=(12, 0))

        buttons = ttk.Frame(holder)
        buttons.pack(anchor="w", pady=(12, 0))
        ttk.Button(buttons, text="Reconnect", command=self.worker.reconnect).pack(side="left")
        ttk.Button(
            buttons, text="Open config folder", command=self._on_open_config,
        ).pack(side="left", padx=(6, 0))

        ttk.Label(
            holder,
            text=(
                "Phrases live in phrases.json in the config folder and are pushed to the board\n"
                "over USB, so editing them needs no rebuild and no reflash. The board keeps its\n"
                "own flashed phrases only for when no PC is attached."
            ),
            foreground="#666",
            justify="left",
        ).pack(anchor="w", pady=(16, 0))

    # -- state in and out ----------------------------------------------------------------

    def _reload_from_config(self) -> None:
        """Push the config into the widgets. Guarded so it does not trip the change handlers."""
        self._building = True
        try:
            self._edit_language.set(LANGUAGE_NAMES[self.config.language])
            self._edit_tone.set(tr(self.config.tone, "en"))
            self._mode.set(self.config.display_mode)
            self._tone.set(self.config.tone)
            self._language.set(self.config.language)
            self._orientation.set(self.config.orientation)
            self._brightness.set(self.config.brightness)
            self._rotate.set(self.config.rotate_seconds)
            self._fade.set(bool(self.config.transition_ms))
            self._clock.set(self.config.send_clock)
            self._startup.set(self.config.start_with_windows)
            self._alert_enabled.set(self.config.alert_enabled)
            self._alert_always.set(self.config.alert_always)
            self._show_hours()
            working = work_hours.normalise_days(self.config.work_days)
            for index, var in enumerate(self._work_days):
                var.set(index in working)
            self._alert_seconds.set(round(self.config.alert_seconds))
            self._alert_cooldown.set(round(self.config.alert_cooldown_seconds))
        finally:
            self._building = False
        self._refresh_statuses()
        self._refresh_connection()
        self._refresh_hours_note()

    def _selected_language(self) -> str:
        chosen = self._edit_language.get()
        for code, name in LANGUAGE_NAMES.items():
            if name == chosen:
                return code
        return self.config.language

    def _selected_tone(self) -> str:
        chosen = self._edit_tone.get()
        for name in TONES:
            if tr(name, "en") == chosen:
                return name
        return self.config.tone

    def _selected_status(self) -> Status:
        selection = self._status_list.curselection()
        return EDITABLE[selection[0]] if selection else EDITABLE[0]

    def _refresh_statuses(self) -> None:
        keep = self._status_list.curselection()
        counts = self.phrases.counts(self._selected_language(), self._selected_tone())
        self._status_list.delete(0, tk.END)
        for status in EDITABLE:
            label = status_label(status, self.config.language)
            self._status_list.insert(tk.END, f"{label}  ({counts[status.value.lower()]})")
        self._status_list.selection_set(keep[0] if keep else 0)
        self._refresh_phrases()

    def _refresh_phrases(self) -> None:
        lines = self.phrases.lines(
            self._selected_language(), self._selected_tone(), self._selected_status()
        )
        self._phrase_list.delete(0, tk.END)
        for line in lines:
            self._phrase_list.insert(tk.END, line)
        self._refresh_previews()

    def _refresh_previews(self) -> None:
        phrase = self._entry.get().strip() or self._first_phrase()
        status = self._selected_status()
        for pane in (self._messages_preview, self._look_preview):
            pane.render(self.config, status, phrase)

    def _first_phrase(self) -> str:
        lines = self.phrases.lines(
            self._selected_language(), self._selected_tone(), self._selected_status()
        )
        return lines[0] if lines else status_label(self._selected_status(), self.config.language)

    def _board_health(self) -> tuple[str, str]:
        port = self.worker.link.port
        if port:
            return f"Connected on {port}", OK
        if self.worker.link.last_error:
            return f"No board: {self.worker.link.last_error}", BAD
        return "Looking for the board - it is probed every few seconds", UNKNOWN

    def _log_health(self) -> tuple[str, str]:
        current = self.worker.watcher.current_log
        if current is not None:
            return f"Reading {current.name}", OK
        return f"No Teams logs in {self.worker.watcher.log_dir}", BAD

    def _gif_health(self) -> tuple[str, str]:
        # There is no command to ask the board what it holds, so this only ever repeats what it
        # has already said: an alert that played, one it refused for want of a file, or nothing.
        state = self.worker._board_has_gif
        if state is True:
            return "On the board", OK
        if state is False:
            return "None on the board - choose one on the Alerts tab", BAD
        return "Unknown until an alert is played - try Test alert now", UNKNOWN

    def _refresh_connection(self) -> None:
        """Repaint the Device tab's health block.

        Cheap, and called from the pump -- so pressing Reconnect or Re-check visibly does
        something even when the answer is that it still does not work. Each row is only
        reconfigured when its text actually changes, because this runs ten times a second.
        """
        answers = {
            "board": self._board_health(),
            "log": self._log_health(),
            "gif": self._gif_health(),
        }
        for key, (text, colour) in answers.items():
            if text != self._health_text[key]:
                self._health_text[key] = text
                self._health[key].configure(text=text, foreground=colour)

    # -- Messages tab handlers -----------------------------------------------------------

    def _on_bank_changed(self) -> None:
        self._entry.delete(0, tk.END)
        self._refresh_statuses()

    def _on_status_selected(self) -> None:
        self._entry.delete(0, tk.END)
        self._refresh_phrases()

    def _on_phrase_selected(self) -> None:
        selection = self._phrase_list.curselection()
        if not selection:
            return
        self._entry.delete(0, tk.END)
        self._entry.insert(0, self._phrase_list.get(selection[0]))
        self._on_entry_changed()

    def _on_entry_changed(self) -> None:
        text = self._entry.get().strip()
        self._warning.configure(text=self._describe(text))
        self._refresh_previews()

    def _describe(self, text: str) -> str:
        """What this phrase will actually do on the panel. Every limit here is a real one."""
        if not text:
            return ""
        problems = []
        if len(text) > MAX_PHRASE_CHARS:
            problems.append(f"longer than {MAX_PHRASE_CHARS} characters, so it will be cut")
        folded, lost = to_display_ascii(text)
        if lost:
            problems.append(
                "the display cannot draw " + " ".join(sorted(set(lost))) + ", shown as ?"
            )
        if self.config.display_mode != "text":
            width = PANEL_SIZE.get(self.config.orientation, PANEL_SIZE["portrait"])[0]
            lines, size, _, truncated = layout_caption(folded, width)
            if truncated:
                problems.append("too long for the band even in the small font, so it will be cut")
            elif size != CAPTION_FONT_BIG:
                # Not an error, just the cost of a long phrase: worth knowing before you wonder
                # why this one looks smaller than the others.
                problems.append(
                    f"over {CAPTION_LINES_BIG} lines, so it drops to the small font"
                )
        return "  -  ".join(problems)

    def _current_lines(self) -> list[str]:
        return list(self._phrase_list.get(0, tk.END))

    def _commit(self, lines: list[str], select: int | None = None) -> None:
        language, tone, status = (
            self._selected_language(), self._selected_tone(), self._selected_status(),
        )
        self.phrases.set_lines(language, tone, status, lines)
        self.phrases.save()
        # The board is showing a phrase from the list that just changed.
        if language == self.config.language and tone == self.config.tone:
            self.worker.refresh_caption()
        self._refresh_statuses()
        if select is not None and 0 <= select < self._phrase_list.size():
            self._phrase_list.selection_set(select)
            self._phrase_list.see(select)

    def _on_add(self) -> None:
        text = self._entry.get().strip()
        if not text:
            return
        lines = self._current_lines()
        lines.append(text)
        self._entry.delete(0, tk.END)
        self._warning.configure(text="")
        self._commit(lines, select=len(lines) - 1)

    def _on_update(self) -> None:
        selection = self._phrase_list.curselection()
        text = self._entry.get().strip()
        if not selection or not text:
            return
        lines = self._current_lines()
        lines[selection[0]] = text
        self._commit(lines, select=selection[0])

    def _on_delete(self) -> None:
        selection = self._phrase_list.curselection()
        if not selection:
            return
        lines = self._current_lines()
        del lines[selection[0]]
        self._entry.delete(0, tk.END)
        self._commit(lines, select=min(selection[0], len(lines) - 1))

    def _on_move(self, delta: int) -> None:
        selection = self._phrase_list.curselection()
        if not selection:
            return
        index = selection[0]
        target = index + delta
        lines = self._current_lines()
        if not 0 <= target < len(lines):
            return
        lines[index], lines[target] = lines[target], lines[index]
        self._commit(lines, select=target)

    def _on_show_on_device(self) -> None:
        text = self._entry.get().strip() or self._first_phrase()
        self.worker.show_caption(text)

    # -- Look tab handlers ---------------------------------------------------------------

    def _apply(self, command: str) -> None:
        if self._building:
            return
        self.config.save()
        self.worker.queue_command(command)
        self._refresh_previews()

    def _on_mode_changed(self) -> None:
        self.config.display_mode = self._mode.get()
        self._apply(f"MODE:{self.config.display_mode}")

    def _on_tone_changed(self) -> None:
        if self._building:
            return
        # set_tone saves, tells the board which face to use, and asks for a fresh phrase.
        self.worker.set_tone(self._tone.get())
        self._edit_tone.set(tr(self.config.tone, "en"))
        self._refresh_statuses()

    def _on_language_changed(self) -> None:
        self.config.language = self._language.get()
        if not self._building:
            self.worker.refresh_caption()
            self._edit_language.set(LANGUAGE_NAMES[self.config.language])
        self._apply(f"LANG:{self.config.language}")
        self._refresh_statuses()

    def _on_orientation_changed(self) -> None:
        self.config.orientation = self._orientation.get()
        self._apply(f"ORIENT:{self.config.orientation}")

    def _on_brightness_changed(self) -> None:
        self.config.brightness = int(self._brightness.get())
        self._apply(f"BRIGHT:{self.config.brightness}")

    def _on_rotate_changed(self) -> None:
        try:
            self.config.rotate_seconds = int(self._rotate.get())
        except (tk.TclError, ValueError):
            return
        self._apply(f"ROTATE:{self.config.rotate_seconds}")

    def _on_fade_changed(self) -> None:
        self.config.transition_ms = 400 if self._fade.get() else 0
        self._apply(f"TRANSITION:{self.config.transition_ms}")

    def _on_clock_changed(self) -> None:
        self.config.send_clock = bool(self._clock.get())
        if not self._building:
            self.config.save()

    # -- Device tab handlers -------------------------------------------------------------

    def _on_startup_changed(self) -> None:
        if self._building:
            return
        from pc_app.tray import _set_run_at_startup

        self.config.start_with_windows = bool(self._startup.get())
        _set_run_at_startup(self.config.start_with_windows)
        self.config.save()

    def _on_open_config(self) -> None:
        path = config_dir()
        path.mkdir(parents=True, exist_ok=True)
        if not (path / "config.json").exists():
            self.config.save()
        # Explorer returns a non-zero exit code even on success, so do not check it.
        subprocess.Popen(["explorer", str(path)])


def run_app(worker, config: Config) -> None:
    """Run the tray icon and the settings window. Blocks until the user quits."""
    App(worker, config).run()
