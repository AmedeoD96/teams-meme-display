# Teams status meme display

A desk gadget that watches your Microsoft Teams presence and shows a matching IT/developer meme
on a little ESP32 screen. Green means people can bother you. Purple means you are in yet another
meeting that could have been an email.

Inspired by the architecture of [vostoklabs/bongo_cat_monitor](https://github.com/vostoklabs/bongo_cat_monitor):
a Windows tray app pushes short lines over USB serial to an ESP32-2432S028R. Here the input is
your Teams status instead of your keyboard, and the output is a meme instead of a cat.

```
Teams log files ──tail──> tray app ──USB serial──> ESP32 ──> meme + caption
                                    "STATUS:IN_MEETING"
```

## What you need

- An **ESP32-2432S028R** ("Cheap Yellow Display", 2.8" 320x240 ILI9341, 4MB flash) and a USB cable.
  No other board or screen size is supported.
- **Windows** with the new Microsoft Teams client. macOS is not supported.
- **Python 3.10+**.

## Statuses

| Shown | When |
|---|---|
| Available | Teams is green |
| Busy | Teams is red, no call in progress |
| In a meeting | Teams is red **and** a call is up |
| Do not disturb | Teams is DND |
| Away / Be right back | Teams is idle |
| Offline | Teams is signed out |
| Unknown | Teams isn't running, or hasn't logged a status yet |
| Disconnected | The board isn't hearing from the PC |

Teams never actually writes "in a meeting" to its logs — it writes `Busy`. In-meeting is derived
by combining that with the call markers in the SlimCore log. See [pc_app/teams_log.py](pc_app/teams_log.py).

## Setup

### 1. Install the Python dependencies

```bash
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt
```

### 2. Check it can read your Teams status

Before touching any hardware, confirm the log parsing works on your machine:

```bash
.venv\Scripts\python pc_app\main.py --dry-run
```

This prints the serial lines it *would* send. Change your status in Teams and watch the tokens
follow. If it stays `UNKNOWN`, see [Troubleshooting](#troubleshooting).

### 3. Build the meme pack

Drop images into `memes/<status>/` — `memes/busy/`, `memes/in_meeting/` and so on. Any of png,
jpg, webp, bmp, gif. Then:

```bash
.venv\Scripts\python tools\build_memes.py --preview
```

This resizes everything for both orientations (320x240 and 240x320), encodes baseline JPEG (the
only kind the ESP32 decoder reads), packs it into `firmware/data/`, and — with `--preview` —
renders both display modes into `preview/` so you can check framing and captions
**without any hardware**. Add `--preview-lang it` for Italian captions, or `--preview-mode text` for just the text-only screens.

The project ships with one generated placeholder per status so it works before you add anything.
Statuses with no memes get a built-in drawn scene instead.

### 4. Flash the board

Plug the board in and find its port -- it appears as a **CH340** device:

```bash
.venv\Scripts\python -c "from serial.tools import list_ports; [print(p.device, p.description) for p in list_ports.comports()]"
```

**Close anything already using that port first** (a serial monitor, the Arduino IDE, or the Bongo
Cat app if you were running it) or the flash fails with *Access is denied* / *the port doesn't
exist*. Then, from `firmware/`:

```bash
..\.venv\Scripts\pio run -t upload --upload-port COM4
```

```bash
..\.venv\Scripts\pio run -t uploadfs --upload-port COM4
```

The first writes the firmware, the second writes the meme pack. Drop `--upload-port` to let
PlatformIO guess. Re-run only `uploadfs` after changing memes or captions.

Check it worked:

```bash
..\.venv\Scripts\pio device monitor
```

You should see `LOG:panel 320x240`, `LOG:N memes across 9 statuses` and `READY:1.0.0`.

### 5. Run the tray app

```bash
.venv\Scripts\python pc_app\main.py
```

It finds the board by itself (it probes COM ports and only keeps one that answers the handshake).
Right-click the tray icon to force a status, skip to the next meme, or start it with Windows.

## Standalone .exe

To get a single file that runs on any Windows PC with no Python installed:

```bash
.venv\Scripts\python tools\build_exe.py
```

This produces **`dist/TeamsMemeDisplay.exe`** (~15 MB). Copy it to any machine, plug in the board,
and double-click it -- it appears in the system tray. Nothing to install.

The exe contains only the PC app. The memes and firmware live on the board itself, so a machine
running the exe needs nothing but the board plugged into USB. That also means you flash the board
once, from this repo, and the exe then works on any PC you move it to.

Because it is a windowed app it has no console, so it logs to
**`%APPDATA%\TeamsMemeDisplay\app.log`** instead (also reachable via tray -> Open config folder).
That log is the first place to look if it seems to do nothing.

## Display mode

**Display** in the tray menu switches between:

- **Image + text** -- a meme filling the screen with the caption in a band along the bottom.
- **Text only** -- no images at all. The background is the Teams status colour, the status name
  sits at the top, and the caption fills the middle. Text is black or white depending on which
  reads better on that colour, so the light green and amber themes stay legible.

In text mode a caption change **cross-fades**: the old line fades into the background and the new
one fades up out of it. Toggle it with **Fade between captions** in the same submenu, or set
`transition_ms` to `0` in the config. The fade interpolates the text colour rather than blending
pixels, so it needs no framebuffer -- which matters on a board with no PSRAM.

Text mode reads nothing from the filesystem except the caption file, so it works fine on a board
with no memes flashed at all.

## Language and orientation

Both are picked from the tray menu (**Language** / **Orientation**) and take effect on the board
immediately. They are saved to the config file and re-sent whenever the app reconnects, so the
board also keeps them across a power cycle.

The defaults are **Italian** and **portrait**; a board with no PC attached falls back to the same
two, so a fresh setup looks the same either way.

**Language** is English or Italian. It changes the captions on the device *and* the tray menu
itself. Both caption banks are always flashed -- they are a few KB -- so switching is instant.

The display font is ASCII only, so Italian accents are folded to the apostrophe form the language
has always used on ASCII-only systems (`perche'`, `e'`, `caffe'`). Write the source files in
`captions/it/` with proper accents; `build_memes.py` does the folding, and warns about any
character it cannot render at all.

**Orientation** is landscape (320x240) or portrait (240x320). Each orientation needs its own
pre-cropped images, so `build_memes.py` builds both by default and the board simply reads a
different folder. That costs twice the flash; if you have a big meme library and run out of
space, build just the one you use:

```bash
.venv\Scripts\python tools\build_memes.py --orientation landscape
```

If a meme only crops well one way round, name it `something.land.png` or `something.port.png` and
it is built for that orientation only. Anything without a suffix is built for both.

## Captions

`captions/<lang>/<status>.txt` holds one joke per line; a random one is drawn each time the meme
changes. Edit them, re-run `build_memes.py`, and `pio run -t uploadfs`.

## Configuration

`%APPDATA%\TeamsMemeDisplay\config.json`, created on first save (tray → Open config folder).

| Key | Default | Meaning |
|---|---|---|
| `port` | `null` | COM port, or auto-detect |
| `log_dir` | `null` | Teams log folder override |
| `cloud_context` | `null` | With several accounts signed in, which one to trust |
| `poll_seconds` | `1.0` | How often to read the logs |
| `debounce_seconds` | `2.0` | How long a status must hold before it is shown |
| `heartbeat_seconds` | `5.0` | Status resend interval; the board's watchdog is 15s |
| `brightness` | `80` | Backlight percent |
| `rotate_seconds` | `30` | Meme rotation; `0` disables |
| `language` | `"it"` | Caption and menu language: `en` or `it` |
| `orientation` | `"portrait"` | `landscape` or `portrait` |
| `display_mode` | `"image"` | `image` (meme + caption) or `text` (caption only) |
| `transition_ms` | `400` | Caption cross-fade duration; `0` switches instantly |

## Testing

```bash
.venv\Scripts\python -m pytest pc_app/tests
```

The board can also be driven by hand with no PC app running — see [docs/PROTOCOL.md](docs/PROTOCOL.md):

```bash
cd firmware && ..\.venv\Scripts\pio device monitor
```

then type `STATUS:DND`, `NEXT`, `BRIGHT:20`, `PING`.

## Troubleshooting

**Status stays UNKNOWN.** Teams only writes presence to its log while its window has been opened
at least once since launch; running purely in the background it writes nothing. Open the Teams
window, then re-run with `--dry-run -v`. The app already walks back through the last few log files
to recover your last known status, but it deliberately refuses anything older than two days rather
than showing you a stale status.

**Wrong account.** With more than one account signed in, set `cloud_context` in the config to a
substring of the right account's cloud context (e.g. `teams.microsoft.com`). Run with `-v` to see
which contexts were found.

**Board not found.** Check Device Manager for a CH340 port; set `port` in the config to pin it.
The app never writes to a port that doesn't answer its handshake, so an unrelated serial device
is safe. Only one program can hold a COM port at a time, so quit any serial monitor first.

**The .exe seems to do nothing.** It is a tray app with no window -- look for the little monitor
icon in the notification area (you may need to expand the hidden icons). If it is not there, read
`%APPDATA%\TeamsMemeDisplay\app.log`.

**Display is blank or garbled.** Lower `SPI_FREQUENCY` in `firmware/platformio.ini` (55MHz →
40MHz). CYD board revisions vary.

**Taps don't change the meme.** Touch uses the separate SPI pins fitted to this board; revisions
differ and this is the one part not verified on hardware. Everything else is unaffected.

## Layout

```
pc_app/          Windows tray app: log tailing, presence state machine, serial link
firmware/        PlatformIO project for the CYD (TFT_eSPI + TJpg_Decoder, no LVGL)
tools/           Meme pack builder and the placeholder art generator
captions/        Joke banks, per language and status
pc_app/i18n.py   Tray menu strings and status labels per language
memes/           Your source images, one folder per status
docs/PROTOCOL.md The serial contract between the two halves
```

## Notes on memes and copyright

Only the generated placeholders are committed; `memes/` is otherwise gitignored. Most real memes
are somebody's copyrighted image, so keep your collection local rather than redistributing it
with this repo.
