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
from tkinter import ttk

from PIL import Image, ImageTk

from pc_app.config import Config, config_dir
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

        self._build()

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
        self._device = ttk.Frame(notebook)
        notebook.add(self._messages, text="Messages")
        notebook.add(self._look, text="Look")
        notebook.add(self._device, text="Device")

        self._build_messages(self._messages)
        self._build_look(self._look)
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

        self._connection = ttk.Label(holder, text="")
        self._connection.pack(anchor="w")

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
        finally:
            self._building = False
        self._refresh_statuses()
        self._refresh_connection()

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

    def _refresh_connection(self) -> None:
        port = self.worker.link.port
        self._connection.configure(
            text=f"Connected on {port}" if port else "No board found yet - it is probed every few seconds"
        )

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
