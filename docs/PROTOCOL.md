# Serial protocol

Transport: USB CDC / CH340 serial, **115200 8N1**, ASCII, `\n`-terminated lines.
Deliberately line-based and human-typable so the firmware can be driven from any serial
terminal without the PC app (see "Testing by hand" below).

Both sides ignore unknown commands and blank lines. Commands are case-sensitive.

## Status tokens

The status enum is the contract between `pc_app/presence.py` and `firmware/src/status.cpp`.
Changing it means changing both.

| Token | Meaning | Derived from |
|---|---|---|
| `AVAILABLE` | Green, free | Teams availability `Available` / `AvailableIdle` |
| `BUSY` | Busy, not in a call | `Busy` / `BusyIdle` with no active call |
| `IN_MEETING` | In a call or meeting | `Busy`/`BusyIdle` **and** an active SlimCore call marker |
| `DND` | Do not disturb | `DoNotDisturb` |
| `AWAY` | Away / idle | `Away` |
| `BRB` | Be right back | `BeRightBack` |
| `OFFLINE` | Signed out / offline | `Offline` |
| `UNKNOWN` | Presence not determinable | `PresenceUnknown`, no log file, or Teams not running |
| `DISCONNECTED` | No PC talking to the device | Firmware-only; set when no line arrives for `PC_TIMEOUT_MS` |

Note that Teams never writes "in a meeting" as an availability value — it writes `Busy`.
`IN_MEETING` is derived by combining `Busy` with the SlimCore call markers. See
`pc_app/teams_log.py`.

`DISCONNECTED` is never sent by the PC; the firmware raises it on its own watchdog.

## PC -> ESP32

| Line | Meaning |
|---|---|
| `STATUS:<TOKEN>` | Current status. Also serves as the heartbeat; resent every `heartbeat_seconds` even when unchanged. |
| `NEXT` | Immediately show a different meme for the current status. |
| `BRIGHT:<0-100>` | Backlight percentage. Persisted in NVS. |
| `ROTATE:<seconds>` | Meme rotation interval. `0` disables rotation. Persisted in NVS. |
| `TIME:HH:MM` | Optional clock in the corner. Not sent if the clock is disabled. |
| `LANG:<code>` | Caption language: `en` or `it`. Persisted in NVS. |
| `ORIENT:<name>` | Screen orientation: `landscape` (or `land`) / `portrait` (or `port`). Persisted in NVS. |
| `MODE:<name>` | `mascot` (the animated character), `image` (meme + caption band) or `text` (caption alone on the status colour). Persisted in NVS. |
| `TONE:<name>` | `normal` / `sarcastic` / `retriever`. Only picks the mascot's expression -- see "Who owns the words" below. Persisted in NVS. |
| `CAPTION:<text>` | The phrase to display now. Takes precedence over the board's own bank until the PC goes quiet. |
| `TRANSITION:<ms>` | Cross-fade duration for a caption change, 0-2000. `0` switches instantly. Persisted in NVS. |
| `PING` | Liveness / port-detection probe. |

## Who owns the words

The PC picks the phrase and sends it with `CAPTION:`; the board displays what it is told. That is
what lets the tray app's editor add a phrase without a rebuild, an `uploadfs` or PlatformIO --
and it is why `TONE:` does not carry any text. The tone changes which bank the PC draws from, and
the board needs to know it only so the mascot can pull the matching face.

The caption bank flashed into LittleFS is the fallback for when no PC is attached. Rules:

- A `CAPTION:` line replaces whatever is on screen and keeps the board from picking its own.
- On the PC timeout the stored caption is dropped, so `DISCONNECTED` shows the board's own bank
  rather than freezing on the last thing the PC said.
- While a PC caption is in force the board stops rotating captions -- the PC runs that timer, on
  `ROTATE:` seconds. In `image` mode the board still rotates the *meme*.
- Only the `normal` tone is flashed (`FLASHED_TONE` in `tools/build_memes.py`). A fallback should
  be the plain-spoken one, and the other tones never need to reach the device.

`LANG:` and `ORIENT:` are re-sent on every connect. The board ignores a value it is already using,
so this costs nothing; an unrecognised value is rejected with a `LOG:` line and changes nothing.

`ORIENT:` switches the panel between 320x240 and 240x320 **and** switches which meme folder is
read, because each orientation has its own pre-cropped images. If you built the pack with
`--orientation landscape` only, switching to portrait leaves the board with no memes for that
orientation and it falls back to the built-in drawn scene.

## ESP32 -> PC

| Line | Meaning |
|---|---|
| `READY:<version>` | Sent once on boot, e.g. `READY:1.0.0`. |
| `PONG` | Reply to `PING`. Used by `serial_link.find_port()` to identify the right COM port. |
| `LOG:<text>` | Free-form diagnostics. The PC app logs these at debug level. |
| `EVT:NEXT` | The screen was tapped. The board picks a new meme itself and this asks the PC for a fresh phrase. |

## Port detection

`pc_app/serial_link.py` enumerates COM ports, tries CH340 devices (VID:PID `1A86:7523`) first
and then any remaining port. For each candidate it opens at 115200, sends `PING`, and accepts the
port only if `PONG` or `READY:` arrives within the probe timeout. This avoids grabbing an
unrelated serial device.

## Testing by hand

With the board attached and the PC app **not** running:

```
pio device monitor -b 115200
```

Then type any of these and watch the display react:

```
PING
STATUS:IN_MEETING
STATUS:DND
NEXT
BRIGHT:20
ROTATE:0
LANG:it
ORIENT:portrait
MODE:mascot
TONE:sarcastic
CAPTION:Technically available. Emotionally, no.
MODE:text
TRANSITION:0
```

In `mascot` mode the screen is filled with the status colour dimmed to about a fifth, the
character is composed into a sprite and pushed at roughly 25 fps, and the caption sits in the
same band image mode uses. The character is drawn from shapes rather than flashed as art: a full
screen animation frame would be ~150 KB as a bitmap and ~100 ms to decode as a JPEG, and this
board has neither the flash nor the PSRAM for that. One consequence is that an expression costs a
few bytes (`firmware/src/mascot_table.h`) instead of an asset, so all 27 status/tone combinations
are free.

Set `TONE:sarcastic` and then `STATUS:AVAILABLE` to see the point of the feature: the presence
badge in the corner stays green and truthful while the face declines to be enthusiastic about it.

The caption fade in mascot mode is stepped from the animation tick rather than run in a blocking
`delay()` loop, because blocking would freeze the character for the length of the transition.

In `text` mode no meme is read at all: the screen is filled with the status colour, a presence
badge goes at the top, and the caption sits in the middle in black or white -- whichever reads
better on that colour. The badge is a disc in that same text colour with the status glyph punched
out of it, because a status-coloured disc on a status-coloured background would show nothing. A caption change cross-fades by interpolating the text colour towards the
background and back, which needs no framebuffer and so costs nothing but time.

Settings survive a power cycle: the board stores brightness, rotation, language and orientation
in NVS, so it comes back the way you left it even with no PC attached. The tray app re-sends its
own values once it connects, so the config file wins over whatever the board remembered.

Stop sending `STATUS:` for longer than the watchdog window and the display should fall back to
`DISCONNECTED` on its own.
