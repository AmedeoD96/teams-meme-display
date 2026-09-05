"""The phrase bank: what the device is told to say, and in which register.

This is the half of the design that lets the GUI work at all. The device still carries a caption
bank in its own filesystem, but that is only the fallback for when no PC is attached; while the
tray app is running it picks the phrase and pushes it over the wire (`CAPTION:` in
docs/PROTOCOL.md). Adding a phrase therefore needs no rebuild, no `uploadfs`, and no PlatformIO --
which is the whole point of being able to add one from a window.

Because the phrasing lives here rather than on the board, *tone* never has to reach the device at
all. The board is told the tone only so the mascot can pull the matching face.

Phrases live in %APPDATA%\\TeamsMemeDisplay\\phrases.json, seeded on first run from the banks
shipped under captions/.
"""

from __future__ import annotations

import json
import logging
import random
import sys
from pathlib import Path

from pc_app.config import config_dir
from pc_app.i18n import DEFAULT_TONE, LANGUAGES, TONES, normalise, normalise_tone, status_label
from pc_app.presence import Status

log = logging.getLogger(__name__)

#: Bumped only when the on-disk shape changes in a way a straight read cannot cope with.
PHRASES_VERSION = 1

#: The tone every other tone falls back to when its bank is empty. Also the only one flashed to
#: the device (FLASHED_TONE in tools/build_memes.py).
FALLBACK_TONE = DEFAULT_TONE

#: Longest phrase we will put on the wire. The firmware's line buffer is 160 bytes and the
#: "CAPTION:" prefix eats 8 of them; the rest is headroom for a caption that is merely unwise
#: rather than malformed.
MAX_PHRASE_CHARS = 120


def resource_dir() -> Path:
    """Where read-only bundled data lives.

    PyInstaller unpacks --add-data into a temporary folder it points sys._MEIPASS at; from a
    source checkout the same files are simply in the repo.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent.parent


def phrases_path() -> Path:
    return config_dir() / "phrases.json"


def _status_keys() -> tuple[str, ...]:
    return tuple(status.value.lower() for status in Status)


def seed_banks() -> dict[str, dict[str, dict[str, list[str]]]]:
    """Read the shipped banks out of captions/<lang>/<tone>/<status>.txt.

    A missing file is not fatal -- it just means that combination starts empty, and `pick` falls
    back. The GUI can then fill it in.
    """
    root = resource_dir() / "captions"
    banks: dict[str, dict[str, dict[str, list[str]]]] = {}
    missing = 0
    for language in LANGUAGES:
        banks[language] = {}
        for tone in TONES:
            banks[language][tone] = {}
            for status in _status_keys():
                path = root / language / tone / f"{status}.txt"
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    missing += 1
                    banks[language][tone][status] = []
                    continue
                banks[language][tone][status] = [
                    line.strip() for line in text.splitlines() if line.strip()
                ]
    if missing:
        log.warning("%d shipped phrase banks missing under %s", missing, root)
    return banks


class PhraseBank:
    """Every phrase, indexed by language, tone and status.

    Reads and writes are cheap and synchronous: the whole thing is a few tens of KB, and the GUI
    wants an edit to be on the device by the next rotation tick.
    """

    def __init__(self, banks: dict[str, dict[str, dict[str, list[str]]]] | None = None):
        self._banks = banks if banks is not None else seed_banks()
        #: Index of the phrase last shown per (language, tone, status), so the same one does not
        #: come up twice running. Mirrors pickDifferent() in firmware/src/content.cpp.
        self._last: dict[tuple[str, str, str], int] = {}

    # -- persistence ---------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "PhraseBank":
        """Load the user's phrases, layered over the shipped ones.

        Starting from the seed rather than from the file means a tone or status added in a later
        version shows up with content instead of blank, while anything the user has actually
        edited -- including a list they deliberately emptied -- still wins.
        """
        path = path or phrases_path()
        banks = seed_banks()
        if not path.exists():
            log.info("no phrase file at %s, using the shipped banks", path)
            return cls(banks)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt phrase file must not stop the app; the shipped banks are a fine fallback.
            log.warning("could not read %s (%s); using the shipped banks", path, exc)
            return cls(banks)

        stored = raw.get("banks", {})
        if not isinstance(stored, dict):
            log.warning("%s has no usable 'banks' object; using the shipped banks", path)
            return cls(banks)

        overlaid = 0
        for language, tones in stored.items():
            if language not in banks or not isinstance(tones, dict):
                continue
            for tone, statuses in tones.items():
                if tone not in banks[language] or not isinstance(statuses, dict):
                    continue
                for status, lines in statuses.items():
                    if status not in banks[language][tone] or not isinstance(lines, list):
                        continue
                    banks[language][tone][status] = [
                        str(line).strip() for line in lines if str(line).strip()
                    ]
                    overlaid += 1
        log.info("loaded %d edited phrase banks from %s", overlaid, path)
        return cls(banks)

    def save(self, path: Path | None = None) -> Path:
        path = path or phrases_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": PHRASES_VERSION, "banks": self._banks}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log.info("saved phrases to %s", path)
        return path

    # -- reading and editing -------------------------------------------------------------

    def lines(self, language: str, tone: str, status: Status | str) -> list[str]:
        """The phrases for one combination. Returns a copy; use set_lines to change them."""
        language, tone, key = self._key(language, tone, status)
        return list(self._banks[language][tone].get(key, []))

    def set_lines(self, language: str, tone: str, status: Status | str, lines: list[str]) -> None:
        language, tone, key = self._key(language, tone, status)
        self._banks[language][tone][key] = [line.strip() for line in lines if line.strip()]
        # The stored index may now point past the end, or at a different phrase entirely.
        self._last.pop((language, tone, key), None)

    def counts(self, language: str, tone: str) -> dict[str, int]:
        """How many phrases each status has, for the GUI's status list."""
        language, tone, _ = self._key(language, tone, Status.UNKNOWN)
        return {status: len(self._banks[language][tone].get(status, [])) for status in _status_keys()}

    # -- the bit the worker calls --------------------------------------------------------

    def pick(self, status: Status | str, tone: str, language: str) -> str:
        """One phrase for this combination, avoiding an immediate repeat.

        Falls back to the normal tone when the chosen one has nothing to say, and finally to the
        status label -- an honest word beats a blank caption band.
        """
        language, tone, key = self._key(language, tone, status)
        choices = self._banks[language][tone].get(key) or []
        if not choices and tone != FALLBACK_TONE:
            tone = FALLBACK_TONE
            choices = self._banks[language][tone].get(key) or []
        if not choices:
            return status_label(_as_status(status), language)

        index = self._pick_different(len(choices), self._last.get((language, tone, key)))
        self._last[(language, tone, key)] = index
        return choices[index]

    @staticmethod
    def _pick_different(count: int, previous: int | None) -> int:
        """An index in [0, count) that is not *previous*. With one phrase there is no choice."""
        if count <= 1:
            return 0
        index = random.randrange(count)
        if index == previous:
            index = (index + 1) % count
        return index

    @staticmethod
    def _key(language: str, tone: str, status: Status | str) -> tuple[str, str, str]:
        return normalise(language), normalise_tone(tone), str(status).lower()


def _as_status(status: Status | str) -> Status:
    if isinstance(status, Status):
        return status
    try:
        return Status(str(status).upper())
    except ValueError:
        return Status.UNKNOWN
